# 部署手册（A4）

> 交付说明：本目录的 Dockerfile / compose / nginx / systemd 是**部署模板**，
> 开发机（Windows，无 Docker）上实测验证的是「裸机 Windows 路径」与
> `tools/backup.py` 备份恢复；Linux/Docker 路径首次在有对应环境的机器上
> 执行时，按报错微调即可（预期差异集中在路径分隔符与文件权限）。

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

**Docker Compose**（含 MySQL 42 表部署路径）：

```bash
cd deploy
cat > .env <<'EOF'
KITCHEN_ADMIN_PASSWORD=<强口令>
KITCHEN_API_SECRET=<32+位随机串>
MYSQL_PASSWORD=<强口令>
MYSQL_ROOT_PASSWORD=<强口令>
EOF
docker compose up -d
```

MySQL 首启自动按 001→014 顺序执行 `migrations/`（42 表/63 索引）。
当前 app 容器仍用 SQLite 运行时库；接 MySQL 只需在 `storage/db.py` 换
连接串——schema 与 MySQL DDL 逐列一致（migrate.py 双向翻译已验证），
这是 A1 的设计预留。

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

恢复验证已实测：备份包解出的快照可正常打开查询（sensor_readings /
alarm_events 行数与备份前一致）。

## 四、常见坑

1. **SSE 每 60s 断连**：nginx 没配 `/api/events` 的 `proxy_buffering off` +
   放长 `proxy_read_timeout`（模板里已配好，别删）。
2. **compose 起来 API 一直重启**：`.env` 三个口令变量没设——compose 用
   `:?` 语法强制报错，这是故意的。
3. **MySQL 首启没建表**：`migrations/` 只在**数据卷为空的首启**执行；
   卷里有旧数据时改用 `docker exec -i kitchen-mysql mysql -ukitchen -p
   kitchen < migrations/0XX_xxx.sql` 手动补。
4. **systemd 起不来**：`kitchen.env` 权限必须是 600 且属主 root（否则
   systemd 拒读），`ProtectSystem=strict` 下只有 var/backups 可写，
   代码目录改文件会被拒。
5. **Windows 上 `python -m kitchen.api.server` 找不到模块**：便携解释器
   是 `._pth` 模式，`-m` 不吃 PYTHONPATH——用仓库根的 pytest 配置跑，或
   `cd 02_项目代码/kitchen/src && python -m kitchen.api.server ...`。
