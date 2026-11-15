# -*- coding: utf-8 -*-
"""#3 明火离人：视觉 AND 火焰、工位锁、刷卡复位、视觉失联降级。

这组测试盯的是「看起来在工作、其实防不住」的形态：

- 只看火焰 → 每天炒菜都报警，功能会被关掉；
- 只看视觉 → 厨师背对摄像头就报警；
- 视觉失联却默认「无人」→ 一次网络抖动把全店燃气阀关掉；
- 刷卡能复位燃气 → 一张工牌把「气还在漏」按成正常。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kitchen.rules.config_store import load_builtin
from kitchen.runtime import KitchenRuntime
from kitchen.safety import FlameGuard, StaffCard, VISION_NO_PERSON

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 11, 12, 11, 0, 0, tzinfo=CST).timestamp()


class FakeClock:
    def __init__(self, start: float = T0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += float(delta)
        return self.t


CARDS = {
    "04A2B3C1": StaffCard("04A2B3C1", "1", "张三"),
    "04A2B3C2": StaffCard("04A2B3C2", "2", "李四", expired=True),
}


def make_guard(clock: FakeClock | None = None, *, timeout: float = 30.0):
    clock = clock or FakeClock()
    rt = KitchenRuntime(load_builtin(), device_id="K-TEST", clock=clock, wall_clock=clock)
    guard = FlameGuard(rt.engine, card_lookup=CARDS.get, clock=clock,
                       vision_timeout_sec=timeout)
    return rt, guard, clock


def feed(rt, guard, clock, *, flame: float, no_person: bool, seconds: float,
         step: float = 5.0) -> list:
    """每帧都刷新视觉信号（真实部署里 ai_service 是持续推的）。"""
    results = []
    for _ in range(max(1, int(round(seconds / step)))):
        clock.advance(step)
        guard.observe_vision(no_person=no_person, headcount=0 if no_person else 2)
        results.append(rt.tick({"flame_b": flame, **guard.vision_frame()}))
    return results


def verdicts(results: list) -> list:
    return [item for result in results for item in result.verdicts]


def fired(results: list) -> list:
    return [item for item in verdicts(results) if item.fired]


# ============================================================ 视觉信号
class TestVisionSignal:
    def test_视觉信号不进遥测契约(self):
        """`vision_no_person` 不在 12 类遥测里（它由 ai_service 注入）。"""
        from kitchen.manifest import TELEMETRY_METRICS

        assert VISION_NO_PERSON not in TELEMETRY_METRICS
        assert len(TELEMETRY_METRICS) == 12

    def test_无人时喂1有人时喂0(self):
        _, guard, _ = make_guard()
        assert guard.observe_vision(no_person=True) == {VISION_NO_PERSON: 1.0}
        assert guard.observe_vision(no_person=False) == {VISION_NO_PERSON: 0.0}

    def test_视觉过期时喂有人而不是无人(self):
        """默认「无人」会让一次网络抖动变成全店停业——这是方向性错误。

        选「有人」+ 单独告警：功能还能用（不误停业），但平台知道这条防线瞎了。
        """
        _, guard, clock = make_guard(timeout=30.0)
        guard.observe_vision(no_person=True)
        assert guard.vision_frame() == {VISION_NO_PERSON: 1.0}
        clock.advance(31)
        assert guard.vision_frame() == {VISION_NO_PERSON: 0.0}
        assert guard.vision_health().stale

    def test_从未收到视觉信号也算离线(self):
        _, guard, _ = make_guard()
        health = guard.vision_health()
        assert not health.online
        assert "从未到达" in health.message

    def test_在线时health不报警(self):
        _, guard, clock = make_guard()
        guard.observe_vision(no_person=False)
        clock.advance(5)
        health = guard.vision_health()
        assert health.online
        assert health.age_sec == pytest.approx(5.0)

    def test_规则上确实挂了视觉前提(self):
        """自检：`requires` 没挂上就等于「只看火焰」，必须能查出来。"""
        _, guard, _ = make_guard()
        conditions = guard.require_conditions()
        assert len(conditions) == 1
        assert conditions[0].metric == VISION_NO_PERSON
        assert conditions[0].op == ">"


# ============================================================ 判据
class TestFlameDetection:
    def test_只有火焰没人不报警(self):
        """正常炒菜：灶上有火、人就站在灶前。这条错了，功能上线三天就被关。"""
        rt, guard, clock = make_guard()
        results = feed(rt, guard, clock, flame=1.0, no_person=False, seconds=40)
        assert fired(results) == []

    def test_只有视觉无人没有火不报警(self):
        """厨师背对摄像头 / 去拿货：没火就没有隐患。"""
        rt, guard, clock = make_guard()
        results = feed(rt, guard, clock, flame=0.0, no_person=True, seconds=40)
        assert fired(results) == []

    def test_有火且无人持续12秒触发(self):
        rt, guard, clock = make_guard()
        assert fired(feed(rt, guard, clock, flame=1.0, no_person=True, seconds=10)) == []
        results = fired(feed(rt, guard, clock, flame=1.0, no_person=True, seconds=10))
        assert len(results) == 1
        assert "明火" in results[0].message

    def test_触发后动作包含关阀断电声光语音(self):
        rt, guard, clock = make_guard()
        results = feed(rt, guard, clock, flame=1.0, no_person=True, seconds=20)
        actions = tuple(a for r in results for a in r.actions)
        for expected in ("gas_valve_close", "power_cut", "alarm_light_on", "voice_play"):
            assert expected in actions, f"缺少动作 {expected}"

    def test_视觉失联时不触发联锁(self):
        """过期 → 喂「有人」→ 规则不成立。宁可漏判也不能天天误关店。"""
        rt, guard, clock = make_guard(timeout=10.0)
        guard.observe_vision(no_person=True)
        results = []
        for _ in range(10):                       # 不再刷新视觉，任其过期
            clock.advance(5)
            results.append(rt.tick({"flame_b": 1.0, **guard.vision_frame()}))
        assert fired(results) == []
        assert guard.vision_health().stale


# ============================================================ 工位锁与刷卡
class TestCardReset:
    def _lock(self):
        rt, guard, clock = make_guard()
        feed(rt, guard, clock, flame=1.0, no_person=True, seconds=20)
        return rt, guard, clock

    def test_触发后工位处于锁定(self):
        _, guard, _ = self._lock()
        assert guard.locked
        assert guard.locked_rules() == ["22"]

    def test_刷卡复位成功_即使火焰仍在(self):
        """刷卡的语义就是「我人回来了」。等传感器反应过来才解锁，厨师会以为卡坏了。

        对比 #1 燃气：不声明 `card_reset`，所以「气还在漏就按确认」依然被拒。
        """
        _, guard, _ = self._lock()
        result = guard.ack_by_card("04A2B3C1")
        assert result.ok, result.describe()
        assert result.reset == ("22",)
        assert guard.locked_rules() == []

    def test_刷卡后视觉同步为有人(self):
        """不回头同步视觉，下一帧又会把工位锁回去——厨师刷完卡 5 秒后又响。"""
        rt, guard, clock = self._lock()
        guard.ack_by_card("04A2B3C1")
        assert guard.vision_frame() == {VISION_NO_PERSON: 0.0}
        clock.advance(5)
        assert fired([rt.tick({"flame_b": 1.0, **guard.vision_frame()})]) == []

    def test_未知卡号拒绝复位(self):
        _, guard, _ = self._lock()
        result = guard.ack_by_card("FFFFFFFF")
        assert not result.ok
        assert "不在工牌名册" in result.reason
        assert guard.locked_rules() == ["22"]

    def test_空卡号拒绝(self):
        _, guard, _ = self._lock()
        assert not guard.ack_by_card("").ok

    def test_健康证过期仍允许复位但留痕(self):
        """过期是合规问题，现场处置是安全问题。合规事后追责，安全不能等。"""
        _, guard, _ = self._lock()
        result = guard.ack_by_card("04A2B3C2")
        assert result.ok
        assert result.card is not None and result.card.expired
        assert any("健康证" in item for item in result.warnings)

    def test_未锁定时刷卡给出明确原因(self):
        _, guard, _ = make_guard()
        result = guard.ack_by_card("04A2B3C1")
        assert not result.ok
        assert "未处于锁定" in result.reason

    def test_刷卡不能复位燃气联锁(self):
        """最危险的越权路径：一张工牌把「气还在漏」按成正常。"""
        rt, guard, clock = make_guard()
        # 燃气冲到泄漏门限（id=1，立即联锁）
        for _ in range(3):
            clock.advance(5)
            rt.tick({"gas_level": 0.80, "flame_b": 0.0})
        assert "1" in rt.engine.snapshot()["latched"], "燃气应先闩锁"

        result = guard.ack_by_card("04A2B3C1")
        assert not result.ok, "刷卡不该能复位燃气"
        assert "1" in rt.engine.snapshot()["latched"]

    def test_复位后再次离人可重新锁定(self):
        rt, guard, clock = self._lock()
        assert guard.ack_by_card("04A2B3C1").ok
        # 复位后要过 60 秒冷却才会重新触发（引擎 anti-flap）
        feed(rt, guard, clock, flame=1.0, no_person=True, seconds=70)
        assert guard.locked

    def test_多灶部署刷卡只解本灶的锁(self):
        """工位隔离：A 灶厨师返岗刷卡，不能把 B 灶的离人锁一起解开。

        多灶时每个灶一个 FlameGuard（vision/link.py「一个实例对应一个灶」），
        并用 rule_ids= 圈定本灶规则——否则一张卡解所有 flame_b 锁，
        B 灶可能真没人。
        """
        from kitchen.rules.model import Rule

        clock = FakeClock()
        rt = KitchenRuntime(load_builtin(), device_id="K-TEST", clock=clock, wall_clock=clock)
        # 在内置 #3（id=22）之外补一条 B 灶的离人规则，模拟双灶
        rules = [r for r in rt.engine.get_rules() if r.metric == "flame_b"]
        extra = Rule(id="9001", metric="flame_b", op=">", threshold=0.5,
                     kind="duration", duration_sec=12.0, latch=True,
                     card_reset=True, requires=rules[0].requires)
        rt.engine.set_rules(rules + [extra])

        guard_a = FlameGuard(rt.engine, card_lookup=CARDS.get, clock=clock,
                             rule_ids=("22",))          # A 灶：只管 22
        guard_b = FlameGuard(rt.engine, card_lookup=CARDS.get, clock=clock,
                             rule_ids=("9001",))        # B 灶：只管 9001

        # 两个灶同时有火且无人 → 两条规则都闩锁
        for _ in range(4):
            clock.advance(5)
            guard_a.observe_vision(no_person=True)
            guard_b.observe_vision(no_person=True)
            rt.tick({"flame_b": 1.0, **guard_a.vision_frame()})
        assert guard_a.locked_rules() == ["22"]
        assert guard_b.locked_rules() == ["9001"]

        # A 灶厨师刷卡：只解 A 灶
        result = guard_a.ack_by_card("04A2B3C1")
        assert result.ok and result.reset == ("22",)
        assert guard_a.locked_rules() == []
        assert guard_b.locked_rules() == ["9001"], "B 灶的锁不该被 A 灶的卡解开"

        # B 灶自己刷卡才解自己的
        assert guard_b.ack_by_card("04A2B3C2").ok
        assert guard_b.locked_rules() == []
