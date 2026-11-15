# 后厨管理系统 - 生产镜像（A4）
# 构建：docker build -t kitchen-api deploy/
# 运行：docker compose up -d（见 docker-compose.yml，含 MySQL）
#
# 注意：开发机为 Windows + 便携 Python；Docker Desktop 4.99 于 11/15 装好、
# 引擎已验证（29.9.3/overlayfs），但容器实测尚未完成（国内拉 Docker Hub
# 镜像超时，见 deploy/README.md §四第 4 条）。本文件按标准 python:3.11-slim
# 路径编写，首次在有 Docker 的机器上构建时如有平台差异按报错微调即可
# （预期问题集中在文件权限与路径分隔符）。

FROM python:3.11-slim AS base

# 安全基线：不用 root 跑服务
RUN useradd --create-home --shell /usr/sbin/nologin kitchen
WORKDIR /app

# 依赖层：本工程运行期零第三方依赖（标准库），只装运行环境需要的部分。
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

ENV PYTHONUNBUFFERED=1 \
    # prod 安全三件套（compose/systemd 里显式覆盖，这里是兜底提示）
    KITCHEN_ADMIN_PASSWORD="" \
    KITCHEN_API_SECRET=""

EXPOSE 8848

# 健康检查：真发一次 HTTP（管理端 meta 接口，无需登录）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"KITCHEN_PORT\",\"8848\")}/api/meta', timeout=4)" || exit 1

# 生产入口：--db 落库（容器内 SQLite；MySQL 部署见 compose 的 mysql 服务）
# 密钥/口令经环境变量注入（KITCHEN_ADMIN_PASSWORD / KITCHEN_API_SECRET）
CMD ["python", "-m", "kitchen.api.server", \
     "--host", "0.0.0.0", "--port", "8848", \
     "--var", "/app/var", \
     "--db", "/app/var/runtime.db", \
     "--schedule"]
