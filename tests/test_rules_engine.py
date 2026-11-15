# -*- coding: utf-8 -*-
"""规则引擎单测：八项扩展能力逐项验证（A2-01 验收）。

覆盖矩阵（每项 ≥ 3 用例，验收标准原文）：

| 能力 | 用例类 |
|---|---|
| `per_rule` 时长 | `TestPerRuleDuration` |
| 窗口聚合 | `TestWindowAggregate` |
| 区间判定 | `TestRangeBound` |
| 档位阈值 | `TestProfileGate` |
| 多源交叉 | `TestCrossSource` |
| 闩锁 + 人工复位 | `TestLatchAndAck` |
| 开机宽限 | `TestBootGrace` |
| NVS 持久化 | `TestNvsPersistence` |

外加两组护栏（不是验收项，但比验收项更容易在半年后救你一命）：

- `TestSeedData`：直接跑 `fixtures/seed_kitchen.sql` 的 21 条真实阈值。
  测试不另抄阈值，所以种子把 `upper_bound` 写成 `lower_bound` 时——
  「超温规则永远不触发」这种事故——这里会立刻红。
- `TestLoader`：脏配置在装载期报错，而不是装进去后静默失效。

时钟一律用 `FakeClock` 手推，不 `sleep`：判定逻辑全是时间敏感的，
用真实时钟写出来的测试要么慢要么偶发失败。
"""
from __future__ import annotations

import json

import pytest

from kitchen.channels import ACTION_BINDINGS
from kitchen.manifest import TELEMETRY_METRICS
from kitchen.rules import (
    EVENT_FIRED,
    EVENT_LATCHED,
    EVENT_RELEASED,
    Condition,
    Rule,
    RuleEngine,
    RuleError,
    RuleStateStore,
    SlidingWindow,
    bounds_to_op,
    compare,
    load_rows,
    load_scenario_rules,
    profile_rules,
    profiles_of,
    validate_rule,
)


# ---------------------------------------------------------------- 测试脚手架
class FakeClock:
    """可手推时钟。判定用 monotonic 语义、闩锁展示用墙钟语义，测试里分开实例化。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def wall() -> FakeClock:
    return FakeClock(start=1_700_000_000.0)


def make_engine(clock: FakeClock, wall: FakeClock, **kwargs) -> RuleEngine:
    kwargs.setdefault("device_id", "kitchen-node-01")
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("wall_clock", wall)
    return RuleEngine(**kwargs)


def feed(engine: RuleEngine, clock: FakeClock, metrics: dict, seconds: float,
         step: float = 1.0) -> list:
    """喂 seconds 秒的数据，返回这段时间内所有 verdict。

    先按当前时刻打一帧（t0），再逐 step 推进 —— 这样 `feed(30)` 就是「越界整
    整 30 秒」，而不是「30 个采样点里只过了 29 秒」。持续性判定的边界最怕这种
    差一秒的错觉。
    """
    events = list(engine.evaluate(metrics, now=clock()))
    for _ in range(int(round(seconds / step))):
        clock.advance(step)
        events.extend(engine.evaluate(metrics, now=clock()))
    return events


def fired(events) -> list:
    return [event for event in events if event.event == EVENT_FIRED]


# ================================================================ 1. per_rule 时长
class TestPerRuleDuration:
    """每条规则自己的持有时长；不填就用引擎默认值。"""

    @staticmethod
    def _rule(**kwargs) -> Rule:
        base = dict(id="r1", metric="fridge_temp", op=">", threshold=8.0)
        base.update(kwargs)
        return Rule(**base)

    def test_持续29秒不触发_第30秒触发(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule(kind="duration", duration_sec=30)])
        assert fired(feed(engine, clock, {"fridge_temp": 12.0}, 29)) == []
        events = fired(feed(engine, clock, {"fridge_temp": 12.0}, 1))
        assert len(events) == 1
        assert events[0].elapsed >= 30

    def test_中途回落会重新计时(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule(kind="duration", duration_sec=30)])
        feed(engine, clock, {"fridge_temp": 12.0}, 20)      # 已持续 20 秒
        feed(engine, clock, {"fridge_temp": 4.0}, 1)        # 回落到正常，计时清零
        assert fired(feed(engine, clock, {"fridge_temp": 12.0}, 20)) == []
        assert len(fired(feed(engine, clock, {"fridge_temp": 12.0}, 10))) == 1

    def test_未配时长走引擎默认值(self, clock, wall):
        engine = make_engine(clock, wall, default_duration_sec=3.0)
        engine.set_rules([self._rule()])                   # 不写 duration_sec
        assert fired(feed(engine, clock, {"fridge_temp": 12.0}, 2)) == []
        assert len(fired(feed(engine, clock, {"fridge_temp": 12.0}, 1))) == 1

    def test_时长为0立即触发(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule(kind="duration", duration_sec=0)])
        events = fired(feed(engine, clock, {"fridge_temp": 12.0}, 1))
        assert len(events) == 1

    def test_两条规则时长互不干扰(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([
            self._rule(id="fast", metric="fridge_temp", kind="duration", duration_sec=2),
            self._rule(id="slow", metric="freezer_temp", op=">", threshold=-18.0,
                       kind="duration", duration_sec=10),
        ])
        events = feed(engine, clock, {"fridge_temp": 12.0, "freezer_temp": -10.0}, 2)
        assert [event.rule_id for event in fired(events)] == ["fast"]
        events = feed(engine, clock, {"fridge_temp": 12.0, "freezer_temp": -10.0}, 8)
        assert "slow" in [event.rule_id for event in fired(events)]

    def test_短暂越界未报过警则回落不发恢复(self, clock, wall):
        """只越界 5 秒（不足 30 秒持续时长）→ 从没报过警，回落时也不该发
        「已恢复正常」。否则用户会收到一条他从没见过的告警的恢复通知。"""
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule(kind="duration", duration_sec=30)])
        assert feed(engine, clock, {"fridge_temp": 12.0}, 5) == []
        assert feed(engine, clock, {"fridge_temp": 4.0}, 5) == []

    def test_冷却期内同一规则只报一次(self, clock, wall):
        engine = make_engine(clock, wall, cooldown_sec=60.0)
        engine.set_rules([self._rule(kind="duration", duration_sec=0)])
        first = fired(feed(engine, clock, {"fridge_temp": 12.0}, 1))
        again = fired(feed(engine, clock, {"fridge_temp": 12.0}, 30))
        assert len(first) == 1 and again == []
        # 冷却结束后允许再报
        assert len(fired(feed(engine, clock, {"fridge_temp": 12.0}, 31))) == 1


# ================================================================ 2. 窗口聚合
class TestWindowAggregate:
    """#2 的「10 分钟温升」与 #1 的「中位数抗脉冲」都靠这一层。"""

    def _window_engine(self, clock, wall, **rule_kwargs) -> RuleEngine:
        base = dict(
            id="hood", metric="hood_temp", op=">", threshold=100.0,
            kind="window", window_sec=600.0, aggregate="rise",
        )
        base.update(rule_kwargs)
        engine = make_engine(clock, wall)
        engine.set_rules([Rule(**base)])
        return engine

    def test_温升超过阈值触发(self, clock, wall):
        """600 秒窗口内从 200℃ 升到 320℃（温升 120℃ > 100℃）应触发。"""
        engine = self._window_engine(clock, wall)
        for index in range(11):
            engine.evaluate({"hood_temp": 200.0 + index * 12}, now=clock())
            clock.advance(30)
        # 窗口规则同样走默认 3 秒去抖（见 Rule.effective_duration 的说明：
        # `rise` 在旧点滑出窗口时会突变，没有去抖就会 fired/released 来回闪）。
        # 因此触发时刻是「温升连续 3 秒超阈值」那一帧，而不是等预热跑完——
        # 真积热不该等满 10 分钟才开始喊。
        events = []
        for index in range(11):
            events.extend(fired(engine.evaluate(
                {"hood_temp": 200.0 + index * 12}, now=clock())))
            clock.advance(30)
        events.extend(fired(engine.evaluate({"hood_temp": 320.0}, now=clock())))
        assert len(events) == 1
        assert events[0].value > 100.0            # value 是聚合后的温升
        assert events[0].raw == 320.0             # raw 是瞬时值，两者不混

    def test_窗口外的旧数据被淘汰(self, clock, wall):
        engine = self._window_engine(clock, wall)
        engine.evaluate({"hood_temp": 150.0}, now=clock())   # 一个早期的低点
        clock.advance(700)                                   # 700 > 600，已被淘汰
        engine.evaluate({"hood_temp": 400.0}, now=clock())
        window = engine.window("hood_temp", 600.0)
        assert window is not None and len(window) == 1
        assert window.aggregate("rise") is None              # 只剩 1 个点，算不出变化量

    def test_中位数抗单点脉冲(self, clock, wall):
        engine = self._window_engine(
            clock, wall, op=">", threshold=0.3, window_sec=60.0, aggregate="median",
            metric="gas_level", id="gas",
        )
        for value in (0.02, 0.02, 0.02, 0.02, 0.02, 0.90, 0.02):   # 中间一个尖峰
            engine.evaluate({"gas_level": value}, now=clock())
            clock.advance(5)
        window = engine.window("gas_level", 60.0)
        assert window is not None
        assert window.aggregate("median") == pytest.approx(0.02)
        assert fired(engine.evaluate({"gas_level": 0.02}, now=clock())) == []

    def test_rate用斜率而非首末差(self, clock, wall):
        window = SlidingWindow(window_sec=60.0)
        # 带噪声的上升趋势：首末差会被噪声放大，斜率更稳
        series = [(0, 0.10), (10, 0.16), (20, 0.13), (30, 0.22), (40, 0.19), (50, 0.28)]
        for moment, value in series:
            window.push(float(moment), value)
        slope = window.aggregate("rate")
        assert slope is not None and 0.002 < slope < 0.005      # 单位/秒

    def test_窗口数据不足时返回None而不是0(self, clock, wall):
        window = SlidingWindow(window_sec=600.0)
        assert window.aggregate("rise") is None
        window.push(0.0, 100.0)
        assert window.aggregate("rise") is None                 # 单点算不出变化
        assert window.aggregate("last") == 100.0
        assert window.aggregate("median") == 100.0

    def test_档位切换不清空窗口数据(self, clock, wall):
        engine = self._window_engine(clock, wall, profile="business", kind="window")
        for index in range(5):
            engine.evaluate({"hood_temp": 200.0 + index}, now=clock())
            clock.advance(10)
        engine.set_profile("prep")
        engine.set_rules([Rule(
            id="hood", metric="hood_temp", op=">", threshold=80.0,
            kind="window", window_sec=600.0, aggregate="rise", profile="prep",
        )])
        window = engine.window("hood_temp", 600.0)
        assert window is not None and len(window) == 5          # 数据还在

    def test_乱序点被丢弃并计数(self):
        window = SlidingWindow(window_sec=60.0)
        window.push(10.0, 1.0)
        assert window.push(5.0, 2.0) is False
        assert window.dropped_out_of_order == 1
        assert window.values() == [1.0]


# ================================================================ 3. 区间判定
class TestRangeBound:
    """#15 的「100~120℃ 达标区」与冷链「0~8℃」都靠 in/out 双边语义。"""

    def test_out区间外违规(self):
        assert compare("out", 8.1, lower=0.0, upper=8.0) is True
        assert compare("out", -0.1, lower=0.0, upper=8.0) is True
        assert compare("out", 4.0, lower=0.0, upper=8.0) is False

    def test_in区间内违规(self):
        assert compare("in", 110.0, lower=100.0, upper=120.0) is True
        assert compare("in", 99.9, lower=100.0, upper=120.0) is False
        assert compare("in", 120.0, lower=100.0, upper=120.0) is True   # 闭区间

    def test_单边边界只判一侧(self):
        assert compare("out", 0.60, upper=0.55) is True
        assert compare("out", 0.10, upper=0.55) is False
        assert compare("out", -5.0, lower=0.0) is True
        assert compare("out", 5.0, lower=0.0) is False

    def test_边界推导比较符(self):
        assert bounds_to_op(None, 0.55) == (">", 0.55)
        assert bounds_to_op(0.0, None) == ("<", 0.0)
        assert bounds_to_op(0.0, 8.0) == ("out", None)
        with pytest.raises(RuleError):
            bounds_to_op(None, None)

    def test_区间下界大于上界报错(self):
        rule = Rule(id="bad", metric="fridge_temp", op="out", lower=8.0, upper=0.0)
        assert any("区间为空" in item for item in validate_rule(rule))

    def test_引擎按区间判定整条链路(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([Rule(
            id="fridge", metric="fridge_temp", op="out", lower=0.0, upper=8.0,
        )])
        assert fired(feed(engine, clock, {"fridge_temp": 4.0}, 10)) == []
        assert len(fired(feed(engine, clock, {"fridge_temp": 9.0}, 10))) == 1


# ================================================================ 4. 档位阈值
class TestProfileGate:
    """#2 营业/备餐/收档三套阈值，同一 metric 不同档位不同边界。"""

    @staticmethod
    def _tier_rules() -> list[Rule]:
        return [
            Rule(id="biz", metric="hood_temp", op=">", threshold=280.0,
                 kind="tier", profile="business", level=1),
            Rule(id="prep", metric="hood_temp", op=">", threshold=250.0,
                 kind="tier", profile="prep", level=2),
            Rule(id="closed", metric="hood_temp", op=">", threshold=200.0,
                 kind="tier", profile="closed", level=2),
            Rule(id="common", metric="fridge_temp", op=">", threshold=8.0),
        ]

    def test_只判定当前档位的规则(self, clock, wall):
        engine = make_engine(clock, wall, profile="business")
        engine.set_rules(self._tier_rules())
        # 260℃：营业档（280）不触发，若按备餐档（250）就该触发
        assert fired(feed(engine, clock, {"hood_temp": 260.0}, 5)) == []

    def test_切换档位后阈值随之变化(self, clock, wall):
        engine = make_engine(clock, wall, profile="business")
        engine.set_rules(self._tier_rules())
        assert fired(feed(engine, clock, {"hood_temp": 260.0}, 5)) == []
        engine.set_profile("prep")
        assert len(fired(feed(engine, clock, {"hood_temp": 260.0}, 5))) == 1

    def test_未声明档位的规则始终生效(self, clock, wall):
        engine = make_engine(clock, wall, profile="closed")
        engine.set_rules(self._tier_rules())
        events = fired(feed(engine, clock, {"fridge_temp": 12.0}, 5))
        assert [event.rule_id for event in events] == ["common"]

    def test_档位配置文件可枚举(self):
        rules = self._tier_rules()
        assert profiles_of(rules) == ["business", "closed", "prep"]
        assert {rule.id for rule in profile_rules(rules, "prep")} == {"prep", "common"}

    def test_tier缺profile装载期报错(self):
        rule = Rule(id="x", metric="hood_temp", op=">", threshold=280.0, kind="tier")
        assert any("必须给 profile" in item for item in validate_rule(rule))


# ================================================================ 5. 多源交叉
class TestCrossSource:
    """#3 明火离人：视觉无人 AND 火焰有效，单靠任一个都不能报。"""

    @staticmethod
    def _rule(**kwargs) -> Rule:
        base = dict(
            id="flame", metric="flame_b", op=">", threshold=0.5,
            requires=(Condition(metric="vision_no_person", op=">", threshold=0.5,
                                label="视觉判定无人"),),
            latch=True,
        )
        base.update(kwargs)
        return Rule(**base)

    def test_只有火焰无人不成立时不触发(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule()])
        assert fired(feed(engine, clock, {"flame_b": 1.0, "vision_no_person": 0.0}, 20)) == []

    def test_两者同时成立才触发(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule()])
        assert fired(feed(engine, clock, {"flame_b": 1.0, "vision_no_person": 1.0}, 2)) == []
        assert len(fired(feed(engine, clock,
                              {"flame_b": 1.0, "vision_no_person": 1.0}, 5))) == 1

    def test_交叉条件缺数据视为不成立(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule()])
        assert fired(feed(engine, clock, {"flame_b": 1.0}, 20)) == []

    def test_交叉条件失效后计时清零(self, clock, wall):
        """视觉恢复（人回来了）再离开时，必须重新等满持续时长，不能残留计时秒触发。"""
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule(kind="duration", duration_sec=10)])
        feed(engine, clock, {"flame_b": 1.0, "vision_no_person": 1.0}, 8)
        feed(engine, clock, {"flame_b": 1.0, "vision_no_person": 0.0}, 1)   # 人回来
        assert fired(feed(engine, clock, {"flame_b": 1.0, "vision_no_person": 1.0}, 8)) == []
        assert len(fired(feed(engine, clock,
                              {"flame_b": 1.0, "vision_no_person": 1.0}, 2))) == 1

    def test_多条件全满足才成立(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([self._rule(requires=(
            Condition(metric="vision_no_person", op=">", threshold=0.5),
            Condition(metric="sct_current", op="<", threshold=0.1, label="排风未运行"),
        ))])
        metrics = {"flame_b": 1.0, "vision_no_person": 1.0, "sct_current": 0.5}
        assert fired(feed(engine, clock, metrics, 5)) == []
        metrics["sct_current"] = 0.02
        assert len(fired(feed(engine, clock, metrics, 5))) == 1


# ================================================================ 6. 闩锁 + 人工复位
class TestLatchAndAck:
    """#1「浓度回落且人工确认后复位」——回落和确认两个条件缺一不可。"""

    @staticmethod
    def _engine(clock, wall, **kwargs) -> RuleEngine:
        engine = make_engine(clock, wall)
        engine.set_rules([Rule(
            id="gas", metric="gas_level", op=">", threshold=0.55, latch=True, level=2,
            **kwargs,
        )])
        return engine

    def test_值回落后仍闩锁_确认后解锁(self, clock, wall):
        engine = self._engine(clock, wall)
        feed(engine, clock, {"gas_level": 0.70}, 5)
        assert engine.snapshot()["latched"] == ["gas"]

        events = feed(engine, clock, {"gas_level": 0.05}, 5)
        assert [event.event for event in events] == [EVENT_RELEASED]
        assert engine.snapshot()["latched"] == ["gas"]           # 回落不解锁

        result = engine.ack("gas", "E009")
        assert result.ok and result.reviewer == "E009"
        assert engine.snapshot()["latched"] == []

    def test_越界期间确认被拒(self, clock, wall):
        engine = self._engine(clock, wall)
        feed(engine, clock, {"gas_level": 0.70}, 5)
        result = engine.ack("gas", "E009")
        assert result.ok is False
        assert "仍处于越界" in result.reason
        assert engine.snapshot()["latched"] == ["gas"]

    def test_未闩锁规则确认被拒(self, clock, wall):
        engine = self._engine(clock, wall)
        result = engine.ack("gas", "E009")
        assert result.ok is False and "未处于闩锁" in result.reason

    def test_复位人不能为空(self, clock, wall):
        engine = self._engine(clock, wall)
        feed(engine, clock, {"gas_level": 0.70}, 5)
        assert engine.snapshot()["latched"] == ["gas"]      # 先确认真的闩锁了
        feed(engine, clock, {"gas_level": 0.02}, 5)
        result = engine.ack("gas", "   ")
        assert result.ok is False and "留痕" in result.reason

    def test_非闩锁规则回落即释放(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([Rule(id="sample", metric="sample_temp", op=">", threshold=25.0)])
        feed(engine, clock, {"sample_temp": 35.0}, 5)
        events = feed(engine, clock, {"sample_temp": 22.0}, 5)
        assert [event.event for event in events] == [EVENT_RELEASED]
        assert events[0].latched is False
        assert engine.snapshot()["latched"] == []

    def test_闩锁期间重复上报带latched标记(self, clock, wall):
        engine = self._engine(clock, wall, )
        engine.cooldown_sec = 5.0
        feed(engine, clock, {"gas_level": 0.70}, 5)
        events = feed(engine, clock, {"gas_level": 0.70}, 10)
        assert any(event.event == EVENT_LATCHED and event.latched for event in events)

    def test_批量确认逐条返回(self, clock, wall):
        engine = make_engine(clock, wall)
        engine.set_rules([
            Rule(id="a", metric="gas_level", op=">", threshold=0.55, latch=True),
            Rule(id="b", metric="hood_temp", op=">", threshold=200.0, latch=True),
        ])
        feed(engine, clock, {"gas_level": 0.7, "hood_temp": 300.0}, 5)
        feed(engine, clock, {"gas_level": 0.02, "hood_temp": 100.0}, 5)
        results = engine.ack_all("E009")
        assert len(results) == 2 and all(item.ok for item in results)
        assert engine.snapshot()["latched"] == []


# ================================================================ 7. 开机宽限
class TestBootGrace:
    """#1「开机前 5 分钟只建基线不判定」，但宽限期内必须继续采样。"""

    @staticmethod
    def _engine(clock, wall, grace: float = 300.0) -> RuleEngine:
        engine = make_engine(clock, wall, boot_grace_sec=grace, default_duration_sec=1.0)
        engine.set_rules([Rule(id="gas", metric="gas_level", op=">", threshold=0.55)])
        engine.boot(now=clock())
        return engine

    def test_宽限期内不判定(self, clock, wall):
        engine = self._engine(clock, wall)
        assert fired(feed(engine, clock, {"gas_level": 0.90}, 299)) == []

    def test_宽限结束立刻可判定(self, clock, wall):
        """关键点：宽限期内照样采样，所以宽限一到就有数据，不用再等一个窗口。"""
        engine = self._engine(clock, wall, grace=60.0)
        feed(engine, clock, {"gas_level": 0.10}, 60)
        assert fired(feed(engine, clock, {"gas_level": 0.90}, 2)) != []

    def test_宽限期为零不宽限(self, clock, wall):
        engine = self._engine(clock, wall, grace=0.0)
        assert fired(feed(engine, clock, {"gas_level": 0.90}, 3)) != []

    def test_剩余宽限时间递减(self, clock, wall):
        engine = self._engine(clock, wall, grace=300.0)
        clock.advance(100)
        assert engine.in_grace() is True
        assert engine.grace_remaining() == pytest.approx(200.0)
        clock.advance(250)
        assert engine.in_grace() is False
        assert engine.grace_remaining() == 0.0

    def test_未调用boot则不宽限(self, clock, wall):
        engine = make_engine(clock, wall, boot_grace_sec=300.0)
        engine.set_rules([Rule(id="gas", metric="gas_level", op=">", threshold=0.55)])
        assert engine.in_grace() is False
        assert fired(feed(engine, clock, {"gas_level": 0.90}, 3)) != []


# ================================================================ 8. NVS 持久化
class TestNvsPersistence:
    """闩锁必须跨重启保留（断电自动解锁 = 安全事故），计时不能持久化。"""

    @staticmethod
    def _engine(clock, wall, device_id: str = "kitchen-node-01") -> RuleEngine:
        engine = make_engine(clock, wall, device_id=device_id)
        engine.set_rules([Rule(
            id="gas", metric="gas_level", op=">", threshold=0.55, latch=True,
        )])
        return engine

    def test_blob往返保留闩锁(self, clock, wall):
        engine = self._engine(clock, wall)
        feed(engine, clock, {"gas_level": 0.70}, 5)
        blob = engine.persist()
        assert "gas|1|" in blob

        restored = self._engine(clock, wall)
        assert restored.restore(nvs_blob=blob) == ["gas"]
        assert restored.snapshot()["latched"] == ["gas"]

    def test_掉电重开不会自动解锁(self, clock, wall):
        engine = self._engine(clock, wall)
        feed(engine, clock, {"gas_level": 0.70}, 5)
        blob = engine.persist()

        # 模拟重启：新引擎、新时钟、值已回落
        reboot_clock = FakeClock(start=0.0)
        rebooted = self._engine(reboot_clock, wall)
        rebooted.restore(nvs_blob=blob)
        events = feed(rebooted, reboot_clock, {"gas_level": 0.02}, 10)
        assert rebooted.snapshot()["latched"] == ["gas"]
        assert all(event.event != EVENT_FIRED for event in events)

    def test_JSON文件往返(self, clock, wall, tmp_path):
        engine = self._engine(clock, wall)
        feed(engine, clock, {"gas_level": 0.70}, 5)
        path = tmp_path / "state" / "rule_state.json"
        engine.persist(str(path))
        assert path.is_file()

        restored = self._engine(clock, wall)
        assert restored.restore(path=str(path)) == ["gas"]
        assert restored.state_store.peek("gas").fire_count >= 1

    def test_坏行跳过不炸(self, clock, wall):
        engine = self._engine(clock, wall)
        blob = "gas|1|1700000000|E009|3\n坏行没有分隔符\n|1|x|\n"
        assert engine.restore(nvs_blob=blob) == ["gas"]
        assert engine.state_store.broken_lines == [2, 3]

    def test_计时不持久化_重启后重新计时(self, clock, wall):
        """breach_since 是单调时钟，存下来会让重启后的规则永远等不到持续时长。"""
        engine = self._engine(clock, wall)
        engine.set_rules([Rule(
            id="gas", metric="gas_level", op=">", threshold=0.55, latch=True,
            kind="duration", duration_sec=30,
        )])
        feed(engine, clock, {"gas_level": 0.70}, 20)
        blob = engine.persist()
        assert "breach" not in blob.lower()

        reboot_clock = FakeClock(start=0.0)
        rebooted = self._engine(reboot_clock, wall)
        rebooted.restore(nvs_blob=blob)
        rebooted.set_rules([Rule(
            id="gas", metric="gas_level", op=">", threshold=0.55, latch=True,
            kind="duration", duration_sec=30,
        )])
        assert fired(feed(rebooted, reboot_clock, {"gas_level": 0.70}, 29)) == []
        assert len(fired(feed(rebooted, reboot_clock, {"gas_level": 0.70}, 2))) == 1

    def test_规则集裁剪会清掉消失的规则状态(self, clock, wall):
        engine = self._engine(clock, wall)
        feed(engine, clock, {"gas_level": 0.70}, 5)
        engine.set_rules([Rule(id="other", metric="t", op=">", threshold=35.0)])
        assert engine.state_store.peek("gas") is None

    def test_空blob恢复不报错(self, clock, wall):
        engine = self._engine(clock, wall)
        assert engine.restore(nvs_blob="") == []
        assert RuleStateStore.from_nvs_blob("").states == {}


# ================================================================ 护栏一：真实种子数据
class TestSeedData:
    """直接跑 `fixtures/seed_kitchen.sql` 的 21 条阈值，验证数据与引擎口径一致。"""

    def test_全部阈值行装载通过(self, seed_threshold_rows, scenario_rules):
        # 21 条环境阈值 + 1 条 #3 明火离人（flame_b，profile=NULL 不限档位）
        assert len(seed_threshold_rows) == 25
        active, disabled = load_rows(seed_threshold_rows, scenario_rules=scenario_rules)
        assert len(active) + len(disabled) == 25

    def test_超温类阈值方向正确(self, seed_threshold_rows):
        """「超温 / 超浓度」必须填 upper_bound。填成 lower_bound 会变成低温告警，
        规则装得进去但永远不触发——这是种子数据最容易犯的错。"""
        active, _ = load_rows(seed_threshold_rows)
        upper_is_upper = {"gas_level", "hood_temp", "sterilize_temp",
                         "fridge_temp", "freezer_temp", "sample_temp"}
        for rule in active:
            if rule.metric in upper_is_upper and rule.op not in ("out", "in"):
                assert rule.op in (">", ">="), (
                    f"{rule.metric} 规则 {rule.id} 的比较符是 {rule.op!r}，"
                    f"超温/超浓度应为 '>'（upper_bound）"
                )

    def test_动作名全部在契约内(self, seed_threshold_rows, scenario_rules):
        active, _ = load_rows(seed_threshold_rows, scenario_rules=scenario_rules)
        used = {action for rule in active for action in rule.actions}
        assert used, "种子数据里一条动作都没有，联锁演示没法演"
        assert used <= set(ACTION_BINDINGS), f"未登记动作：{used - set(ACTION_BINDINGS)}"

    def test_燃气的动作不含切断电源(self, seed_threshold_rows, scenario_rules):
        """04 文档 §0.4：燃气泄漏时不主动切断灶台电源（继电器动作可能产生火花）。"""
        active, _ = load_rows(seed_threshold_rows, scenario_rules=scenario_rules)
        gas = [rule for rule in active if rule.metric == "gas_level"]
        assert gas and all("power_cut" not in rule.actions for rule in gas)

    def test_判定指标都在遥测契约内(self, seed_threshold_rows):
        active, _ = load_rows(seed_threshold_rows)
        outside = {rule.metric for rule in active} - set(TELEMETRY_METRICS)
        assert outside == set(), f"这些指标不在 12 类遥测契约里：{outside}"

    def test_排烟三档阈值随档位递进(self, seed_threshold_rows):
        active, _ = load_rows(seed_threshold_rows)
        hood = {(rule.profile, rule.kind): rule for rule in active if rule.metric == "hood_temp"}
        assert hood[("business", "tier")].threshold == 280.0
        assert hood[("prep", "tier")].threshold == 250.0
        assert hood[("closed", "tier")].threshold == 200.0
        # 营业档仅预警 → 动作里不能有动继电器的 gas_valve_close / fan_on
        assert "gas_valve_close" not in hood[("business", "tier")].actions

    def test_燃气两级处置的等级与阈值(self, seed_threshold_rows, scenario_rules):
        active, _ = load_rows(seed_threshold_rows, scenario_rules=scenario_rules)
        gas = {rule.id: rule for rule in active if rule.metric == "gas_level"}
        immediate, gradual = gas["1"], gas["2"]
        assert immediate.level == 2 and immediate.threshold == 0.55
        assert gradual.level == 1 and gradual.threshold == 0.45
        assert gradual.duration_sec == 30
        assert "gas_valve_close" in immediate.actions
        assert "gas_valve_close" not in gradual.actions       # 预警级不动作


# ================================================================ 护栏二：脏配置在装载期报错
class TestLoader:
    def test_duration缺边界报错(self):
        rows = [{"id": 1, "metric": "gas_level", "kind": "duration",
                 "point_id": 1, "duration_sec": 30, "level": 1}]
        with pytest.raises(RuleError):
            load_rows(rows)

    def test_window缺aggregate报错(self):
        rows = [{"id": 1, "metric": "hood_temp", "kind": "window",
                 "point_id": 2, "upper_bound": 100.0, "window_sec": 600.0,
                 "aggregate": "last", "level": 1}]
        with pytest.raises(RuleError):
            load_rows(rows)

    def test_缺metric时报错并指出行号(self):
        rows = [{"id": 7, "kind": "range", "upper_bound": 1.0, "level": 1}]
        with pytest.raises(RuleError) as excinfo:
            load_rows(rows)
        assert "env_thresholds.id=7" in str(excinfo.value)

    def test_规则id重复报错(self):
        rows = [
            {"id": 1, "metric": "t", "upper_bound": 35.0, "level": 1},
            {"id": 1, "metric": "h", "upper_bound": 90.0, "level": 1},
        ]
        with pytest.raises(RuleError) as excinfo:
            load_rows(rows)
        assert "id 重复" in str(excinfo.value)

    def test_停用行也校验但不进引擎(self):
        rows = [
            {"id": 1, "metric": "t", "upper_bound": 35.0, "level": 1, "enabled": 1},
            {"id": 2, "metric": "h", "upper_bound": 90.0, "level": 1, "enabled": 0},
        ]
        active, disabled = load_rows(rows)
        assert [rule.id for rule in active] == ["1"]
        assert [rule.id for rule in disabled] == ["2"]

    def test_双层配置合并(self, tmp_path):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"rules": {"gas_level": {
            "latch": True, "actions": ["gas_valve_close"],
        }}}), encoding="utf-8")
        scenario = load_scenario_rules(path)
        rows = [{"id": 1, "metric": "gas_level", "upper_bound": 0.55, "level": 2,
                 "point_id": 1}]
        active, _ = load_rows(rows, scenario_rules=scenario)
        assert active[0].latch is True
        assert active[0].actions == ("gas_valve_close",)
        assert active[0].threshold == 0.55

    def test_by_profile覆盖动作(self, seed_threshold_rows, scenario_rules):
        active, _ = load_rows(seed_threshold_rows, scenario_rules=scenario_rules)
        biz = next(rule for rule in active
                   if rule.metric == "hood_temp" and rule.profile == "business"
                   and rule.kind == "tier")
        closed = next(rule for rule in active
                      if rule.metric == "hood_temp" and rule.profile == "closed"
                      and rule.kind == "tier")
        assert biz.actions == ("voice_play",)          # 营业档仅预警
        assert "fan_on" in closed.actions              # 收档档要排风


# ================================================================ 端到端：燃气两级处置
class TestGasLeakScenario:
    """把 #1 完整演一遍：平稳 → 突升 → 预警 → 联锁 → 关阀 → 回落 → 人工复位。"""

    def test_两级处置全流程(self, clock, wall, seed_threshold_rows, scenario_rules):
        active, _ = load_rows(seed_threshold_rows, scenario_rules=scenario_rules)
        engine = make_engine(clock, wall, cooldown_sec=30.0)
        engine.set_rules(active)

        # 平稳期：0.06 全程无事件
        assert feed(engine, clock, {"gas_level": 0.06}, 30) == []

        # 突升到 0.50：只过预警阈值（0.45），需持续 30 秒
        events = feed(engine, clock, {"gas_level": 0.50}, 35)
        warn = fired(events)
        assert len(warn) == 1 and warn[0].level == 1
        assert "gas_valve_close" not in warn[0].actions

        # 冲到 0.70：立即联锁级（0.55，无等待）
        events = fired(feed(engine, clock, {"gas_level": 0.70}, 2))
        assert events and events[-1].level == 2
        assert "gas_valve_close" in events[-1].actions
        assert engine.snapshot()["latched"] == ["1"]

        # 回落：释放事件
        events = feed(engine, clock, {"gas_level": 0.05}, 40)
        assert any(event.event == EVENT_RELEASED for event in events)
        assert engine.snapshot()["latched"] == ["1"]        # 仍锁着

        # 人工确认后解锁
        assert engine.ack("1", "E009").ok is True
        assert engine.snapshot()["latched"] == []

    def test_snapshot可用于演示页(self, clock, wall, seed_threshold_rows, scenario_rules):
        active, _ = load_rows(seed_threshold_rows, scenario_rules=scenario_rules)
        engine = make_engine(clock, wall, profile="business", boot_grace_sec=300.0)
        engine.set_rules(active)
        engine.boot(now=clock())
        snap = engine.snapshot()
        assert snap["profile"] == "business"
        assert snap["in_grace"] is True
        assert len(snap["rules"]) == len(active)
        assert "hood_temp@600s" in snap["windows"]
        # 营业档下，备餐/收档的规则应标记为不生效
        closed_rules = [rid for rid, item in snap["rules"].items()
                        if item["profile"] == "closed"]
        assert closed_rules and all(
            snap["rules"][rid]["profile_active"] is False for rid in closed_rules
        )
