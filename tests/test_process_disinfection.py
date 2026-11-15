# -*- coding: utf-8 -*-
"""#15 消毒曲线与复消闭环（A4-06）。

验收口径直译：
- 「注入达标曲线自动关单（约 30 秒）」→ `TestPass` + `TestDemoScale`
- 「注入常温判不达标」→ `TestFail`
- 「120℃ 正常工艺温度不报警」→ `TestSessionGating`（非消毒时段更不报）
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kitchen.domain import CST, NotFoundError
from kitchen.process import (
    DEMO_TIME_SCALE,
    DisinfectionError,
    DisinfectionPolicy,
    DisinfectionService,
    MemoryProcessRepository,
    NotRunningError,
)

T0 = datetime(2026, 11, 12, 20, 0, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make(**policy_kwargs):
    clock = FakeClock()
    repo = MemoryProcessRepository.from_seed()
    policy = DisinfectionPolicy(**policy_kwargs) if policy_kwargs else DisinfectionPolicy()
    svc = DisinfectionService(repo, policy=policy, clock=clock)
    return clock, svc


def feed_curve(svc, clock, temps, *, step=2.0):
    """按固定间隔喂一串温度，返回最后一次有效判定。

    会话判定到位（达标/不达标）之后 `feed()` 会返回 None（会话已结束，读数按门控
    丢弃），所以这里遇到 None 就停——继续喂只会把结果覆盖成 None。
    """
    last = None
    for temp in temps:
        clock.advance(step)
        reading = svc.feed(temp)
        if reading is None:
            break
        last = reading
    return last


# ================================================================ 会话门控
class TestSessionGating:
    def test_没开会话时读数被丢弃(self):
        """#15 的第一条铁律：非消毒时段喂 25℃ 不能报「低于下限」。"""
        clock, svc = make()
        clock.advance(10)
        assert svc.feed(25.0) is None
        assert svc.ignored == 1
        assert svc.events == ()                 # 不落事件 = 不进报警通道
        assert svc.forbid_outbound() is False

    def test_120度是正常工艺温度(self):
        _clock, svc = make()
        svc.start(batch_id=1)
        reading = svc.feed(120.0)
        assert reading.state == "running"
        assert reading.failed is False

    def test_会话结束后不再接受读数(self):
        clock, svc = make()
        svc.start(batch_id=1)
        svc.finish()
        assert svc.feed(120.0) is None

    def test_重复会话号被拒(self):
        _clock, svc = make()
        svc.start(batch_id=1, session_uid="DI-X")
        with pytest.raises(DisinfectionError):
            svc.start(batch_id=2, session_uid="DI-X")

    def test_没有会话时结束会报错(self):
        _clock, svc = make()
        with pytest.raises(NotRunningError):
            svc.finish()


# ================================================================ 达标
class TestPass:
    def test_累计十分钟自动达标(self):
        clock, svc = make()
        svc.start(batch_id=1, session_uid="DI-1")
        # 120℃ × 2s 间隔 × 300 次 = 600s
        reading = feed_curve(svc, clock, [120.0] * 301)
        assert reading.passed
        assert reading.cumulative_sec == 600.0
        record = svc.records[0]
        assert record.state == "passed"
        assert record.forbid_outbound is False
        assert record.end_ts == T0 + timedelta(seconds=600)

    def test_累计不足结束时判不达标(self):
        clock, svc = make()
        svc.start(batch_id=1)
        feed_curve(svc, clock, [110.0] * 100)     # 200s
        reading = svc.finish()
        assert reading.failed
        assert reading.reason == "short"
        assert svc.records[0].forbid_outbound is True

    def test_达标后解禁并留痕(self):
        clock, svc = make()
        svc.start(batch_id=1)
        feed_curve(svc, clock, [105.0] * 301)
        states = [event.state for event in svc.events]
        assert states[0] == "started"
        assert "passed" in states
        assert svc.records[0].forbid_outbound is False


# ================================================================ 不达标
class TestFail:
    def test_常温判不达标(self):
        clock, svc = make()
        svc.start(batch_id=1)
        feed_curve(svc, clock, [25.0] * 30)
        reading = svc.finish()
        assert reading.failed
        assert reading.reason == "short"
        assert reading.cumulative_sec == 0.0

    def test_超过150度立即异常超温(self):
        clock, svc = make()
        svc.start(batch_id=1)
        clock.advance(2)
        reading = svc.feed(152.0)
        assert reading.failed
        assert reading.reason == "abnormal_overtemp"
        record = svc.records[0]
        assert record.forbid_outbound is True
        assert record.rework_order_id is not None

    def test_超过120持续十分钟判控温失效(self):
        clock, svc = make()
        svc.start(batch_id=1)
        feed_curve(svc, clock, [125.0] * 301)
        record = svc.records[0]
        assert record.state == "failed"
        assert record.reason == "hold_overtime"
        # 高温不算达标累计：工艺温度跑高的时间不能凑数
        assert record.cumulative_sec == 0.0

    def test_长时间不达标判超时(self):
        clock, svc = make()
        svc.start(batch_id=1)
        # 90℃ 永远到不了达标区间，会话时长超过 1.5×600s
        reading = feed_curve(svc, clock, [90.0] * 460)
        assert reading.failed
        assert reading.reason == "timeout"

    def test_断点保护_一次采样只算上限(self):
        """断电十分钟再上电，不能一帧就「达标」。"""
        clock, svc = make()
        svc.start(batch_id=1)
        clock.advance(600)
        reading = svc.feed(120.0)
        assert reading.cumulative_sec == 120.0        # max_gap_sec 兜住
        assert reading.state == "running"

    def test_温度读数异常报错(self):
        _clock, svc = make()
        svc.start(batch_id=1)
        with pytest.raises(DisinfectionError) as exc:
            svc.feed(-120.0)
        assert "MAX6675" in str(exc.value)


# ================================================================ 复消闭环
class TestRework:
    def test_不达标开单_复消达标关单解禁(self):
        clock, svc = make()
        svc.start(batch_id=1)
        clock.advance(2)
        svc.feed(160.0)
        ticket = svc.tickets[0]
        assert ticket.state == "open"
        assert svc.forbid_outbound(batch_id=1) is True

        session = svc.start_rework(ticket.id)
        assert session.rework is True
        feed_curve(svc, clock, [118.0] * 301)
        assert svc.ticket(ticket.id).state == "closed"
        assert svc.forbid_outbound(batch_id=1) is False
        states = [event.state for event in svc.events]
        assert "rework_opened" in states and "rework_closed" in states

    def test_只开一张复消单(self):
        clock, svc = make()
        svc.start(batch_id=1)
        clock.advance(2)
        svc.feed(160.0)
        assert len(svc.tickets) == 1
        # 已结束的会话不再接读数，不会再开第二张
        assert svc.feed(160.0) is None
        assert len(svc.tickets) == 1

    def test_已关单的复消不能再消(self):
        clock, svc = make()
        svc.start(batch_id=1)
        clock.advance(2)
        svc.feed(160.0)
        ticket = svc.tickets[0]
        svc.start_rework(ticket.id)
        feed_curve(svc, clock, [118.0] * 301)
        with pytest.raises(DisinfectionError):
            svc.start_rework(ticket.id)

    def test_不存在的单据报错(self):
        _clock, svc = make()
        with pytest.raises(NotFoundError):
            svc.start_rework(99)
        with pytest.raises(NotFoundError):
            svc.record(99)


# ================================================================ 演示加速与策略
class TestDemoScale:
    def test_600秒压成30秒(self):
        clock, svc = make(time_scale=DEMO_TIME_SCALE)
        assert abs(DEMO_TIME_SCALE - 20.0) < 1e-9
        svc.start(batch_id=1)
        reading = feed_curve(svc, clock, [120.0] * 16)   # 15×2s×20 = 600s 虚拟
        assert reading.passed
        assert reading.cumulative_sec == 600.0
        assert (clock() - T0).total_seconds() < 40       # 真实只花了半分钟

    def test_策略必须递增(self):
        with pytest.raises(DisinfectionError):
            DisinfectionPolicy(target_c=120.0, hold_c=100.0)

    def test_从阈值表构造(self):
        rows = [
            {"kind": "range", "lower_bound": 100.0, "upper_bound": 120.0},
            {"kind": "duration", "upper_bound": 120.0, "duration_sec": 600},
            {"kind": "tier", "upper_bound": 150.0},
        ]
        policy = DisinfectionPolicy.from_thresholds(rows)
        assert (policy.target_c, policy.hold_c, policy.abnormal_c) == (100.0, 120.0, 150.0)
        assert policy.target_sec == 600.0
        assert policy.timeout_sec == 900.0

    def test_快照字段(self):
        clock, svc = make()
        svc.start(batch_id=1)
        feed_curve(svc, clock, [110.0] * 5)
        snap = svc.snapshot()
        assert snap["running"]
        assert snap["cumulative_sec"] == [10.0]
        assert snap["ignored_samples"] == 0
