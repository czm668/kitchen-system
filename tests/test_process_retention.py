# -*- coding: utf-8 -*-
"""#14 留样称重 + #6 的 48h 销毁任务（A4-05 / A4-07）。

验收口径直译：
- 「124g 被拒、126g 通过」→ `TestWeigh`
- 「留痕可查」→ `TestTrace`（`retention_samples` + `process_events`）
- 「加速模式下 1 分钟出销毁任务」→ `TestDestroy`
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from kitchen.domain import CST, NotFoundError
from kitchen.process import (
    MemoryProcessRepository,
    RetentionError,
    RetentionService,
    NotDueError,
    RETENTION_MIN_G,
)

T0 = datetime(2026, 11, 12, 11, 40, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make(**kwargs):
    clock = FakeClock()
    # 种子批次锚定「装配日 11:30」，必须按 FakeClock 的日期装配，否则
    # plan(date(2026, 11, 12)) 查不到当日批次（同 test_process_assign 的口径）。
    repo = MemoryProcessRepository.from_seed(today=T0.date())
    svc = RetentionService(repo, clock=clock, **kwargs)
    return clock, repo, svc


# ================================================================ 应留样清单
class TestPlan:
    def test_按当日批次生成清单(self):
        _clock, _repo, svc = make()
        targets = svc.plan(date(2026, 11, 12))
        assert len(targets) == 1
        target = targets[0]
        assert target.batch_no == "B-DEMO-001"
        assert target.dish_name == "青椒肉丝"

    def test_其他日期没有应留样(self):
        _clock, _repo, svc = make()
        assert svc.plan(date(2026, 11, 13)) == ()

    def test_未留样的都算待办(self):
        _clock, _repo, svc = make()
        assert len(svc.pending(date(2026, 11, 12))) == 1
        svc.weigh("B-DEMO-001", 130)
        assert svc.pending(date(2026, 11, 12)) == ()
        assert svc.summary(date(2026, 11, 12))["stored"] == 1


# ================================================================ 称重
class TestWeigh:
    def test_124g_被拒(self):
        clock, _repo, svc = make()
        check = svc.weigh("B-DEMO-001", 124, staff_id=1)
        assert not check.ok
        assert check.short_g == 1.0
        assert "补足" in check.detail
        assert svc.sample_of(1) is None          # 不达标不落库
        assert svc.events[-1].state == "short"
        assert svc.events[-1].staff_id == 1

    def test_126g_通过(self):
        clock, _repo, svc = make()
        check = svc.weigh("B-DEMO-001", 126, staff_id=1)
        assert check.ok
        sample = svc.sample_of(1)
        assert sample is not None
        assert sample.weight_g == 126
        assert sample.sampled_at == clock()
        assert sample.state == "stored"

    def test_恰好_125g_算达标(self):
        _clock, _repo, svc = make()
        assert svc.weigh("B-DEMO-001", RETENTION_MIN_G).ok

    def test_补足后重新称覆盖不新增(self):
        clock, _repo, svc = make()
        svc.weigh("B-DEMO-001", 118, staff_id=1)
        clock.advance(30)
        svc.weigh("B-DEMO-001", 128, staff_id=1)
        clock.advance(600)
        svc.weigh("B-DEMO-001", 132, staff_id=3)      # 复核又称一次
        assert len(svc.samples) == 1
        sample = svc.sample_of(1)
        assert sample.weight_g == 132
        assert sample.sampled_at == clock()              # 48h 从最后一次达标起算
        states = [event.state for event in svc.events if event.state != "scanned"]
        assert states == ["short", "stored", "stored"]

    def test_负读数直接拒绝(self):
        _clock, _repo, svc = make()
        with pytest.raises(RetentionError) as exc:
            svc.weigh("B-DEMO-001", -5)
        assert "去皮" in str(exc.value)

    def test_扫码与批次号都能定位(self):
        _clock, _repo, svc = make()
        by_code = svc.resolve("tc-demo-0001")         # 溯源码（小写也能认）
        by_no = svc.resolve("B-DEMO-001")
        assert by_code.batch_id == by_no.batch_id == 1
        assert by_code.dish_name == "青椒肉丝"

    def test_扫到不存在的码报错(self):
        _clock, _repo, svc = make()
        with pytest.raises(NotFoundError):
            svc.resolve("TC-NOT-EXIST")
        with pytest.raises(RetentionError):
            svc.resolve("   ")


# ================================================================ 48h 销毁
class TestDestroy:
    def test_真实模式_48h_才到期(self):
        clock, _repo, svc = make()
        svc.weigh("B-DEMO-001", 130, staff_id=1)
        clock.advance(48 * 3600 - 60)
        assert svc.due_for_destroy() == ()
        clock.advance(120)
        tasks = svc.due_for_destroy()
        assert len(tasks) == 1
        assert tasks[0].order_no == "WO-RD-0001"
        assert tasks[0].batch_no == "B-DEMO-001"
        assert tasks[0].notices[0]["channel"] == "miniapp"

    def test_加速模式_1_分钟出任务(self):
        """48 小时 → 1 分钟（time_scale=2880）。"""
        clock, _repo, svc = make(time_scale=2880)
        svc.weigh("B-DEMO-001", 130, staff_id=1)
        clock.advance(59)
        assert svc.due_for_destroy() == ()
        clock.advance(2)
        tasks = svc.due_for_destroy()
        assert len(tasks) == 1
        assert tasks[0].state == "open"

    def test_重复扫描只开一张单(self):
        clock, _repo, svc = make(time_scale=2880)
        svc.weigh("B-DEMO-001", 130)
        clock.advance(61)
        first = svc.due_for_destroy()
        clock.advance(5)
        again = svc.due_for_destroy()
        assert len(first) == len(again) == 1
        assert first[0].id == again[0].id
        assert len(svc.tasks) == 1

    def test_未到期不许销毁(self):
        clock, _repo, svc = make()
        svc.weigh("B-DEMO-001", 130)
        with pytest.raises(NotDueError) as exc:
            svc.destroy(batch_id=1)
        assert "未满保存期" in str(exc.value)

    def test_到期后销毁留痕(self):
        clock, _repo, svc = make(time_scale=2880)
        svc.weigh("B-DEMO-001", 130, staff_id=1)
        clock.advance(61)
        task = svc.due_for_destroy()[0]
        done = svc.destroy(task.id, staff_id=3)
        assert done.state == "closed"
        sample = svc.sample_of(1)
        assert sample.state == "destroyed"
        assert sample.destroyed_at == clock()
        event = svc.events[-1]
        assert event.state == "destroyed"
        assert event.staff_id == 3

    def test_重复销毁报错(self):
        clock, _repo, svc = make(time_scale=2880)
        svc.weigh("B-DEMO-001", 130)
        clock.advance(61)
        task = svc.due_for_destroy()[0]
        svc.destroy(task.id, staff_id=1)
        with pytest.raises(RetentionError):
            svc.destroy(task.id, staff_id=1)

    def test_没留样就没有销毁任务(self):
        clock, _repo, svc = make(time_scale=2880)
        clock.advance(3600)
        assert svc.due_for_destroy() == ()

    def test_销毁必须给参数(self):
        _clock, _repo, svc = make()
        with pytest.raises(RetentionError):
            svc.destroy()

    def test_time_scale_必须为正(self):
        _clock, repo, _svc = make()
        with pytest.raises(RetentionError):
            RetentionService(repo, time_scale=0)
