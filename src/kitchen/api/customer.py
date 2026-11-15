# -*- coding: utf-8 -*-
"""顾客端通道：免登录点餐 / 查单 / 催菜 / 溯源 / 通知（A9）。

## 为什么单独开一条「顾客通道」而不是给顾客发账号

四角色体系（viewer/operator/lead/manager）是**给后厨和管理人员的**。顾客如果
登这套账号体系，等于把「谁能下单」的权限判断和「这个单是谁的」的归属判断
混在一起——guest 账号下单后，所有人都能用 guest 看到彼此的单。

顾客的真实诉求是「扫码开桌 → 点菜 → 看进度」，**没有登录环节**。所以这条
通道用「一次性桌台令牌」：

1. `POST /api/customer/session` 领一个 token（`secrets.token_hex`，纯内存）；
2. 下单 / 查单 / 催菜 / 通知都带这个 token，服务端按 token 过滤归属；
3. 令牌只活到服务重启——演示场景完全够（重新开桌即可），文档如实写。

## 判定仍在服务端

顾客通道**不复制任何业务逻辑**：下单走 `OrderService.place_order`（价格/配方
快照/沽清拒绝都是同一份代码），催菜走 `OrderService.urge`。这条通道只做三件
事：令牌校验、归属过滤、**顾客专属限频**（催菜 30 秒一次——不限频，演示时
观众连点二十下，后厨屏全是催菜红条）。

## 「哪个顾客下的单」能答到什么程度

顾客是匿名的，**没有姓名、手机号、openid、会员号**。但后厨和管理端确实需要
区分「这一桌的这一单」和「另一桌的那一单」，所以开桌时额外发一个脱敏编号
`customer_ref`（形如 `T03-A1B2`，桌号 + 令牌前 4 位）：

- 它会落到 `Order`、事件流、审计记录和 `/api/orders`，管理端看板 / TFT 队列屏
  / 小程序订单页都能看到；
- 它**不是身份**，只是「本次服务进程内的一次开桌会话」，重启后失去意义；
- 完整令牌绝不外发，也绝不出现在事件流或审计里；
- 顾客通知端点仍按 `order_no` 白名单过滤，A 桌拿不到 B 桌的编号。

真正按人识别（会员、积分、跨设备历史订单）需要 `wx.login` 换 openid +
用户授权 + 会员表，属于设计文档 09 §一明确划掉的范围，当前不做。

## 匿名路由与安全边界

`/api/customer/*` 全部 `anonymous=True`，handler 内部自校验 token。能看到的
数据只有：菜单（本来就是公开的）、自己的单、溯源（食品溯源按法规本来就是
给顾客看的）。**看不到**工位/人员/阈值/遥测——那些路由仍要求员工令牌。
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from ..domain import DomainError

__all__ = [
    "CustomerError", "CustomerAuthError", "CustomerRateError", "CustomerService",
]

#: 顾客催菜最小间隔（秒）。真实顾客可配置；演示 30s 足够看出「按钮被限频」
URGE_MIN_INTERVAL_SEC = 30.0

#: 通知事件的种类。顾客只关心自己的单怎么走，环境报警/工单一概不给
_CUSTOMER_KINDS = ("order", "cooking", "customer", "complaint")


class CustomerError(DomainError):
    """顾客通道参数错误（400）。"""


class CustomerAuthError(DomainError):
    """令牌缺失或无效（401）。"""


class CustomerRateError(CustomerError):
    """催菜太频繁（429）。"""


class CustomerService:
    """顾客会话 + 归属簿 + 催菜限频 + 通知过滤。挂进 `AppState`。"""

    def __init__(self, state) -> None:
        self.state = state
        # token → set[order_id]（归属簿，内存）
        self._orders: dict[str, set[int]] = {}
        # token → 会话档案。与归属簿同生命周期（都是纯内存、重启即失效）。
        # 顾客是匿名的，但后厨需要区分「哪一桌的哪一次开桌」，所以给一个
        # 脱敏编号；**绝不存姓名/手机号/openid**，也绝不把完整令牌外发。
        self._profiles: dict[str, dict[str, Any]] = {}
        # (token, item_id) → 上次催菜墙钟（限频）
        self._last_urge: dict[tuple[str, int], float] = {}

    # ------------------------------------------------------------ 会话
    def register(self, table_id: Any = None, guest_key: Any = None,
                 tags: Any = ()) -> dict[str, Any]:
        """开桌：发令牌 + 脱敏顾客编号。token 只在服务端内存里，重启即失效。

        `customer_ref` 形如 `T03-A1B2`（桌号 + 令牌前 4 位）或散客 `G-A1B2`。
        只取 4 位 hex 是刻意的：够在本次服务进程内区分不同开桌，又不能反推
        完整令牌。它会出现在订单、事件流和审计里，但**不是身份**，服务重启后
        同一个编号就失去意义。真正需要「谁下的单」必须走会员体系（当前不做）。
        """
        token = secrets.token_hex(16)
        table = self._coerce_table(table_id)
        suffix = token[:4].upper()
        ref = f"T{table:02d}-{suffix}" if table is not None else f"G-{suffix}"
        key = str(guest_key or "").strip()[:64]
        # 设备级匿名档案（A11）：小程序首次启动生成并落本地，跨开桌不变。
        # 只在开桌时绑定一次，之后查画像/客诉都必须「令牌 + guest_key」双校验。
        if key:
            self.state.guest.bind(key, tags or ())
        self._orders[token] = set()
        self._profiles[token] = {
            "table_id": table, "customer_ref": ref,
            "guest_key": key or None,
        }
        return {
            "customer_token": token,
            "customer_ref": ref,
            "table_id": table,
            "guest_key": key or None,
            "note": "令牌仅本次服务进程有效，重启后请重新开桌",
            "identity_note": "匿名会话编号，不含姓名/手机号，后厨按此区分本桌订单",
        }

    @staticmethod
    def _coerce_table(value: Any) -> int | None:
        """桌号容错：缺字段、乱填、越界一律当散客，不让脏数据进订单。"""
        if value is None or str(value).strip() == "":
            return None
        try:
            table = int(value)
        except (TypeError, ValueError):
            return None
        return table if 0 < table < 1000 else None

    def profile_of(self, token: Any) -> dict[str, Any]:
        """已校验令牌的会话档案（只读）。内部用，不走 HTTP 路由。"""
        return dict(self._profiles.get(self._require_token(token), {}))

    def order_ids_of(self, token: Any) -> set[int]:
        """当前令牌名下的订单 id 集合（A11 客诉关联订单时做归属校验）。"""
        return set(self._orders[self._require_token(token)])

    def require_guest(self, token: Any, guest_key: Any) -> dict[str, Any]:
        """A11 双因子校验：令牌有效 **且** guest_key 与该令牌绑定一致。

        `guest_key` 是客户端生成的，单凭它查数据等于把接口敞开——别人猜到
        一个 key 就能看别人画像和客诉。所以先过令牌，再比对绑定关系。
        """
        key = self._require_token(token)
        profile = self._profiles.get(key, {})
        bound = profile.get("guest_key")
        asked = str(guest_key or "").strip()
        if not bound or not asked or bound != asked:
            raise CustomerAuthError("顾客档案与当前会话不匹配，请重新开桌")
        return {"guest_key": bound,
                "customer_ref": profile.get("customer_ref", ""),
                "table_id": profile.get("table_id")}

    def _require_token(self, token: Any) -> str:
        text = str(token or "").strip()
        if not text or text not in self._orders:
            raise CustomerAuthError("顾客令牌缺失或已失效，请重新开桌")
        return text

    # ------------------------------------------------------------ 下单
    def place(self, token: Any, items: list, table_id: Any) -> dict[str, Any]:
        """顾客下单。直接走 `OrderService.place_order`——价格快照、配方版本
        锁定、沽清整单拒绝（并列出全部沽清项）全部复用，**这里零业务逻辑**。"""
        key = self._require_token(token)
        if not isinstance(items, list) or not items:
            raise CustomerError("items 至少要一项：[{code, qty}]")
        resolved: list[tuple[str, int]] = []
        for row in items:
            if not isinstance(row, dict):
                raise CustomerError("items 每项是 {code, qty}")
            code = str(row.get("code") or row.get("id") or "").strip()
            if not code:
                raise CustomerError("items 每项缺 code")
            try:
                qty = max(1, int(row.get("qty", 1)))
            except (TypeError, ValueError):
                raise CustomerError("qty 必须是正整数") from None
            resolved.append((code, qty))
        table = None
        if table_id is not None and str(table_id).strip():
            try:
                table = int(table_id)
            except (TypeError, ValueError):
                raise CustomerError("table_id 必须是整数") from None
        profile = self._profiles.get(key, {})
        order = self.state.orders.place_order(
            table_id=table, items=resolved,
            source="miniapp", channel="customer",
            client_event_id=None, now=self.state.to_datetime(),
            customer_ref=profile.get("customer_ref"),
            guest_key=profile.get("guest_key"),
        )
        self._orders[key].add(order.id)
        # 和员工下单同一条事件流：管理端看板/TFT 队列屏/顾客通知三端共用，
        # **不在顾客通道另开一套事件**——否则管理端看不到顾客单的状态推进。
        # 带上 customer_ref 后，后厨/审计能追到「哪一桌的哪次开桌」，
        # 但通知端点仍按 order_no 白名单过滤，别人的编号不会被推给顾客。
        self.state.emit("order", {"order_no": order.order_no,
                                  "state": order.state.value,
                                  "by": "customer", "channel": "customer",
                                  "customer_ref": order.customer_ref,
                                  "table_id": order.table_id})
        return {"order_id": order.id, "order_no": order.order_no,
                "table_id": order.table_id,
                "customer_ref": order.customer_ref,
                "guest_key": order.guest_key}

    # ------------------------------------------------------------ 查单
    def orders_of(self, token: Any) -> dict[str, Any]:
        """顾客自己的单 + 预计出餐时间。**只回自己的**，别人的单一个字段都不给。"""
        key = self._require_token(token)
        profile = self._profiles.get(key, {})
        now = self.state.to_datetime()
        rows = []
        for order_id in sorted(self._orders[key]):
            try:
                order = self.state.orders.order(order_id)
            except Exception:  # noqa: BLE001 - 单被清理/不存在就跳过
                continue
            items = []
            for item in order.items:
                remain = self._remain_minutes(order.due_at, now)
                items.append({
                    "id": item.id,
                    "dish_name": item.dish_name,
                    "qty": item.qty,
                    "state": item.state.value,
                    "station_id": item.station_id,
                    "urge_count": item.urge_count,
                    # 三档进度语义给前端直接画条：做完了/快好了/还在做
                    "eta_state": self._eta_state(item.state.value, remain),
                    "remain_minutes": remain,
                })
            rows.append({
                "id": order.id, "order_no": order.order_no,
                "state": order.state.value, "table_id": order.table_id,
                "customer_ref": order.customer_ref,
                "created_at": order.created_at.isoformat(timespec="seconds"),
                "due_at": order.due_at.isoformat(timespec="seconds"),
                "due_epoch": order.due_at.timestamp(),
                "remain_minutes": self._remain_minutes(order.due_at, now),
                "items": items,
            })
        return {"total": len(rows), "orders": rows,
                "customer_ref": profile.get("customer_ref"),
                "table_id": profile.get("table_id"),
                "server_time": now.isoformat(timespec="seconds")}

    @staticmethod
    def _remain_minutes(due_at, now) -> int:
        if due_at is None:
            return 0
        remain = (due_at - now).total_seconds() / 60.0
        return max(0, round(remain))

    @staticmethod
    def _eta_state(state: str, remain: int) -> str:
        if state in ("served", "done", "completed"):
            return "done"
        if remain <= 3:
            return "soon"
        return "cooking"

    # ------------------------------------------------------------ 催菜
    def urge(self, token: Any, item_id: Any) -> dict[str, Any]:
        """顾客催菜：走 `OrderService.urge`，叠加顾客专属限频。

        限频用**墙钟**不受 `time_scale` 影响——演示时钟加速时顾客也不应该
        每 0.1 秒催一次。"""
        key = self._require_token(token)
        try:
            iid = int(item_id)
        except (TypeError, ValueError):
            raise CustomerError("item_id 必须是整数") from None
        # 归属校验：不是这个顾客的单，连「有没有这单」都不告诉他（404 而非 403）
        owner = self._owner_of(iid)
        if owner is None or owner != key:
            # 不是这个顾客的单：404 而非 403——不把别人的单是否存在泄漏给陌生 token
            from ..domain import NotFoundError

            raise NotFoundError("没有这个菜品条目（或它不属于当前桌台）")
        gate = (key, iid)
        last = self._last_urge.get(gate)
        now = time.monotonic()
        if last is not None and now - last < URGE_MIN_INTERVAL_SEC:
            wait = int(URGE_MIN_INTERVAL_SEC - (now - last)) + 1
            raise CustomerRateError(
                f"催菜太频繁，请 {wait} 秒后再试（每道菜 30 秒一次）")
        order = self.state.orders.urge(iid, actor="customer",
                                       now=self.state.to_datetime())
        self._last_urge[gate] = now
        item = next((it for it in order.items if it.id == iid), None)
        count = 0 if item is None else item.urge_count
        self.state.emit("order", {"item_id": iid, "urge": count,
                                   "order_no": order.order_no,
                                   "by": "customer", "channel": "customer"})
        return {"item_id": iid, "urge_count": count}

    def _owner_of(self, item_id: int) -> str | None:
        for token, order_ids in self._orders.items():
            for order_id in order_ids:
                try:
                    order = self.state.orders.order(order_id)
                except Exception:  # noqa: BLE001
                    continue
                if any(it.id == item_id for it in order.items):
                    return token
        return None

    # ------------------------------------------------------------ 通知
    def notifications(self, token: Any, since: Any = 0) -> dict[str, Any]:
        """订阅消息的**轮询模拟**（A9-06）：真机要接微信订阅消息模板（需企业
        资质，H4）；模拟版先给「服务端通知队列 + 小程序轮询」，等资质到位后
        把 `push()` 的消费者换成微信 API 即可，数据流不用动。"""
        key = self._require_token(token)
        try:
            since_seq = int(since or 0)
        except (TypeError, ValueError):
            raise CustomerError("since 必须是整数") from None
        mine_items: set[int] = set()
        mine_orders: set[str] = set()
        mine_guest = self._profiles.get(key, {}).get("guest_key")
        for order_id in self._orders[key]:
            try:
                order = self.state.orders.order(order_id)
            except Exception:  # noqa: BLE001
                continue
            mine_orders.add(order.order_no)
            mine_items.update(it.id for it in order.items)
        rows = []
        for row in self.state.events(since=since_seq, limit=200):
            if row.get("kind") not in _CUSTOMER_KINDS:
                continue
            # 客诉事件没有 order_no/item_id，按 guest_key 归属匹配；
            # 别人的客诉编号/内容一个字段都不给（A10 联调的教训）
            if (row.get("order_no") not in mine_orders
                    and row.get("item_id") not in mine_items
                    and not (mine_guest and row.get("guest_key") == mine_guest)):
                continue
            rows.append({
                "seq": row["seq"], "kind": row["kind"], "time": row["time"],
                "order_no": row.get("order_no"), "item_id": row.get("item_id"),
                "state": row.get("state"), "urge": row.get("urge"),
                "message": self._message_of(row),
            })
        return {"total": len(rows), "notifications": rows,
                "event_seq": self.state.event_seq}

    @staticmethod
    def _message_of(row: dict[str, Any]) -> str:
        kind = row.get("kind")
        state = row.get("state")
        if kind == "complaint":
            return str(row.get("message") or "客诉状态更新")
        if kind == "order" and row.get("urge") is not None:
            return f"已催菜（第 {row['urge']} 次），后厨已收到"
        if kind == "order" and state == "created":
            return "订单已受理，后厨正在安排"
        if kind == "customer" and row.get("op") == "urge":
            return f"已催菜（第 {row.get('urge_count')} 次），后厨已收到"
        if kind == "cooking" and row.get("op") == "serve":
            return "您的菜已出餐，请留意取餐"
        if kind == "order" and state in ("cooking", "queued", "dispatched"):
            return "您的菜正在制作"
        if kind == "order" and state in ("served", "done"):
            return "您的菜已上桌"
        if kind == "order" and state == "cancelled":
            # 退菜必须通知（A10-01 联调补）：顾客不下单界面刷新就永远不知道
            return "您的菜已退，如有疑问请联系服务员"
        if kind == "ticket":
            return "您的订单有异常，请联系服务员"
        if kind == "customer":
            return "订单状态更新"
        return "订单状态更新"


# --------------------------------------------------------------------- handlers
# 路由层（routes.py `_customer()`）直接查这张表。**保持 handler 纯函数风格**：
# 只做参数解析与转发，业务判定在 CustomerService / 域层。
def _session(state, data: dict[str, Any]) -> dict[str, Any]:
    # 开桌时把桌号一并登记：后厨看到的桌号不再只依赖下单那一刻的传值。
    payload = state.customer.register(
        data.get("table_id"), data.get("guest_key"),
        data.get("tags") or ())
    # 开桌也发一条事件：管理端「今日开桌数」不用另开口子。
    # 该事件没有 order_no/item_id，顾客通知端点会把它过滤掉，不互漏桌台。
    state.emit("customer", {"op": "session", "table_id": payload["table_id"],
                            "customer_ref": payload["customer_ref"]})
    return payload


def _menu(state, data: dict[str, Any]) -> dict[str, Any]:
    """顾客菜单：复用 dish_view，**但砍掉内部字段**（配方 id / 工位类型）——
    顾客不需要，也不该知道后厨怎么分工。"""
    from .views import dish_view

    payload = dish_view(state, {})
    dishes = []
    for row in payload["dishes"]:
        if not row["enabled"]:
            continue  # 停售菜不上顾客菜单（跟纸菜单一致）
        dishes.append({
            "id": row["id"], "code": row["code"], "name": row["name"],
            "category": row["category"], "price": row["price"],
            "sold_out": row["sold_out"],
            "estimate_minutes": row["estimate_minutes"],
        })
    return {"total": len(dishes), "dishes": dishes,
            "note": "" if dishes else "菜单还没就绪，请稍候"}


def _place(state, data: dict[str, Any]) -> dict[str, Any]:
    payload = state.customer.place(
        data.get("customer_token") or data.get("token"),
        data.get("items"), data.get("table_id"))
    state.audit.record(
        actor="customer", role="customer", action="customer.order",
        target=payload["order_no"],
        after={"items": data.get("items"),
               "customer_ref": payload.get("customer_ref"),
               "table_id": payload.get("table_id")})
    state.emit("customer", {"op": "order", "order_no": payload["order_no"],
                            "customer_ref": payload.get("customer_ref")})
    return payload


def _orders(state, data: dict[str, Any]) -> dict[str, Any]:
    return state.customer.orders_of(
        data.get("customer_token") or data.get("token"))


def _urge(state, data: dict[str, Any]) -> dict[str, Any]:
    payload = state.customer.urge(
        data.get("customer_token") or data.get("token"),
        data.get("item_id"))
    state.emit("customer", {"op": "urge", "item_id": payload["item_id"],
                            "urge_count": payload["urge_count"]})
    return payload


def _trace(state, data: dict[str, Any]) -> dict[str, Any]:
    """扫码溯源（公开只读）。`view()` 里不存在/为空会抛 DomainError 子类，
    统一转 404/400，不让堆栈漏出去。"""
    code = str(data.get("code") or "").strip()
    view = state.trace.view(code)
    payload = view.to_dict()
    # 顾客端的码图直接指向本服务的 label.png 端点（server.py 专门分支返回 PNG）
    payload["qr_url"] = f"/api/customer/trace/label.png?code={payload['code']}"
    return payload


def _notifications(state, data: dict[str, Any]) -> dict[str, Any]:
    return state.customer.notifications(
        data.get("customer_token") or data.get("token"), data.get("since"))


#: 路由层查这张表（`Router._customer()`）
CUSTOMER_HANDLERS = {
    "session": _session,
    "menu": _menu,
    "place": _place,
    "orders": _orders,
    "urge": _urge,
    "trace": _trace,
    "notifications": _notifications,
}
