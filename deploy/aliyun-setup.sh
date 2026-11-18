#!/usr/bin/env bash
# 后厨管理系统 - 阿里云 ECS 一键初始化（A20）
#
# 用法（在服务器上、项目根目录执行，必须是 root）：
#   sudo bash deploy/aliyun-setup.sh <你的域名> [邮箱]
# 例：
#   sudo bash deploy/aliyun-setup.sh kitchen.mydomain.com me@example.com
#
# 它做这些事（可重复执行，幂等）：
#   1. 装 python3-venv / nginx / rsync / certbot
#   2. 建 kitchen 系统用户
#   3. 同步代码到 /opt/kitchen（排除 .git / node_modules / backups 等）
#   4. 建 venv，生成 /etc/kitchen/kitchen.env（随机口令，已存在则不覆盖）
#   5. 装 systemd 服务并启动，本机 8848 自检
#   6. 装 nginx 站点：有证书 → 全量 HTTPS 配置；没证书 → 先只开 80 端口，
#      让 certbot 能签下来；签完再跑一次本脚本即自动切到 HTTPS。
#
# 域名 ICP 备案、安全组放行、小程序后台登记域名这三件事脚本做不了，
# 见 deploy/README.md「五、阿里云 ECS 上线清单」。

set -euo pipefail

DOMAIN="${1:-${DOMAIN:-}}"
EMAIL="${2:-${EMAIL:-}}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/kitchen
CERT="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 执行：sudo bash deploy/aliyun-setup.sh <域名>" >&2
  exit 1
fi
if [ -z "$DOMAIN" ]; then
  echo "缺少域名参数。用法：sudo bash deploy/aliyun-setup.sh kitchen.mydomain.com" >&2
  exit 1
fi

echo "==> [0/6] 检查内存与 swap"
MEM_MB="$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
if [ "${MEM_MB:-0}" -lt 1500 ]; then
  echo "    检测到 ${MEM_MB} MB 内存，补一块 2G swap（防初始化峰值 OOM）"
  bash "$SRC/deploy/setup-swap.sh" 2G || echo "    ⚠️ swap 创建失败，继续（SQLite 路径通常仍可跑）"
else
  echo "    内存 ${MEM_MB} MB，无需额外 swap"
fi

echo "==> [1/6] 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv nginx rsync certbot

echo "==> [2/6] 创建 kitchen 用户"
if ! id -u kitchen >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin kitchen
  echo "    已创建系统用户 kitchen"
else
  echo "    用户 kitchen 已存在，跳过"
fi

echo "==> [3/6] 同步代码到 ${DEST}"
mkdir -p "$DEST"
rsync -a \
  --exclude '.git' --exclude '.local' --exclude 'backups' \
  --exclude 'node_modules' --exclude '.pytest_cache' --exclude 'runs' \
  --exclude '__pycache__' --exclude '.esbuild-tmp' \
  "$SRC"/ "$DEST"/
mkdir -p "$DEST/var" "$DEST/backups"

echo "==> [4/6] 准备 Python 环境与配置"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  python3 -m venv "$DEST/.venv"
fi
# 核心零第三方依赖，venv 只做解释器隔离；要连 MySQL 才需 pymysql：
#   "$DEST/.venv/bin/pip" install "pymysql>=1.1,<2"

mkdir -p /etc/kitchen
if [ ! -f /etc/kitchen/kitchen.env ]; then
  ADMIN_PW="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)"
  API_SECRET="$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-48)"
  cat >/etc/kitchen/kitchen.env <<EOF
# 后厨管理系统 - 生产环境变量（自动生成，请妥善保管）
# 管理端登录口令：
KITCHEN_ADMIN_PASSWORD=${ADMIN_PW}
# 令牌签名密钥（改这里会导致所有已登录会话失效）：
KITCHEN_API_SECRET=${API_SECRET}
# —— 客诉 AI（可选）：填了才切在线模型，不填走离线规则层 ——
# KITCHEN_LLM_API_KEY=
# KITCHEN_LLM_BASE_URL=https://api.deepseek.com/v1
# KITCHEN_LLM_MODEL=deepseek-chat
# KITCHEN_LLM_TIMEOUT_SEC=12
# KITCHEN_LLM_USE_PROXY=false
EOF
  chmod 600 /etc/kitchen/kitchen.env
  chown root:root /etc/kitchen/kitchen.env
  echo "    已生成 /etc/kitchen/kitchen.env"
  echo "    ⚠️  管理端初始口令：${ADMIN_PW}   （请立即记录，脚本不再显示）"
else
  echo "    /etc/kitchen/kitchen.env 已存在，不覆盖"
fi

chown -R kitchen:kitchen "$DEST"

if [ ! -d "$DEST/web/dist" ]; then
  echo "    ⚠️  未找到 web/dist：前端没构建。请在本地执行 cd web && npm run build" >&2
  echo "        再把它同步上来的路径是 ${DEST}/web/dist" >&2
fi

echo "==> [5/6] 安装并启动 systemd 服务"
install -m 644 "$SRC/deploy/kitchen-api.service" /etc/systemd/system/kitchen-api.service
systemctl daemon-reload
systemctl enable kitchen-api >/dev/null 2>&1 || true
systemctl restart kitchen-api
sleep 2
if curl -fsS --max-time 5 http://127.0.0.1:8848/api/health >/dev/null; then
  echo "    ✅ 后端健康检查通过（127.0.0.1:8848）"
else
  echo "    ❌ 后端未就绪，看日志：journalctl -u kitchen-api -n 50 --no-pager" >&2
  exit 1
fi

echo "==> [6/6] 配置 nginx（域名 ${DOMAIN}）"
if [ -f "$CERT" ]; then
  echo "    检测到证书，写入 HTTPS 全量配置"
  sed "s/kitchen\.example\.com/${DOMAIN}/g" "$SRC/deploy/nginx.conf" >/etc/nginx/conf.d/kitchen.conf
  NGINX_MODE=https
else
  echo "    未检测到证书，先只开 80 端口（供 certbot 签发）"
  cat >/etc/nginx/conf.d/kitchen.conf <<EOF
# 临时 HTTP 配置：certbot 签发成功后重跑本脚本会自动换成 HTTPS 全量配置
limit_req_zone \$binary_remote_addr zone=login:10m rate=10r/m;

server {
    listen 80;
    server_name ${DOMAIN};
    client_max_body_size 10m;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type "text/plain";
    }

    location / {
        proxy_pass http://127.0.0.1:8848;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
    }
}
EOF
  NGINX_MODE=http
fi

# 默认站点会抢占 80 端口，先摘掉（保留文件以便回滚）
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl reload nginx

echo
echo "==================== 完成 ===================="
echo "后端单元：systemctl status kitchen-api"
echo "后端日志：journalctl -u kitchen-api -f"
if [ "$NGINX_MODE" = "http" ]; then
  echo
  echo "下一步（签发 HTTPS 证书后，小程序才能连）："
  echo "  sudo certbot certonly --webroot -w /var/www/html -d ${DOMAIN} --agree-tos -m ${EMAIL:-you@example.com}"
  echo "  sudo bash deploy/aliyun-setup.sh ${DOMAIN}   # 再跑一次，自动切到 HTTPS"
else
  echo
  echo "访问：https://${DOMAIN}"
  echo "⚠️ 小程序要能用，还需：① 域名已 ICP 备案 ② 小程序后台登记 request 合法域名"
fi
echo
echo "别忘了阿里云安全组：只放行 22（建议限来源 IP）/ 80 / 443，不要开放 8848。"
