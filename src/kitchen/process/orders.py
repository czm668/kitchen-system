# -*- coding: utf-8 -*-
"""订单与状态机（A4-01）：`order_events` 是唯一事实源，`orders.state` 是物化结果。

## 四条不能省的设计

**1. 订单状态是算出来的**（`rollup_order`）。手改状态一定会漏：小程序退了凉菜、
TFT 又确认了热菜，两个入口各改各的，最终 `orders.state` 与 `order_items` 打架，
而甘特图、日报、大屏读的都是前者。

**2. 每次写状态都落一条 `order_events`**，`replay()` 能从事件流复原出完全一致的
当前状态。这条不是「为了审计好看」——断网补传（#17）时，服务端收到的就是
乱序事件，没有可重放的推进逻辑，补传只能按时间戳覆盖，一覆盖就会把后到的
「已出餐」冲成「制作中」。所以 `test_重放事件流得到同一状态` 是本模块的验收项。

**3. 幂等键 `(channel, client_event_id)`**。三端并发时同一动作会重复上送
（Web 重试、TFT 双击、MQTT QoS1 的至少一次投递）。重复事件必须**返回既有
结果且不推进状态**：否则「出餐」被投递两次，第二遍会因为 `ready→ready` 报
状态机违例，操作员看到红字以为失败，又点第三次。

**4. 乐观锁 `version`**。并发的两次写都通过校验后，后写的那次必须失败并提示
刷新。没有它，`READY → SERVED` 和 `READY → CANCELLED` 同时进来时，谁后落库谁
说了算，另一方的操作静默丢失。

## 沽清与退菜

- 沽清用 `dishes.sold_out` 独立字段（见 `repository.set_sold_out`），不动 `enabled`。
  下单时命中沽清菜整单拒绝，并**列出全部**沽清项——只报第一道，会让顾客改一轮
  再被拒一轮。
- 已 `cooking` 的项退菜要 `force=True`（组长操作），且必须带原因：
  「菜已下锅」与「还没派单」不是一个量级的决定，不给同一条路径。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

from kitchen.domain import (
    CST,
    DomainError,
    InvalidTransition,
    NotFoundError,
    StaleVersionError,
)

from .models import (
    ItemState,
    Order,
    OrderEvent,
    OrderItem,
    OrderState,
    rollup_order,
    require_transition,
)
from .repository import MemoryProcessRepository

log = logging.getLogger(__name__)

__all__ = [
    "OrderError", "SoldOutError", "OrderService", "DEFAULT_CHANNEL",
    "replay_events",
]

DEFAULT_CHANNEL = "tft"

#: 已开火的项不允许直接退：必须带 force + 原因
CANCELLABLE_WITHOUT_FORCE = frozenset({ItemState.PENDING, ItemState.DISPATCHED})


class OrderError(DomainError):
    """订单域的拒绝基类。"""


class SoldOutError(OrderError):
    """下单命中沽清菜品。`dishes` 给出全部命中项，便于一次改完。"""

    def __init__(self, dishes: Sequence[str]) -> None:
        self.dishes = tuple(dishes)
        super().__init__("以下菜品已沽清：" + "、".join(self.dishes))


def replay_events(events: Iterable[OrderEvent]) -> dict[str, object]:
    """从事件流复原状态。

    只重放**子项**迁移，再由 `rollup_order` 推出订单状态。刻意不信任事件里
    记的 `to_state`（订单级事件也带了它）：事件流的价值在于「用同一条推进规则
    再算一遍能得到同一个结果」，直接采信事件里那个字符串，等于没验证。
    """
    items: dict[int, ItemState] = {}
    for event in events:
        item_id = event.item_id
        if item_id is None:
            continue
        # 子项状态记在 `detail.item_to`：`from_state/to_state` 两列留给订单级
        # 迁移（表结构就是这么定义的），子项迁移只是在订单推进的一条记录里附带。
        raw = event.detail.get("item_to") or event.to_state
        items[item_id] = ItemState(str(raw))
    return {
        "items": items,
        "state": rollup_order(list(items.values())) if items else OrderState.CREATED,
    }


class OrderService:
    """订单写入与状态推进。**没有数据库**：写入落到内存列表，接口形状与 SQL 一致。"""

    def __init__(
        self,
        repo: MemoryProcessRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        sink: Callable[[Order, OrderEvent], None] | None = None,
    ) -> None:
        self.repo = repo
        self._clock = clock or (lambda: datetime.now(CST))
        #: 每条事件落地后的旁路写器（A23）：订单/子项快照 UPSERT + 事件追加。
        #: 写失败在写器内计数不抛——事件流本身仍是唯一事实源，落库丢了
        #: 也不能影响状态机推进。
        self._sink = sink
        self._orders: dict[int, Order] = {}
        self._events: dict[int, list[OrderEvent]] = {}
        self._idem: dict[tuple[str, str], OrderEvent] = {}
        self._next_order = 1
        self._next_item = 1

    # ------------------------------------------------------------ 时间
    def now(self) -> datetime:
        return self._clock()

    def align_ids(self, order_id: int = 0, item_id: int = 0) -> None:
        """把自增游标推到「运行时库里已有最大主键」之后。

        订单是**内存态 + 写穿镜像**：进程启动时内存是空的、游标回到 1，而库里
        还留着上次运行的行。不先对齐，重启后新下的单会拿到 1、2、3 去 UPSERT，
        把历史订单整行覆盖掉——不报错、不留痕，只是数据悄悄没了（A24 实测：
        容器重启后 `orders` 表出现 id=1、2 的新行，而原 id=3 仍孤立存在）。
        只往前推，不会把游标改小。
        """
        self._next_order = max(self._next_order, int(order_id or 0) + 1)
        self._next_item = max(self._next_item, int(item_id or 0) + 1)

    # ------------------------------------------------------------ 读
    def order(self, order_id: int) -> Order:
        found = self._orders.get(int(order_id))
        if found is None:
            raise NotFoundError(f"订单 {order_id} 不存在")
        return found

    def orders(self, *, state: OrderState | None = None) -> tuple[Order, ...]:
        items = [self._orders[key] for key in sorted(self._orders)]
        if state is not None:
            items = [item for item in items if item.state is state]
        return tuple(items)

    def open_orders(self) -> tuple[Order, ...]:
        """未结束的订单：下单、排队、制作、已出餐待上桌。"""
        done = (OrderState.SERVED, OrderState.CANCELLED)
        return tuple(order for order in self.orders() if order.state not in done)

    def events(self, order_id: int) -> tuple[OrderEvent, ...]:
        return tuple(self._events.get(int(order_id), ()))

    def item(self, item_id: int) -> tuple[Order, OrderItem]:
        for order in self._orders.values():
            for item in order.items:
                if item.id == int(item_id):
                    return order, item
        raise NotFoundError(f"订单子项 {item_id} 不存在")

    # ------------------------------------------------------------ 写：下单
    def place_order(
        self,
        *,
        table_id: int | None = None,
        items: Sequence[tuple[str | int, int]] = (),
        source: str = "web",
        channel: str = DEFAULT_CHANNEL,
        client_event_id: str | None = None,
        customer_ref: str | None = None,
        guest_key: str | None = None,
        now: datetime | None = None,
    ) -> Order:
        """建单。`items` 是 `(菜品 id 或编码, 份数)`。

        价格、配方版本、SOP 都在这一刻**快照**下来：之后改配方不影响在做的单。
        `customer_ref` 只由顾客通道传入，是脱敏的会话编号（非账号）；
        员工建单留 None，这样看板能一眼区分「扫码单」和「手工单」。
        """
        moment = now or self.now()
        if client_event_id:
            cached = self._idem.get((channel, client_event_id))
            if cached is not None:
                return self.order(cached.order_id)

        if not items:
            raise OrderError("订单至少要有一项")
        sold_out: list[str] = []
        resolved: list[tuple[int, int]] = []
        for ref, qty in items:
            dish = (self.repo.dish(ref) if isinstance(ref, int)
                    else self.repo.dish_by_code(str(ref)))
            if not dish.enabled:
                raise OrderError(f"菜品「{dish.name}」已停售")
            # 过期门禁：所有关联批次都过期 → 视同沽清，整单拒绝并列出。
            # 与顾客菜单口径一致（menu 也用 expired_batch_of），避免菜单隐藏了
            # 但直接调下单接口还能买到的绕过。
            expired = self.repo.expired_batch_of(dish.id, moment)
            if dish.sold_out or expired is not None:
                sold_out.append(dish.name)
                continue
            resolved.append((dish.id, max(1, int(qty))))
        if sold_out:
            raise SoldOutError(sold_out)

        order = Order(
            id=self._next_order,
            order_no=f"O{moment.strftime('%m%d')}-{self._next_order:03d}",
            table_id=table_id,
            source=source,
            state=OrderState.CREATED,
            version=0,
            created_at=moment,
            updated_at=moment,
            customer_ref=customer_ref,
            guest_key=guest_key,
        )
        self._next_order += 1

        for dish_id, qty in resolved:
            dish = self.repo.dish(dish_id)
            recipe = self.repo.recipe_for(dish_id)
            order.items.append(OrderItem(
                id=self._next_item,
                order_id=order.id,
                dish_id=dish_id,
                dish_name=dish.name,
                qty=qty,
                recipe_id=recipe.id if recipe else None,
                # 价格快照：客诉补偿金额以后只认这个值
                unit_price=float(getattr(dish, "price", 0.0) or 0.0),
            ))
            self._next_item += 1

        # 交期按最慢那道菜算，不按平均：一桌菜要一起上，
        # 按平均算出来的交期永远比实际差一两分钟，催菜提示会天天误报。
        lead_min = max(self.repo.estimate_minutes(item.dish_id) for item in order.items)
        order.promised_at = moment + timedelta(minutes=lead_min)
        order.state = rollup_order([item.state for item in order.items])

        self._orders[order.id] = order
        self._events[order.id] = []
        self._record(order, OrderState.CREATED, OrderState.CREATED,
                     actor="system", channel=channel,
                     client_event_id=client_event_id, moment=moment,
                     detail={"action": "place", "lead_min": lead_min,
                             "customer_ref": customer_ref})
        log.info("下单 %s：%s", order.order_no, order.describe())
        return order

    # ------------------------------------------------------------ 写：子项迁移
    def move_item(
        self,
        item_id: int,
        target: ItemState,
        *,
        actor: str = "",
        channel: str = DEFAULT_CHANNEL,
        client_event_id: str | None = None,
        expected_version: int | None = None,
        station_id: int | None = None,
        detail: dict | None = None,
        now: datetime | None = None,
    ) -> Order:
        """推进一个子项的状态，并顺带把订单状态算出来。

        `expected_version` 给了就做乐观锁校验；`client_event_id` 给了就做幂等
        （重复的返回当前订单，不抛错——重试的客户端要的是「已知结果」）。
        """
        order, item = self.item(item_id)
        if client_event_id:
            cached = self._idem.get((channel, client_event_id))
            if cached is not None:
                return order
        if expected_version is not None and expected_version != order.version:
            raise StaleVersionError(
                f"订单 {order.order_no} 已被修改（本地 v{expected_version}，"
                f"当前 v{order.version}），请刷新后重试"
            )
        require_transition(item.state, target, what=f"「{item.dish_name}」")
        moment = now or self.now()
        before = order.state
        item_before = item.state
        item.state = target
        if station_id is not None:
            item.station_id = station_id
        elif target in (ItemState.PENDING, ItemState.SERVED, ItemState.CANCELLED):
            # 释放工位：退菜与上桌之后，这条单不再占灶
            item.station_id = None
        order.state = rollup_order([row.state for row in order.items])
        order.updated_at = moment
        extra = dict(detail or {})
        extra.update({
            "item_id": item.id,
            "dish": item.dish_name,
            "item_from": item_before.value,
            "item_to": target.value,
        })
        self._record(order, before, order.state, actor=actor, channel=channel,
                     client_event_id=client_event_id, moment=moment, detail=extra)
        return order

    def dispatch_item(self, item_id: int, station_id: int, **kwargs) -> Order:
        """派单：`pending → dispatched` 并绑工位。**不做占用**——占用在 `dispatch.py`。"""
        return self.move_item(item_id, ItemState.DISPATCHED, station_id=station_id, **kwargs)

    def start_cooking(self, item_id: int, **kwargs) -> Order:
        return self.move_item(item_id, ItemState.COOKING, **kwargs)

    def mark_ready(self, item_id: int, **kwargs) -> Order:
        return self.move_item(item_id, ItemState.READY, **kwargs)

    def serve(self, item_id: int, **kwargs) -> Order:
        return self.move_item(item_id, ItemState.SERVED, **kwargs)

    # ------------------------------------------------------------ 写：退菜
    def cancel_item(
        self,
        item_id: int,
        *,
        reason: str,
        actor: str = "",
        force: bool = False,
        channel: str = DEFAULT_CHANNEL,
        now: datetime | None = None,
    ) -> Order:
        """退菜。

        已开火的项（`cooking`）默认拒绝：锅里已经有菜了，「退」这个动作在厨房
        意味着报废，必须由组长 `force` 并写原因。不给同一条路径，是为了让
        「误点」与「真的退」在库里长得不一样——事后追责时这就是证据。
        """
        order, item = self.item(item_id)
        if item.state is ItemState.CANCELLED:
            return order
        if item.state not in CANCELLABLE_WITHOUT_FORCE and not force:
            raise OrderError(
                f"「{item.dish_name}」当前 {item.state.value}，退单需组长确认（force=True）"
            )
        if not str(reason).strip():
            raise OrderError("退单必须填写原因")
        return self.move_item(
            item_id, ItemState.CANCELLED, actor=actor, channel=channel, now=now,
            detail={"action": "cancel", "reason": reason.strip(), "force": bool(force)},
        )

    # ------------------------------------------------------------ 写：催菜 / 沽清
    def urge(self, item_id: int, *, actor: str = "", now: datetime | None = None) -> Order:
        """催菜。累加计数并落事件；派单排序按「催一次提前 3 分钟」生效。"""
        order, item = self.item(item_id)
        if item.finished:
            raise OrderError(f"「{item.dish_name}」已结束，不能催菜")
        moment = now or self.now()
        before = order.state
        item.urge_count += 1
        order.updated_at = moment
        self._record(order, before, order.state, actor=actor, channel="web",
                     moment=moment, detail={
                         "action": "urge", "item_id": item.id,
                         "urge_count": item.urge_count,
                     })
        log.info("催菜：%s 第 %d 次", item.dish_name, item.urge_count)
        return order

    def mark_sold_out(self, dish_id: int, *, actor: str = "",
                      now: datetime | None = None) -> list[int]:
        """沽清，并**自动退掉还没派单的同一菜品**。

        已派单/已开火的不动：那些菜的材料已经配好、灶也占了，退掉只是把原料
        变成垃圾。返回被自动退掉的子项 id（调用方据此提示服务员去解释）。
        """
        dish = self.repo.set_sold_out(dish_id, True)
        moment = now or self.now()
        cancelled: list[int] = []
        for order in self.orders():
            for item in order.items:
                if item.dish_id != dish.id or item.state is not ItemState.PENDING:
                    continue
                self.move_item(
                    item.id, ItemState.CANCELLED, actor=actor, channel="web", now=moment,
                    detail={"action": "auto_cancel", "reason": f"「{dish.name}」已沽清"},
                )
                cancelled.append(item.id)
        if cancelled:
            log.info("沽清 %s：自动退掉 %d 项未派单子项", dish.name, len(cancelled))
        return cancelled

    # ------------------------------------------------------------ 内部
    def _record(
        self,
        order: Order,
        from_state: OrderState,
        to_state: OrderState,
        *,
        actor: str,
        channel: str,
        moment: datetime,
        client_event_id: str | None = None,
        detail: dict | None = None,
    ) -> OrderEvent:
        """落一条 `order_events` 并推进版本号。

        版本号按**事件条数**递增，不按状态变化次数：同一状态下的催菜、称重
        留痕也要参与并发校验，否则「催菜」和「出餐」可以同时成功、谁也没发现
        对方存在。
        """
        # `from_state/to_state` 永远记**订单级**状态（表结构如此定义）；
        # 子项迁移附带在 `detail.item_from/item_to` 里，重放时按后者复原。
        event = OrderEvent(
            order_id=order.id,
            from_state=from_state.value,
            to_state=to_state.value,
            ts=moment,
            actor=actor,
            channel=channel,
            client_event_id=client_event_id,
            detail=detail,
        )
        self._events.setdefault(order.id, []).append(event)
        if client_event_id:
            self._idem[(channel, client_event_id)] = event
        order.version += 1
        if self._sink is not None:
            try:
                self._sink(order, event)
            except Exception:  # pragma: no cover - 写器自身已吞错，这里是双保险
                log.warning("订单落库旁路异常（订单 %s）", order.order_no, exc_info=True)
        return event
