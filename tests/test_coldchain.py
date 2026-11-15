# -*- coding: utf-8 -*-
"""#5 冷链两路 + #6 留样柜测试（A2-05 / A2-06 验收）。

## 验收标准 → 用例

| 验收原文 | 用例 |
|---|---|
| 短时开门不报警 | `TestDoorPhase::test_开门三十秒全程不报警` |
| 持续超温 2 分钟报警并生成工单 | `TestTiering::test_持续超温两分钟报警并开单` |
| 演示阈值（冷藏 8℃ / 冷冻 5℃）可配置 | `TestTiering::test_演示阈值来自种子行` |
| 演示 25℃ 生效，温水 35℃ 触发、常温水 22℃ 不触发 | `TestSampleCabinet` |

## 为什么「开门过滤」和「分级」分开测

它们是两个独立的机制，坏起来的症状也完全不同：

- 过滤器坏了 → **漏报**（真超温被当成开门波动吃掉），现场毫无反应；
- 分级坏了 → **误报刷屏**（瞬时开门就升级成工单），人会把告警静音。

合成一个端到端用例测不出是哪一层坏了，所以先各自单测，再补两条端到端串起来。
"""
from __future__ import annotations

import pytest

from kitchen.rules.config_store import load_builtin
from kitchen.rules.engine import EVENT_FIRED, RuleEngine
from kitchen.rules.model import Rule
from kitchen.runtime import KitchenRuntime
from kitchen.safety import (
    DEFAULT_COLD_METRICS,
    ColdChainGuard,
    DoorOpenFilter,
)

# 演示用墙钟：2026-11-12 11:00:00 CST（营业档，虽然冷链规则按口径筛不看档位，
# 但运行时每次 tick 都会算档位，给个真实值省得将来加档位规则时踩坑）
BUSINESS_TS = 1_763_000_400.0


class FakeClock:
    def __init__(self, start: float = BUSINESS_TS) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


def make_runtime(clock: FakeClock) -> KitchenRuntime:
    return KitchenRuntime(load_builtin(), device_id="K-COLD", clock=clock, wall_clock=clock)


def drive(rt: KitchenRuntime, clock: FakeClock, plan, step: float = 1.0):
    """`plan` = [(秒数, metrics), ...]，按 step 逐帧 tick，返回所有 TickResult。"""
    results = []
    for seconds, metrics in plan:
        for _ in range(max(1, int(round(seconds / step)))):
            clock.advance(step)
            results.append(rt.tick(metrics))
    return results


def verdicts(results) -> list:
    return [verdict for result in results for verdict in result.verdicts]


def fired_ids(results) -> list[str]:
    return [verdict.rule_id for verdict in verdicts(results) if verdict.fired]


def all_tickets(results) -> tuple[str, ...]:
    return tuple(ticket for result in results for ticket in result.tickets)


# ================================================================ 开门波动过滤器
class TestDoorOpenFilter:
    """纯波形识别：不接引擎，只看「这一帧该不该参与判定」。"""

    def test_缓慢升温不被当成开门波动(self):
        """压缩机慢慢失效是最危险的情形，绝不能过滤掉。"""
        flt = DoorOpenFilter()
        moment = 0.0
        value = 4.0
        held = 0
        for _ in range(120):                       # 2 分钟，1℃/min
            moment += 1.0
            value += 1.0 / 60.0
            ok, _ = flt.observe("fridge_temp", value, moment)
            held += 0 if ok else 1
        assert held == 0
        assert flt.holding_metrics() == {}

    def test_开门陡升触发过滤(self):
        flt = DoorOpenFilter()
        moment = 0.0
        for _ in range(30):                        # 30 秒平稳 4℃
            moment += 1.0
            flt.observe("fridge_temp", 4.0, moment)
        events = []
        value = 4.0
        for _ in range(20):                        # 20 秒冲到 8℃（12℃/min）
            moment += 1.0
            value += 0.2
            ok, event = flt.observe("fridge_temp", value, moment)
            if event:
                events.append(event)
        assert [item.kind for item in events] == ["hold_enter"]
        assert events[0].baseline == pytest.approx(4.0)
        assert flt.holding_metrics().get("fridge_temp")

    def test_回落到位解除过滤(self):
        flt = DoorOpenFilter()
        moment, value = 0.0, 4.0
        for _ in range(30):
            moment += 1.0
            flt.observe("fridge_temp", 4.0, moment)
        for _ in range(20):                        # 升到 8℃
            moment += 1.0
            value += 0.2
            flt.observe("fridge_temp", value, moment)
        events = []
        for _ in range(30):                        # 门关上，回落到 4℃
            moment += 1.0
            value -= 0.15
            ok, event = flt.observe("fridge_temp", value, moment)
            if event:
                events.append(event)
        assert [item.kind for item in events] == ["hold_release"]
        assert flt.holding_metrics() == {}

    def test_升幅过大时放弃过滤放行(self):
        """门开了忘关、或者干脆是故障：宁可报警，也不能一直吃样本。"""
        flt = DoorOpenFilter()
        moment, value = 0.0, 4.0
        for _ in range(30):
            moment += 1.0
            flt.observe("fridge_temp", 4.0, moment)
        events = []
        for _ in range(60):                        # 一路冲到 18℃（Δ14 > escape 8）
            moment += 1.0
            value += 0.25
            ok, event = flt.observe("fridge_temp", value, moment)
            if event:
                events.append(event)
        assert [item.kind for item in events] == ["hold_enter", "hold_escape"]
        assert events[-1].passed is True

    def test_过滤超时后放行(self):
        """门一直开着、温度卡在 10℃ 不回落：不能无限过滤。"""
        flt = DoorOpenFilter(hold_sec=60.0)
        moment, value = 0.0, 4.0
        for _ in range(30):
            moment += 1.0
            flt.observe("fridge_temp", 4.0, moment)
        for _ in range(20):
            moment += 1.0
            value += 0.2
            flt.observe("fridge_temp", value, moment)
        events = []
        for _ in range(70):                        # 卡在 8℃ 不动
            moment += 1.0
            ok, event = flt.observe("fridge_temp", 8.0, moment)
            if event:
                events.append(event)
        assert [item.kind for item in events] == ["hold_timeout"]

    def test_参数自洽性校验(self):
        with pytest.raises(ValueError):
            DoorOpenFilter(jump_c=5.0, escape_c=5.0)      # escape 必须 > jump
        with pytest.raises(ValueError):
            DoorOpenFilter(lookback_sec=10.0, rise_window_sec=20.0)


# ================================================================ Guard 前置层
class TestColdChainGuard:
    def test_只拦冷链指标_其他指标原样透传(self):
        guard = ColdChainGuard()
        frame = guard.observe({"hood_temp": 260.0, "gas_level": 0.1, "flame_a": 1.0}, now=0.0)
        assert frame.passed == {"hood_temp": 260.0, "gas_level": 0.1, "flame_a": 1.0}
        assert frame.held == {}
        assert frame.filtered is False

    def test_被扣指标不进入判定流(self):
        """过滤期间该指标必须从 metrics 里消失，否则引擎照旧计时。"""
        guard = ColdChainGuard()
        moment = 0.0
        for _ in range(30):
            moment += 1.0
            guard.observe({"fridge_temp": 4.0}, now=moment)
        value = 4.0
        held_frames = 0
        for _ in range(20):
            moment += 1.0
            value += 0.2
            frame = guard.observe({"fridge_temp": value, "sample_temp": 6.0}, now=moment)
            if "fridge_temp" in frame.held:
                held_frames += 1
                assert "fridge_temp" not in frame.passed
                assert frame.passed["sample_temp"] == 6.0
        assert held_frames > 0

    def test_过滤事件必须可上送(self):
        guard = ColdChainGuard()
        moment = 0.0
        for _ in range(30):
            moment += 1.0
            guard.observe({"fridge_temp": 4.0}, now=moment)
        events: list[FilterEvent] = []
        value = 4.0
        for _ in range(20):
            moment += 1.0
            value += 0.2
            frame = guard.observe({"fridge_temp": value}, now=moment)
            events.extend(frame.events)
        assert events and all(event.metric == "fridge_temp" for event in events)
        payload = events[0].to_payload("K-COLD")
        assert payload["kind"] == "cold_chain_filter"
        assert payload["device_id"] == "K-COLD"
        assert payload["reason"]

    def test_覆盖范围就是三路(self):
        assert DEFAULT_COLD_METRICS == ("fridge_temp", "freezer_temp", "sample_temp")


# ================================================================ 分级超限
class TestTiering:
    """分级靠两条阈值行叠加：range=预警（3 秒去抖），duration=报警（120 秒）。"""

    def test_瞬时越界只报预警_不开单不闩锁(self):
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [(10, {"fridge_temp": 12.0})])
        ids = fired_ids(results)
        assert "14" in ids          # demo 口径的预警级
        assert "23" not in ids      # 报警级还差得远
        assert all_tickets(results) == ()
        assert rt.engine.state_store.peek("14").latched is False

    def test_持续超温两分钟报警并开单(self):
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [(10, {"fridge_temp": 12.0}), (130, {"fridge_temp": 12.0})])
        ids = fired_ids(results)
        assert "14" in ids and "23" in ids
        assert all_tickets(results) == ("cold_chain",)
        assert rt.engine.state_store.peek("23").latched is True

    def test_中途回落会重新计时(self):
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [
            (100, {"fridge_temp": 12.0}),
            (30, {"fridge_temp": 4.0}),
            (60, {"fridge_temp": 12.0}),      # 只持续 60 秒，不够 120
        ])
        assert "23" not in fired_ids(results)
        assert all_tickets(results) == ()

    def test_冷冻两路同样分级(self):
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [(10, {"freezer_temp": 8.0}), (130, {"freezer_temp": 8.0})])
        ids = fired_ids(results)
        assert "17" in ids and "24" in ids
        assert all_tickets(results) == ("cold_chain",)

    def test_低于下限也算越界(self):
        """冷藏 0~8℃：低于 0℃ 会冻伤原料，同样越界（op=out 双边语义）。"""
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [(10, {"fridge_temp": -2.0})])
        assert "14" in fired_ids(results)

    def test_演示阈值来自种子行(self):
        """验收要求「演示阈值可配置」：值本身在 seed 里，代码不硬编码。"""
        rows = {row["id"]: row for row in load_builtin().rows_for()}
        assert rows[14]["upper_bound"] == 8.0     # 冷藏演示上限
        assert rows[23]["duration_sec"] == 120
        assert rows[17]["upper_bound"] == 5.0     # 冷冻演示上限
        assert rows[24]["duration_sec"] == 120

    def test_压制低等级时动作不能一起吞掉(self):
        """预警级配了语音、报警级只配了声光时，语音必须被合并上来。

        #5 的两级恰好都有 `voice_play`，所以真实配置看不出这个缺陷。这里用合成
        规则把它逼出来：一旦引擎把低等级的 `actions` 连同判决一起丢掉，现场就会
        出现「页面上有报警、喇叭一声不响」。
        """
        clock = FakeClock()

        def build(id_, level, actions, duration):
            return Rule(id=id_, metric="fridge_temp", op=">", threshold=8.0,
                        kind="duration", duration_sec=duration, level=level,
                        actions=actions, latch=False)

        engine = RuleEngine(device_id="K-TEST", clock=clock, wall_clock=clock)
        # 两条同时成立（都是 60 秒），才会在同一帧里发生压制——这是 #5 的真实
        # 情形：预警级的 duration 比报警级短，报警级成立那一刻预警级同时在越界。
        engine.set_rules([
            build("warn", 1, ("voice_play",), 60),
            build("alarm", 2, ("alarm_light_on",), 60),
        ])
        events = []
        for _ in range(70):
            clock.advance(1.0)
            events.extend(engine.evaluate({"fridge_temp": 12.0}, now=clock()))
        top = [item for item in events if item.rule_id == "alarm" and item.event == EVENT_FIRED]
        assert len(top) == 1
        assert set(top[0].actions) == {"alarm_light_on", "voice_play"}
        assert all(item.rule_id != "warn" for item in events if item.event == EVENT_FIRED)


# ================================================================ #6 留样柜
class TestSampleCabinet:
    def test_温水三十五度触发报警(self):
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [(10, {"sample_temp": 35.0}), (130, {"sample_temp": 35.0})])
        ids = fired_ids(results)
        assert "20" in ids and "25" in ids
        actions = tuple(action for result in results for action in result.actions)
        assert "voice_play" in actions
        assert "alarm_light_on" in actions

    def test_常温水二十二度不触发(self):
        """演示阈值 25℃：22℃ 的常温水放进去不该响。"""
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [(300, {"sample_temp": 22.0})])
        assert fired_ids(results) == []

    def test_留样柜不开工单(self):
        """#6 只要「当场听见」，48h 销毁与工单在 A4。"""
        clock = FakeClock()
        rt = make_runtime(clock)
        results = drive(rt, clock, [(300, {"sample_temp": 40.0})])
        assert all_tickets(results) == ()


# ================================================================ 端到端：门开一次
class TestDoorPhase:
    """把过滤器接到运行时上，验证「短时开门不报警」这条验收。"""

    def test_开门三十秒全程不报警(self):
        """4℃ → 2 分钟内冲到 10℃ → 回落 4℃：全程不得产生任何判定事件。

        这正是验收里那句「短时开门不报警」。用真实波形跑，不 mock 过滤器。
        """
        clock = FakeClock()
        rt = make_runtime(clock)
        guard = ColdChainGuard(clock=clock)

        def frame_at(value: float):
            clock.advance(1.0)
            frame = guard.observe({"fridge_temp": value}, now=clock())
            return frame, rt.tick(frame.passed)

        for _ in range(30):                       # 平稳 4℃
            frame, _ = frame_at(4.0)
            assert not frame.held
        for step in range(1, 31):                 # 开门：30 秒升到 10℃
            frame, _ = frame_at(4.0 + 6.0 * step / 30.0)
        assert frame.held, "开门陡升应当进入过滤"
        for step in range(1, 31):                 # 关门：30 秒回落到 4℃
            frame, result = frame_at(10.0 - 6.0 * step / 30.0)
            assert not result.verdicts, f"回落第 {step} 秒冒出了判定事件"
        for _ in range(60):                       # 再稳 1 分钟，确认没有迟到的事件
            frame, result = frame_at(4.0)
            assert not result.verdicts
        assert rt.engine.state_store.latched_rules() == []

    def test_门一直开着最终仍会报警(self):
        """过滤不是无限的：升幅超过 escape 就放行，「持续 120 秒」照样接管。

        这条是过滤器最容易做错的地方——只要「开门波动」这四个字被理解成
        「凡陡升都放过」，冷链就彻底失效了。
        """
        clock = FakeClock()
        rt = make_runtime(clock)
        guard = ColdChainGuard(clock=clock)

        for _ in range(30):                       # 平稳 4℃
            clock.advance(1.0)
            frame = guard.observe({"fridge_temp": 4.0}, now=clock())
            rt.tick(frame.passed)

        escaped = False
        for step in range(1, 61):                 # 一路升到 16℃（Δ12 > escape 8）
            clock.advance(1.0)
            frame = guard.observe({"fridge_temp": 4.0 + 12.0 * step / 60.0}, now=clock())
            escaped = escaped or any(event.kind == "hold_escape" for event in frame.events)
            rt.tick(frame.passed)
        assert escaped, "升幅过大却仍在过滤"

        results = []
        for _ in range(130):                      # 稳在 16℃，等 120 秒确认
            clock.advance(1.0)
            frame = guard.observe({"fridge_temp": 16.0}, now=clock())
            results.append(rt.tick(frame.passed))
        assert "23" in fired_ids(results)
        assert all_tickets(results) == ("cold_chain",)
