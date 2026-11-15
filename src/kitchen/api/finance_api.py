# -*- coding: utf-8 -*-
"""经营账本模块的路由与 handler（A12 扩展模块，仅管理端）。

## 为什么只上管理端

经营账（毛利、净利、期间费用）是**店内经营数据**，顾客小程序、后厨 TFT
都不该看到。上一轮 A11 客诉模块是三端都有，是因为它本来就是顾客发起的；
这一轮若为了「三端对称」硬塞进小程序，等于把成本结构暴露给顾客。
功能上三端对称不是目标，**权责对称**才是。

## 两条权限

| 权限 | 角色 | 说明 |
|---|---|---|
| `finance.read` | manager | 看账、看日报、导出 |
| `finance.write` | manager | 维护标准成本、录入期间费用 |

组长（lead）/ 操作员（operator）/ 观察员（viewer）一律 403。经营数据
比阈值配置更敏感，宁可收窄也不要在答辩现场被问「谁能改成本」。
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["register_finance_routes"]


def _num(value: Any, default: int = 7) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _view(func: Callable) -> Callable:
    def handler(state, session, params, data):
        merged = dict(data)
        merged.update(params)
        return func(state, merged)
    return handler


# ---------------------------------------------------------------- 读视图
def finance_overview(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.finance.overview(
        days=_num(q.get("days"), 7),
        end=q.get("end") or None, start=q.get("start") or None)


def finance_entries(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.finance.entries_view(
        days=_num(q.get("days"), 7), end=q.get("end") or None,
        start=q.get("start") or None,
        kind=str(q.get("kind") or ""), limit=_num(q.get("limit"), 300))


def finance_dishes(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.finance.dish_costs_view()


def finance_overhead(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.finance.overhead_view(q.get("date") or None)


def finance_report(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.finance.report(q.get("date") or None)


def finance_export(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.finance.export_csv(
        days=_num(q.get("days"), 7), end=q.get("end") or None,
        start=q.get("start") or None)


def finance_ai_runs(state, q: dict[str, Any]) -> dict[str, Any]:
    runs = state.finance.ai_runs(_num(q.get("limit"), 50))
    return {"total": len(runs), "runs": runs,
            "provider": {
                "mode": getattr(state.finance.provider, "mode", "rule"),
                "model": getattr(state.finance.provider, "model", "rule-engine-v1"),
                "prompt_version": "finance-daily-v1",
            },
            "journal": state.finance.journal_stats}


# ---------------------------------------------------------------- 写动作
def finance_set_cost(state, session, params, data) -> dict[str, Any]:
    return {"dish": state.finance.set_cost(
        data.get("dish_id"), data.get("cost"),
        actor=session.subject if session else "unknown")}


def finance_set_overhead(state, session, params, data) -> dict[str, Any]:
    return {"overhead": state.finance.set_overhead(
        str(data.get("date") or ""),
        labor=data.get("labor") or 0, rent=data.get("rent") or 0,
        utilities=data.get("utilities") or 0, other=data.get("other") or 0,
        actor=session.subject if session else "unknown")}


# ---------------------------------------------------------------- 注册
def register_finance_routes(router) -> None:
    """由 `Router._register()` 末尾调用。整体摘掉扩展只需删这一行。"""
    read = "finance.read"
    write = "finance.write"

    router.add("GET", "/api/finance/overview",
               _view(finance_overview), permission=read)
    router.add("GET", "/api/finance/entries",
               _view(finance_entries), permission=read)
    router.add("GET", "/api/finance/dishes",
               _view(finance_dishes), permission=read)
    router.add("GET", "/api/finance/overhead",
               _view(finance_overhead), permission=read)
    router.add("GET", "/api/finance/report",
               _view(finance_report), permission=read)
    router.add("GET", "/api/finance/export",
               _view(finance_export), permission=read)
    router.add("GET", "/api/finance/ai-runs",
               _view(finance_ai_runs), permission=read)

    router.add("POST", "/api/finance/costs",
               finance_set_cost, permission=write)
    router.add("POST", "/api/finance/overhead",
               finance_set_overhead, permission=write)
