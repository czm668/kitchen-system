# 部署手册（A4）

> 交付说明：本目录的 Dockerfile / compose / nginx / systemd 是**部署模板**。
> 答辩演示机走「Windows 裸机路径 A」（8848 端口），实测验证的是该路径与
> `tools/backup.py` 备份恢复。
>
> **11/15 14:12 容器实测已完成**：Docker Desktop 4.99 / 引擎 29.9.3，
> `docker compose up -d --build` 三容器全起（api healthy、mysql healthy、
> backup running），`/api/health` 200、登录后 5 个核心接口全 200，
> MySQL 首启 17 份迁移建出 50 张表。详见
> `05_系统记录/20261115_A17容器实测记录.md`。
> 镜像源只留 `docker.m.daocloud.io`（实测 4.73 MB/s；`docker.1ms.run`
> 仅 19 KB/s，多源按序不切换会拖死 build，见 `INSTALL-GUIDE.md` 六-bis）。

## 一、两条部署路径

### 路径 A：Windows 裸机（答辩演示机就是这个）

```bat
:: 1) 起服务（生产口径：落库 + 定时任务 + 局域网可见）
python -m kitchen.api.server --host 0.0.0.0 --port 8848 ^
    --var var/api --db var/runtime.db --schedule ^
    --secret <32+位随机串>

:: 2) 每日备份（任务计划程序挂一条 02:00 的任务，或手动）
python tools/backup.py --db var/runtime.db --keep 14
```

Windows 进程守护不用 NSSM/任务计划也行——答辩演示场景「一键脚本常开
窗口」已够（见 `tools/start_miniapp_server.bat`）；要无人值守就挂任务计划
程序（注意：本机安全策略拦过开机自启注册，手动图形界面配置不受影响）。

### 路径 B：Linux（systemd 或 Docker）

**systemd 裸机**：

```bash
sudo useradd -r -s /usr/sbin/nologin kitchen
sudo rsync -a --exclude '.local' --exclude 'backups' ./ /opt/kitchen/
cd /opt/kitchen && python3 -m venv .venv && .venv/bin/pip install .
sudo cp deploy/kitchen-api.service /etc/systemd/system/
# 编辑 /etc/kitchen/kitchen.env：KITCHEN_ADMIN_PASSWORD / KITCHEN_API_SECRET
sudo systemctl enable --now kitchen-api
journalctl -u kitchen-api -f    # 看日志
```

**Docker Compose**（含 MySQL 50 表部署路径）：

```bash
cd deploy
cp .env.example .env        # Windows PowerShell：Copy-Item .env.example .env
# 编辑 .env，把 4 个 CHANGE_ME 全改掉（随机串生成命令在模板注释里）
docker compose config       # 先校验变量，不启容器
docker compose up -d --build
```

`.env` 已被 `.gitignore` 排除，真实口令不要提交进版本库。

MySQL 首启自动按 001→017 顺序执行 `migrations/`（**17 份 / 50 表 / 71 索引**）。
⚠️ **当前 app 容器仍用 SQLite 运行时库**：compose 传的 `KITCHEN_DB_URL`
**没有任何代码消费**（Dockerfile 的 CMD 写死 `--db /app/var/runtime.db`），
MySQL 容器是空跑，数据进 `api_var` 卷。接 MySQL 需在 `storage/db.py` 换
连接串——schema 与 MySQL DDL 逐列一致（`migrate.py` 双向翻译已验证），
这是 A1 的设计预留，属**架构改动，尚未实施**。答辩口径务必照实说。

### 反向代理与 HTTPS（两路径通用）

`deploy/nginx.conf`：反代 8848、HTTPS 证书位、**SSE 长连接配置**
（`/api/events` 禁缓冲 + 1h 读超时——不配的话前端事件流每 60s 断连）、
登录接口限速、`X-Forwarded-For` 转发。`certbot` 换真证书即可。

> ⚠️ **`--trust-proxy` 与 XFF 的信任边界（A5）**：登录防爆破的锁定键是
> 「服务端看到的来源 IP」。默认**只信 TCP 对端地址**（直连场景 XFF 是
> 请求头，攻击者可伪造绕过锁定）；只有部署在 nginx 这类可信反代后面时
> 才加 `--trust-proxy`（nginx 用 `$proxy_add_x_forwarded_for` 覆盖追加，
> 模板已配好）。直接裸露公网又开 `--trust-proxy` 等于没防爆破。

## 二、dev / prod 差异表

| 维度 | dev（现状） | prod（本目录口径） |
|---|---|---|
| 管理口令 | 默认 kitchen2026（启动有 WARNING） | `KITCHEN_ADMIN_PASSWORD` 或 `KITCHEN_ACCOUNTS`（PBKDF2 摘要） |
| API 签名密钥 | 进程随机（重启令牌全失效） | `KITCHEN_API_SECRET` 固定（多实例互认） |
| 数据库 | 纯内存 / `--db` SQLite | SQLite 挂卷 或 MySQL（migrations 自动建表） |
| 定时任务 | 手动触发 | `--schedule` 托管（留样 30s + 证件每日 06:00） |
| 传输 | 明文 http://IP:8848 | nginx 终结 TLS（Let's Encrypt），80→443 强跳 |
| 登录防爆破 | `LoginLimiter` 5次/5分钟锁60s（键=服务端鉴定的来源 IP） | 同左 + nginx 登录限速；反代后加 `--trust-proxy` |
| 备份 | 无 | `tools/backup.py`（SQLite 在线快照 + 轮转）/ compose 备份侧车 |
| 进程 | 手动脚本 | systemd（自动重启 + 沙箱加固）/ Docker restart policy |

## 三、备份与恢复

**备份**（纯标准库，Windows/Linux 通用）：

```bash
python tools/backup.py --db var/runtime.db --keep 14
# → backups/kitchen_backup_YYYYMMDD_HHMMSS.zip
#   内含 var/（队列+审计）与 db/runtime.db（SQLite 在线安全快照，热备安全）
```

**恢复**：

```bash
# 1) 停服务（systemd: systemctl stop kitchen-api）
# 2) 解包 zip，把 db/runtime.db 放回 --db 声明的路径，var/ 覆盖到 --var
# 3) 起服务；GET /api/admin/db/stats 核对表行数与备份前一致
```

**compose 备份侧车**（11/15 实测）：`deploy/backup-sidecar.sh` 以 `:ro`
挂进容器，每天 02:00（容器 UTC，约合北京时间 10:00）把 `api_var` 卷
打成 `/dest/api_var_YYYYMMDD.tar.gz` 并删 14 天前的旧包。
**只归档卷，不做 mysqldump**；MySQL 逻辑备份未接入，属未实施项，
答辩别把侧车说成"数据库备份已闭环"。手动验证归档链路：

```bash
docker exec kitchen-backup sh -c 'tar czf /dest/probe.tar.gz -C /source var'
# 实测产出 617K 的 tar 包；验证完删掉 probe 文件
```

恢复验证已实测：备份包解出的快照可正常打开查询（sensor_readings /
alarm_events 行数与备份前一致）。

## 四、常见坑

1. **SSE 每 60s 断连**：nginx 没配 `/api/events` 的 `proxy_buffering off` +
   放长 `proxy_read_timeout`（模板里已配好，别删）。
2. **compose 起来 API 一直重启**：`.env` 四个口令变量没设（模板见
   `deploy/.env.example`）——compose 用 `:?` 语法强制报错，这是故意的。
   先跑 `docker compose config` 校验，比 `up` 起来再排查快得多。
3. **MySQL 首启没建表**：`migrations/` 只在**数据卷为空的首启**执行；
   卷里有旧数据时改用 `docker exec -i kitchen-mysql mysql -ukitchen -p
   kitchen < migrations/0XX_xxx.sql` 手动补（或清空 `mysql_data` 卷重建）。
   当前共 17 份（001→017），逐份补时按文件名序。
4. **国内拉镜像超时 / 慢源拖死 build**：`python:3.11-slim` / `mysql:8.4`
   从 Docker Hub 拉取，直连超时报 `registry-1.docker.io:443` 连接失败。
   在 Docker Desktop → Settings → Docker Engine 配 `registry-mirrors`，
   或 Resources → Proxies 填 HTTPS 代理，然后 Apply & Restart。与代码无关。
   **实测（11/15）**：同时配两个源时 `docker.1ms.run` 只有 19 KB/s、
   `docker.m.daocloud.io` 有 4.73 MB/s；Docker 按序尝试、慢源不报错就
   **不会自动切到快源**，导致 build 超时退出。删掉慢源只留一个即解决。
5. **systemd 起不来**：`kitchen.env` 权限必须是 600 且属主 root（否则
   systemd 拒读），`ProtectSystem=strict` 下只有 var/backups 可写，
   代码目录改文件会被拒。
6. **Windows 上 `python -m kitchen.api.server` 找不到模块**：便携解释器
   是 `._pth` 模式，`-m` 不吃 PYTHONPATH——用仓库根的 pytest 配置跑，或
   `cd 02_项目代码/kitchen/src && python -m kitchen.api.server ...`。
7. **backup 容器 `Restarting (2)`**：早期 compose 把备份逻辑写成内联
   `sh -c "if [ $$(( ... %% ... )) ...]"`；compose 的 `$$` 会被插值成 `$`，
   而 `%` **不是** compose 转义符（`%%` 会原样传给 sh），算术表达式非法，
   容器每分钟崩一次。现改为独立脚本 `deploy/backup-sidecar.sh` 以
   `:ro` 挂载、`entrypoint: ["sh", "/backup-sidecar.sh"]`，无插值歧义。
8. **admin 口令不是 `kitchen2026`**：api 启动日志里那句是没设环境变量时
   的兜底演示账号；compose 通过 `.env` 注入 `KITCHEN_ADMIN_PASSWORD`
   会覆盖它。登录用 `.env` 里的真实口令，别拿日志里的默认值试。
9. **MySQL 索引数对不上**：`tools/migrate.py check` 报 **71 索引**，而
   MySQL `information_schema.statistics` 查出来 **217 行**——后者把
   52 个 PRIMARY 与各列的 UNIQUE/隐式索引都算进去了，71 是迁移脚本里
   显式 `CREATE INDEX` 条数。两者口径不同，不是迁移漏执行；**表数 50
   才是对齐判据**。
