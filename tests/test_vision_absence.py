# -*- coding: utf-8 -*-
"""#11 离岗检测与任务重排（A5-05）。

两层：
- `TestMonitor`：纯视觉帧驱动的离岗积分（不碰订单/人员）；
- `TestReassign`：接真的 `DispatchBoard`，验证「未开火可转、已开火不转」。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from vision_support import P1, P2, ROIS, bare, frame, full_kit

from kitchen.domain import CST
from kitchen.process import DispatchBoard, ItemState, MemoryProcessRepository, OrderService
from kitchen.vision.absence import (
    AbsenceEvent,
    AbsenceMonitor,
    AbsencePolicy,
    AbsenceReassigner,
)
from kitchen.vision.postprocess import analyze

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass
class FakeMatcher:
    qualified: frozenset[int] = frozenset()
    pool: frozenset[int] = frozenset()

    def qualifies(self, staff_id: int, requirement: str) -> bool:
        return int(staff_id) in self.qualified

    def candidates(self, requirement: str) -> tuple[int, ...]:
        return tuple(sorted(self.pool))


def empty_frame(seq: int, at: float):
    return analyze([], frame(seq, ts=T0 + timedelta(seconds=at)), ROIS)


def present_frame(seq: int, at: float, box=P1):
    return analyze(full_kit(box), frame(seq, ts=T0 + timedelta(seconds=at)), ROIS)


def make_monitor(*, occupants=None, policy=None, start_present=True):
    seats = dict(occupants or {1: 1})
    monitor = AbsenceMonitor(occupant_of=lambda sid: seats.get(sid), policy=policy,
                             clock=lambda: T0)
    if start_present:
        # 先喂一帧「有人在」，否则 monitor 不知道最后见到人是什么时候
        monitor.observe(present_frame(1, 0.0))
    return monitor, seats


class TestMonitor:
    def test_未达阈值不报(self):
        monitor, _ = make_monitor(policy=AbsencePolicy(absence_sec=12))
        assert monitor.observe(empty_frame(2, 5.0)) == []
        assert monitor.observe(empty_frame(3, 10.0)) == []

    def test_达到阈值报一次(self):
        monitor, _ = make_monitor(policy=AbsencePolicy(absence_sec=12))
        assert monitor.observe(empty_frame(2, 13.0)) != []
        assert monitor.observe(empty_frame(3, 20.0)) == [], "同一段离岗只报一次"

    def test_工位本来没人不报(self):
        monitor = AbsenceMonitor(occupant_of=lambda sid: None, clock=lambda: T0)
        monitor.observe(present_frame(1, 0.0))
        assert monitor.observe(empty_frame(2, 60.0)) == [], "没人上灶的工位谈不上离岗"

    def test_人回来清状态(self):
        monitor, _ = make_monitor(policy=AbsencePolicy(absence_sec=12))
        monitor.observe(empty_frame(2, 13.0))
        assert monitor.absent_now(T0 + timedelta(seconds=15)) == [1]
        monitor.observe(present_frame(3, 20.0))
        assert monitor.absent_now(T0 + timedelta(seconds=21)) == []

    def test_回来后再次离岗_冷却内不重报(self):
        monitor, _ = make_monitor(
            policy=AbsencePolicy(absence_sec=12, cooldown_sec=300))
        handled = monitor.observe(empty_frame(2, 13.0))
        assert handled
        monitor.observe(present_frame(3, 20.0))
        assert monitor.observe(empty_frame(4, 40.0)) == []

    def test_冷却过后可重报(self):
        monitor, _ = make_monitor(
            policy=AbsencePolicy(absence_sec=12, cooldown_sec=30))
        assert monitor.observe(empty_frame(2, 13.0))
        monitor.observe(present_frame(3, 20.0))
        assert monitor.observe(empty_frame(4, 60.0)) != []

    def test_刷卡证据可救回视觉漏检(self):
        monitor, _ = make_monitor(policy=AbsencePolicy(absence_sec=12))
        monitor.observe(empty_frame(2, 8.0))
        monitor.mark_present(1, T0 + timedelta(seconds=9))
        assert monitor.observe(empty_frame(3, 10.0)) == []

    def test_事件带离岗人和秒数(self):
        monitor, _ = make_monitor(policy=AbsencePolicy(absence_sec=5))
        events = monitor.observe(empty_frame(2, 9.0))
        assert events[0].staff_id == 1
        assert events[0].seconds == pytest.approx(9.0)
        assert "ST1" in events[0].describe()

    def test_两个工位独立(self):
        monitor = AbsenceMonitor(occupant_of=lambda sid: sid, clock=lambda: T0)
        monitor.observe(analyze(full_kit(P1) + full_kit(P2), frame(1, ts=T0), ROIS))
        events = monitor.observe(empty_frame(2, 20.0))
        assert {e.station_id for e in events} == {1, 2}

    def test_snapshot结构(self):
        monitor, _ = make_monitor(policy=AbsencePolicy(absence_sec=12))
        monitor.observe(empty_frame(2, 13.0))
        snap = monitor.snapshot()
        assert snap["absent"] == [1]
        assert snap["policy"]["absence_sec"] == 12


def make_board(matcher=None):
    clock = FakeClock()
    repo = MemoryProcessRepository.from_seed()
    orders = OrderService(repo, clock=clock)
    board = DispatchBoard(repo, orders, clock=clock, matcher=matcher)
    return clock, repo, orders, board


def make_order_and_dispatch(staff: int = 1):
    _clock, repo, orders, board = make_board(
        FakeMatcher(qualified=frozenset({1, 3}), pool=frozenset({1, 3})))
    order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
    item = order.items[0]
    decisions = board.dispatch_pending(staff_plan={item.id: staff})
    assert decisions[0].ok and decisions[0].staff_id == staff
    return repo, orders, board, item


def event_for(station_id: int, staff_id: int | None, seconds: float = 13.0) -> AbsenceEvent:
    return AbsenceEvent(station_id=station_id, staff_id=staff_id, since=T0,
                        last=T0 + timedelta(seconds=seconds), seconds=seconds)


class TestReassign:
    def test_转派给在岗合格人员(self):
        _repo, _orders, board, item = make_order_and_dispatch(1)
        assert board.assigned_staff() == {item.id: 1}
        pushes: list[tuple[str, dict]] = []
        reassigner = AbsenceReassigner(board, notify=lambda t, p: pushes.append((t, p)))
        result = reassigner.handle(event_for(1, 1))
        assert result.moved[0]["to"] == 3
        assert board.assigned_staff()[item.id] == 3
        assert pushes[0][0] == "absence_reassign"

    def test_已开火的菜不转派只报(self):
        _repo, _orders, board, item = make_order_and_dispatch(1)
        board.occupy(1, 1, item_id=item.id)
        reassigner = AbsenceReassigner(board)
        result = reassigner.handle(event_for(1, 1))
        assert result.moved == ()
        assert any("已开火不能换人" in b for b in result.blocked)
        assert board.station_staff(1) == 1, "灶还是他的，只是提醒组长接手"

    def test_无人可接退回队列(self):
        _clock, _repo, orders, board = make_board(
            FakeMatcher(qualified=frozenset({1}), pool=frozenset({1})))
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        board.dispatch_pending(staff_plan={item.id: 1})
        reassigner = AbsenceReassigner(board)
        result = reassigner.handle(event_for(1, 1))
        assert result.moved == ()
        assert item.id in result.released
        assert board.assigned_staff() == {}
        assert any("退回队列" in b for b in result.blocked)

    def test_没有人员池时不硬转(self):
        _clock, _repo, orders, board = make_board(matcher=None)
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        board.dispatch_pending()
        reassigner = AbsenceReassigner(board)
        result = reassigner.handle(event_for(1, None))
        assert result.moved == ()

    def test_require_on_duty_过滤掉不在岗的人(self):
        _repo, _orders, board, item = make_order_and_dispatch(1)
        reassigner = AbsenceReassigner(board, require_on_duty=lambda sid: sid != 3)
        result = reassigner.handle(event_for(1, 1))
        assert result.moved == (), "3 号虽在候选集里，但此刻不在岗"
        assert item.id in result.released

    def test_从未占用时用板上的人(self):
        _repo, _orders, board, item = make_order_and_dispatch(1)
        board.occupy(1, 1)          # 先占灶再挑单
        reassigner = AbsenceReassigner(board)
        result = reassigner.handle(event_for(1, None))
        assert result.absent_staff == 1

    def test_描述(self):
        _repo, _orders, board, _item = make_order_and_dispatch(1)
        result = AbsenceReassigner(board).handle(event_for(1, 1))
        assert "离岗重排" in result.describe()
        assert "3号" in result.describe()

    def test_history留痕(self):
        _repo, _orders, board, _item = make_order_and_dispatch(1)
        reassigner = AbsenceReassigner(board)
        reassigner.handle(event_for(1, 1))
        assert len(reassigner.history) == 1


class TestTakeover:
    def test_资质不符拒绝(self):
        _repo, _orders, board, item = make_order_and_dispatch(1)
        from kitchen.process import QualificationMismatch
        with pytest.raises(QualificationMismatch):
            board.takeover(item.id, 9)

    def test_已开火拒绝(self):
        _repo, _orders, board, item = make_order_and_dispatch(1)
        board.occupy(1, 1, item_id=item.id)
        from kitchen.domain import InvalidTransition
        with pytest.raises(InvalidTransition, match="已开火"):
            board.takeover(item.id, 3)

    def test_撤销预约(self):
        _repo, _orders, board, item = make_order_and_dispatch(1)
        assert board.release_assignment(item.id) == 1
        assert board.assigned_staff() == {}
