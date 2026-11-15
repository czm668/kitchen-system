# -*- coding: utf-8 -*-
"""订单状态机（A4-01）：状态推算、事件重放、幂等、乐观锁、沽清、退菜。

这一组里最重要的是 `TestReplay`：它验证「订单状态是算出来的」。断网补传（#17）
时服务端拿到的就是乱序事件，没有可重放的推进逻辑，补传只能按时间戳覆盖，
一覆盖就会把后到的「已出餐」冲成「制作中」。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kitchen.domain import CST, DomainError, InvalidTransition, NotFoundError, StaleVersionError
from kitchen.process import (
    ItemState,
    MemoryProcessRepository,
    OrderError,
    OrderService,
    OrderState,
    SoldOutError,
    replay_events,
    rollup_order,
)

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture()
def repo() -> MemoryProcessRepository:
    return MemoryProcessRepository.from_seed()


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def svc(repo, clock) -> OrderService:
    return OrderService(repo, clock=clock)


def simple_order(svc: OrderService, *refs) -> object:
    return svc.place_order(table_id=1, items=list(refs) or [("D-QJRS", 1)])


# ================================================================ 状态推算
class TestRollup:
    def test_全退才是取消(self):
        assert rollup_order([ItemState.CANCELLED, ItemState.CANCELLED]) is OrderState.CANCELLED

    def test_一项还在做就不算取消(self):
        """退了一道、另一道还在锅里，订单不能报取消——否则甘特图上菜凭空消失。"""
        assert rollup_order([ItemState.CANCELLED, ItemState.COOKING]) is OrderState.COOKING

    def test_出一道不算就绪(self):
        """五道菜出一道报 ready，页面就会显示「已完成」，传菜会漏单。"""
        assert rollup_order([ItemState.READY, ItemState.PENDING]) is OrderState.COOKING

    def test_全就绪才是就绪(self):
        assert rollup_order([ItemState.READY, ItemState.SERVED]) is OrderState.READY

    def test_全上桌才是完成(self):
        assert rollup_order([ItemState.SERVED, ItemState.SERVED]) is OrderState.SERVED

    def test_派过单就是排队中(self):
        assert rollup_order([ItemState.DISPATCHED, ItemState.PENDING]) is OrderState.QUEUED

    def test_空单退到created(self):
        assert rollup_order([]) is OrderState.CREATED


# ================================================================ 建单
class TestPlace:
    def test_交期按最慢那道菜算(self, repo, svc):
        """一桌菜要一起上；按平均算，催菜提示会天天误报。"""
        order = simple_order(svc, ("D-QJRS", 1), ("D-GBJD", 1))
        lead = (order.promised_at - order.created_at).total_seconds() / 60
        assert lead == max(repo.estimate_minutes(1), repo.estimate_minutes(2))

    def test_建单落一条事件(self, svc):
        order = simple_order(svc)
        events = svc.events(order.id)
        assert len(events) == 1
        assert events[0].detail["action"] == "place"
        assert order.version == 1

    def test_配方版本在建单时快照(self, repo, svc):
        """改配方只影响新单；在做的单中途换版，已确认的步骤号会对不上。"""
        order = simple_order(svc)
        assert order.items[0].recipe_id == 1
        repo.add_recipe(recipe_id=9, dish_id=1, version=2, name="v2")
        assert order.items[0].recipe_id == 1

    def test_空单被拒(self, svc):
        with pytest.raises(OrderError):
            svc.place_order(table_id=1, items=[])

    def test_沽清菜整单拒绝并列出全部(self, repo, svc):
        repo.set_sold_out(1, True)
        repo.set_sold_out(2, True)
        with pytest.raises(SoldOutError) as excinfo:
            simple_order(svc, ("D-QJRS", 1), ("D-GBJD", 1))
        assert len(excinfo.value.dishes) == 2, "只报第一道会让人改一轮被拒一轮"
        assert svc.orders() == ()

    def test_停售菜被拒(self, repo, svc):
        repo.add_dish(dish_id=9, code="D-STOP", name="下架菜", enabled=False)
        with pytest.raises(OrderError):
            simple_order(svc, ("D-STOP", 1))


# ================================================================ 状态推进
class TestMove:
    def test_全链路推进(self, svc):
        order = simple_order(svc)
        item = order.items[0]
        svc.dispatch_item(item.id, 1)
        svc.start_cooking(item.id, actor="1")
        svc.mark_ready(item.id)
        svc.serve(item.id)
        assert order.state is OrderState.SERVED

    def test_状态机拒绝倒流(self, svc):
        order = simple_order(svc)
        svc.dispatch_item(order.items[0].id, 1)
        svc.start_cooking(order.items[0].id)
        svc.mark_ready(order.items[0].id)
        with pytest.raises(InvalidTransition):
            svc.start_cooking(order.items[0].id)

    def test_出餐后不能退菜(self, svc):
        """已装盘的菜不能退——那要走售后，不是厨房改状态。"""
        order = simple_order(svc)
        svc.dispatch_item(order.items[0].id, 1)
        svc.start_cooking(order.items[0].id)
        svc.mark_ready(order.items[0].id)
        with pytest.raises(OrderError):
            svc.cancel_item(order.items[0].id, reason="客人不要了")
        # 就算组长强推，状态机也不给过：READY 没有到 CANCELLED 的边
        with pytest.raises(InvalidTransition):
            svc.cancel_item(order.items[0].id, reason="客人不要了", force=True)

    def test_上桌释放工位(self, svc):
        order = simple_order(svc)
        svc.dispatch_item(order.items[0].id, 1)
        assert order.items[0].station_id == 1
        svc.start_cooking(order.items[0].id)
        svc.mark_ready(order.items[0].id)
        svc.serve(order.items[0].id)
        assert order.items[0].station_id is None, "灶不能一直挂在已上桌的单上"

    def test_查不到的子项(self, svc):
        with pytest.raises(NotFoundError):
            svc.item(999)


# ================================================================ 幂等
class TestIdempotency:
    def test_同一客户端事件只落一次(self, svc):
        order = simple_order(svc)
        item = order.items[0]
        svc.dispatch_item(item.id, 1, channel="tft", client_event_id="evt-1")
        version = order.version
        svc.dispatch_item(item.id, 1, channel="tft", client_event_id="evt-1")
        assert order.version == version
        assert len(svc.events(order.id)) == 2      # place + dispatch

    def test_重复事件返回既有结果而不是报错(self, svc):
        """重试的客户端要的是「已知结果」；抛错会让人点第三次。"""
        order = simple_order(svc)
        svc.dispatch_item(order.items[0].id, 1, channel="tft", client_event_id="evt-1")
        again = svc.dispatch_item(order.items[0].id, 1, channel="tft",
                                  client_event_id="evt-1")
        assert again.id == order.id

    def test_同号不同渠道算两条(self, svc):
        """`uk_oe_idem` 是 `(channel, client_event_id)`：三端各自生成的事件号会撞。"""
        order = simple_order(svc)
        item = order.items[0]
        svc.dispatch_item(item.id, 1, channel="tft", client_event_id="1")
        svc.start_cooking(item.id, channel="web", client_event_id="1")
        assert order.items[0].state is ItemState.COOKING

    def test_建单也幂等(self, svc):
        first = svc.place_order(table_id=1, items=[("D-QJRS", 1)],
                                channel="web", client_event_id="order-1")
        second = svc.place_order(table_id=1, items=[("D-QJRS", 1)],
                                 channel="web", client_event_id="order-1")
        assert first.id == second.id
        assert len(svc.orders()) == 1


# ================================================================ 乐观锁
class TestOptimisticLock:
    def test_版本不符拒绝写入(self, svc):
        order = simple_order(svc)
        item = order.items[0]
        svc.dispatch_item(item.id, 1)
        with pytest.raises(StaleVersionError):
            svc.start_cooking(item.id, expected_version=0)

    def test_版本一致可写(self, svc):
        order = simple_order(svc)
        item = order.items[0]
        svc.dispatch_item(item.id, 1)
        svc.start_cooking(item.id, expected_version=order.version)
        assert item.state is ItemState.COOKING

    def test_催菜也推进版本(self, svc):
        """催菜与出餐必须互相看得见，否则同一时刻两边都成功、一方静默丢失。"""
        order = simple_order(svc)
        item = order.items[0]
        svc.dispatch_item(item.id, 1)
        before = order.version
        svc.urge(item.id)
        assert order.version == before + 1


# ================================================================ 重放
class TestReplay:
    def test_重放事件流得到同一状态(self, svc):
        order = simple_order(svc, ("D-QJRS", 1), ("D-GBJD", 1))
        first, second = order.items
        svc.dispatch_item(first.id, 1)
        svc.start_cooking(first.id)
        svc.mark_ready(first.id)
        svc.dispatch_item(second.id, 2)
        svc.cancel_item(second.id, reason="客人退菜")

        replayed = replay_events(svc.events(order.id))
        live = {item.id: item.state for item in order.items}
        assert replayed["items"] == live
        assert replayed["state"] is order.state

    def test_重放不受事件顺序以外的信息影响(self, svc):
        """事件里带的是订单级 from/to，重放靠 detail.item_to——两处都不能被误用。"""
        order = simple_order(svc)
        svc.dispatch_item(order.items[0].id, 1)
        events = svc.events(order.id)
        assert events[-1].from_state == "created"
        assert events[-1].to_state == "queued"
        assert events[-1].detail["item_to"] == "dispatched"


# ================================================================ 催菜 / 沽清 / 退菜
class TestUrgeAndSoldOut:
    def test_催菜把有效交期提前(self, svc):
        order = simple_order(svc)
        before = order.due_at
        svc.urge(order.items[0].id)
        assert order.due_at < before

    def test_不能催已结束的项(self, svc):
        order = simple_order(svc)
        svc.dispatch_item(order.items[0].id, 1)
        svc.cancel_item(order.items[0].id, reason="退菜")
        with pytest.raises(OrderError):
            svc.urge(order.items[0].id)

    def test_沽清自动退未派单项(self, svc):
        order = simple_order(svc, ("D-QJRS", 1), ("D-GBJD", 1))
        svc.dispatch_item(order.items[0].id, 1)
        cancelled = svc.mark_sold_out(1)
        assert cancelled == []
        assert order.items[0].state is ItemState.DISPATCHED, "已派单的不动：灶已占、料已配"

    def test_沽清退掉还在排队的单(self, repo, svc):
        order = simple_order(svc)
        cancelled = svc.mark_sold_out(1)
        assert cancelled == [order.items[0].id]
        assert order.state is OrderState.CANCELLED

    def test_已开火退菜必须force(self, svc):
        order = simple_order(svc)
        item = order.items[0]
        svc.dispatch_item(item.id, 1)
        svc.start_cooking(item.id)
        with pytest.raises(OrderError):
            svc.cancel_item(item.id, reason="客人不要了")
        svc.cancel_item(item.id, reason="客人不要了", force=True, actor="3")
        assert item.state is ItemState.CANCELLED

    def test_退菜必须写原因(self, svc):
        order = simple_order(svc)
        with pytest.raises(OrderError):
            svc.cancel_item(order.items[0].id, reason="   ")

    def test_重复退菜是幂等的(self, svc):
        order = simple_order(svc)
        svc.cancel_item(order.items[0].id, reason="退")
        version = order.version
        svc.cancel_item(order.items[0].id, reason="退")
        assert order.version == version


# ================================================================ 视图
class TestViews:
    def test_未结束订单不含已完成(self, svc):
        done = simple_order(svc)
        svc.dispatch_item(done.items[0].id, 1)
        svc.cancel_item(done.items[0].id, reason="退")
        open_order = simple_order(svc, ("D-GBJD", 1))
        assert [order.id for order in svc.open_orders()] == [open_order.id]

    def test_按状态筛(self, svc):
        simple_order(svc)
        assert len(svc.orders(state=OrderState.CREATED)) == 1
        assert svc.orders(state=OrderState.SERVED) == ()
