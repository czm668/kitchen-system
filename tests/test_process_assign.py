# -*- coding: utf-8 -*-
"""派单与人员资质联调（A4-09 / #10 落点）。

验收口径直译：「资质不符 → 自动改派 → 甘特与 TFT 队列同步刷新」→ `TestAssign`。

两层：
- `TestFakeMatcher`：纯派单逻辑（不碰人员域，用假 matcher 定死候选），改动
  `dispatch` 的排序/改派分支时第一时间变红；
- `TestRealStaff`：接真的 `AdmissionController`，验证「候选集与刷卡准入同源」——
  资质在册但健康证过期的人**不能**被派单选中（否则他刷卡会被拒，单已挂在灶上）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from kitchen.domain import CST
from kitchen.process import (
    DispatchBoard,
    ItemState,
    MemoryProcessRepository,
    OrderService,
    QualificationMismatch,
)
from kitchen.staff import (
    AdmissionController,
    MemoryStaffRepository,
    StaffMatcherAdapter,
)

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
    """可控的人员池：`qualified` 是资质在册的人，`candidates` 是四查都过的人。"""

    qualified: frozenset[int] = frozenset()
    pool: frozenset[int] = frozenset()

    def qualifies(self, staff_id: int, requirement: str) -> bool:
        return int(staff_id) in self.qualified

    def candidates(self, requirement: str) -> tuple[int, ...]:
        return tuple(sorted(self.pool))


def make_board(matcher=None):
    clock = FakeClock()
    repo = MemoryProcessRepository.from_seed()
    orders = OrderService(repo, clock=clock)
    board = DispatchBoard(repo, orders, clock=clock, matcher=matcher)
    return clock, repo, orders, board


# ================================================================ 纯派单逻辑
class TestFakeMatcher:
    def test_指定人员资质不符自动改派(self):
        _clock, _repo, orders, board = make_board(
            FakeMatcher(qualified=frozenset({1, 3}), pool=frozenset({1, 3}))
        )
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        decisions = board.dispatch_pending(staff_plan={item.id: 9})   # 9 号没资质
        assert decisions[0].ok
        assert decisions[0].staff_id == 1
        assert decisions[0].reassigned_from == 9
        assert "改派" in decisions[0].reason
        assert "改派" in decisions[0].describe()

    def test_指定人员合格就照办(self):
        _clock, _repo, orders, board = make_board(
            FakeMatcher(qualified=frozenset({1, 3}), pool=frozenset({1, 3}))
        )
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        decisions = board.dispatch_pending(staff_plan={item.id: 3})
        assert decisions[0].staff_id == 3
        assert decisions[0].reassigned_from is None
        assert not decisions[0].reassigned

    def test_一个合格人都没有就不派单(self):
        _clock, _repo, orders, board = make_board(FakeMatcher(qualified=frozenset(), pool=frozenset()))
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        decisions = board.dispatch_pending()
        assert decisions[0].ok is False
        assert "四查" in decisions[0].reason
        # 单子还留在队列里、灶也没被占——不能因为没人就把工位锁死
        assert item.state is ItemState.PENDING
        assert board.station_state(1) == "idle"

    def test_正忙的人被跳过(self):
        clock, _repo, orders, board = make_board(
            FakeMatcher(qualified=frozenset({1}), pool=frozenset({1}))
        )
        first = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        second = orders.place_order(table_id=2, items=[("D-QJRS", 1)])
        decisions = board.dispatch_pending()
        assert decisions[0].staff_id == 1
        # 两个灶但只有一个合格的人：第二单因为没人而排队，不是派给同一个人
        assert decisions[1].ok is False
        assert "四查" in decisions[1].reason
        assert first.items[0].state is ItemState.DISPATCHED
        assert second.items[0].state is ItemState.PENDING

    def test_上灶时资质不符带候选人提示(self):
        _clock, _repo, orders, board = make_board(
            FakeMatcher(qualified=frozenset({1, 3}), pool=frozenset({1, 3}))
        )
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        board.dispatch_pending(staff_plan={item.id: 1})
        with pytest.raises(QualificationMismatch) as exc:
            board.occupy(1, 9, item_id=item.id)
        assert exc.value.candidates == (1, 3)
        assert "可改派" in str(exc.value)

    def test_没接人员池时只派工位(self):
        _clock, _repo, orders, board = make_board(None)
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        decisions = board.dispatch_pending()
        assert decisions[0].ok
        assert decisions[0].staff_id is None
        assert board.busy_staff() == frozenset()
        board.occupy(1, 42, item_id=order.items[0].id)      # 不校验资质

    def test_指派表在上灶后清空(self):
        _clock, _repo, orders, board = make_board(
            FakeMatcher(qualified=frozenset({1}), pool=frozenset({1}))
        )
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        board.dispatch_pending()
        assert board.assigned_staff() == {item.id: 1}
        board.occupy(1, 1, item_id=item.id)
        assert board.assigned_staff() == {}
        assert board.dispatch_summary()["assigned"] == {}


# ================================================================ 接真人员域
class TestRealStaff:
    def make(self):
        clock = FakeClock()
        repo = MemoryProcessRepository.from_seed()
        orders = OrderService(repo, clock=clock)
        # 种子的 CURDATE() 必须按 FakeClock 的日期装配：业务域用 2026-11-12，
        # 人员域若按「运行当天」就会查不到当日排班，候选集为空、派单全拒。
        staff_repo = MemoryStaffRepository.from_seed(today=clock().date())
        admission = AdmissionController(staff_repo)
        # 当日晨检：张三 / 王组长通过，李四不做（他健康证也已过期）
        for card_uid in ("A1B2C3D4", "C9D0E1F2"):
            outcome = admission.submit_morning(card_uid, {
                "temp_c": 36.5, "diarrhea": False,
                "pharyngitis": False, "hand_wound": False,
            }, now=clock())
            assert outcome.ok, outcome.describe()
        board = DispatchBoard(repo, orders, clock=clock,
                              matcher=StaffMatcherAdapter(admission, clock=clock))
        return clock, orders, admission, board

    def test_资质在册但健康证过期的人不会被派(self):
        clock, orders, _admission, board = self.make()
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        # 指定李四（2 号，stove 资质在册，但健康证 2026-06-30 已过期）
        decisions = board.dispatch_pending(staff_plan={item.id: 2})
        assert decisions[0].ok
        assert decisions[0].staff_id != 2
        assert decisions[0].reassigned_from == 2
        assert decisions[0].staff_id in (1, 3)

    def test_候选集与刷卡准入同源(self):
        clock, orders, admission, board = self.make()
        order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
        item = order.items[0]
        decision = board.dispatch_pending()[0]
        # 派给谁，谁就必须刷得开
        result = admission.scan_staff(decision.staff_id, station_id=1, now=clock())
        assert result.ok, result.describe()

    def test_李四刷不开灶(self):
        _clock, _orders, admission, _board = self.make()
        clock = _clock
        result = admission.scan_staff(2, station_id=1, now=clock())
        assert not result.ok
