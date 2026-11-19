# 部署手册（A4）

> 本目录的 Dockerfile / compose 是**当前部署路径**：云端实跑
> `docker-compose.1g.yml`（api + MySQL + backup 三容器，`--static` 单端口直连 80）。
>
> **`legacy/` 是归档的裸机可选路径，云端未使用**：`nginx.conf` +
> `kitchen-api.service` 对应 systemd + nginx 反代那套部署。保留是为了"三种部署形态"
> 叙述完整（`aliyun-setup.sh` 仍可用），但**线上没走它**——别看到就以为生产环境有 nginx。
> 见审查报告 §2.1 P2。
>
> 答辩演示机跑 compose 栈（api + MySQL 同机，8848 端口），**存储引擎为 MySQL 8**,
> 已实测通过；无 MySQL 环境时 `server` 自动回退 SQLite（零依赖开发回退，非设计主线）。
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
sudo cp deploy/legacy/kitchen-api.service /etc/systemd/system/
# 编辑 /etc/kitchen/kitchen.env：KITCHEN_ADMIN_PASSWORD / KITCHEN_API_SECRET
sudo systemctl enable --now kitchen-api
journalctl -u kitchen-api -f    # 看日志
```

**Docker Compose**（含 MySQL 51 表部署路径）：

```bash
cd deploy
cp .env.example .env        # Windows PowerShell：Copy-Item .env.example .env
# 编辑 .env，把 4 个 CHANGE_ME 全改掉（随机串生成命令在模板注释里）
docker compose config       # 先校验变量，不启容器
docker compose up -d --build
```

`.env` 已被 `.gitignore` 排除，真实口令不要提交进版本库。

MySQL 首启自动按 001→019 顺序执行 `migrations/`（**19 份 / 51 表**，索引数由迁移脚本运行时统计）。

**A18 起 api 真正连 MySQL**（11/15 14:44 实测）：compose 注入的
`KITCHEN_DB_URL` 由 `server.py --db-url` → `AppStateConfig.db_url` →
`storage/backend.py` 消费，走 pymysql 后端。启动时日志会打印
`运行时库已就绪：mysql://kitchen:***@mysql:3306/kitchen（MySQL）`，
口令已脱敏；`/api/admin/db/stats` 返回 `backend=mysql`。
`mysql://` 一旦设置，**连不上就明确抛错，不静默退回 SQLite**。
Dockerfile CMD 里保留的 `--db /app/var/runtime.db` 只是「未注入
`KITCHEN_DB_URL` 时」的兜底（例如单独 `docker run` 不带 compose）；
compose 场景下 db_url 优先，SQLite 文件不再被写。若需裸机（无 Docker）直跑
并真连 MySQL，设置环境变量 `KITCHEN_DB_URL` 指向可达的 MySQL 实例即可，无需改代码。

方言差异（占位符 `?`/`%s`、事务上下文、异常类型、表清单查询）收敛在
`backend.py`，`recorders.py` 的 SQL 只有一份，**SQLite 路径行为零变化**
（1376 例单测全绿；裸机无 MySQL 时自动回退 SQLite 仍可运行）。`db.py` 未改动。

⚠️ **如实边界，答辩务必照说**：
- 运行时写库只覆盖 `sensor_readings`（遥测帧）与 `alarm_events`（告警事件）
  **2 张表**；其余 48 张是 DDL 空表，订单/菜品/人员等业务状态仍是内存态
  （A1 设计如此，「业务写路径第二轮」从未实施，不是接线漏了）。
- 备份侧车 `backup-sidecar.sh` 每天 02:00 同时做 **mysqldump（kitchen 库，
  `--single-transaction` 一致性快照）与 `api_var` 卷 tar 归档**，14 天滚动
  （A19 已接入，恢复演练实测通过；见 §三）。⚠️ 其余 48 张空表只在 dump 里有
  DDL，没数据可备。
- 架构文档 §7 所谓「生产化不在范围」指 nginx / HTTPS / 浏览器实机点检等规模化加固，
  **不指存储引擎**——MySQL 是设计起就定的运行时库（见 §一与 `03_数据库设计.md`）。

### 反向代理与 HTTPS（两路径通用）

`deploy/legacy/nginx.conf`：反代 8848、HTTPS 证书位、**SSE 长连接配置**
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

**compose 备份侧车**（11/15 A19 更新）：`deploy/backup-sidecar.sh` 以 `:ro`
挂进容器，镜像用 `mysql:8.4`（与 mysql 服务同镜像，自带 mysqldump，零新增
下载）。每天 02:00（容器 UTC，约合北京时间 10:00）做两件事，产物落
`api_backups` 卷、14 天滚动：

1. `mysqldump -h mysql -ukitchen --single-transaction kitchen` →
   `/dest/mysql_YYYYMMDD.sql.gz`（一致性快照，不锁表；kitchen 账号只有
   kitchen 库权限，不碰 root）
2. `tar` 打包 `api_var` 卷 → `/dest/api_var_YYYYMMDD.tar.gz`

**恢复（mysqldump 路径，A19 已实测演练）**：

```bash
# 1) 停写入：docker compose stop api
# 2) 灌回：zcat /dest/mysql_YYYYMMDD.sql.gz | mysql -h 127.0.0.1 -uroot -p kitchen
#    （或先灌进临时库核对行数，再择机切换）
# 3) 起服务；GET /api/admin/db/stats 核对 backend=mysql 与表行数
```

> 演练记录（2026-11-15）：probe dump 灌入临时库 `kitchen_restore_probe`，
> 50 表 / sensor_readings 13020 行 / alarm_events 5011 行与源库时点快照
> 一致，核对后临时库已删。注意 `kitchen` 账号无建库权限，整库恢复用 root。

**api_var 卷恢复**：解 tar 包，把 `var/` 覆盖回卷（审计 `audit.jsonl`、
队列 `queue.jsonl`）。2026-11-15 A17 遗留的 15MB 旧 `runtime.db` 已归档
（`_backup_20261115/runtime-db-a17-archive.db`）并从卷内删除——api 自 A18
起走 MySQL，不再读写该文件。

## 四、常见坑

1. **SSE 每 60s 断连**：nginx 没配 `/api/events` 的 `proxy_buffering off` +
   放长 `proxy_read_timeout`（模板里已配好，别删）。
2. **compose 起来 API 一直重启**：`.env` 四个口令变量没设（模板见
   `deploy/.env.example`）——compose 用 `:?` 语法强制报错，这是故意的。
   先跑 `docker compose config` 校验，比 `up` 起来再排查快得多。
3. **MySQL 首启没建表**：`migrations/` 只在**数据卷为空的首启**执行；
   卷里有旧数据时改用 `docker exec -i kitchen-mysql mysql -ukitchen -p
   kitchen < migrations/0XX_xxx.sql` 手动补（或清空 `mysql_data` 卷重建）。
   当前共 19 份（001→019），逐份补时按文件名序。
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
9. **MySQL 索引数对不上**：`tools/migrate.py check` 报的索引数与 MySQL
   `information_schema.statistics` 行数天然不等——后者把每个 PRIMARY 与
   各列的 UNIQUE/隐式索引都算进去了。两者口径不同，不是迁移漏执行；
   **表数 51 才是对齐判据**（A20 起 019 加了 `ingredient_batches`）。

## 五、阿里云 ECS 上线清单（A20）

> 目标：一台阿里云 ECS 上跑 `api + nginx`，对外只开 80/443，8848 留在回环口。
> 存储走 SQLite（零第三方依赖，最简单）；要 MySQL 就改 `KITCHEN_DB_URL` 或走
> §一 的 Docker Compose 路径。

### 5.1 机型与账号

- **规格**：2 核 2G / 40G 系统盘足够（本项目是纯标准库 HTTP 服务，不吃内存）。
- **镜像**：Ubuntu 22.04 或 24.04 LTS。
- **学生免费**：阿里云飞天加速计划（`university.aliyun.com`）学信网认证后
  有 300 元券 / 1+6 个月免费 ECS；新用户免费试用在 `free.aliyun.com`。
- ⚠️ **免费试用机不能开备案服务号**：阿里云要求实例为包年包月且剩余时长
  ≥ 3 个月才发备案服务号。**只跑答辩演示 → 免费机够；要让小程序真机连 →
  必须换成 ≥3 个月的付费实例并提前备案（20–30 天，别等最后一周）。**

#### 轻量应用服务器（1 GiB 内存套餐）注意事项

轻量入门套餐常见 **2 vCPU / 1 GiB / 30 GiB**，能跑，但必须守住两条：

1. **镜像只能选 Ubuntu 22.04 / 24.04**。Alibaba Cloud Linux、Anolis、CentOS
   是 `dnf/yum` 系，`deploy/aliyun-setup.sh` 按 apt 写的，选错直接装不上。
2. **MySQL 三容器要调参，不能直接套 `docker-compose.yml`**。`mysql:8.4`
   默认参数空库就吃 350–450 MB，加上 Docker 守护进程和 api，1 GiB 余量很小，
   初始化阶段容易被 OOM Killer 杀掉（表现为容器反复 `Restarting`）。
   两条路：**只想跑通 → 用 `docker-compose.lite.yml`（只 api + SQLite）；
   要生产级 MySQL → 用 `docker-compose.1g.yml` 并先加 swap，见 §5.6**。
   轻量只支持升配、不支持降配，想要更省心直接上 2 GiB 也行。

流量方面：套餐显示「无固定流量」表示**流量费已含在套餐价内，不产生超额
流量费**，只需关注峰值带宽（200 Mbps 对答辩演示绰绰有余）。只有「每月
固定流量」的套餐才按约 0.8 元/GB 收超额费。

轻量控制台没有「安全组」，对应的是**实例详情页的「防火墙」**页签，
规则等价于 §5.2 那张表。

### 5.2 安全组（这一步漏了会表现为"网站打不开"）

| 端口 | 来源 | 说明 |
|---|---|---|
| 22 | 你的公网 IP/32 | **不要** 0.0.0.0/0，公网爆破很凶 |
| 80 | 0.0.0.0/0 | HTTP（证书签发 + 301 跳转） |
| 443 | 0.0.0.0/0 | HTTPS |
| 8848 | **不开放** | 后端只监听 127.0.0.1，由 nginx 反代 |

### 5.3 部署步骤

```bash
# 0) 本地先构建前端（服务器不装 Node）
cd web && npm run build && cd ..

# 1) 上传代码（在本地执行；.env 里是 LLM key，一并带上但别进 git）
rsync -a --exclude '.git' --exclude 'node_modules' --exclude 'backups' \
      --exclude '.local' ./ root@<ECS公网IP>:/opt/kitchen-src/

# 2) 服务器上执行一键初始化
ssh root@<ECS公网IP>
cd /opt/kitchen-src && bash deploy/aliyun-setup.sh kitchen.你的域名.com you@example.com

# 3) 签 HTTPS（脚本没证书时只开了 80，这一步补上）
certbot certonly --webroot -w /var/www/html -d kitchen.你的域名.com --agree-tos -m you@example.com
bash deploy/aliyun-setup.sh kitchen.你的域名.com   # 再跑一次 → 自动切 HTTPS 全量配置
```

脚本做的事：装依赖 → 建 `kitchen` 用户 → 同步到 `/opt/kitchen` → 建 venv →
生成 `/etc/kitchen/kitchen.env`（随机口令，**首跑会打印一次，务必存下**）→
装并启动 systemd → 装 nginx 站点。**可重复执行**，第二次跑会自动识别证书。

### 5.4 上线后自检

```bash
systemctl status kitchen-api --no-pager     # active (running)
curl -s http://127.0.0.1:8848/api/health    # 200
curl -sI https://你的域名/                   # 200 + HSTS 头
curl -sN https://你的域名/api/events | head  # 有事件流出（SSE 没被缓冲）
```

管理端默认口令在 `/etc/kitchen/kitchen.env` 的 `KITCHEN_ADMIN_PASSWORD`；
`/etc/kitchen/kitchen.env` 若留空，**服务会直接启动失败**（A20 故意去掉
`-` 前缀）——宁可起不来，也不能用模板占位符当口令裸奔在公网上。

### 5.5 阿里云特有坑

1. **安全组没放 80/443**：本机 `curl 127.0.0.1` 通、外网打不开，九成是这个。
2. **域名没备案**：未备案域名解析到阿里云大陆节点会被拦截提示；小程序也
   根本不会放行未备案域名。
3. **`http2 on;` 报 unknown directive**：Ubuntu 自带 nginx 1.18/1.24，
   该指令要 1.25.1+。模板已改成兼容写法 `listen 443 ssl http2;`。
4. **`limit_req_zone` 未声明**：nginx 直接起不来（`unknown limit_req_zone
   "login"`）。模板已把声明放进 `conf.d` 文件顶部（conf.d 属于 http{} 作用域）。
5. **挂在 nginx 后没加 `--trust-proxy`**：后端看到的来源全是 127.0.0.1，
   登录防爆破变成"一人失败全体锁定"。systemd 单元已带该参数。
6. **公网机器缺 `KITCHEN_API_SECRET`**：会退化成进程级随机密钥，重启即
   全员掉线。脚本已自动生成固定密钥。
7. **服务器无外网代理**：`KITCHEN_LLM_USE_PROXY=false`，否则到 DeepSeek 的
   请求会被环境代理掐断，表现为客诉 AI 静默降级到离线规则。
8. **用标准 `docker-compose.yml` 上线会绕过 nginx**：它把 api 发布成
   `8848:8848`（监听 0.0.0.0），而 `docker-compose.1g.yml` 是
   `127.0.0.1:8848:8848`。公网机器**必须用后者**，或把端口映射改成
   `127.0.0.1:8848:8848`——否则既能被直连绕过 TLS 与限流，也让"8848 不开放"
   这条安全组规则形同虚设（安全组挡的是外部入站，容器仍在本机监听全部网卡）。

### 5.6 1 GiB 机型跑 MySQL（完整生产口径）

> 目标：**内存只有 1 GiB，但毕设要写"生产级 MySQL"**。做法不是砍架构，
> 而是把 MySQL 的内存参数压到适配，再加一块 swap 兜住初始化峰值。
> 编排文件：`deploy/docker-compose.1g.yml`（api + mysql + backup 三容器，
> 与 `docker-compose.yml` 架构一致，只改内存参数与端口绑定）。

#### 内存账（调优后）

| 组件 | 常驻 | 上限（mem_limit） |
|---|---|---|
| mysql:8.4（buffer pool 64M，关 P_S 与 X Plugin） | **实测 136 MB**（空库初始化后）/ 稳态约 180–220 MB | 512m |
| api（Python 标准库） | **实测 36–41 MB** | 256m |
| backup 侧车（只跑 sh 循环 + mysqldump） | 实测 20 MB | 64m |
| dockerd + containerd | 60–100 MB | — |
| Ubuntu 24.04 最小安装 | 180–260 MB | — |
| **合计** | **约 450–520 MB** | 1 GiB 余约 500 MB |

> **2026-11-18 本机实测**（Docker 29.9.3 / mysql 8.4.13，`docker stats --no-stream`）：
> 同一份数据、同一台机器上，**默认参数 500.9 MiB → 本编排参数 136.2 MiB**，
> 省下约 360 MB。这就是 1 GiB 能不能装下 MySQL 的决定性差别：默认参数下
> `mysql 501 + api 41 + backup 20 + dockerd 80 + 系统 200 ≈ 840 MB`，余量只剩
> 一百多 MB，一次大查询或并发就可能把整机推到 swap 甚至 OOM；调优后合计
> 450–520 MB，余量近 500 MB，属于"能从容跑"而不是"勉强挤进去"。

默认参数下 MySQL 8.4.13 空库实测就吃掉 **500.9 MiB**（buffer pool 128M +
performance_schema 约 100M + mysqlx + mysqld 基础约 150M + 预分配的
innodb 内部结构）。这个数字比按参数表估算出来的 350–450 MB 更高，
说明"按文档估"不如"起个容器量一次"——下面这些参数是省内存的关键：

| 参数 | 默认 | 本编排 | 作用 |
|---|---|---|---|
| `innodb_buffer_pool_size` | 128M | **64M** | 最大头；51 张空表 + 演示数据够用 |
| `performance_schema` | ON | **OFF** | 省约 100 MB；演示不需要 P_S 采样 |
| `mysqlx` | ON | **OFF** | 关掉 33060 的 X Plugin，省 20–30 MB |
| `max_connections` | 151 | **40** | 每连接都有缓冲占用，按实际并发收窄 |
| `sort/join_buffer_size` | 256K | **512K/512K** | 单会话上限，40 连接也不会失控 |
| `tmp_table_size` | 16M | **16M** | 内存临时表上限，超出自动落盘 |

> **没有动 `innodb_flush_log_at_trx_commit`（保持默认 1）。** 调成 2 能提升
> 写入吞吐，但会牺牲崩溃时的持久性——那就不叫"生产级"了，写论文时这点
> 反而是加分项：能说清"我为了内存做了哪些取舍、哪些底线不能碰"。

#### 启动顺序（顺序错了会在初始化阶段被 OOM 打断）

```bash
# 1) 先加 swap —— 必须在 up 之前
sudo bash deploy/setup-swap.sh 2G
free -m                      # 确认 Swap 一列不是 0

# 2) 装 Docker（阿里云镜像源）
curl -fsSL https://get.docker.com | sh -s -- --mirror Aliyun

# 3) 配镜像加速（Docker Hub 直连很慢；有阿里云专属加速器更好）
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "registry-mirrors": ["https://docker.m.daocloud.io"] }
EOF
sudo systemctl restart docker

# 4) 起三容器
cd /opt/kitchen/deploy
cp .env.example .env         # 改 4 个 CHANGE_ME
docker compose -f docker-compose.1g.yml config    # 先校验，比 up 再排查快
docker compose -f docker-compose.1g.yml up -d --build

# 4.5) 宿主机 nginx 反代 + HTTPS
#      容器只绑 127.0.0.1:8848，不装 nginx 外网是访问不到的
sudo apt-get install -y nginx certbot
sudo systemctl stop nginx                     # 让 certbot 独占 80 签发
sudo certbot certonly --standalone -d 你的域名 --agree-tos -m you@example.com
sudo sed "s/kitchen\.example\.com/你的域名/g" deploy/legacy/nginx.conf > /etc/nginx/conf.d/kitchen.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl enable --now nginx

# 5) 验证
docker compose -f docker-compose.1g.yml ps        # 三个都是 healthy/running
curl -s http://127.0.0.1:8848/api/health
curl -s http://127.0.0.1:8848/api/admin/db/stats  # 登录后应为 backend=mysql
docker stats --no-stream                          # 核对真实内存
```

> **还没域名怎么办**：先跳过 4.5，把 compose 的端口改成 `"8848:8848"`，
> 再在轻量「防火墙」放行 8848，用 `http://公网IP:8848` 直接演示。
> 这是临时口径——8848 对公网开放有风险，答辩前务必改回 `127.0.0.1:8848`
> 并补上 nginx + 证书。小程序无论如何都用不了 IP，必须走域名。

首次启动时 MySQL 要跑完 19 份迁移建 51 张表，healthcheck 已给 180 秒
`start_period`，这段时间 `docker ps` 显示 `health: starting` 是正常的。

#### 1 GiB 特有的排障

1. **容器反复 Restarting，日志停在 `Initializing database`**：swap 没开或太小。
   先 `sudo bash deploy/setup-swap.sh 2G` 再 `up`。
2. **`unknown variable 'xxx'` 起不来**：个别参数在特定小版本被移除。删掉报错的
   那一行即可，其余参数独立生效（`docker compose -f ... config` 不校验这个，
   只有真正启动才会暴露）。
3. **`docker stats` 里 mysql 接近 512m 上限**：说明 buffer pool 或连接数仍偏高，
   先把 `max_connections` 降到 20，再看是否需要升配到 2 GiB。
4. **备份时间对不上**：本编排给容器注入了 `TZ=Asia/Shanghai`，侧车脚本的
   02:00 是**北京时间**；§三 里"02:00 容器 UTC ≈ 北京 10:00"说的是未注入
   TZ 的旧编排，两处别混。
5. **轻量只能升配不能降配**：真要长期跑 MySQL，2 GiB 套餐会舒服很多；
   1 GiB 这套是"能跑且稳"，不是"能扛并发"。

### 5.7 升级已部署的实例（两个必查项）

> 场景：服务器上**已经有数据卷和旧镜像**，不是第一次部署。直接 `up -d --build`
> 有两个坑，2026-11-18 本机实测都踩到了。

#### 坑一：`docker-entrypoint-initdb.d` 只在卷为空时执行一次

MySQL 官方镜像的初始化脚本**不会**在已有数据卷上重跑。往 `migrations/` 里加了
新迁移后，卷里的表结构还停在旧版本，api 启动即退，日志是：

```
pymysql.err.OperationalError: (1054, "Unknown column 'prep_minutes' in 'field list'")
pymysql.err.OperationalError: (1146, "Table 'kitchen.ingredient_batches' doesn't exist")
```

**判断方法**（不用猜，直接查库）：

```bash
docker exec kitchen-mysql mysql -uroot -p -N -e \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='kitchen';"
# 019 之后应为 51；少几份迁移就差几张表
```

**修法**：用仓库里的补迁移脚本（幂等，空库/存量库/已最新库都能跑）：

```bash
bash deploy/apply-migrations.sh --dry-run   # 先看会执行哪些
bash deploy/apply-migrations.sh             # 真跑
docker compose -f deploy/docker-compose.yml up -d --force-recreate api
```

脚本按文件名序把 `migrations/*.sql` 逐份送进容器；迁移全是 DDL，重复执行只会
撞到「已存在」类错误（1050/1060/1061 等），脚本判为跳过，不算失败。结尾会打印
当前表数，和 51 对不上就说明迁移没补齐。口令从 `deploy/.env` 读 root 口令，
用 `MYSQL_PWD` 传给容器内客户端，不落在进程 argv 里。

> **不要图省事 `docker compose down -v`**：那会删掉 `mysql_data` 卷，等于清库重来。
> 演示数据没了、审计日志没了，比补迁移麻烦得多。

#### 坑二：改了 Dockerfile 必须重建镜像，`up -d` 不会自动重建

原来镜像只装了 `pymysql`，连 MySQL 8.4 时会抛：

```
RuntimeError: 'cryptography' package is required for sha256_password or
caching_sha2_password auth methods
```

原因是 8.4 默认 `caching_sha2_password`：服务端密码缓存未预热、连接又没走 TLS 时，
pymysql 必须用服务端 RSA 公钥加密口令，需要 `cryptography`。**已在本仓库
Dockerfile 中补上 `cryptography==43.0.3`。**

这个坑最阴的地方是**它会自愈**：mysql 容器 healthcheck 用 `mysqladmin ping`
把密码缓存打热之后，pymysql 走 fast-path 认证就不再需要 `cryptography`，
api 于是"重启几次之后自己好了"（实测 `RestartCount=7` 后连上）。看起来像启动慢，
实际是认证依赖侥幸通过；MySQL 一旦重启清缓存，同样的循环会重演。

```bash
docker compose -f deploy/docker-compose.yml build api   # 必须显式 build
docker compose -f deploy/docker-compose.yml up -d --force-recreate api
docker inspect kitchen-api --format 'RestartCount={{.RestartCount}} | Health={{.State.Health.Status}}'
# 期望 RestartCount=0（容器重建后计数归零）、Health=healthy
```

**验证真修好了而不是又"侥幸"连上**：清掉服务端认证缓存再连一次，能连上才算过。

```bash
docker exec kitchen-mysql mysql -uroot -p -e "FLUSH PRIVILEGES;"
docker run --rm --network deploy_default --entrypoint python deploy-api -c \
  "import pymysql,os;from urllib.parse import urlparse as u;p=u(os.environ['KITCHEN_DB_URL']);\
   c=pymysql.connect(host=p.hostname,port=p.port or 3306,user=p.username,password=p.password,database=p.path.lstrip('/'));\
   print('认证成功', c.get_server_info());c.close()"
```

#### 升级的标准顺序

```bash
git pull                                   # 或 rsync 新代码上来
bash deploy/setup-swap.sh 2G               # 仅当机器是 1 GiB 且之前没开过
docker compose -f deploy/docker-compose.yml build api
bash deploy/apply-migrations.sh            # 先补结构，再起新代码
docker compose -f deploy/docker-compose.yml up -d
curl -s http://127.0.0.1:8848/api/health
```

顺序不能反：**先补迁移再起新代码**。反过来的话 api 会因缺列缺表反复重启，
错误信息藏在 `docker logs` 里，容易被误判成"启动慢"。

### 5.8 轻量「Docker 应用镜像」上线清单（A23）

> 适用：购买时选了 **应用镜像 → Docker**（控制台显示 `Docker 26.1.3` 之类），
> 底层系统可能是 Ubuntu 22.04，也可能是 Alibaba Cloud Linux 3（yum 系）。
> **这条路上不要跑 `aliyun-setup.sh`**——它是按 apt + systemd 写的，遇到 yum 系
> 会在第一步 `apt-get` 就失败。Docker 路径不依赖发行版包管理器。

#### 架构：单容器、单端口

前端 `web/dist` 已打进镜像，由 api 的 `--static` 同源托管，所以：

- **一个容器同时是站点和 API**，不需要宿主机 nginx，也没有跨域/代理问题
- **8848 不对外发布**：compose 里用 `KITCHEN_HTTP_PORT=80` 让容器直接接管 80
- SSE（`/api/events`）不经过任何反向代理，不存在 `proxy_buffering` 把事件流
  截断的问题——这是省掉 nginx 的额外收益

> 2026-11-18 修正：`.dockerignore` 原来整目录排除 `web/`，镜像里没有任何页面，
> 而 server 的 `static if static.is_dir() else None` 会**静默降级**成「仅 API」。
> 表现是 `/api/health` 200、首页 404，很难联想到是前端没进镜像。现已改为随镜像
> 打包，且 `COPY web/dist/` 在目录缺失时会让 build 直接失败，不再静默。

#### 步骤

```bash
# 0) 本地构建前端（改了前端就必须重跑，否则镜像里是旧页面）
cd web && npm run build && cd ..

# 1) 只传必要目录。不要传 datasets/（2.3G）、var/（199M）、web/node_modules
#    --exclude='deploy/.env' 很关键：本地 .env 里是本机口令，传上去会让
#    gen-env.sh 以为「已配置」而拒绝生成新口令，也等于把本地凭据复制到公网机器
tar czf - --exclude='deploy/.env' --exclude='__pycache__' --exclude='*.pyc' \
    src migrations fixtures scenarios tools deploy web/dist pyproject.toml \
  | ssh root@<公网IP> 'mkdir -p /opt/kitchen && tar xzf - -C /opt/kitchen'

# 2) 服务器上先加 swap —— 1 GiB 跑 MySQL 首次初始化必须要有
cd /opt/kitchen
sudo bash deploy/setup-swap.sh 2G && free -m

# 3) 生成 .env（口令全自动，只在生成时打印一次管理口令）
bash deploy/gen-env.sh --public
#    --public 会把 api 发布到 0.0.0.0:80；本机调试用默认值（127.0.0.1:8848）

# 4) 起三容器
docker compose -f deploy/docker-compose.1g.yml up -d --build

# 5) 验证
docker compose -f deploy/docker-compose.1g.yml ps
curl -s http://127.0.0.1/api/health     # 注意是 80，不是 8848
docker stats --no-stream
```

**轻量服务器控制台 →「防火墙」**：加 `TCP 80`（授权对象 `0.0.0.0/0`）。
22 默认已开。**不要加 8848**——容器没发布它，加了也没用，只会扩大暴露面。

然后浏览器打开 `http://<公网IP>`，用 `gen-env.sh` 打印的 admin 口令登录。

#### 这套配置下的实测占用（1 GiB 机型）

| 组件 | 实测 |
|---|---|
| mysql（调优参数） | 136 MB（空库）/ 稳态 180–220 MB |
| api | 36–41 MB |
| backup | 20 MB |
| dockerd（应用镜像自带，已常驻） | 60–100 MB |
| 系统 | 180–260 MB |
| **合计** | **约 450–570 MB** |

#### 这条路的限制

1. **1 个月试用机开不了备案服务号**（要求包年包月且剩余 ≥3 个月），所以没有
   域名、没有 HTTPS。小程序只能：开发者工具里勾「不校验合法域名、web-view
   （业务域名）、TLS 版本以及 HTTPS 证书」，把 `config.js` 的 `PROD_BASE_URL`
   指向 `http://<公网IP>` 直连。**真机正式版走不通。**
2. **80 端口是明文 HTTP**：管理口令请用 `gen-env.sh` 生成的 24 位随机串，别用
   `kitchen2026`。答辩结束建议 `docker compose ... down` 或把发布地址改回
   `127.0.0.1:8848`。
3. **`docker compose` 插件可能缺失**（ALinux3 系）：先 `docker compose version`，
   报 `'compose' is not a docker command` 就 `sudo dnf install docker-compose-plugin`
   （Ubuntu 系：`sudo apt install docker-compose-plugin`）。
4. **以后加迁移**：卷已存在，initdb 不会再跑。先
   `bash deploy/apply-migrations.sh` 再 `up -d`，顺序见 §5.7。
5. **访问 80 返回 502**：**先别急着找"谁占了 80"**。2026-11-18 实测阿里云
   轻量 Docker 应用镜像，实例内 `ss -ltnp` 只看得到 22，80 上什么都没有，
   但公网 `curl http://IP/` 仍返回 `502 Bad Gateway` +
   `upstream connect failed ... (os error 10061)`——这个 502 是**阿里云边缘
   转发层**在"实例内 80 无监听"时给出的，不是实例里的服务。容器绑上 80 之后
   它自然变成 200，无需改防火墙、无需停任何服务。
   真的撞上 `port is already allocated` 才是本地占用，那时再
   `ss -ltnp 'sport = :80'` 找到进程停掉，或改 `KITCHEN_HTTP_PORT=8080`。

### 5.9 首次真实上线的记录（A24，2026-11-18）

> 环境：阿里云轻量 `Docker-kchi`，华东1（杭州），2 vCPU / 1 GiB / 30 GiB ESSD，
> 镜像 **Docker 26.1.3（应用镜像，底层 Alibaba Cloud Linux 3.2104）**，
> 到期 2026-12-18（1 个月试用）。公网 IP `101.37.82.186`。

#### 实际耗时与结果

| 阶段 | 结果 |
|---|---|
| 代码上传（865 KB tar） | 秒级 |
| swap 2G | 秒级 |
| 镜像构建（含 pip 装 pymysql+cryptography） | 约 20 秒 |
| MySQL 首启 + 19 份迁移建 51 表 | 约 15 秒 |
| **总计到公网可访问** | **1 分钟左右** |

实测占用（`docker stats`，运行稳定后）：

| 组件 | 实测 | 上限 |
|---|---|---|
| kitchen-mysql | 165 MB | 512m |
| kitchen-api | 33 MB | 256m |
| kitchen-backup | 1 MB | 64m |
| 整机 used | 376 / 896 MB | — |

业务自检：`backend=mysql`、**51 张表**、24 道菜、49 个原料批次、
24 个溯源码、5 个供应商；公网首页 200 且标题正确。

#### 两个只有上云才会暴露的问题

**1. 清华 PyPI 源对云服务器出口 IP 回 403**

```
ERROR: Could not find a version that satisfies the requirement pymysql==1.1.1
       (from versions: none)
```

`curl -I https://pypi.tuna.tsinghua.edu.cn/simple/` → `HTTP/2 403`。
同一台机器访问 `https://mirrors.aliyun.com/pypi/simple/` → `200`。
**不是网络不通，是被拒**，而 pip 的报错写的是"找不到版本"，极易误判成包名写错。

修法：两套 compose 的 `build.args` 都接了
`PIP_INDEX_URL: "${PIP_INDEX_URL:-清华源}"`，云上在 `.env` 里设
`PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/` 即可，Dockerfile 不用改。
`gen-env.sh --public` 现在会自动写入这一行。

**2. 80 端口的 502 是阿里云边缘层返回的，不是实例里有服务占位**

详见 §5.8 排障第 5 条。判断依据：`ss -ltnp 'sport = :80'` 空、但公网 502——
那就直接用 80，不要浪费时间去找"占用者"。

#### 重启后的存活检查（都通过）

```bash
systemctl is-enabled docker     # enabled  → 开机自启
grep swapfile /etc/fstab        # 有记录    → swap 不丢
docker inspect kitchen-api --format '{{.HostConfig.RestartPolicy.Name}}'
                                # unless-stopped → 容器自动拉起
```

#### 这台机器的现实限制

- **1 个月试用机不能开备案服务号** → 没有域名、没有 HTTPS，只有
  `http://<IP>`。小程序只能在开发者工具里勾「不校验合法域名」直连 IP，
  真机/正式版走不通。
- 80 是明文 HTTP，管理口令必须用 `gen-env.sh` 生成的 24 位随机串。

### 5.10 上线后加固：四个只有真跑起来才会暴露的问题（A24 续）

> 2026-11-18 部署完成当天，按「进程活着 ✅ ≠ 业务可用 ✅」的标准又做了一轮
> 端到端验证。下面三个问题都不会让容器崩，但会静默损坏数据或让每日备份失效。

#### 问题一：重启后订单 id 从 1 重排，会覆盖历史订单

订单只存在于内存，运行时库是**写穿镜像**（UPSERT 落库）。进程重启后内存清空、
`_next_order` 回到 1，而库里还留着上次运行的行 —— 于是重启后新下的单会拿到
id=1、2、3 去 UPSERT，把历史订单整行覆盖掉。**不抛异常、不写审计、日志无痕迹。**

实测现场（`docker restart kitchen-api` 之后）：

```
id=1  O1118-001  table_id=NULL  created_at=11:03:56   ← 重启后被"播种"的演示单
id=2  O1118-002  table_id=NULL  created_at=11:03:56
id=3  O1118-003  table_id=8     created_at=10:52:36   ← 重启前的真实订单，id 还被占着
```

**修法**（已进代码）：

1. `SqliteRecorder.order_id_bounds()` 启动时读 `COALESCE(MAX(id),0)`；
2. `OrderService.align_ids(max_order, max_item)` 把两个自增游标推到历史之后；
3. `server.py` 在 `state.start()` 之前调用，顺序不能反——先对齐，后接单。

回归用例：`tests/test_order_id_alignment.py`（5 项，含"同一库文件重启两次不覆盖"
的端到端用例）。

#### 问题二：每次启动都播种演示单，重启几次就多几批假单

旧行为是 `main()` 里无条件 `state.demo.seed_orders()`。配上 `restart: unless-stopped`，
崩一次补一批，答辩当天重启三次就多九条时间对不上的订单。

**改法**：只在「运行时库还没有订单」时播种一次（首次启动），否则打日志跳过：

```
INFO kitchen.api.server: 运行时库已有 2 条订单快照，跳过自动播种演示单
（需要就点「生成演示订单」或加 --seed-demo）
```

要在已有数据上再造演示单：界面点「生成演示订单」，或 `--seed-demo` 启动。
`--no-seed` 语义不变（永不播种）。

> 注意：内存态与库是两回事——重启后**内存里的订单列表是空的**（库里的行不会
> 被读回内存），界面上看不到重启前的单。这是本项目的既有设计（订单只活在进程
> 内、库用于留痕与统计），不是这次改动引入的。演示前若要"有单可看"，点一次
> 「生成演示订单」即可。

#### 问题三：夜间备份其实会失败，且失败信息被 `2>/dev/null` 吞掉

`mysqldump` 在 MySQL 8 上默认会去导表空间信息，而业务账号 `kitchen` 没有
`PROCESS` 权限：

```
mysqldump: Error: 'Access denied; you need (at least one of) the PROCESS
privilege(s) for this operation' when trying to dump tablespaces
```

数据其实已经导出来了，但客户端以非零码退出；侧车脚本用 `&&` 串联，于是判定
`mysql dump FAILED`，**备份文件不会落盘**，而 `2>/dev/null` 让它看起来什么都没发生。

**修法**：加 `--no-tablespaces`（表空间是实例级信息，与逻辑备份无关）：

```bash
mysqldump -h mysql -ukitchen -p"$MYSQL_PASSWORD" \
        --single-transaction --quick --routines --triggers \
        --no-tablespaces kitchen > /tmp/mysql_$day.sql
```

实测：退出码 0、stderr 空、37 KB、51 张表结构齐全。

#### 问题四：故障工单每帧重复「超时升级」，一天灌 26 万行告警

容器跑起来 25 分钟后查库，发现 `alarm_events` 从 0 涨到 **4729 行**，内容全是
同 6 张卡死工单反复升级：

```
mysql 25 分钟增量：sensor_readings 17688 行（12 指标 × 1 帧/秒，正常）
                  alarm_events     4729 行（≈3 行/秒，全是 fault-escalate）
```

链路上两个独立缺陷，单看代码都不显眼：

1. **两个时钟域**：工单的 `opened_at / due_at` 由 `state.now()`（单调秒）盖章，
   而 `_collect` 用 `state.wall()`（epoch，1.79e9）去判 `due_escalations`。
   `now >= due_at + 900` 对 epoch 永远成立 —— 工单一开就地「超时」，
   升级说明还会算出「超期 2988 万分钟」这种荒谬值。
2. **只发事件不升级**：`due_escalations()` 是只读查询，`escalated_at` 要靠调用方
   `escalate()` 写回。少调这一次，`is_due_for_escalation` 每帧都成立，
   于是一张单每秒发 3 行，直到进程重启。

**修法**（`src/kitchen/api/state.py::_collect`）：判定、升级、发事件统一用
`self.now()`，且先 `escalate()` 再 `_emit()`：

```python
moment = self.now()
for ticket in self.health.due_escalations(now=moment):
    self.health.escalate(ticket.id, now=moment)   # 写回 escalated_at，只升级一次
    self._emit("fault-escalate", ticket.to_payload())
```

回归用例：`tests/test_fault_escalation.py`（用可控时钟推进 80 帧制造卡死，
断言「未到 30 分钟不发、到点只发一条、之后 10 帧不重复」；把修复回退掉该用例立即失败）。

修完后的正确语义：读数卡死 5 分钟开单（L1）→ 再 30 分钟没人接单升到 L2（发一条）
→ 之后安静。工单可正常在界面接单/关闭。

> 另外发现 `_emit` 是 `{"seq":…, "kind": kind, **body}`，而工单 payload 自带
> `"kind": "stuck"`，会把事件种类覆盖掉：落库不受影响（`record_event` 用的是函数
> 参数，`alarm_events.scenario` 仍是 `fault-escalate`），但 SSE 行里看不到
> `kind=fault-escalate`。属独立老问题，本次未改，避免牵动前端已经依赖的字段。

#### 顺带：一条命令验完整个站

新增 `deploy/verify-cloud.sh`，按**业务可用**而不是「进程存活」来验：

```bash
cd /opt/kitchen
set -a && . deploy/.env && set +a
bash deploy/verify-cloud.sh http://127.0.0.1        # 服务器上（含容器状态）
bash deploy/verify-cloud.sh http://101.37.82.186    # 从外部验（只验对外可见的部分）
```

它查 10 项：健康检查、首页（前端是否真在镜像里）、JS 资源、管理端登录、51 张表、
菜单 24 道且参考时长齐全、溯源码→原料批次、客诉 AI 是 live 还是 rule、实时事件、
容器健康与内存。任何一项不对都会给出可操作的提示，退出码非 0 便于脚本调用。

实测输出（服务器上跑）：

```
✔ 健康检查 /api/health       200，ticks=224，0.001054s
✔ 首页（前端在镜像里）        200，后厨智能调度与食安合规管理系统
✔ 前端 JS 资源               200，/assets/index-BJakniu6.js
✔ 管理端登录                 200，已取得令牌
✔ 数据库结构与表数            mysql，51 张表
✔ 顾客端菜单                 200，24 道菜，参考时长 24/24 有值
✔ 溯源（码→原料批次）        TC-DEMO-0001 → 青椒肉丝，4 条原料
✔ 客诉 AI 通道               live（deepseek-chat）
✔ 实时事件 /api/events       200
✔ api 容器状态               running/healthy/restarts=0
通过 10 项，失败 0 项 / 结论：可以对外演示。
```

#### 端到端验证过的业务链路（不是只 curl 了 /api/health）

| 链路 | 结果 |
|---|---|
| 顾客开桌 → 下单 → 查单 | 200；订单号 `O1118-003`，子项落 `order_items` |
| 客诉提交（走 DeepSeek） | `ai_mode=live`，1.4–1.9 s 返回，分类`温度`、SLA 30 min |
| 对话式客服 | 意图 `complaint`、置信度 0.5，自动催菜 + 引用订单快照 |
| 管理端 30 个 GET 接口 | 27 个 200；另 4 个是「只接受 POST」的写接口（非故障） |
| 容器重启后顾客令牌 | 旧 token 仍 200（会话 journal 持久化生效），api 4 s 内恢复 |
| mysqldump 备份 | 退出码 0、51 表、37 KB（修 `--no-tablespaces` 后） |

### 5.11 小程序连云端：当前配置与三个开关

`miniapp/config.js` 的两个地址已指向云端（A24）：

```js
const BASE_URL      = 'http://101.37.82.186';   // 所有请求实际用的地址
const PROD_BASE_URL = 'http://101.37.82.186';   // 发布地址（此处仅作占位）
```

选 IP 做默认值是为了**不依赖现场网络**：校园 WiFi、4G 热点、答辩教室都能连；
而局域网 IP 一换网就失效。回本地开发时改回 `http://<电脑局域网IP>:8848`
（`python tools/miniapp_check.py --serve --lan` 会自动写回这一行），或在
「我的 → 服务地址」里当场覆盖（存 storage，不用重编译）。

**三个开关，缺一个就连不上**（因为用的是 http + IP，微信默认全部拦截）：

| 场景 | 要打开什么 | 说明 |
|---|---|---|
| 开发者工具 | 详情 → 本地设置 → 勾「不校验合法域名、web-view（业务域名）、TLS 版本以及 HTTPS 证书」 | 勾一次长期有效 |
| 真机预览 / 真机调试 | 手机端小程序右上角 →「开启调试模式」 | 关掉调试模式会立刻请求失败 |
| 体验版 / 正式版 | 无解 | 必须备案域名 + HTTPS，见下面第 1 条限制 |

路由器/校园网封端口的情况：云端用的是 **80**，属于必放行端口，一般不受影响；
本地 8848 才容易被封。

> 改完 `config.js` 一定要在开发者工具点一次「编译」，否则代码包里还是旧地址。

**为什么不能直接发正式版**：这台机器是 1 个月试用套餐，开不了备案服务号
（阿里云要求包年包月且剩余 ≥3 个月），所以没有域名、没有 HTTPS。微信只认
ICP 备案域名 + HTTPS，IP 与 http 一律拒绝。这是套餐的客观约束，不是实现缺陷；
答辩时如实说明即可。要真机正式版，就得换 ≥3 个月的付费实例并提前 20–30 天备案。

### 5.12 小程序链路的云端验收（可复跑，A26）

`tools/miniapp_check.py` 验的是**代码本身**：它在本机起一个内存里的 AppState，
压根不出网，所以「云端 Docker + MySQL + 反向代理这一整套对顾客端通不通」
它答不了。补一个从外网按真机口径打请求的脚本：

```bash
python tools/miniapp_cloud_check.py                 # 只读体检，地址取 miniapp/config.js
python tools/miniapp_cloud_check.py --write         # 加跑真下单/真催菜/真对话
python tools/miniapp_cloud_check.py --base http://127.0.0.1:8848   # 验本机也行
```

口径与 `miniapp/utils/api.js` 严格一致（GET 把令牌放 query、POST 放 body；
2xx + `{ok:true,data}` 才算成功）。**只读模式不动任何业务数据**，答辩前随手跑一遍：

```
$ python tools/miniapp_cloud_check.py
目标 http://101.37.82.186   桌号 3   模式 只读
[1] 匿名探活        GET /api/customer/menu 200 130ms 24 道菜
[2] 开桌            token 32 位，customer_ref=T03-8177，187ms
[3] 令牌边界        无令牌→401，伪造令牌→401
[4] 四个读接口      菜单 total 一致 / 订单数组 / 通知可用
[5] A11 扩展        画像、我的工单列表可用
[6] 溯源            TC-DEMO-0001 可查，二维码 PNG 586B
[7] 轮询抖动        p50=157ms p100=216ms（2s 轮询不会积压）
=== miniapp_cloud_check: 16/16 passed ===
```

加 `--write` 再验 8 项，覆盖顾客端唯一的写路径和两个易错点：

```
[8] 真下单          200 O1118-003 205ms，两个菜品条目都落库
[9] 催菜与限频      第一次 200 count=1，30s 内二次 429
[10] AI 客服        200 199ms intent=urge，mode=live（云端 key 真的生效）
=== miniapp_cloud_check: 24/24 passed ===
```

**真下单会在云端留一条真订单**（`O1118-003`）。要还原成出厂演示态，删的时候
注意三张外键子表，只删 `orders` 会被 `fk_oe_order` 拦住：

```sql
SET @oid := (SELECT id FROM orders WHERE order_no='O1118-003');
DELETE FROM guest_complaints WHERE order_id=@oid;
DELETE FROM order_events     WHERE order_id=@oid;
DELETE FROM order_items      WHERE order_id=@oid;
DELETE FROM orders           WHERE id=@oid;
```

实测云端 24/24 通过后库回到 `orders=2 / items=2 / events=2`（出厂的两条演示单）。

#### 真机上再走一遍（开发者工具 → 手机）

1. 微信开发者工具「导入项目」→ 目录选 `miniapp/`，AppID `wxe9190b8ab70df245`
   （必须是你账号下有权限的号；`touristappid` 能编译但**不能**真机预览）。
2. 确认「详情 → 本地设置」里「不校验合法域名、web-view（业务域名）、TLS 版本
   以及 HTTPS 证书」已勾（`project.config.json` 的 `urlCheck=false` 只是默认值，
   以工具面板为准）。
3. 点「编译」，模拟器里走一遍：开桌 → 点菜 → 下单 → 订单进度 → 催菜 → 消息中心
   → 溯源查 `TC-DEMO-0001` → 智能客服说一句「我的菜还要多久」。
4. 点「预览」扫码，手机端**先在右上角「…」里打开调试模式**，再进小程序；
   「我的」页会显示当前生效地址与一次探活结果，连不上会直接把 BASE_URL 打在
   错误卡上。
5. 换了网络不想重新扫码：在手机「我的 → 服务地址」填 `http://101.37.82.186`，
   存 storage 立即生效（优先级高于代码包里的 `BASE_URL`）。

常见报错与对应关系：

| 现象 | 原因 |
|---|---|
| `request:fail url not in domain list` | 手机端调试模式没开（真机只认备案域名 + HTTPS） |
| `request:fail timeout`，模拟器却正常 | 手机不在同一网络 / 服务只绑了 127.0.0.1（云端是公网，不适用） |
| 「我的」页显示的是旧 IP | 代码包里还是旧值：点「编译」或重新扫码；或手动改「服务地址」 |
| 401 不断重开桌 | 服务端重启过（顾客令牌是纯内存的），属预期行为 |

### 5.13 遥测表每天长 100 万行：滚动清理放在备份侧车里

`sensor_readings` 是 1 帧/秒 × 12 个指标 = **12 行/秒**，一天约 104 万行。
实测每行（含索引）约 300 B，即**每天约 300 MB**：

```
sensor_readings 16490 行 → data 1.52 MB + index 3.34 MB ≈ 4.9 MB（297 B/行）
```

30 GB 系统盘，跑满一个月就是 9 GB 左右，顺带让曲线查询越来越慢。演示真正
用得上的只有最近几天的曲线，所以 `deploy/backup-sidecar.sh` 在每天 02:00
的备份流程末尾加了一步滚动清理（**先备份再删，顺序不能反**）：

```
2026-11-18T11:23:24+08:00 purge sensor_readings: keep 7d, deleted 0 rows
```

- `sensor_readings` 保留 7 天（≈2 GB 封顶），`alarm_events` 保留 30 天；
- 分批删：单批 2 万行、批间隔 1 秒、单次最多 40 批 —— 1 GiB 机型上一次删
  100 万行会长时间占住 undo 和磁盘 IO，宁可按晚摊开；
- 清理失败只记一行日志，不影响备份成败；
- 想改保留天数就改脚本末尾那两行 `purge_table <表> <天> <批> <轮>`，
  scp 回服务器后 `docker compose up -d --force-recreate backup` 生效
  （脚本是 bind mount，只 `up -d` 不会重建容器，改完必须 force-recreate）。

单独试跑清理函数（不动真实数据，保留 7 天时现网 0 行可删）：

```bash
docker exec kitchen-backup sh -c \
  'eval "$(sed -n "/^purge_table()/,/^}/p" /backup-sidecar.sh)"; purge_table sensor_readings 7 20000 3'
```

---

### 5.14 答辩前凭据隔离清单（审查报告 §0 P0-4 / §6 D1）

`deploy/.env` 含真实 DeepSeek Key 与 MySQL 口令。它已被 `.gitignore` 拦住、**未进 GitHub**
（`git ls-files` 查不到），但"没进 git"不等于"没进答辩包"——打包/投屏时仍可能误带。

**机器可查的部分**（本地跑，非零退出就是有泄漏）：

```bash
python tools/delivery_check.py .                  # 扫仓库：期望只有 .env 命中（这是本地文件，不打包它）
python tools/delivery_check.py D:/答辩包/kitchen  # 扫真正要带走的目录：必须 OK
python tools/delivery_check.py --strict D:/答辩包 # 连 .env.example 模板也提示移除
```

脚本只扫文件名与疑似 Key 前缀，**不读也不打印任何凭据内容**；命中即说明该目录不能外带。

**必须人工做的部分**（脚本替不了）：

- [ ] 答辩包/投屏目录**只放 `docs/`、源码、`web/dist`、论文图表**；不要整个仓库压缩带走。
- [ ] 从答辩包移除 `.env`、`*.pem` 私钥、`firmware/esp32cam/secrets.h`（真 WiFi 口令）。
- [ ] **答辩前轮换 DeepSeek Key**：现用 Key 曾出现在本地 `.env`，一旦投屏/录屏入镜即视为泄露；
      在 DeepSeek 控制台作废旧 Key、生成新 Key，只填服务器上的 `.env`，**不要回填到本地答辩包**。
- [ ] MySQL 口令同理：答辩演示用只读账号或临时口令，答辩后改回。
- [ ] 演示用 `http://101.37.82.186` 裸 IP + HTTP，投屏时不要输入任何真实口令截图。

> 这两条是"可辩护边界"的组成部分：代码里凭据管理做得再干净，投屏时漏一张含 Key 的
> 截图就全部作废。审查报告把它列为四个 P0 之一，不要当成形式清单。
