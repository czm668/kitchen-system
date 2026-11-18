#!/usr/bin/env bash
# 给「已存在的 mysql_data 卷」补跑新增迁移。
#
# 为什么需要它：
#   MySQL 官方镜像只在数据卷为空时执行 /docker-entrypoint-initdb.d 里的 *.sql。
#   卷一旦建好（哪怕里面还是空库），后续往 migrations/ 里新增的迁移就再也不会
#   自动执行。表现是 api 启动即退，日志里是类似
#     pymysql.err.OperationalError: (1054, "Unknown column 'prep_minutes' in 'field list'")
#   或 (1146, "Table 'kitchen.ingredient_batches' doesn't exist")。
#   2026-11-18 实测复现：11/15 建的卷停在 50 表，018/019 从未执行，api 反复重启。
#
# 本脚本做的事：
#   按文件名序把 migrations/*.sql 逐份送进容器执行。迁移全是 DDL（无 INSERT/UPDATE），
#   所以「重复执行」只会撞到 已存在 类错误，脚本把这些视为「已应用」跳过。
#   不新增版本表，不依赖任何外部状态，对空库/存量库/已最新库都安全。
#
# 用法：
#   bash deploy/apply-migrations.sh              # 补迁移
#   bash deploy/apply-migrations.sh --dry-run    # 只列出将要执行的文件
#   bash deploy/apply-migrations.sh --container kitchen-mysql --user root --db kitchen
#
# 退出码：0 = 全部已应用或已存在；1 = 有非「已存在」类错误，需人工看输出。

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
MIGDIR="$PROJECT_ROOT/migrations"

CONTAINER=kitchen-mysql
DB_USER=root
DB_NAME=kitchen
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --container) CONTAINER=${2:?--container 缺少值}; shift 2 ;;
        --user) DB_USER=${2:?--user 缺少值}; shift 2 ;;
        --db) DB_NAME=${2:?--db 缺少值}; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
    esac
done

[ -d "$MIGDIR" ] || { echo "找不到迁移目录：$MIGDIR" >&2; exit 2; }

mapfile -t FILES < <(ls -1 "$MIGDIR"/*.sql 2>/dev/null | sort)
[ ${#FILES[@]} -gt 0 ] || { echo "迁移目录为空：$MIGDIR" >&2; exit 2; }

echo "迁移目录：$MIGDIR"
echo "共 ${#FILES[@]} 份，目标：$CONTAINER / 库 $DB_NAME / 用户 $DB_USER"
echo

if [ "$DRY_RUN" = "1" ]; then
    for f in "${FILES[@]}"; do echo "  将要执行：$(basename "$f")"; done
    echo
    echo "（--dry-run 未连接数据库）"
    exit 0
fi

# 口令优先级：环境变量 > deploy/.env。用 MYSQL_PWD 传给容器内客户端，
# 避免口令出现在容器进程的 argv 里（ps 可见）。
if [ -n "${MYSQL_ROOT_PASSWORD:-}" ]; then
    PW=$MYSQL_ROOT_PASSWORD
elif [ -f "$PROJECT_ROOT/deploy/.env" ]; then
    PW=$(grep -E '^MYSQL_ROOT_PASSWORD=' "$PROJECT_ROOT/deploy/.env" | head -1 | cut -d= -f2- | tr -d '\r')
fi
[ -n "${PW:-}" ] || { echo "拿不到 root 口令：请设置 MYSQL_ROOT_PASSWORD 或在 deploy/.env 里配置" >&2; exit 2; }

docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "容器不存在：$CONTAINER" >&2; exit 2; }
[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = "true" ] \
    || { echo "容器未运行：$CONTAINER（先 docker compose up -d mysql）" >&2; exit 2; }

# 「已存在」类错误：重复执行迁移时会撞到，属正常，不算失败。
BENIGN='1050|1051|1060|1061|1062|1091|121|1826'
applied=0
skipped=0
failed=0
FAILED_LIST=()

for f in "${FILES[@]}"; do
    name=$(basename "$f")
    err=$(docker exec -e MYSQL_PWD="$PW" -i "$CONTAINER" \
            mysql -u"$DB_USER" --default-character-set=utf8mb4 --force "$DB_NAME" \
            < "$f" 2>&1 >/dev/null)

    if [ -z "$err" ]; then
        # 无报错。多数迁移用了 IF NOT EXISTS，重跑时静默通过，
        # 所以这里只能说「执行无报错」，不能断言它这次真的改了结构。
        printf '  [执行] %s\n' "$name"
        applied=$((applied + 1))
        continue
    fi

    # 有报错：逐条判断是否全为「已存在」
    real=$(printf '%s\n' "$err" | grep -E '^ERROR [0-9]+' | grep -vE "^ERROR ($BENIGN)\b" || true)
    if [ -z "$real" ]; then
        printf '  [跳过] %s（对象已存在）\n' "$name"
        skipped=$((skipped + 1))
    else
        printf '  [失败] %s\n' "$name"
        printf '%s\n' "$real" | sed 's/^/         /'
        failed=$((failed + 1))
        FAILED_LIST+=("$name")
    fi
done

echo
echo "结果：执行无报错 $applied 份 / 已存在跳过 $skipped 份 / 失败 $failed 份"

if [ "$failed" -gt 0 ]; then
    echo "失败文件：${FAILED_LIST[*]}" >&2
    echo "这些不是「已存在」类错误，需要人工处理后重跑。" >&2
    exit 1
fi

# 结构自检：表数应与迁移预期一致（当前 51）。只提示，不阻断。
tables=$(docker exec -e MYSQL_PWD="$PW" "$CONTAINER" \
        mysql -u"$DB_USER" -N -e \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB_NAME';" 2>/dev/null)
echo "当前 $DB_NAME 库表数：${tables:-未知}（019 之后应为 51）"
echo
echo "完成。若 api 容器此前在重启循环里，执行：docker compose -f deploy/docker-compose.yml up -d --force-recreate api"
