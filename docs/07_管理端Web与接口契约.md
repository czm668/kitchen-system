# 07 · 管理端 Web 与接口契约

> 适用版本：2026-11-12（A7 完成）
> 对应任务：`05_系统记录/20261112_全项目流程任务清单.md` 的 **A7-01 ~ A7-09**
> 上游依赖：`01_系统总体架构.md`（五域分层）、`04_功能规格与联动阈值.md`（17 条功能判据）
> 代码落点：`src/kitchen/api/`（10 模块）+ `web/`（Vue3 工程）

---

## 一、范围与验收口径

A7 的验收原文是「**8 个页面全部可用，数据来自真实链路或演示注入；断网期降级标记正确**」。
这句话里三个词决定了整个技术选型，先摊开说清：

| 验收词 | 含义 | 本方案的对应做法 |
|---|---|---|
| 8 个页面 | 不是「能打开」，是「每页都有数据、都能操作」 | 每页各有一条只读链路 + 至少一个写入口，`tools/web_check.py` 逐条验 |
| 真实链路 | 数据必须从 `AppState` 的六域算出来 | 页面全部走 `/api/*`，**没有任何前端 mock 数据** |
| 演示注入 | 硬件不在时用注入模拟传感器 | `POST /api/demo/inject`，注入帧打 `demo` 标记并写审计 |
| 降级标记正确 | 断网/视觉离线时三端说法一致 | 后端 `build_degrade()` 统一算，前端只渲染，`DegradeBanner` 全站唯一出口 |

**页面清单**

| # | 页面 | 路由 | 主要接口 | 关键判据落点 |
|---|---|---|---|---|
| 1 | 总览与 KPI | `#/kpi` | `/api/overview` `/api/kpi` | #12 出餐时长、#16 违规数、消毒达标率 |
| 2 | 订单与调度 | `#/orders` | `/api/orders` `/api/dishes` | #4 派单、沽清、催菜置顶 |
| 3 | 工位甘特 | `#/stations` | `/api/stations` `/api/stations/gantt` | #11 转派留痕、ETA 偏差、点火确认 |
| 4 | 环境与设备 | `#/environment` | `/api/telemetry` `/api/interlock` | #1/#2/#3/#5/#7 曲线 + 三通道状态机 |
| 5 | 配方与工艺 | `#/recipes` | `/api/recipes` `POST .../versions` | #12 称重容差、#13 SOP/CCP 步骤 |
| 6 | 批次与资质 | `#/compliance` | `/api/batches` `/api/staff*` `/api/morning` `/api/admission/scan` | #6 留样、#8 晨检、#9/#10 证照、#14 溯源 |
| 7 | 工单中心 | `#/tickets` | `/api/tickets` | 三类工单合流、超时升级、复消闭环 |
| 8 | 远程控制与参数 | `#/control` | `/api/params` `/api/meta` `/api/audit` `/api/channels/*` | 三通道动作、阈值热更新、**演示值↔真实值对照** |

---

## 二、架构：为什么是「自研 HTTP 层」而不是 FastAPI

```
浏览器 (Vue3 + ECharts)
   │  GET /api/overview   ← 5KB 聚合快照，400ms 合并刷新
   │  GET /api/stream     ← SSE，snapshot + 增量事件
   ▼
ApiServer  (ThreadingHTTPServer，标准库)
   ├── Router        路由表 + 权限校验（27 条 JSON 接口）
   ├── 静态托管       web/dist + SPA fallback + 防路径穿越
   ├── SSE 分支       不走 Router，长连接写流
   └── 限流/登录锁定   同 IP 失败 5 次锁 5 分钟
   ▼
AppState（线程安全的门面）
   ├── KitchenRuntime  规则引擎（12 遥测 → Verdict）
   ├── InterlockSupervisor  三通道裁决
   ├── 六域服务        staff / process / vision / resilience / safety / rules
   └── LocalQueue      离线队列 + MemoryOfflineStore 补传落地
```

**选型对比（答辩会被问）**

| 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|
| FastAPI + uvicorn | 自动文档、依赖注入、异步 | 多 6 个三方依赖；本项目 12 路遥测 1Hz，**并发量个位数**；毕设答辩环境是离线机器，装包可能失败 | ❌ |
| Flask | 轻 | 仍需 WSGI 服务器；SSE 需要额外长连接处理 | ❌ |
| **标准库 `ThreadingHTTPServer`** | **零依赖**，`python -m kitchen.api.server` 直接跑；SSE 就是一个不结束的 handler | 要自己写路由/JSON/CORS/静态托管（约 700 行，一次性成本） | ✅ |

真正的理由不是「省依赖」这么简单：**这套代码要在答辩现场的一台陌生 Windows 上跑起来**。
`pip install` 失败、端口被占、防火墙弹窗，任何一个环节出问题都是现场事故。标准库没有任何
可失败的环节。

---

## 三、鉴权与权限矩阵

四角色、九权限，**默认拒绝**（`auth._PERMISSIONS` 里没登记的一律 403）。

| 权限 | viewer | operator | lead | manager | 说明 |
|---|:--:|:--:|:--:|:--:|---|
| `state.read` | ✅ | ✅ | ✅ | ✅ | 所有只读页面 |
| `order.write` | | ✅ | ✅ | ✅ | 下单/派单/催菜/上灶/刷卡 |
| `recipe.write` | | | ✅ | ✅ | 配方新版本 |
| `param.write` | | | ✅ | ✅ | 阈值、加速因子、断网模拟 |
| `ticket.write` | | | ✅ | ✅ | 接单/关单/晨检复核 |
| `channel.trip` | | | ✅ | ✅ | 关阀/开风机/切电 |
| `channel.review` | | | ✅ | ✅ | CH3 复位投一票 |
| `channel.restore` | | | | ✅ | **只有管理员能复位** |
| `staff.write` | | | | ✅ | 人员/证照维护 |

**三条硬口径**

1. **`lead` 不能单方面复位 CH3**：组长能投一票（`channel.review`），但 `power_restore`
   要 `channel.restore`，只有 `manager` 有。这是「双通道复核」在权限层的体现——
   一票在 TFT、一票在 Web，任何一端都不能自己凑齐两票。
2. **令牌放 `sessionStorage`**：管理端是「一个标签页干完就关」的场景，比 `localStorage`
   少一个「忘关电脑、第二天别人打开还是登录态」的风险。XSS 两者都防不住，真正的防线
   是服务端不返回可执行脚本。
3. **登录失败锁 IP**：5 次失败锁 5 分钟。演示口令印在文档里，必须有一层防爆破。

---

## 四、数据通道：一个聚合快照 + 一路事件流

九个页面如果各拉三四个接口，切一次页面就是十几次往返；现场 wifi 2~5Mbps、延迟抖动大，
页面会明显「一顿一顿」。所以：

| 通道 | 内容 | 频率 |
|---|---|---|
| `GET /api/overview` | 12 遥测 + 三通道 + 降级 + 计数 + 保护状态 + 服务端时间（约 5KB） | 事件触发后 400ms 合并刷新 |
| `GET /api/stream` (SSE) | `hello` → `snapshot` → 增量事件（规则/保护/订单/通道/网络/准入/参数/工单） | 有事件就推 |
| 页面明细 | 订单、配方、人员、工单列表 | **只在进入页面时拉一次** |

**降级链**：SSE 连不上 → 自动退化为 3 秒轮询（页面顶部显示「轮询模式」）→ 再失败显示
「未连接到设备」。这条链保证「实时通道坏了」不会变成「页面白屏」。

**为什么事件要合并刷新**：一条 `param.save` 会连带产生 3 条事件（参数、审计、配置版本），
逐条刷新就是 3 次往返。400ms 窗口内的所有事件合并成一次 `overview`。

**事件上限 200 条**：后厨跑一整天能有几万条事件，无上限数组会让浏览器内存一路涨到卡死。

---

## 五、并发：三个必须加锁的地方

`ThreadingHTTPServer` + 采样线程 + 多个浏览器标签页 = 真并发。踩过的三个点：

1. **`TelemetryRing` 的 `deque`**：采样线程 `append`、请求线程遍历，会抛
   `RuntimeError: deque mutated during iteration`。症状是曲线页偶发 500，只在「正好在采样」
   的那一刻复现。→ 所有读写都过一把 `threading.Lock`。
2. **`AppState.overview()`**：内部要读队列、引擎、互锁、健康监控四处状态。不加锁会拼出
   一个「昨天的时间戳 + 今天的通道状态」的混合快照。→ 整段持锁（快照本身是纯读，锁竞争很小）。
3. **`LocalQueue` 落盘**：补传线程与采样线程同时写同一个 JSONL。→ 文件写入串行化 +
   `.part` 临时文件 + `os.fsync` + `os.replace` 原子替换。

---

## 六、接口契约（43 条 JSON + 1 条 SSE）

**只读**

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/overview` | 登录 | 九页共用聚合快照 |
| GET | `/api/health` | 匿名 | 探活（供反代/演示脚本） |
| GET | `/api/meta` | 登录 | 遥测口径、状态枚举、动作清单、角色 |
| GET | `/api/degrade` | 登录 | 降级报告（三端文案） |
| GET | `/api/telemetry?points=N` | 登录 | 12 路曲线（定长环形缓冲） |
| GET | `/api/interlock` | 登录 | 三通道状态机 + 触发源 + 复核票 |
| GET | `/api/orders` | 登录 | 订单/子项（可按状态过滤） |
| GET | `/api/dishes` | 登录 | 菜品 + 沽清 |
| GET | `/api/stations` | 登录 | 工位 + 占用 + 点火 |
| GET | `/api/stations/gantt` | 登录 | 占用段 + 转派留痕 + 排风 |
| GET | `/api/recipes` | 登录 | 配方 + SOP + **版本历史** |
| GET | `/api/batches` | 登录 | 批次 / 留样 / 消毒 / 溯源码 |
| GET | `/api/staff` `/api/staff/certificates` | 登录 | 人员资质、健康证台账 |
| GET | `/api/morning` | 登录 | 晨检问卷与复核状态 |
| GET | `/api/tickets` | 登录 | 三类工单合流 |
| GET | `/api/kpi` | 登录 | 出餐时长、违规数、处置时长、消毒达标率 |
| GET | `/api/params` | 登录 | 阈值表 + 档位 + **演示值↔真实值** |
| GET | `/api/audit?limit=N` | 登录 | 操作留痕（含被拒绝的动作） |
| GET | `/api/stream` | 令牌（query） | SSE（**不在 Router 上**，HTTP 层直接处理） |

**写入**

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` `/logout` | 匿名/登录 | 令牌 8 小时 |
| POST | `/api/orders` | order.write | 下单 |
| POST | `/api/orders/items/{id}/dispatch` | order.write | 派单（走资质四查） |
| POST | `/api/orders/items/{id}/urge` | order.write | 催菜（计数 + 置顶） |
| POST | `/api/orders/items/{id}/state` | order.write | 状态流转（非法跃迁 409） |
| POST | `/api/dishes/{id}/soldout` | order.write | 沽清/恢复 |
| POST | `/api/stations/occupy` `/release` | order.write | 上灶 / 出餐释放 |
| POST | `/api/admission/scan` | order.write | 刷卡准入（与 TFT 同一条链） |
| POST | `/api/morning/submit` `/review` | order.write / ticket.write | 晨检提交 / 复核放行 |
| POST | `/api/tickets/{id}/ack` `/close` | ticket.write | 接单 / 关单（消毒类拒关） |
| POST | `/api/recipes/{dish_id}/versions` | recipe.write | 配方**另存新版本** |
| PUT | `/api/params` | param.write | 阈值热更新（按 id 合并字段） |
| POST | `/api/params/time-scale` | param.write | 加速因子（1~3600） |
| POST | `/api/channels/{ch}/actions` | trip / restore | 三通道动作（`manual_confirmed`） |
| POST | `/api/channels/{ch}/review` | channel.review | 双通道复核投一票 |
| POST | `/api/network` | param.write | 模拟断网/恢复（触发补传） |
| POST | `/api/admin/flush` | param.write | 手动补传 |
| POST | `/api/demo/orders` `/inject` | order.write / channel.trip | 演示种子、信号注入 |

**错误语义**：`400` 参数错、`401` 未登录/令牌过期、`403` 权限不够、`404` 路径不存在、
`409` 状态冲突（互锁拒绝、非法跃迁、恢复未确认）、`429` 登录锁定。前端只按 `401` 跳登录页，
其余一律显示后端 `detail` 原文——**后端文案就是给现场看的话术**。

---

## 七、前端契约测试：为什么用「静态扫描 + 真 payload」双向核

三类 bug 在纯前端项目里几乎不可能靠人眼发现，而且都很「安静」：

| 症状 | 真实原因 | 现场表现 |
|---|---|---|
| 页面空白 / 一直转圈 | 前端调了不存在的接口，404 | 「这页没做吧」 |
| 图标长得不对 | 图标名拼错，`Icon.vue` fallback 到仪表盘 | 没人会报 |
| 表格整列空白 | 读了后端没给的字段，`undefined` 渲染成空串 | 「数据没采到？」 |

`tests/test_api_web_contract.py` 的做法：

1. **正向**：扫 `web/src` 里所有 `/api/...` 字面量（含 `` `/api/channels/${x}/actions` `` 这种模板串），
   归一化后逐条到路由表里查，缺一条就红；
2. **反向**：`POST/PUT` 接口如果前端一处都没调用，多半是页面漏做了，同样红；
3. **字段**：直调接口，断言页面渲染依赖的字段全在（`params.demo_map.rows` 的 5 个键、
   `meta.actions` 的 5 个键、`overview` 的 11 个分块……）；
4. **图标**：把 `<Icon name="x">` 与 `PATHS` 的键做集合比对；
5. **壳层**：8 条路由、视图文件、`index.html` 挂载点。

这套测试写完当场抓到 **3 个真实缺口**：工位页没有「上灶/释放」入口（甘特的空态文案却在说
「点『上灶』」）、资质页没有刷卡准入入口、`ControlView` 的审计字段写成 `entry.at`（后端给的是
`time`）。三个都是「页面能打开但功能不可用」，靠点页面很难发现。

---

## 八、八条硬口径

1. **`tick()` 内不做网络 IO**：`#1` 窗口 30 秒、`#3` 12 秒，HTTP 超时默认 3~10 秒；采样线程
   同步上传会把间隔拉到十几秒——保护还在，窗口全乱。补传只走 `flush()`。
2. **三通道动作必须带 `manual_confirmed`**：恢复类动作缺这个字段服务端直接 409。页面要求
   原样输入动作名只是 UI 防误触，**真正的闸门在服务端**，绕过页面调接口一样拦得住。
3. **阈值只提交「改过的行、改过的字段」**：`PUT /api/params` 按 `id` 合并；整表提交会把别人
   刚改的那一行用本地旧值覆盖回去，两个人同时维护时必然丢失修改且无提示。
4. **配方改版一律「另存新版本」**：订单创建时快照了 `recipe_id`。原地改会让「正在做的单」
   和「刚下的单」按两套配料称重——同一个菜两个标准，两个都不合格。新版本号取**最大 +1**
   （不是「当前启用 +1」），否则「停用 v3、启用 v2」时会撞上已存在的 v3。
5. **降级文案只在一处生成**：`build_degrade()` 出三端文案，前端 `DegradeBanner` 是唯一渲染点。
   各端自己拼文案必然出现「Web 说设备离线 / TFT 说网络异常 / 小程序说数据更新中」。
6. **断网 ≠ 不安全**：离线文案必须写「本地保护仍生效，数据待补传 N 条」。只显示红色「设备离线」，
   现场第一反应是去拉总闸，反而制造停业事故。
7. **消毒/复消类工单在管理端不可关单**：闭环是「重新消毒一次并达标」，跑一遍
   `DisinfectionService` 才会自动关单。页面上点「关单」等于跳过复消，批次照样出库，
   合规记录却是假的。后端对 `dis-*` / `rw-*` 直接拒绝。
8. **演示加速只压时间轴**：`time_scale` 只作用于「到期时刻」的换算（12s→12s/60），判定逻辑与
   阈值常数一条都没改。参数页必须能查到「设计值 ↔ 当前生效值」的对照表（A7-09 原文要求）。

---

## 九、部署与构建

**开发环境（dev）**

```bash
# 后端（热重载不需要，改代码重启即可）
python -m kitchen.api.server --port 8848 --time-scale 60

# 前端（vite dev，/api 代理到 8848）
cd web && npm run dev        # http://127.0.0.1:5173
```

**生产环境（prod，单机内网）**

```bash
cd web && npm ci && npm run build       # 产物 web/dist（约 745KB 未压缩 / 258KB gzip）
python -m kitchen.api.server --port 8848 --static web/dist
```

**部署与安全清单**

| 项 | dev | prod |
|---|---|---|
| CORS | `--allow-origin http://127.0.0.1:5173` | **不开**（同源托管 dist） |
| 账号口令 | 默认 `kitchen2026` | 必须设 `KITCHEN_ADMIN_PASSWORD` 或 `KITCHEN_ACCOUNTS` |
| 监听地址 | `127.0.0.1` | 内网 `0.0.0.0` 或反代 `nginx` + HTTPS |
| 静态缓存 | — | `index.html` no-cache、`/assets/*` 长缓存（文件名带 hash） |
| 演示接口 | 开启 | 关闭 `POST /api/demo/*` |
| 令牌 | 8 小时 | 同上（答辩期间够用，生产应更短 + 刷新） |

**两个本机踩到的坑**

1. **esbuild 清理临时文件报 `Access is denied`**：本机 `TEMP` 是 8.3 短名（`C:\Users\CHENZH~1\...`），
   esbuild 收尾时删不掉自己写的临时文件，构建直接失败。→ 构建前把 `TEMP`/`TMP` 指到普通路径：
   ```bash
   TEMP=/tmp/web-build TMP=$TEMP npm run build
   ```
2. **Windows 上 `SO_REUSEADDR` 语义相反**：Unix 下它能避免重启撞 `TIME_WAIT`，Windows 下它允许
   **第二个进程抢绑正在监听的端口**——表现是「新版本起来了，但请求有时打到旧进程」。
   所以按平台分开设（`allow_reuse_address = os.name != "nt"`）。

---

## 十、已知边界（诚实标注）

| 项 | 现状 | 影响 |
|---|---|---|
| 浏览器实机渲染 | **未在真实浏览器里逐页点过**（本机无头环境），靠 `vite build` 编译通过 + 接口契约测试保证 | 样式细节、极端分辨率需人工看一眼 |
| ECharts 组件 | 按需引入（Line/Bar/Gauge/Custom + 6 个组件），已从 1.03MB 降到 557KB | 新增图表类型必须补 `use()`，漏了是「空白且不报错」 |
| 前端单元测试 | **无**（无 JS 测试框架） | 逻辑正确性靠后端测试 + 契约测试兜 |
| 浏览器兼容 | 只按 Chromium 内核写（`ResizeObserver`、CSS 变量、`?.`） | 现场用 Chrome/Edge；国产双核浏览器需切极速模式 |
| 实时通道 | SSE，非 WebSocket | 单向足够（写操作走 POST）；nginx 需关 `proxy_buffering` |
| 审计落盘 | 内存 + `var/api/audit.jsonl` | 真实 MySQL `audit_log` 入库留给 A10 |
