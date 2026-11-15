# -*- coding: utf-8 -*-
"""视觉 → #3 明火离人（A5-06）。"""
from __future__ import annotations

from datetime import timedelta

from vision_support import P1, P2, P_PASS, ROIS, bare, frame, full_kit

from kitchen.domain import CST
from kitchen.rules.config_store import load_builtin
from kitchen.runtime import KitchenRuntime
from kitchen.safety import FlameGuard, VISION_NO_PERSON
from kitchen.vision.link import FlameVisionLink
from kitchen.vision.postprocess import analyze

from datetime import datetime

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += float(delta)
        return self.t


def make_guard(*, timeout: float = 30.0):
    clock = FakeClock()
    runtime = KitchenRuntime(load_builtin(), device_id="K-VISION",
                             clock=clock, wall_clock=clock)
    guard = FlameGuard(runtime.engine, card_lookup=lambda uid: None,
                       vision_timeout_sec=timeout, clock=clock)
    return clock, guard


def view(dets, seq: int = 1):
    return analyze(dets, frame(seq, ts=T0 + timedelta(seconds=seq)), ROIS)


class TestStationScoped:
    def test_本工位无人_别处有人也算无人(self):
        """2 米外的服务员不能让灶前的火变成「有人看着」。"""
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        result = link.push(view(full_kit(P2)))
        assert result.headcount == 0
        assert result.no_person is True
        assert result.signals[VISION_NO_PERSON] == 1.0

    def test_本工位有人(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        result = link.push(view(full_kit(P1)))
        assert result.headcount == 1
        assert result.signals[VISION_NO_PERSON] == 0.0

    def test_过道的人不算工位有人(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        assert link.push(view(bare(P_PASS))).no_person is True

    def test_未指定工位时用全画面人数(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, clock=clock)
        assert link.push(view(full_kit(P2))).no_person is False

    def test_空画面判无人(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        assert link.push(view([])).no_person is True


class TestOffline:
    def test_离线不投递(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        result = link.push(view([]), online=False)
        assert result.fed is False
        assert link.fed == 0
        assert link.skipped == 1

    def test_离线后守卫按有人降级(self):
        """离线不喂 → 守卫保持上一次值；超时后 vision_frame() 返回「有人」。"""
        clock, guard = make_guard(timeout=5.0)
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        link.push(view([]))                       # 先喂一次「无人」
        assert guard.vision_frame() == {VISION_NO_PERSON: 1.0}
        clock.advance(10)
        assert guard.vision_frame() == {VISION_NO_PERSON: 0.0}, "失联必须降级为有人，不能关气"

    def test_离线不刷新健康时间戳(self):
        clock, guard = make_guard(timeout=5.0)
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        link.push(view(full_kit(P1)))
        clock.advance(10)
        link.push(view(full_kit(P1)), online=False)
        assert guard.vision_health().online is False, "离线还喂等于给瞎掉的防线续命"


class TestDescription:
    def test_描述含工位与人数(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=2, clock=clock)
        text = link.push(view(full_kit(P2))).describe()
        assert "ST2" in text and "1 人" in text

    def test_离线描述(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        assert "未投递" in link.push(view([]), online=False).describe()

    def test_snapshot(self):
        clock, guard = make_guard()
        link = FlameVisionLink(guard, station_id=1, clock=clock)
        link.push(view(full_kit(P1)))
        snap = link.snapshot()
        assert snap["fed"] == 1
        assert snap["station_id"] == 1
        assert snap["online"] is True
