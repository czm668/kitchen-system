# 后厨管理系统 - 生产镜像（A4 / A18）
# 构建：docker compose -f deploy/docker-compose.yml build
# 运行：docker compose up -d（见 docker-compose.yml，含 MySQL）
#
# 开发机为 Windows + 便携 Python；Docker Desktop 4.99 于 11/15 装好，
# 11/15 14:12 `compose up -d --build` 容器实测通过（见
# 05_系统记录/20261115_A17容器实测记录.md）。A18 起 api 真正连 MySQL，
# 不再写死 SQLite 文件。

FROM python:3.11-slim AS base

# 安全基线：不用 root 跑服务
RUN useradd --create-home --shell /usr/sbin/nologin kitchen
WORKDIR /app

# A18：运行期唯一第三方依赖是 MySQL 驱动（纯 Python，无编译）。
# 只有设置 KITCHEN_DB_URL 才会 import；SQLite / 裸机演示路径仍是零依赖标准库。
# 固定版本保证可复现；国内源只是加速，换环境可用 --build-arg PIP_INDEX_URL 覆盖。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --disable-pip-version-check \
        --index-url "${PIP_INDEX_URL}" "pymysql==1.1.1"

# 包在 /app/src 下，`python -m kitchen.api.server` 的默认 sys.path[0] 是
# WORKDIR(/app)，不加这一行容器启动会 No module named kitchen。
# （裸机 Windows 走仓库根 pytest 配置，不需要；容器是标准 CPython，PYTHONPATH 生效）
ENV PYTHONPATH=/app/src

# 代码层：核心业务仍是纯标准库；唯一第三方依赖是上面的 pymysql，
# 且只在 KITCHEN_DB_URL 存在时才被 import。
# 单独 COPY pyproject 是为了利用 Docker 层缓存——代码改动不触发重装。
COPY pyproject.toml ./
COPY src/ ./src/
COPY tools/ ./tools/
COPY fixtures/ ./fixtures/
COPY migrations/ ./migrations/
COPY scenarios/ ./scenarios/

# 运行期目录：落盘（队列/审计）与运行时库挂卷持久化
RUN mkdir -p /app/var /app/backups && chown -R kitchen:kitchen /app
USER kitchen

# prod 安全：KITCHEN_ADMIN_PASSWORD / KITCHEN_API_SECRET 只由 compose/systemd
# 运行时注入，镜像里不写任何默认值（Dockerfile lint 会拦 ENV 中的敏感变量名）。
ENV PYTHONUNBUFFERED=1

EXPOSE 8848

# 健康检查：真发一次 HTTP。必须用 /api/health（anonymous=True，免鉴权），
# 不能用 /api/meta——后者需登录，无 token 返回 401，会让容器永远 unhealthy。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"KITCHEN_PORT\",\"8848\")}/api/health', timeout=4)" || exit 1

# 生产入口（A18）：--db-url 由 server 从 KITCHEN_DB_URL 环境变量读取，
# compose 注入 mysql://... 即连 MySQL；未注入时 --db 兜底走容器内 SQLite。
# 密钥/口令经环境变量注入（KITCHEN_ADMIN_PASSWORD / KITCHEN_API_SECRET /
# MYSQL_PASSWORD），镜像里不写任何默认值。
CMD ["python", "-m", "kitchen.api.server", \
     "--host", "0.0.0.0", "--port", "8848", \
     "--var", "/app/var", \
     "--db", "/app/var/runtime.db", \
     "--schedule"]
