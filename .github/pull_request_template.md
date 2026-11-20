## 改动类型

- [ ] 缺陷修复
- [ ] 新功能
- [ ] AI 场景（只加 `AdvSpec` + `specs.py:SCENARIOS` 一行）
- [ ] 文档 / 口径
- [ ] 部署 / 运维
- [ ] 重构 / 命名

## 影响面

改了哪些域？（`api` / `guest` / `finance` / `compliance` / `ai` / `storage` / `mqtt` / `tft` / `miniapp` / `web` / `deploy`）

是否触碰业务状态机（订单 / 派单 / 门禁 / 阈值）？

> AI 改动必须确认：**只出建议，不改业务状态**。结果里不得有 `approve` / `decision` / `amount`；
> 异常不关阀、派单不下发、问答不改门禁。

## 本地验证

```bash
python tools/ci.py
```

关卡：审查守卫（RL001/RL002）/ ruff 语法 / R5 时钟 / 数字守卫 / pytest 全量 / migrate check / web_check / miniapp_check / PR 体积提示。贴结果（`X/X 关通过`）：

## 云端验证（改 `deploy/` 或 API 时必填）

```bash
bash deploy/verify-cloud.sh http://127.0.0.1
python tools/miniapp_cloud_check.py
```

> 验收脚本如从开发机跑且出现 p100 抖动，加 `--self-host http://127.0.0.1`
> 或 `--self-host-json baseline.json` 做 R6 两端对照，不要直接判服务端回归。

贴结果（`10/10`、只读/写模式通过数）：

## 是否需要迁移

- [ ] 否
- [ ] 是：编号 `NNN_xxx`，已在**非空存量卷**跑 `bash deploy/apply-migrations.sh`（R2），`python tools/migrate.py check` 通过

## 是否需要改文档数字

- [ ] 否
- [ ] 是：只改 `docs/requirements.json`（唯一数据源），再跑 `python tools/consistency_check.py --fix` 同步 README。**不手抄数字**（R4）

## 自检清单（审查报告 R1–R6）

- [ ] **R1**：凡"服务起来了"的结论，同时验了 HTTP 状态 **和** 业务内容（首页标题 / JS 资源 / 菜单条数 / 订单数）
- [ ] **R2**：改 DDL 已在非空存量卷验证，且 `migrate check` 通过
- [ ] **R3**：改 `deploy/` 已本地 `docker compose config` + 云端 `verify-cloud.sh` 双验
- [ ] **R4**：数字来自 `docs/requirements.json`，未手抄；`consistency_check.py` 绿
- [ ] **R5**：参数名没有用 `now` 传 epoch；`state.py` 只暴露 `monotonic()` / `wall_epoch()`
- [ ] **R6**：外部验收带了服务器本机基线对照，抖动归因先比两端
- [ ] 六条 AI 护栏未放松：只出建议 · 离线降级 · 白名单 · 逐次审计 · 不喂隐私 · 可复现

## 审查结论（审查者填，作者不要代填）

> 标准与清单见 [`docs/15_代码审查标准与流程.md`](../docs/15_代码审查标准与流程.md)。
> 自审同样要填：写"自审（24h 冷却）"即可，别留空。

- 审查人 / 日期：
- 审查方式：自审（24h 冷却）/ 自审 + AI 预审 / 外部（姓名）
- ⛔ P0：0 条（**必须为 0 才能合并**）
- 🔴 P1：N 条 → ☐ 已修 ☐ 已开 issue #___（期限：___）
- 🟡 P2：N 条 → ☐ 已记入债务表
- 结论：☐ 通过　☐ 有条件通过（P1 限期）　☐ 打回

> 合并门禁：CI 全绿 + P0=0 + P1 有结论 + 上表填完。红线：净变更 > 400 行或 > 10 文件须说明为何不能拆。
