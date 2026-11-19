# -*- coding: utf-8 -*-
"""多融合 AI 中枢（A28 扩展模块）。

## 这个包解决什么

开题后学校明确"功能基本不删减，主要要求是多融合 AI"。项目里 AI 已有三处：
`guest`（画像 + 客服）、`finance`（日报点评）、`compliance`（合规点评）。它们的
共同底座是 `guest/llm.py`：可插拔 provider + 六条护栏 + 双层降级 + `llm_runs`
审计。但有两个问题：

1. 三个域**各自直接调 provider**，护栏只覆盖 guest，日报点评是各写各的；
2. 新增 AI 场景没有统一入口，容易被各处直连模型 → 幻觉改业务状态。

本包把"场景"抽象成 `guest/llm.py::AdvSpec` 的实例：自定义提示词 + 白名单 +
确定性离线规则。`AiService` 只做三件事：按 spec 调 `run_guarded`、只读聚合各域
已有审计、输出一屏总览。**它不持有任何业务状态，也不写任何业务表。**

## 六条底线（与 guest/llm.py 完全一致，不因"多融合"而放松）

1. AI 只出建议——`AiResult` 无 approve/decision/amount，派单/关阀/门禁仍由规则裁决；
2. 必须有离线规则降级——断网、缺 key、限流都不返回 500；
3. 结构化输出 + 白名单——模型自由文本只进 summary/reply；
4. 逐次审计——model / prompt_version / latency / degraded_reason 全留痕；
5. 不喂隐私——只传聚合状态，无 openid / 手机号 / 卡号 / 顾客 key；
6. 可复现——prompt_version 随结果落库，同一 facts 与 mode 输出稳定。

## 三个已落地场景

| scenario | 域 | 只出建议，不改 |
|---|---|---|
| `anomaly_explain` | 安全链路 | 不关阀、不改告警状态 |
| `dispatch_hint` | 派单 | 不改订单/工位状态 |
| `compliance_qa` | 食安合规 | 不改门禁、不改检查结论 |
"""
from .service import AiService, ai_overview, ai_runs, advise

__all__ = ["AiService", "advise", "ai_overview", "ai_runs"]
