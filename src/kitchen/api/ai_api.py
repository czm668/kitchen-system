# -*- coding: utf-8 -*-
"""多融合 AI 中枢的路由（A28 扩展模块，仅管理端）。

## 权限与边界

| 权限 | 角色 | 说明 |
|---|---|---|
| `ai.read` | 观察员及以上 | 看多融合面板与审计（只读，无隐私数据） |
| `ai.advise` | 组长及以上 | 请求一次建议（不写业务状态，但需要权限防止顾客端刷模型调用） |

三个 `advise` 端点返回的都是**建议**：异常归因不关阀、派单建议不下发、
合规问答不改门禁。前端必须把"建议"和"已执行"在 UI 上分开表述——后端已经在
每个响应里带了 `advice_only=true` 与 `slot_boundary` 字段供界面标注。

只上管理端：这三场景是给值班/管理人员看的解释，顾客小程序与 TFT 不暴露。
"""
from __future__ import annotations

from typing import Any, Callable

from ..ai import advise as _advise
from ..ai import ai_overview, ai_runs

__all__ = ["register_ai_routes"]


def _view(func: Callable) -> Callable:
    def handler(state, session, params, data):
        merged = dict(data)
        merged.update(params)
        return func(state, merged)
    return handler


def ai_overview_view(state, q: dict[str, Any]) -> dict[str, Any]:
    return ai_overview(state)


def ai_runs_view(state, q: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = int(q.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return ai_runs(state, limit)


def _adviser(scenario: str) -> Callable:
    """生成一个只读取 facts、返回建议的 handler（不碰 session 身份）。"""
    def handler(state, session, params, data) -> dict[str, Any]:
        # facts 只收白名单字段的浅拷贝，前端塞再多字段也不会被喂给模型；
        # 隐私字段（openid/phone/token/key）在 llm.py 层还有第二道过滤。
        allowed = {
            "kind", "reading", "threshold", "detail", "advice",           # anomaly
            "queue", "idle_stations", "free_staff", "staff_pool_enabled",  # dispatch
            "question", "items", "overall", "date",                        # compliance_qa
        }
        facts = {k: v for k, v in dict(data or {}).items() if k in allowed}
        return _advise(state, scenario, facts)
    return handler


def register_ai_routes(router) -> None:
    """由 `Router._register()` 末尾调用。删这一行即可整体摘掉扩展。"""
    read, write = "ai.read", "ai.advise"

    router.add("GET", "/api/ai/overview", _view(ai_overview_view), permission=read)
    router.add("GET", "/api/ai/runs", _view(ai_runs_view), permission=read)
    router.add("POST", "/api/ai/advise/anomaly",
               _adviser("anomaly_explain"), permission=write)
    router.add("POST", "/api/ai/advise/dispatch",
               _adviser("dispatch_hint"), permission=write)
    router.add("POST", "/api/ai/advise/compliance",
               _adviser("compliance_qa"), permission=write)
