# -*- coding: utf-8 -*-
"""#4 灶台占用与派单约束（A4-02）。

三个验收项各自对应一组：
- 「同一工位不可能被派两单」→ `TestOccupancy` / `TestDispatch`
- 「交期近的先派」→ `TestDispatchOrder`
- 「点火校验失败有明确提示」→ `TestFlame`
- 「SCT 读数用于排风是否在转」→ `TestExhaust`
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kitchen.domain import CST, NotFoundError
from kitchen.process import (
    DispatchBoard,
    ItemState,
    MemoryProcessRepository,
    OrderService,
    StaffBusyError,
    StationBusyError,
)

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make(**kwargs):
    clock = FakeClock()
    repo = MemoryProcessRepository.from_seed()
    svc = OrderService(repo, clock=clock)
    board = DispatchBoard(repo, svc, clock=clock, **kwargs)
    return clock, repo, svc, board


# ================================================================ 派单顺序
class TestDispatchOrder:
    def test_交期近的先派(self):
        clock, _repo, svc, board = make()
        late = svc.place_order(table_id=1, items=[("D-QJRS", 1)])
        clock.advance(60 * 20)
        early = svc.place_order(table_id=2, items=[("D-GBJD", 1)])
        # 手工把 early 的交期提到 late 之前：模拟「先下的单反而交期晚」
        early.promised_at = late.promised_at - timedelta(minutes=5)

        decisions = board.dispatch_pending()
        assert decisions[0].item_id == early.items[0].id
        assert decisions[0].ok

    def test_催菜会插到前面(self):
        clock, _repo, svc, board = make()
        first = svc.place_order(table_id=1, items=[("D-QJRS", 1)])
        clock.advance(60 * 20)
        second = svc.place_order(table_id=2, items=[("D-QJRS", 1)])
        second.promised_at = first.promised_at + timedelta(minutes=10)
        for _ in range(5):                    # 催 5 次 = 提前 15 分钟
            svc.urge(second.items[0].id)
        assert board.queue()[0].id == second.items[0].id

    def test_排序稳定(self):
        """两次扫描顺序必须一致，否则「为什么先做这单」无法复现。"""
        _clock, _repo, svc, board = make()
        svc.place_order(table_id=1, items=[("D-QJRS", 1), ("D-GBJD", 1)])
        first = [item.id for item in board.queue()]
        second = [item.id for item in board.queue()]
        assert first == second


# ================================================================ 派单约束
class TestDispatch:
    def test_派过的单不再出现在队列(self):
        _clock, _repo, svc, board = make()
        order = svc.place_order(table_id=1, items=[("D-QJRS", 1)])
        board.dispatch_pending()
        assert order.items[0].state is ItemState.DISPATCHED
        assert board.queue() == ()

    def test_已派出但没上灶的工位不再派第二单(self):
        """`reserved` 也算占用，否则两单会派到同一个灶。"""
        _clock, _repo, svc, board = make()
        one = svc.place_order(table_id=1, items=[("D-QJRS", 1)])
        two = svc.place_order(table_id=2, items=[("D-QJRS", 1)])
        three = svc.place_order(table_id=3, items=[("D-QJRS", 1)])

        decisions = board.dispatch_pending()

        assert [d.ok for d in decisions] == [True, True, False]
        assert {decisions[0].station_id, decisions[1].station_id} == {1, 2}
        assert one.items[0].station_id != two.items[0].station_id
        assert decisions[-1].item_id == three.items[0].id
        assert "全部工位占用" in decisions[-1].reason

    def test_全忙时排队而不是报错(self):
        """营业高峰全忙是常态；抛异常会让前端每分钟弹一次错。"""
        _clock, _repo, svc, board = make()
        for index in range(3):
            svc.place_order(table_id=index + 1, items=[("D-QJRS", 1)])
        decisions = board.dispatch_pending()
        queued = [d for d in decisions if not d.ok]
        assert len(queued) == 1
        assert queued[0].queue_position == 3

    def test_派单上限(self):
        _clock, _repo, svc, board = make()
        for index in range(2):
            svc.place_order(table_id=index + 1, items=[("D-QJRS", 1)])
        decisions = board.dispatch_pending(limit=1)
        assert sum(1 for d in decisions if d.ok) == 1
        assert any("本轮派单已达上限" in d.reason for d in decisions)

    def test_冷菜要冷菜工位(self):
        _clock, repo, svc, board = make()
        repo.add_dish(dish_id=9, code="D-LC", name="凉拌黄瓜", category="凉菜")
        order = svc.place_order(table_id=1, items=[("D-LC", 1)])
        decisions = board.dispatch_pending()
        assert not decisions[0].ok, "只有灶台工位，冷菜不该被派上去"
        assert order.items[0].state is ItemState.PENDING


# ================================================================ 占用会话
class TestOccupancy:
    def test_上灶开到点作业(self):
        _clock, _repo, svc, board = make()
        order = svc.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        board.dispatch_pending()
        session = board.occupy(1, staff_id=1, item_id=item.id)
        assert session.open
        assert item.state is ItemState.COOKING
        assert board.station_state(1) == "busy"

    def test_同一工位不能被两个人占(self):
        _clock, _repo, svc, board = make()
        board.occupy(1, staff_id=1)
        with pytest.raises(StationBusyError):
            board.occupy(1, staff_id=2)

    def test_一个人不能同时占两个灶(self):
        _clock, _repo, svc, board = make()
        board.occupy(1, staff_id=1)
        with pytest.raises(StaffBusyError):
            board.occupy(2, staff_id=1)

    def test_出餐释放工位(self):
        _clock, _repo, svc, board = make()
        board.occupy(1, staff_id=1)
        session = board.close(1)
        assert session.ended_at is not None
        assert not session.open
        assert board.station_state(1) == "idle"

    def test_没有占用就释放要报错(self):
        _clock, _repo, _svc, board = make()
        with pytest.raises(NotFoundError):
            board.close(1)

    def test_释放后可以重新占(self):
        _clock, _repo, _svc, board = make()
        board.occupy(1, staff_id=1)
        board.close(1)
        board.occupy(1, staff_id=2)
        assert board.occupancy_of(1).staff_id == 2

    def test_工位状态以会话为准(self):
        """`work_stations.state` 是缓存：重启后列里还写着 busy，会话已经没了。"""
        _clock, repo, _svc, board = make()
        repo.with_station_state(1, "busy")
        assert board.station_state(1) == "idle"
        assert board.idle_stations()


# ================================================================ 点火校验
class TestFlame:
    def test_上灶后窗口期内不报未点火(self):
        """一上灶就报未点火，每次开火都会先弹一次假告警。"""
        clock, _repo, _svc, board = make()
        board.occupy(1, staff_id=1)
        clock.advance(5)
        assert board.flame_check(1).ok
        assert not board.flame_check(1).verified

    def test_火焰够了就确认(self):
        clock, _repo, _svc, board = make()
        board.occupy(1, staff_id=1)
        board.observe_flame(1, 0.92)
        verdict = board.flame_check(1)
        assert verdict.ok and verdict.verified
        assert "0.92" in verdict.detail

    def test_窗口期过还没火就报(self):
        clock, _repo, _svc, board = make()
        board.occupy(1, staff_id=1)
        clock.advance(31)
        verdict = board.flame_check(1)
        assert not verdict.ok
        assert "刷卡占位" in verdict.detail

    def test_已确认点火后不再因读数抖动报(self):
        """火焰传感器偶发掉零，不该把已确认的灶翻回「未点火」。"""
        clock, _repo, _svc, board = make()
        board.occupy(1, staff_id=1)
        board.observe_flame(1, 0.9)
        board.flame_check(1)
        clock.advance(60)
        board.observe_flame(1, 0.0)
        assert board.flame_check(1).ok

    def test_列出到期未点火的工位(self):
        clock, _repo, _svc, board = make()
        board.occupy(1, staff_id=1)
        board.occupy(2, staff_id=2)
        board.observe_flame(2, 0.8)
        clock.advance(40)
        pending = board.pending_flame_checks()
        assert [verdict.station_id for verdict in pending] == [1]

    def test_没有占用时校验通过(self):
        _clock, _repo, _svc, board = make()
        assert board.flame_check(1).ok


# ================================================================ 排风
class TestExhaust:
    def test_没读数说不知道(self):
        """默认「停转」会让排风正常的今天也弹告警，几天后没人再看这条提示。"""
        _clock, _repo, _svc, board = make()
        state = board.exhaust_state()
        assert state.state == "unknown"
        assert not state.running

    def test_电流够就是在转(self):
        _clock, _repo, _svc, board = make()
        board.observe_exhaust(0.62)
        assert board.exhaust_state().running

    def test_电流不足判停转(self):
        _clock, _repo, _svc, board = make()
        board.observe_exhaust(0.03)
        state = board.exhaust_state()
        assert state.state == "stopped"
        assert "排风可能未开启" in state.detail

    def test_排风不影响派单(self):
        """排风停了是 #2 的事；派单层再拦一次会让整条线派不出单。"""
        _clock, _repo, svc, board = make()
        board.observe_exhaust(0.0)
        svc.place_order(table_id=1, items=[("D-QJRS", 1)])
        assert board.dispatch_pending()[0].ok


# ================================================================ 一屏视图
class TestSummary:
    def test_摘要含工位排风队列(self):
        _clock, _repo, svc, board = make()
        svc.place_order(table_id=1, items=[("D-QJRS", 1)])
        board.observe_exhaust(0.5)
        view = board.dispatch_summary()
        assert len(view["stations"]) == 2
        assert "在转" in view["exhaust"]
        assert len(view["queue"]) == 1
