# -*- coding: utf-8 -*-
"""路由表与分发。

## 为什么自己写路由而不是用框架

工程口径是**零三方依赖**（`pyproject.toml` 里 `dependencies = []`）。管理端要在
答辩机、树莓派、甚至路由器后面的一台旧笔记本上直接 `python -m kitchen.api.server`
跑起来，多一个 pip 依赖就多一个装不上的现场。标准库 `http.server` 够用：
并发量是「一间后厨 + 几个评委」，QPS 个位数。

## 分发要处理的三件事

1. **路径参数**：`/api/orders/12/items/3/dispatch` → `{"order_id": "12", "item_id": "3"}`。
   只支持「整段是占位符」，不做正则路径——路径里混正则，出问题时没人愿意读。
2. **GET 的 query 与 POST 的 body 统一成 `data`**：handler 只认一个字典，
   否则每个 handler 都要写「GET 读 query、POST 读 body」两套分支，必然有一处写反。
3. **异常 → 状态码**：在这里统一映射，handler 内部只抛业务异常。

## 一条硬规则：写接口不缓存

所有写接口的响应都带 `Cache-Control: no-store`（在 `server.py` 加）。浏览器
缓存一个「复位成功」的响应，用户点十次看起来只生效一次，现场会以为设备坏了。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..channels import ActuationError, InterlockBlocked, MisrouteError, UnknownActionError
from ..domain import DomainError, InvalidTransition, NotFoundError, StaleVersionError
from ..process.orders import OrderError, SoldOutError
from . import views
from .actions import ACTIONS
from .auth import AuthError, PermissionDenied, Session
from .accounts import LoginService
from .customer import (
    CUSTOMER_HANDLERS,
    CustomerAuthError,
    CustomerRateError,
    CustomerService,
)

log = logging.getLogger("kitchen.api.routes")

__all__ = ["NotFound", "Route", "Router", "http_status"]

_PARAM = re.compile(r"^\{([a-zA-Z_][a-zA-Z0-9_]*)\}$")


class NotFound(LookupError):
    """路径不存在（与「资源不存在」区分：后者是 404 但走业务异常）。"""


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    handler: Callable[[Any, Session, dict, dict], Any]
    #: 是否允许匿名访问（只有登录与健康检查）
    anonymous: bool = False
    #: 需要的权限（None = 只要求已登录）
    permission: str | None = None

    def match(self, method: str, segments: list[str]) -> dict[str, str] | None:
        if self.method != method:
            return None
        parts = [part for part in self.pattern.strip("/").split("/") if part]
        if len(parts) != len(segments):
            return None
        params: dict[str, str] = {}
        for template, actual in zip(parts, segments):
            found = _PARAM.match(template)
            if found:
                params[found.group(1)] = actual
                continue
            if template != actual:
                return None
        return params


def http_status(exc: BaseException) -> int:
    """业务异常 → HTTP 状态码。**顺序敏感**：子类必须排在父类前面。"""
    if isinstance(exc, PermissionDenied):
        return 403
    if isinstance(exc, CustomerRateError):
        return 429
    if isinstance(exc, CustomerAuthError):
        # 顾客令牌缺失/失效：和员工 401 同语义，前端统一引导重新开桌
        return 401
    if isinstance(exc, AuthError):
        return int(getattr(exc, "status", 401))
    if isinstance(exc, PermissionError):
        return 401
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, InterlockBlocked):
        # 409：请求本身合法，但当前通道状态不允许（有人正在处理同一件事）
        return 409
    if isinstance(exc, (MisrouteError, UnknownActionError)):
        return 400
    if isinstance(exc, (SoldOutError, InvalidTransition, StaleVersionError)):
        return 409
    from ..process.cooking import NotReadyError, StepLockedError

    if isinstance(exc, (NotReadyError, StepLockedError)):
        # 409：请求合法但当前状态不允许——出餐门禁没过 / 称重超差锁步骤。
        # 400 会让 TFT 以为是「报文写错了」，现场会去查请求而不是补配菜。
        return 409
    if isinstance(exc, (DomainError, ActuationError, OrderError)):
        return 400
    if isinstance(exc, KeyError):
        return 404
    if isinstance(exc, (TypeError, ValueError)):
        return 400
    return 500


class Router:
    """路由注册 + 分发。构造完即为只读，多个请求线程共用。"""

    def __init__(self, state, *, login: LoginService | None = None) -> None:
        self.state = state
        self.login = login
        self.routes: list[Route] = []
        self._register()

    # ------------------------------------------------------------ 注册
    def add(self, method: str, pattern: str, handler: Callable,
            *, anonymous: bool = False, permission: str | None = None) -> None:
        self.routes.append(Route(
            method=method.upper(), pattern="/" + pattern.strip("/"),
            handler=handler, anonymous=anonymous, permission=permission,
        ))

    def _view(self, name: str) -> Callable:
        func = getattr(views, name)

        def handler(state, session, params, data):
            merged = dict(data)
            merged.update(params)
            return func(state, merged)

        return handler

    def _action(self, name: str) -> Callable:
        func = ACTIONS[name]

        def handler(state, session, params, data):
            return func(state, session, params, data)

        return handler

    def _customer(self, name: str) -> Callable:
        """顾客通道的 handler 包装。**不套 Session**（顾客不在四角色体系里），
        但写动作的审计 actor 固定为 `customer`，留痕可追。"""

        def handler(state, session, params, data):
            merged = dict(data)
            merged.update(params)
            return CUSTOMER_HANDLERS[name](state, merged)

        return handler

    def _register(self) -> None:
        # ---------------- 鉴权
        self.add("POST", "/api/auth/login", self._login, anonymous=True)
        self.add("POST", "/api/auth/logout", self._logout)
        self.add("GET", "/api/auth/me", self._me)

        # ---------------- 状态与环境
        self.add("GET", "/api/overview", self._overview)
        self.add("GET", "/api/health", self._ping, anonymous=True)
        self.add("GET", "/api/meta", self._view("meta_view"))
        self.add("GET", "/api/degrade", self._degrade)
        self.add("GET", "/api/telemetry", self._view("telemetry_view"))
        self.add("GET", "/api/interlock", self._view("interlock_view"))
        self.add("GET", "/api/events", self._view("events_view"))

        # ---------------- A1 运行时库：体检 + 跨重启历史
        self.add("GET", "/api/admin/db/stats", self._view("db_stats_view"))
        self.add("GET", "/api/admin/db/history", self._view("db_history_view"))
        self.add("GET", "/api/admin/scheduler", self._view("scheduler_view"))

        # ---------------- A7-02 订单与调度
        self.add("GET", "/api/orders", self._view("order_view"))
        self.add("POST", "/api/orders", self._action("order.place"), permission="order.write")
        self.add("POST", "/api/orders/items/{item_id}/dispatch",
                 self._action("order.dispatch"), permission="order.write")
        self.add("POST", "/api/orders/items/{item_id}/urge",
                 self._action("order.urge"), permission="order.write")
        self.add("POST", "/api/orders/items/{item_id}/state",
                 self._action("order.move"), permission="order.write")
        self.add("GET", "/api/dishes", self._view("dish_view"))
        self.add("POST", "/api/dishes/{dish_id}/soldout",
                 self._action("dish.soldout"), permission="recipe.write")

        # ---------------- A7-03 工位甘特
        self.add("GET", "/api/stations", self._view("station_view"))
        self.add("GET", "/api/stations/gantt", self._view("gantt_view"))
        self.add("POST", "/api/stations/occupy",
                 self._action("station.occupy"), permission="order.write")
        self.add("POST", "/api/stations/release",
                 self._action("station.release"), permission="order.write")

        # ---------------- A7-05 配方与工艺
        self.add("GET", "/api/recipes", self._view("recipe_view"))
        self.add("POST", "/api/recipes/dishes",
                 self._action("recipe.dish"), permission="recipe.write")
        self.add("POST", "/api/recipes/{dish_id}/versions",
                 self._action("recipe.version"), permission="recipe.write")

        # ---------------- A8 工序分步（#12 称重 / #13 SOP / 出餐门禁）
        self.add("GET", "/api/cooking", self._view("cooking_view"))
        self.add("GET", "/api/cooking/{item_id}", self._view("cooking_view"))
        self.add("POST", "/api/cooking/{item_id}/weigh",
                 self._action("cooking.weigh"), permission="order.write")
        self.add("POST", "/api/cooking/{item_id}/confirm",
                 self._action("cooking.confirm"), permission="order.write")
        self.add("POST", "/api/cooking/{item_id}/serve",
                 self._action("cooking.serve"), permission="order.write")
        self.add("POST", "/api/cooking/{item_id}/reset",
                 self._action("cooking.reset"), permission="order.write")

        # ---------------- A7-06 批次与人员
        self.add("GET", "/api/batches", self._view("batch_view"))
        self.add("GET", "/api/staff", self._view("staff_view"))
        self.add("GET", "/api/staff/certificates", self._view("certificate_view"))
        self.add("GET", "/api/morning", self._view("morning_view"))
        self.add("POST", "/api/admission/scan",
                 self._action("admission.scan"), permission="order.write")
        self.add("POST", "/api/morning/submit",
                 self._action("morning.submit"), permission="order.write")
        self.add("POST", "/api/morning/review",
                 self._action("morning.review"), permission="ticket.write")

        # ---------------- A7-07 工单
        self.add("GET", "/api/tickets", self._view("ticket_view"))
        self.add("POST", "/api/tickets/{ticket_id}/ack",
                 self._action("ticket.ack"), permission="ticket.write")
        self.add("POST", "/api/tickets/{ticket_id}/close",
                 self._action("ticket.close"), permission="ticket.write")

        # ---------------- A7-08 KPI
        self.add("GET", "/api/kpi", self._view("kpi_view"))

        # ---------------- A7-09 远程控制与参数
        self.add("GET", "/api/params", self._view("param_view"))
        self.add("PUT", "/api/params",
                 self._action("param.save"), permission="param.write")
        self.add("POST", "/api/params/time-scale",
                 self._action("param.time_scale"), permission="param.write")
        self.add("POST", "/api/channels/{channel}/actions",
                 self._action("channel.action"))
        self.add("POST", "/api/channels/{channel}/review",
                 self._action("channel.review"), permission="channel.review")

        # ---------------- 网络与补传
        self.add("POST", "/api/network",
                 self._action("network.set"), permission="param.write")
        self.add("POST", "/api/admin/flush",
                 self._action("queue.flush"), permission="param.write")

        # ---------------- 演示
        self.add("POST", "/api/demo/orders",
                 self._action("demo.seed_orders"), permission="order.write")
        self.add("POST", "/api/demo/inject",
                 self._action("demo.inject"), permission="channel.trip")

        # ---------------- 审计
        self.add("GET", "/api/audit", self._view("audit_view"),
                 permission="param.write")

        # ---------------- A9 顾客端通道（免登录：令牌在 query/body 自校验）
        # 1. 能看到的只有：菜单、自己的单、溯源。工位/人员/阈值仍是员工接口。
        # 2. handler 内部自己校验 customer_token，Router 的 anonymous 只表示
        #    不要求员工登录。
        self.add("POST", "/api/customer/session",
                 self._customer("session"), anonymous=True)
        self.add("GET", "/api/customer/menu",
                 self._customer("menu"), anonymous=True)
        self.add("POST", "/api/customer/orders",
                 self._customer("place"), anonymous=True)
        self.add("GET", "/api/customer/orders",
                 self._customer("orders"), anonymous=True)
        self.add("POST", "/api/customer/orders/items/{item_id}/urge",
                 self._customer("urge"), anonymous=True)
        self.add("GET", "/api/customer/trace",
                 self._customer("trace"), anonymous=True)
        self.add("GET", "/api/customer/notifications",
                 self._customer("notifications"), anonymous=True)

        # ---------------- A11 扩展模块：顾客画像 / AI 客服 / 客诉与补偿审批
        # 独立注册，想整体摘掉这个扩展只需删这一行调用
        from .guest_api import register_guest_routes
        register_guest_routes(self)
        # A12 经营账本（仅管理端）。同样独立成文件，删这一行即可整体摘掉。
        from .finance_api import register_finance_routes
        register_finance_routes(self)

    # ------------------------------------------------------------ 特殊 handler
    def _login(self, state, session, params, data) -> dict[str, Any]:
        if self.login is None:
            raise PermissionDenied("服务端未配置登录服务")
        subject = str(data.get("username") or data.get("subject") or "").strip()
        password = str(data.get("password") or "")
        if not subject or not password:
            # 缺凭据属于「认证没通过」，401 让前端统一跳登录页；
            # 用 403 会让前端以为「登进来了但没权限」
            raise AuthError("请填写账号与口令")
        token, issued = self.login.login(
            subject, password, source=str(data.get("source") or ""),
            audit=state.audit,
        )
        return {"token": token, "session": issued.to_payload()}

    def _logout(self, state, session, params, data) -> dict[str, Any]:
        # 无状态令牌：服务端没有会话可清，这里只留一条审计。
        # 前端负责丢 token；真要全端失效得换 secret 重启（见 auth 模块说明）。
        state.audit.record(actor=session.subject, role=session.role,
                           action="auth.logout", target=session.subject)
        return {"ok": True, "note": "已退出（令牌仍在有效期内，前端丢弃即可）"}

    def _me(self, state, session, params, data) -> dict[str, Any]:
        return {"session": session.to_payload(), "server_time":
                state.to_datetime().isoformat(timespec="seconds")}

    def _overview(self, state, session, params, data) -> dict[str, Any]:
        return state.overview()

    def _degrade(self, state, session, params, data) -> dict[str, Any]:
        return state.degrade()

    def _ping(self, state, session, params, data) -> dict[str, Any]:
        return {"ok": True, "ticks": state.ticks,
                "server_time": state.to_datetime().isoformat(timespec="seconds")}

    # ------------------------------------------------------------ 匹配
    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        segments = [part for part in path.strip("/").split("/") if part]
        for route in self.routes:
            params = route.match(method.upper(), segments)
            if params is not None:
                return route, params
        raise NotFound(f"{method.upper()} {path} 没有对应接口")

    def call(
        self,
        method: str,
        path: str,
        *,
        data: Mapping[str, Any] | None = None,
        session: Session | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """分发一次请求。**任何异常都转成 (status, payload)**，不让线程炸掉。"""
        from .auth import require

        body = dict(data or {})
        try:
            route, params = self.resolve(method, path)
        except NotFound as exc:
            return 404, {"error": "not_found", "detail": str(exc), "path": path}
        try:
            if route.anonymous:
                active = session
            else:
                if session is None:
                    # 401 而不是 403：前端靠这个码决定「跳登录页」还是「提示无权」
                    raise AuthError("需要登录（缺少或过期的令牌）")
                active = session
                if route.permission:
                    require(active.role, route.permission)
            payload = route.handler(self.state, active, params, body)
            return 200, {"ok": True, "data": payload}
        except Exception as exc:  # noqa: BLE001 - 统一出口，按类型映射状态码
            status = http_status(exc)
            if status >= 500:
                log.exception("接口 %s %s 内部错误", method, path)
            return status, {
                "ok": False,
                "error": type(exc).__name__,
                "detail": str(exc),
                "status": status,
            }
