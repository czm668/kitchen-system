#!/usr/bin/env bash
# 生成 deploy/.env（12 个 CHANGE_ME 之外的真口令），供 docker compose 读取。
#
# 为什么要有这个脚本：
#   docker-compose.yml / docker-compose.1g.yml 对 4 个值用了 `${VAR:?}` 强制校验，
#   缺失或仍是 CHANGE_ME 时 compose 直接报错——这是有意设的护栏。但手工填有
#   两个坑：一是真会有人填成短口令；二是 MYSQL_PASSWORD 会被拼进
#   `KITCHEN_DB_URL=mysql://kitchen:口令@mysql:3306/kitchen`，一旦含 @ : / # 等
#   URL 保留字，连接串就断在错误的位置，报错还很难懂。
#   所以这里只用 [A-Za-z0-9]，长度 24–48，天然 URL-safe。
#
# 用法：
#   bash deploy/gen-env.sh                # 本机/有 nginx：绑 127.0.0.1:8848
#   bash deploy/gen-env.sh --public       # 云上直连：绑 0.0.0.0:80（无需 nginx）
#   bash deploy/gen-env.sh --llm-key sk-xxx   # 显式指定客诉 AI 的 key
#   bash deploy/gen-env.sh                     # 不指定则从项目根 .env 继承
#   bash deploy/gen-env.sh --force        # 覆盖已有的 .env（会让旧口令失效）
#
# 安全：文件以 0600 写入，管理端口令只在生成时打印一次。

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE="$SCRIPT_DIR/.env"

PUBLIC=0
FORCE=0
LLM_KEY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --public) PUBLIC=1; shift ;;
        --force) FORCE=1; shift ;;
        --llm-key) LLM_KEY=${2:?--llm-key 缺少值}; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
    esac
done

if [ -f "$ENV_FILE" ] && [ "$FORCE" != "1" ]; then
    echo "$ENV_FILE 已存在，未改动。" >&2
    echo "确认要重新生成（旧口令会立即失效）请加 --force。" >&2
    exit 1
fi

# 没显式给 --llm-key 时，从项目根的 .env 继承（本机已经配过 DeepSeek key，
# 部署到服务器不该再手工抄一遍）。根 .env 不在构建上下文里，只在本地读取。
if [ -z "$LLM_KEY" ] && [ -f "$SCRIPT_DIR/../.env" ]; then
    LLM_KEY=$(grep -E '^KITCHEN_LLM_API_KEY=' "$SCRIPT_DIR/../.env" | head -1 | cut -d= -f2- | tr -d '\r')
fi

# 只用字母数字：见文件头，避免 url 保留字破坏 KITCHEN_DB_URL。
rand() {
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c "${1:-24}"
}

ADMIN_PW=$(rand 24)
API_SECRET=$(rand 48)
DB_PW=$(rand 24)
DB_ROOT_PW=$(rand 24)

if [ "$PUBLIC" = "1" ]; then
    BIND_ADDR=0.0.0.0
    HTTP_PORT=80
    # 云服务器上清华源会对部分厂商出口 IP 回 403（实测阿里云杭州），
    # 直接给阿里云 PyPI 镜像，省掉一次"构建失败再排查"。
    PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"
else
    BIND_ADDR=127.0.0.1
    HTTP_PORT=8848
    PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
fi

umask 077
{
    echo "# 由 deploy/gen-env.sh 生成于 $(date '+%Y-%m-%d %H:%M:%S')"
    echo "# 含真实口令，已在 .gitignore 中排除，切勿提交。"
    echo
    echo "# 管理端口令（登录 admin 用）"
    echo "KITCHEN_ADMIN_PASSWORD=$ADMIN_PW"
    echo "# 令牌签名密钥：换掉它等于让所有已登录会话立即失效"
    echo "KITCHEN_API_SECRET=$API_SECRET"
    echo "# MySQL 业务账号口令（会拼进 KITCHEN_DB_URL，故只用字母数字）"
    echo "MYSQL_PASSWORD=$DB_PW"
    echo "MYSQL_ROOT_PASSWORD=$DB_ROOT_PW"
    echo
    echo "# api 端口发布地址：0.0.0.0:80 表示容器直接接管 80（云上直连，无需 nginx）"
    echo "KITCHEN_BIND_ADDR=$BIND_ADDR"
    echo "KITCHEN_HTTP_PORT=$HTTP_PORT"
    echo "# 构建期 pip 源（清华源对部分云厂商出口 IP 回 403，云上自动切阿里云镜像）"
    echo "PIP_INDEX_URL=$PIP_INDEX"
    if [ -n "$LLM_KEY" ]; then
        echo
        echo "# 客诉 AI（DeepSeek，OpenAI 兼容）"
        echo "KITCHEN_LLM_API_KEY=$LLM_KEY"
        echo "KITCHEN_LLM_BASE_URL=https://api.deepseek.com/v1"
        echo "KITCHEN_LLM_MODEL=deepseek-chat"
        echo "# 云服务器没有环境代理，必须 false，否则请求被掐断后静默降级"
        echo "KITCHEN_LLM_USE_PROXY=false"
    fi
} > "$ENV_FILE"

chmod 600 "$ENV_FILE" 2>/dev/null || true

echo "已生成：$ENV_FILE"
echo
echo "  api 发布地址 : $BIND_ADDR:$HTTP_PORT"
echo "  MySQL 业务账号: kitchen / ${DB_PW:0:4}****（完整值见文件）"
echo "  客诉 AI      : $([ -n "$LLM_KEY" ] && echo '已配置' || echo '未配置（走离线规则）')"
echo
echo "=========================================================="
echo " 管理端口令（只打印这一次，请立刻记下）："
echo
echo "   $ADMIN_PW"
echo
echo "=========================================================="
echo
echo "下一步："
echo "  docker compose -f deploy/docker-compose.1g.yml up -d --build"
echo "  curl -s http://127.0.0.1:$HTTP_PORT/api/health"
