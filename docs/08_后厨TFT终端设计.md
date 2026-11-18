# 08 · 后厨 TFT 终端设计（模拟版先行 + 真机路线）

> 适用版本：2026-11-12（A8 模拟版完成）
> 对应任务：`05_系统记录/20261112_全项目流程任务清单.md` 的 **A8-01 ~ A8-06**
> 上游依赖：`07_管理端Web与接口契约.md`（43 条接口与本终端共用）、
> `04_功能规格与联动阈值.md`（#3/#8/#12/#13/#16 判据）
> 代码落点：`src/kitchen/tft/`（4 模块）+ `src/kitchen/api/`（新增 cooking 路由）
> 演示入口：`python tools/tft_demo.py`

---

## 一、范围与验收口径

A8 验收原文是「**真机与手机终端至少一条能完整演示**」。硬件（2.8" TFT +
RC522 + DFPlayer）尚未到货，所以本阶段交付的是**模拟终端**——行为与固件
同构（接口、屏结构、离线策略、语音编号全部一致），板子到货后按本文的映射
表写固件，`tools/tft_demo.py` 即验收脚本。手机终端（A8-06 降级方案）已由
A7 的 Web 端覆盖：旧手机浏览器打开管理端即可。

| A8 任务 | 模拟版落点 | 真机对应 |
|---|---|---|
| A8-01 字库/触摸/电源 | 未做（等硬件），本文 §六 路线 | 2.8" ILI9341 + lv_font_conv 裁剪 |
| A8-02 核心 4 屏 | `tft/screens.py` 帧模型 + `render.py` | LVGL 页面（§五映射表） |
| A8-03 RFID 联动 | `scan()` + `/api/admission/scan` + 晨检链 | RC522 中断 → 同一条接口 |
| A8-04 DFPlayer 播报 | `tft/audio.py` 编号表 + `DfPlayerSim` | DFPlayer Mini（§四编号表） |
| A8-05 断网重连与队列缓存 | `tft/terminal.py` + `LocalQueue` | NVS 环形缓冲 |
| A8-06 降级方案 | A7 Web 端（旧手机浏览器） | 不需要额外开发 |

---

## 二、架构：终端是「瘦客户端 + 本地缓冲」，不是独立系统

```
ESP32-TFT（固件）/ 模拟终端（src/kitchen/tft/）
   │  GET /api/orders?open=1     ← 5s 轮询：队列快照（离线时用本地缓存）
   │  GET /api/events?since=N    ← 增量事件 → 播报（A8-04 的驱动源）
   │  GET /api/staff             ← 白名单缓存（离线刷卡弱校验）
   │  POST /api/admission/scan   ← 刷卡（#8 晨检/#3 返岗都在服务端判）
   │  POST /api/cooking/*        ← 称重/步骤确认/出餐（#12/#13）
   ▼
ApiServer（43+5 条接口，与 Web 端共用同一份）
   ▼
AppState → AdmissionController / CookingBoard / LocalQueue ...
```

**为什么 TFT 不直连业务域（进程内调用）而走 HTTP**：真机只能走 HTTP；
如果模拟版用进程内调用，两版行为会分叉（离线模拟就是假的）。模拟版与
固件的差异被压缩到**传输层一处**（`DirectTransport` ↔ `esp_http_client`），
其余逻辑（离线策略、弱校验、播报）共用。

### 模拟版与真机的同构表

| 模拟版 | 固件 |
|---|---|
| `DirectTransport`（进程内 Router.call） | HTTP 客户端（WiFi + token） |
| `set_link(False)` | WiFi 断开事件 |
| `LocalQueue`（jsonl 落盘） | NVS/Flash 环形缓冲 |
| `DfPlayerSim.play(no)` | UART 指令 `0x01` 播曲目 N |
| `render.py` 60x22 文本帧 | LVGL 页面（§五） |

---

## 三、离线策略（A8-05 论文口径）

**判定永远在服务端，离线只做缓冲**——这条是刻意的设计决策，理由：

1. 资质/晨检/证照数据每天变，终端缓存一定落后；离线时终端**没有资格**做最终放行；
2. 把判定放终端，意味着「两套判定逻辑」，联调时会出现「TFT 放行、管理端
   拒绝」的灵异事件，答辩演示翻车。

| 场景 | 行为 |
|---|---|
| 在线刷卡 | POST `/api/admission/scan`，服务端全量判定（证/晨检/排班） |
| 离线刷卡·未知卡 | **红拒不入队**（宁可误拒：进后厨是食安事故，误拒只是走一趟管理端） |
| 离线刷卡·证无效 | 红拒不入队（弱校验用缓存白名单） |
| 离线刷卡·白名单通过 | **黄屏放行** + 动作入 `LocalQueue`（恢复后服务端复核） |
| 离线工序确认/称重 | 动作入队，屏显示「已入队待同步」 |
| 恢复 | `sync()` 逐条回放：4xx=业务拒绝（记 `denied` + 播报 006，**不重试**）；5xx=网络问题（留队列下次再试） |
| 离线期间队列屏 | 渲染**最后一次在线快照**，状态栏红框 +「离线 xx 秒 + 待同步 N 条」 |

**队列缓存的一致性代价（要如实写进论文的局限）**：离线时新订单不会出现在
队列屏（服务端收不到终端的轮询）；师傅看的是红框 + 时间戳，自行判断
「这份队列还能不能信」。这是离线优先模式的固有边界，不是 bug。

### ack 的 at-least-once 语义

`LocalQueue.ack()` 只改内存，journal 重写按 `compact_min_acked=64` 阈值
触发——**掉电重启后已 ack 的条目可能复活重放**。采集端由服务端
`offline_records UNIQUE(device_id,seq)` 幂等去重兜住；TFT 动作类接口中
`scan`/`confirm`/`weigh` 天然幂等、`serve` 由状态机 `InvalidTransition`
挡住，所以重复回放是安全的（at-least-once + 各端幂等）。

---

## 四、DFPlayer 语音编号表（固件与 SD 卡的契约）

**编号一经发布不得改**（固件烧录后 mp3 文件名与编号绑定）；新语音只往后追加。

| 编号 | 键 | 文本 | 优先级 | 触发 |
|---|---|---|---|---|
| 001 | boot | 设备自检完成 | 5 | 上电 |
| 002 | new_order | 来了新单，请注意 | 3 | 事件 `order.created` |
| 003 | urge | 有客人催菜 | 2 | 事件 `order.urge` |
| 004 | alarm | 请注意，设备报警 | 0 | 事件 `rule`/`guard` |
| 005 | card_ok | 身份确认 | 4 | 事件 `admission.ok` |
| 006 | card_denied | 身份未通过，请联系组长 | 1 | 事件 `admission.denied` / 同步被拒 |
| 007 | weigh_bad | 配料超差，请重新称重 | 1 | 事件 `cooking.weigh` 且 `ok=False` |
| 008 | serve | 出餐确认 | 4 | 事件 `cooking.serve` |
| 009 | offline | 网络断开，转入离线模式 | 2 | WiFi 断开 |
| 010 | replay | 网络恢复，数据已同步 | 3 | `sync()` 完成 |
| 011 | ccp_block | 关键控制点未确认，禁止出餐 | 1 | 出餐 409 且含 CCP |
| 012 | sensor_fault | 传感器故障，请检修 | 0 | 事件 `fault-escalate` |

播放规则（固件同样遵守）：同一编号 **3 秒去抖**（报警抖动不重播）；
优先级数值小者抢占。**称重合格不响**（否则师傅被播到麻木，真正的超差
反而被忽略）。

---

## 五、4 屏帧模型 → LVGL 映射表

帧模型（`tft/screens.py`）是**唯一契约**：渲染层只做「帧 → 像素」的机械
翻译。320x240、16px 字模 → 每行约 20 汉字、正文 13 行。

| 屏 | 帧模型字段 | LVGL 对象 | 行数预算 |
|---|---|---|---|
| status（帧头） | `clock/device/online/banner/offline_sec/pending_actions/hint` | 顶部 `lv_bar` + 2 个 `lv_label`；`banner=offline` 时整屏四周红色边框 `lv_obj_set_style_border_color` | 2~3 |
| queue | `rows[{group,dish,order_no,qty,urge,station,mark}] / truncated / empty_text` | `lv_table` 或 `lv_list`；`mark=催` 行描红 | 13 |
| steps | `weighs[{ingredient,target_g,measured_g,state}] / steps[{step_no,title,state,flags}] / locked / ready` | 上半 `lv_table`（称重）、下半 `lv_list`（步骤）；`state=current` 高亮、`flags` 含 CCP 时加图标 | 13 |
| card | `tone(green/yellow/red) / name / lines[]` | 大字号 `lv_label` 居中 + 底色按 tone；4 秒后自动返回队列屏 | 全屏 |

**队列截断规则**：`QUEUE_MAX_ROWS=10`，超出部分计数进 `truncated`——
模拟器放得下 20 行也不许放，否则「模拟器能看 20 行、真机只能 12 行」
会被当成 bug。

**离线红框必须在每一屏**：状态栏是帧头不是独立屏，`render_frame()`
和 LVGL 的容器边框都从同一个 `status.banner` 派生。师傅在工序屏时断网
看不见红框，离线模式就形同虚设。

---

## 六、真机路线（A8-01，板子到货后）

1. **屏驱动**：2.8" ILI9341 SPI 320x240，ESP32-S3 直驱；LVGL 8.x
   （`lv_port` 模板裁剪，帧缓冲 1/10 屏）。
2. **字库**：`lv_font_conv` 按「帧模型实际用字集合」生成 16px 字模——
   不内嵌全量 GB2312（1.5MB Flash 放不下）；集合从 `tools/tft_demo.py`
   的输出与接口 `note/detail` 字段抓取，脚本生成清单后人工补漏。
3. **触摸校准**：XPT2046 四点校准，系数存 NVS；开机 `boot` 语音 +
   「点十字」引导页。
4. **电源**：5V/2A 适配器 + AMS1117-3.3；背光 GPIO PWM 可调（夜里降 30%）。
5. **RC522**：SPI + IRQ 中断触发（不轮询），UID 归一化与
   `staff.repository.normalize_uid` 同规则（去 `:`/空格、大写）。
6. **固件主循环**：`tft_demo.py` 的九段就是九个状态机状态；HTTP 客户端
   走同样的 5s 轮询 + token（`Authorization: Bearer`）。

---

## 七、服务端新增接口（A8 一并落地）

| 接口 | 权限 | 说明 |
|---|---|---|
| `GET /api/cooking` | 登录即可 | 活动工序会话列表 |
| `GET /api/cooking/{item_id}` | 登录即可 | 工序分步屏数据（没开会话也给全 pending 骨架） |
| `POST /api/cooking/{item_id}/weigh` | order.write | #12 称重（超差锁下一步，409） |
| `POST /api/cooking/{item_id}/confirm` | order.write | #13 步骤确认（顺序/锁定/身份三道闸门） |
| `POST /api/cooking/{item_id}/serve` | order.write | 出餐（门禁一次列全，409） |
| `POST /api/cooking/{item_id}/reset` | order.write | 显式重开会话（进度清零） |
| `GET /api/events?since=N` | 登录即可 | 事件流轮询版（TFT 5s 拉，浏览器走 SSE） |

同时两处既有接口的行为补全：

- `POST /api/admission/scan` 现在会刷 **#3 返岗复位**（`offline.mark_human`）——
  不接这一笔，师傅明明回来了、气阀却被 12 秒前的那次离人关掉；
- `CookingBoard` 挂进 `AppState`（`process/cooking.py`），#12/#13 从
  「域层已写、无人调用」变成「跨请求保留进度、API 可达」。

**错误码口径**：`NotReadyError`/`StepLockedError` 映射 **409**（请求合法、
当前状态不允许）；400 会让 TFT 以为是「报文写错了」，现场会去查请求
而不是补配菜。

---

## 八、8 条硬口径（写错即事故）

1. **判定永远在服务端**，终端离线只缓冲不裁决（§三）。
2. **离线刷卡宁可误拒**：未知卡/证无效红拒不入队，白名单通过才黄屏放行入队。
3. **恢复回放 4xx 不重试**：业务拒绝重试还是拒绝；记 `denied` + 播报 006。
   5xx 留队列下次 sync。
4. **语音编号只追加不改号**；同一编号 3 秒去抖；称重合格不响。
5. **离线红框在每一屏**（状态栏是帧头，不是一屏）。
6. **队列屏超行截断**：`QUEUE_MAX_ROWS=10`，模拟器与真机同预算。
7. **出餐门禁一次列全**（NotReadyError 409 的 detail 含全部原因）——
   「补一样被拒一次」的循环会把师傅逼到打电话叫人关系统。
8. **emit 的 body 不用 `kind` 做键名**：`_emit()` 先放 `kind` 再展开 body，
   body 里的 `kind` 会覆盖事件种类，订阅端按 kind 过滤就漏了（本阶段踩过：
   cooking 事件全漏播）。改用 `op`。

---

## 九、测试与验收证据

- `tests/test_api_cooking.py`（21 条）：三道闸门（顺序/锁定/身份）、出餐
  门禁、权限、审计、刷卡副作用；
- `tests/test_tft_terminal.py`（21 条）：播报去抖/合格不响、离线队列缓存、
  弱校验三个方向、恢复回放（含「断电重启队列不丢」）、帧模型行数；
- 契约测试 `test_api_web_contract.py` 的 EXEMPT 白名单登记 cooking 四条
  （TFT 端接口，Web 不提供工序入口是刻意的：要刷卡身份，防远程代确认）；
- 全量 **1203 passed**；`tools/tft_demo.py` 退出码 0（九段全链路）。

## 十、边界与未做（答辩如实说）

- 模拟终端未在真 LVGL 环境渲染（文本帧与 LVGL 是同一契约的两份实现）；
- 真机（字库/触摸/电源/RC522 实物）等板子到货，按 §六 路线做；
- 离线期间新订单不进队列屏（§三局限）；
- ack 复活重放由各接口幂等性兜住，未做端侧去重表。
