# -*- coding: utf-8 -*-
"""食安合规凭证的路由与 handler（A13，仅管理端）。

## 为什么全是 GET

检查表、一页纸、日报、导出都是**从已有事实读物化出的交付物**，不产生新的
业务事实。做成 POST 会让人误以为「点一下就完成了一次合规检查」——实际上
检查结论取决于当天真实采集到的晨检 / 留样 / 消毒 / 工单数据，接口改不了它。

唯一有副作用的是 `report()` 会写一份日报快照和一条 LLM 审计，但那是**留痕**
不是业务动作，且按日期幂等（同日重生成覆盖），放 GET 语义上可接受。

## 权限

`compliance.read` = manager + lead。组长要拿这张表去现场核缺口，不给说不过去；
但观察员（viewer）/ 操作员（operator）不给——合规缺口明细属于管理信息。
导出同样只到组长一级，不单独放宽。
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["register_compliance_routes"]


def _num(value: Any, default: int = 50) -> int:
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
def c_checklist(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.compliance.checklist(q.get("date"))


def c_dossier(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.compliance.dossier(q.get("batch_id"))


def c_report(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.compliance.report(q.get("date"))


def c_export(state, q: dict[str, Any]) -> dict[str, Any]:
    return state.compliance.export_csv(q.get("date"))


def c_runs(state, q: dict[str, Any]) -> dict[str, Any]:
    runs = state.compliance.runs(_num(q.get("limit"), 50))
    return {
        "total": len(runs), "runs": runs,
        "snapshots": state.compliance.snapshots(30),
        "provider": {
            "mode": getattr(state.compliance.provider, "mode", "rule"),
            "model": getattr(state.compliance.provider, "model", "rule-engine-v1"),
            "prompt_version": "compliance-daily-v1",
        },
        "journal": state.compliance.journal_stats,
    }


# ---------------------------------------------------------------- 注册
def register_compliance_routes(router) -> None:
    """由 `Router._register()` 末尾调用。整体摘掉扩展只需删这一行。"""
    read = "compliance.read"
    router.add("GET", "/api/compliance/checklist",
               _view(c_checklist), permission=read)
    router.add("GET", "/api/compliance/dossier",
               _view(c_dossier), permission=read)
    router.add("GET", "/api/compliance/report",
               _view(c_report), permission=read)
    router.add("GET", "/api/compliance/export",
               _view(c_export), permission=read)
    router.add("GET", "/api/compliance/runs",
               _view(c_runs), permission=read)
