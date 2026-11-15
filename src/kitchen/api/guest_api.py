# -*- coding: utf-8 -*-
"""A11 顾客服务模块的路由与 handler（顾客端 + 管理端）。

## 为什么单独一个文件注册路由

主 `routes.py::_register()` 已经 120+ 行，再塞 12 条路由会更难读；而且这个
模块是**扩展演示模块**，独立成文件后想整体摘掉只需删 `register_guest_routes`
一行调用，不会在主路由表里留一堆悬空引用。

## 两条通道的信任边界

| 通道 | 认证 | 能做什么 |
|---|---|---|
| `/api/customer/guest/*` | `anonymous=True` + handler 内 `require_guest(token, guest_key)` | 只看/改自己的档案与工单；提交、补证 |
| `/api/guest/*` | 员工登录态 + Router 的 permission | 看全部工单、核证据、终审、看 AI 调用审计 |

顾客端**绝不**只凭 `guest_key` 查数据：`guest_key` 是客户端生成的，伪造一个
就能冒用别人档案。`require_guest()` 会同时校验令牌有效、令牌绑定的
`guest_key` 与传参一致，两者缺一不可。

## AI 的边界

`submit` 同步返回 AI 的分类与安抚话术（`ai_reply`），但所有状态迁移、补偿
金额、审批结论都只在管理端 action 里发生。这个模块里没有任何一处把
`ai_suggestion.need_human` 或 `confidence` 当作审批依据。
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["register_guest_routes", "CUSTOMER_GUEST_HANDLERS"]


# ---------------------------------------------------------------- 工具
def _int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是整数") from None


def _token(data: dict[str, Any]) -> str:
    return str(data.get("customer_token") or data.get("token") or "").strip()


# ---------------------------------------------------------------- 顾客通道
def _c_profile(state, data: dict[str, Any]) -> dict[str, Any]:
    """「我的」页：匿名档案 + 画像 + 本档案下的工单。"""
    key = str(data.get("guest_key") or "").strip()
    state.customer.require_guest(_token(data), key)
    return state.guest.profile_view(key, refresh=bool(data.get("refresh")))


def _c_complaints(state, data: dict[str, Any]) -> dict[str, Any]:
    key = str(data.get("guest_key") or "").strip()
    state.customer.require_guest(_token(data), key)
    return state.guest.complaint_view(guest_key=key,
                                      state=str(data.get("state") or "") or None)


def _c_submit(state, data: dict[str, Any]) -> dict[str, Any]:
    return state.guest.submit(
        token=_token(data), guest_key=str(data.get("guest_key") or "").strip(),
        text=data.get("text"), order_id=data.get("order_id"),
        item_id=data.get("item_id"),
        wants_refund=bool(data.get("wants_refund")),
        # 请求体字段名刻意叫 compensation 而不是 method：method 会和
        # `Router.call(method, path, data=...)` / 测试夹具的 method 形参撞名，
        # 而且「补偿方式」跟 HTTP 方法同名本身就是歧义。
        method=str(data.get("compensation") or data.get("method") or "redo"),
        evidences=data.get("evidences"),
        client_event_id=data.get("client_event_id"),
    )


def _c_supplement(state, data: dict[str, Any]) -> dict[str, Any]:
    complaint = state.guest.supplement(
        _int(data.get("complaint_id") or data.get("id"), "complaint_id"),
        token=_token(data), guest_key=str(data.get("guest_key") or "").strip(),
        evidences=data.get("evidences") or [],
        expected_version=data.get("expected_version"),
    )
    # 与管理端 action 同形状：写操作统一返回 {complaint: ...}
    return {"complaint": complaint}


CUSTOMER_GUEST_HANDLERS: dict[str, Callable] = {
    "profile": _c_profile,
    "complaints": _c_complaints,
    "submit": _c_submit,
    "supplement": _c_supplement,
}


# ---------------------------------------------------------------- 管理端 views
def guest_complaint_view(state, query: dict[str, Any]) -> dict[str, Any]:
    return state.guest.complaint_view(
        guest_key=str(query.get("guest_key") or "") or None,
        state=str(query.get("state") or "") or None)


def guest_profile_view(state, query: dict[str, Any]) -> dict[str, Any]:
    key = str(query.get("guest_key") or "").strip()
    return state.guest.profile_view(key, refresh=bool(query.get("refresh")))


def guest_llm_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """AI 调用审计：答辩用来解释「哪条是模型出的、哪条是规则降级」。"""
    runs = state.guest.llm_runs(int(query.get("limit") or 50))
    return {
        "total": len(runs), "runs": runs,
        "provider": {"mode": state.guest.provider.mode,
                     "model": state.guest.provider.model,
                     "prompt_version": "guest-v1"},
        "journal": state.guest.journal_stats,
    }


def guest_overview_view(state, query: dict[str, Any]) -> dict[str, Any]:
    rows = state.guest.all_complaints()
    by_state: dict[str, int] = {}
    for row in rows:
        by_state[row.state] = by_state.get(row.state, 0) + 1
    runs = state.guest.llm_runs(200)
    return {
        "total": len(rows), "pending": state.guest.pending_count,
        "overdue": state.guest.overdue(),
        "by_state": by_state,
        "profiles": len(state.guest._profiles),  # 只读汇总，不外泄 key
        "ai": {
            "calls": len(runs),
            "live": sum(1 for r in runs if r["mode"] == "live"),
            "rule": sum(1 for r in runs if r["mode"] == "rule"),
            "degraded": sum(1 for r in runs if r["degraded_reason"]),
        },
        "fund_note": "演示环境无支付链路，补偿审批只生成凭证，不发生实际退款",
    }


# ---------------------------------------------------------------- 管理端 actions
def guest_review(state, session, params, data) -> dict[str, Any]:
    payload = state.guest.review_evidence(
        _int(params.get("complaint_id"), "complaint_id"),
        actor=session.subject if session else "unknown",
        evidence_ids=data.get("evidence_ids") or [],
        expected_version=data.get("expected_version"),
        note=str(data.get("note") or ""),
    )
    return {"complaint": payload}


def guest_approve(state, session, params, data) -> dict[str, Any]:
    payload = state.guest.approve(
        _int(params.get("complaint_id"), "complaint_id"),
        actor=session.subject if session else "unknown",
        role=session.role if session else "",
        expected_version=data.get("expected_version"),
        method=str(data.get("compensation") or data.get("method") or "redo"),
        note=str(data.get("note") or ""),
    )
    return {"complaint": payload}


def guest_reject(state, session, params, data) -> dict[str, Any]:
    payload = state.guest.reject(
        _int(params.get("complaint_id"), "complaint_id"),
        actor=session.subject if session else "unknown",
        role=session.role if session else "",
        reason=str(data.get("reason") or ""),
        expected_version=data.get("expected_version"),
    )
    state.audit.record(actor=session.subject if session else "unknown",
                       role=session.role if session else "",
                       action="guest.complaint_reject",
                       target=payload["complaint_no"],
                       after={"reason": payload["reject_reason"]})
    return {"complaint": payload}


def guest_portrait_refresh(state, session, params, data) -> dict[str, Any]:
    key = str(params.get("guest_key") or data.get("guest_key") or "").strip()
    return state.guest.profile_view(key, refresh=True)


GUEST_ACTIONS: dict[str, Callable] = {
    "review": guest_review,
    "approve": guest_approve,
    "reject": guest_reject,
    "portrait": guest_portrait_refresh,
}


# ---------------------------------------------------------------- 注册
def register_guest_routes(router) -> None:
    """由 `Router._register()` 末尾调用。权限点见 `api/auth.py::_PERMISSIONS`。"""

    def _cust(name: str) -> Callable:
        def handler(state, session, params, data):
            merged = dict(data)
            merged.update(params)
            return CUSTOMER_GUEST_HANDLERS[name](state, merged)
        return handler

    def _view(func: Callable) -> Callable:
        def handler(state, session, params, data):
            merged = dict(data)
            merged.update(params)
            return func(state, merged)
        return handler

    def _act(name: str) -> Callable:
        def handler(state, session, params, data):
            return GUEST_ACTIONS[name](state, session, params, data)
        return handler

    # 顾客端：匿名，但 handler 内 require_guest 同时校验令牌与档案绑定
    router.add("GET", "/api/customer/guest/profile",
               _cust("profile"), anonymous=True)
    router.add("GET", "/api/customer/guest/complaints",
               _cust("complaints"), anonymous=True)
    router.add("POST", "/api/customer/guest/complaints",
               _cust("submit"), anonymous=True)
    router.add("POST", "/api/customer/guest/complaints/{complaint_id}/supplement",
               _cust("supplement"), anonymous=True)

    # 管理端：登录即可看，写操作按权限点收口
    router.add("GET", "/api/guest/overview", _view(guest_overview_view))
    router.add("GET", "/api/guest/complaints", _view(guest_complaint_view))
    router.add("GET", "/api/guest/profiles/{guest_key}", _view(guest_profile_view))
    router.add("GET", "/api/guest/llm-runs", _view(guest_llm_view))
    router.add("POST", "/api/guest/complaints/{complaint_id}/review",
               _act("review"), permission="guest.review")
    router.add("POST", "/api/guest/complaints/{complaint_id}/approve",
               _act("approve"), permission="guest.approve")
    router.add("POST", "/api/guest/complaints/{complaint_id}/reject",
               _act("reject"), permission="guest.review")
    router.add("POST", "/api/guest/profiles/{guest_key}/portrait",
               _act("portrait"), permission="guest.review")
