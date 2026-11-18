#!/usr/bin/env bash
# 上线自检（A24）：一条命令确认「这台服务器是不是真的能对外服务」。
#
# 为什么要有这个脚本：容器 healthy、端口在听、curl 首页 200，这三件事都成立时，
# 业务仍可能坏在别处——数据库少了几张表、菜单查出来 0 条、溯源码查不到原料、
# 客诉 AI 悄悄降级成离线规则。这些都不会让进程崩，却会让答辩现场翻车。
# 所以这里按「业务可用」而不是「进程存活」来验。
#
# 用法：
#   bash deploy/verify-cloud.sh                          # 验本机 127.0.0.1
#   bash deploy/verify-cloud.sh http://101.37.82.186     # 验公网
#   bash deploy/verify-cloud.sh https://域名 -p 管理口令
#   口令也可用环境变量：KITCHEN_ADMIN_PASSWORD=xxx bash deploy/verify-cloud.sh
#
# 依赖：curl、python3（缺 python3 时只验状态码，不中断）。
set -u

BASE="${1:-http://127.0.0.1}"
BASE="${BASE%/}"
shift 2>/dev/null || true

PW="${KITCHEN_ADMIN_PASSWORD:-}"
USER_NAME="${KITCHEN_ADMIN_USER:-admin}"
while [ $# -gt 0 ]; do
    case "$1" in
        -p|--password) PW="${2:-}"; shift 2 ;;
        -u|--user)     USER_NAME="${2:-}"; shift 2 ;;
        *)             shift ;;
    esac
done

PASS=0
FAIL=0
SKIP=0
TOKEN=""

# 响应体落临时文件而不是拼进命令替换：`cut -f1` 遇到多行输入时，
# 没有分隔符的行会原样输出，状态码会粘上整段 HTML，判定必然出错。
BODY_FILE="${TMPDIR:-/tmp}/verify-cloud-body.$$"
trap 'rm -f "$BODY_FILE"' EXIT

c_ok()   { printf '  \033[32m✔\033[0m %-30s %s\n' "$1" "$2"; PASS=$((PASS + 1)); }
c_bad()  { printf '  \033[31m✘\033[0m %-30s %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }
c_skip() { printf '  \033[33m—\033[0m %-30s %s\n' "$1" "$2"; SKIP=$((SKIP + 1)); }

# jget <点分路径>  （JSON 从标准输入读出，取不到时输出空串）
jget() {
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit(0)
for k in sys.argv[1].split("."):
    if not k:
        continue
    if isinstance(d, list):
        d = d[int(k)] if k.isdigit() and int(k) < len(d) else None
    elif isinstance(d, dict):
        d = d.get(k)
    else:
        d = None
    if d is None:
        break
print("" if d is None else d)
' "$1" 2>/dev/null
}

# fetch <METHOD> <PATH> [BODY] → 只输出 "状态码<TAB>耗时"，响应体留在 BODY_FILE
fetch() {
    local method="$1" path="$2" body="${3:-}"
    local -a auth=()
    [ -n "$TOKEN" ] && auth=(-H "Authorization: Bearer $TOKEN")
    if [ -n "$body" ]; then
        curl -sS -m 25 -o "$BODY_FILE" -w '%{http_code}\t%{time_total}\n' \
             -X "$method" -H 'Content-Type: application/json' \
             ${auth[@]+"${auth[@]}"} -d "$body" "$BASE$path" 2>/dev/null
    else
        curl -sS -m 25 -o "$BODY_FILE" -w '%{http_code}\t%{time_total}\n' \
             -X "$method" \
             ${auth[@]+"${auth[@]}"} "$BASE$path" 2>/dev/null
    fi
}

body() { cat "$BODY_FILE" 2>/dev/null; }

echo
echo "上线自检：$BASE"
echo "──────────────────────────────────────────────────────────────"

# ── 1. 进程层
resp=$(fetch GET /api/health)
code=$(printf '%s' "$resp" | cut -f1)
secs=$(printf '%s' "$resp" | cut -f2)
ticks=$(body | jget data.ticks)
if [ "$code" = "200" ] && [ -n "$ticks" ]; then
    c_ok "健康检查 /api/health" "200，ticks=$ticks，${secs}s"
else
    c_bad "健康检查 /api/health" "HTTP ${code:-无响应}（服务没起来或端口不对）"
    echo
    echo "服务不可用，后续检查跳过。先看：docker compose -f deploy/docker-compose.1g.yml ps"
    exit 1
fi

# ── 2. 站点层
resp=$(fetch GET /)
code=$(printf '%s' "$resp" | cut -f1)
secs=$(printf '%s' "$resp" | cut -f2)
if [ "$code" = "200" ] && body | grep -q 'id="app"'; then
    title=$(body | grep -oE '<title>[^<]*</title>' | head -1 | sed 's/<[^>]*>//g')
    c_ok "首页（前端在镜像里）" "200，${title:-无标题}，${secs}s"
elif [ "$code" = "200" ]; then
    c_bad "首页（前端在镜像里）" "200 但返回的不是前端页面（--static 指向或产物不对）"
else
    c_bad "首页（前端在镜像里）" "HTTP $code（web/dist 没进镜像，或 --static 指错）"
fi

asset=$(body | grep -oE '/assets/index-[A-Za-z0-9_-]+\.js' | head -1)
if [ -n "$asset" ]; then
    acode=$(curl -sS -m 15 -o /dev/null -w '%{http_code}' "$BASE$asset" 2>/dev/null)
    [ "$acode" = "200" ] && c_ok "前端 JS 资源" "200，$asset" \
                         || c_bad "前端 JS 资源" "HTTP $acode，$asset"
else
    c_skip "前端 JS 资源" "首页未引用 index-*.js（可能不是 vite 产物）"
fi

# ── 3. 业务层
if [ -z "$PW" ]; then
    c_skip "管理端登录" "未提供口令（-p 或 KITCHEN_ADMIN_PASSWORD）"
else
    resp=$(fetch POST /api/auth/login "{\"username\":\"$USER_NAME\",\"password\":\"$PW\"}")
    code=$(printf '%s' "$resp" | cut -f1)
    TOKEN=$(body | jget data.token)
    if [ -n "$TOKEN" ]; then
        c_ok "管理端登录" "200，已取得令牌"
    else
        c_bad "管理端登录" "HTTP $code，口令不对或用户不存在"
    fi
fi

if [ -n "$TOKEN" ]; then
    resp=$(fetch GET /api/admin/db/stats)
    backend=$(body | jget data.backend)
    ntab=$(body | python3 -c 'import json,sys
try: d=json.load(sys.stdin)["data"]["tables"]
except Exception: print(0); raise SystemExit
print(len(d) if isinstance(d,(list,dict)) else 0)' 2>/dev/null)
    case "$backend" in
        mysql|sqlite)
            if [ "${ntab:-0}" -ge 51 ] 2>/dev/null; then
                c_ok "数据库结构与表数" "$backend，${ntab} 张表"
            else
                c_bad "数据库结构与表数" "$backend，只有 ${ntab:-0} 张（应为 51，迁移没补齐）"
            fi ;;
        *) c_bad "数据库结构与表数" "读不到 backend（HTTP $(printf '%s' "$resp" | cut -f1)）" ;;
    esac
fi

resp=$(fetch GET /api/customer/menu)
code=$(printf '%s' "$resp" | cut -f1)
total=$(body | jget data.total)
pm=$(body | python3 -c 'import json,sys
try: ds=json.load(sys.stdin)["data"]["dishes"]
except Exception: print(""); raise SystemExit
vals=[d.get("estimate_minutes") for d in ds if d.get("estimate_minutes")]
print("%d/%d" % (len(vals), len(ds)))' 2>/dev/null)
if [ "${total:-0}" -ge 20 ] 2>/dev/null; then
    c_ok "顾客端菜单" "200，${total} 道菜，参考时长 ${pm} 有值"
else
    c_bad "顾客端菜单" "HTTP $code，菜品数 ${total:-0}（prep_minutes 缺列会 500）"
fi

# ── 4. 溯源链路：最能暴露「表建了但数据没进去」
if [ -n "$TOKEN" ]; then
    tcode=$(curl -sS -m 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/batches" 2>/dev/null \
        | python3 -c 'import json,sys
try: cs=json.load(sys.stdin)["data"]["trace_codes"]
except Exception: print(""); raise SystemExit
print(cs[0].get("code","") if cs else "")' 2>/dev/null)
    if [ -n "$tcode" ]; then
        resp=$(fetch GET "/api/customer/trace?code=$tcode")
        dish=$(body | jget data.dish)
        ning=$(body | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin)["data"].get("ingredients") or []))
except Exception: print(0)' 2>/dev/null)
        if [ "${ning:-0}" -ge 1 ]; then
            c_ok "溯源（码→原料批次）" "$tcode → ${dish:-?}，${ning} 条原料"
        else
            c_bad "溯源（码→原料批次）" "$tcode 查不到原料（ingredient_batches 可能为空）"
        fi
    else
        c_skip "溯源（码→原料批次）" "库里没有溯源码"
    fi

    # ── 5. AI 通道
    resp=$(fetch GET /api/guest/llm-runs)
    mode=$(body | jget data.provider.mode)
    model=$(body | jget data.provider.model)
    case "$mode" in
        live) c_ok "客诉 AI 通道" "live（$model）" ;;
        rule) c_skip "客诉 AI 通道" "rule（离线规则；没配 key 属正常，配了却降级要查网络）" ;;
        "")   c_bad "客诉 AI 通道" "读不到 provider（HTTP $(printf '%s' "$resp" | cut -f1)）" ;;
        *)    c_ok "客诉 AI 通道" "$mode（$model）" ;;
    esac

    # ── 6. 实时事件
    resp=$(fetch GET '/api/events?since=0')
    code=$(printf '%s' "$resp" | cut -f1)
    [ "$code" = "200" ] && c_ok "实时事件 /api/events" "200" \
                        || c_bad "实时事件 /api/events" "HTTP $code"
fi

# ── 7. 容器层（只在验本机时查，否则验的是「另一台机器上的容器」）
case "$BASE" in
    *127.0.0.1*|*localhost*)
        if command -v docker >/dev/null 2>&1 && docker inspect kitchen-api >/dev/null 2>&1; then
            st=$(docker inspect kitchen-api --format '{{.State.Status}}/{{.State.Health.Status}}/restarts={{.RestartCount}}')
            case "$st" in
                running/healthy/*) c_ok "api 容器状态" "$st" ;;
                # 刚 recreate 时 healthcheck 还没跑完，start_period 最长 180 秒；
                # 这里算「稍后再看」，不算失败，否则每次部署完第一次跑都报红。
                running/starting/*) c_skip "api 容器状态" "$st（启动中，等 30 秒再跑一次）" ;;
                *) c_bad "api 容器状态" "$st（看 docker logs kitchen-api）" ;;
            esac
            mem=$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null \
                  | grep -E '^kitchen-' | tr '\n' ';' | sed 's/;$//')
            [ -n "$mem" ] && printf '  \033[36m·\033[0m %-30s %s\n' "容器内存" "$mem"
        else
            c_skip "api 容器状态" "本机没有 kitchen-api 容器（可能是裸机 systemd 路径）"
        fi ;;
    *) c_skip "api 容器状态" "验的是远程地址，容器状态请在服务器上跑" ;;
esac

echo "──────────────────────────────────────────────────────────────"
printf '通过 %d 项' "$PASS"
if [ "$FAIL" -gt 0 ]; then printf '，\033[31m失败 %d 项\033[0m' "$FAIL"; else printf '，失败 0 项'; fi
[ "$SKIP" -gt 0 ] && printf '，跳过 %d 项' "$SKIP"
echo
if [ "$FAIL" -eq 0 ]; then
    echo "结论：可以对外演示。"
else
    echo "结论：有失败项，按上面提示排查。"
fi
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
