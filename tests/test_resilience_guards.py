# -*- coding: utf-8 -*-
"""三条保守保护（A6-03）。"""
from __future__ import annotations

import pytest
from resilience_support import metrics

from kitchen.resilience.guards import (
    GUARD_FLAME_UNATTENDED,
    GUARD_GAS_SURGE,
    GUARD_THERMAL_FAN,
    ConservativeGuards,
    GuardPolicy,
)
from kitchen.resilience.offline import _Verdicts
from kitchen.rules.engine import EVENT_FIRED, EVENT_RELEASED
from kitchen.safety.interlock import InterlockSupervisor


def fires(verdicts, guard: str) -> list:
    return [v for v in verdicts if v.rule_id == guard and v.event == EVENT_FIRED]


def releases(verdicts, guard: str) -> list:
    return [v for v in verdicts if v.rule_id == guard and v.event == EVENT_RELEASED]


class TestGasSurge:
    def test_单个采样点尖峰不关阀(self):
        """MQ-2 的尖峰（油烟、继电器吸合）不能变成一次停业。"""
        guards = ConservativeGuards()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        assert fires(guards.evaluate(metrics(gas_level=0.9), now=1.0), GUARD_GAS_SURGE) == []
        assert fires(guards.evaluate(metrics(gas_level=0.05), now=2.0), GUARD_GAS_SURGE) == []

    def test_连续两个采样点立即关阀(self):
        guards = ConservativeGuards()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        guards.evaluate(metrics(gas_level=0.9), now=1.0)
        verdicts = fires(guards.evaluate(metrics(gas_level=0.92), now=2.0), GUARD_GAS_SURGE)
        assert len(verdicts) == 1
        assert verdicts[0].actions == ("gas_valve_close",)
        assert "突增" in verdicts[0].message

    def test_绝对门限触发(self):
        guards = ConservativeGuards(GuardPolicy(gas_surge_abs=0.55))
        guards.evaluate(metrics(gas_level=0.56), now=0.0)
        verdicts = fires(guards.evaluate(metrics(gas_level=0.58), now=1.0), GUARD_GAS_SURGE)
        assert verdicts and "泄漏门限" in verdicts[0].message

    def test_基线过低不走比值法(self):
        """0.01 → 0.03 涨了 3 倍，但绝对量只是噪声。"""
        guards = ConservativeGuards()
        for i in range(6):
            guards.evaluate(metrics(gas_level=0.01), now=float(i))
        assert fires(guards.evaluate(metrics(gas_level=0.03), now=7.0), GUARD_GAS_SURGE) == []
        assert fires(guards.evaluate(metrics(gas_level=0.035), now=8.0), GUARD_GAS_SURGE) == []

    def test_基线高时按倍数判(self):
        guards = ConservativeGuards(GuardPolicy(gas_surge_abs=0.95))
        for i in range(5):
            guards.evaluate(metrics(gas_level=0.20), now=float(i))
        guards.evaluate(metrics(gas_level=0.45), now=6.0)
        verdicts = fires(guards.evaluate(metrics(gas_level=0.46), now=7.0), GUARD_GAS_SURGE)
        assert verdicts, "0.46 / 0.20 = 2.3 倍，应触发比值门限"

    def test_触发后不重复触发(self):
        guards = ConservativeGuards()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        guards.evaluate(metrics(gas_level=0.9), now=1.0)
        assert fires(guards.evaluate(metrics(gas_level=0.92), now=2.0), GUARD_GAS_SURGE)
        for t in (3.0, 4.0, 5.0):
            assert fires(guards.evaluate(metrics(gas_level=0.93), now=t),
                         GUARD_GAS_SURGE) == []

    def test_回落到门限八成以下才解除(self):
        guards = ConservativeGuards()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        guards.evaluate(metrics(gas_level=0.9), now=1.0)
        guards.evaluate(metrics(gas_level=0.9), now=2.0)
        assert releases(guards.evaluate(metrics(gas_level=0.5), now=3.0), GUARD_GAS_SURGE) == []
        assert releases(guards.evaluate(metrics(gas_level=0.1), now=4.0), GUARD_GAS_SURGE)

    def test_解除后可以再次触发(self):
        guards = ConservativeGuards()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        guards.evaluate(metrics(gas_level=0.9), now=1.0)
        guards.evaluate(metrics(gas_level=0.9), now=2.0)
        guards.evaluate(metrics(gas_level=0.05), now=3.0)
        guards.evaluate(metrics(gas_level=0.9), now=10.0)
        assert fires(guards.evaluate(metrics(gas_level=0.9), now=11.0), GUARD_GAS_SURGE)

    def test_缺读数不判定(self):
        guards = ConservativeGuards()
        assert guards.evaluate({"t": 20.0}, now=0.0) == ()


class TestThermalFan:
    def test_罩温超限开风机(self):
        guards = ConservativeGuards()
        verdicts = fires(guards.evaluate(metrics(hood_temp=285.0), now=0.0), GUARD_THERMAL_FAN)
        assert verdicts and verdicts[0].actions == ("fan_on",)

    def test_消毒柜超限开风机(self):
        guards = ConservativeGuards()
        verdicts = fires(guards.evaluate(metrics(sterilize_temp=135.0), now=0.0),
                         GUARD_THERMAL_FAN)
        assert verdicts and verdicts[0].metric == "sterilize_temp"

    def test_只开风机不碰燃气阀(self):
        guards = ConservativeGuards()
        verdicts = guards.evaluate(metrics(hood_temp=300.0), now=0.0)
        assert all("gas_valve_close" not in v.actions for v in verdicts)

    def test_滞回避免继电器反复吸合(self):
        guards = ConservativeGuards()
        guards.evaluate(metrics(hood_temp=285.0), now=0.0)
        # 阈值 280、滞回 5：274 还在「未解除」区间
        assert releases(guards.evaluate(metrics(hood_temp=277.0), now=1.0),
                        GUARD_THERMAL_FAN) == []
        assert releases(guards.evaluate(metrics(hood_temp=274.0), now=2.0),
                        GUARD_THERMAL_FAN)

    def test_两路超温各报一条(self):
        guards = ConservativeGuards()
        verdicts = guards.evaluate(metrics(hood_temp=290.0, sterilize_temp=140.0), now=0.0)
        hits = fires(verdicts, GUARD_THERMAL_FAN)
        assert {v.metric for v in hits} == {"hood_temp", "sterilize_temp"}


class TestFlameUnattended:
    def test_满一小时无人工活动才触发(self):
        guards = ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0))
        guards.evaluate(metrics(flame_b=1.0), now=0.0)
        assert fires(guards.evaluate(metrics(flame_b=1.0), now=3599.0),
                     GUARD_FLAME_UNATTENDED) == []
        verdicts = fires(guards.evaluate(metrics(flame_b=1.0), now=3601.0),
                         GUARD_FLAME_UNATTENDED)
        assert verdicts
        assert verdicts[0].actions == ("gas_valve_close",)
        assert verdicts[0].ticket == "unattended"
        assert "无人看管" in verdicts[0].message

    def test_演示加速因子把一小时压成六十秒(self):
        guards = ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0, time_scale=60.0))
        assert guards.policy.hold_effective_sec == 60.0
        guards.evaluate(metrics(flame_b=1.0), now=0.0)
        assert fires(guards.evaluate(metrics(flame_b=1.0), now=59.0),
                     GUARD_FLAME_UNATTENDED) == []
        assert fires(guards.evaluate(metrics(flame_b=1.0), now=61.0), GUARD_FLAME_UNATTENDED)

    def test_中途刷卡重新计时(self):
        guards = ConservativeGuards(GuardPolicy(flame_hold_sec=600.0, time_scale=60.0))
        guards.evaluate(metrics(flame_b=1.0), now=0.0)
        guards.mark_human("card", now=30.0)
        # 从刷卡时刻起算 5 秒 → 还不到门限（600s / 60 = 10s）
        assert fires(guards.evaluate(metrics(flame_b=1.0), now=35.0),
                     GUARD_FLAME_UNATTENDED) == []
        assert fires(guards.evaluate(metrics(flame_b=1.0), now=95.0),
                     GUARD_FLAME_UNATTENDED)

    def test_火焰熄灭解除(self):
        guards = ConservativeGuards(GuardPolicy(flame_hold_sec=60.0, time_scale=60.0))
        guards.evaluate(metrics(flame_b=1.0), now=0.0)
        guards.evaluate(metrics(flame_b=1.0), now=2.0)
        assert releases(guards.evaluate(metrics(flame_b=0.0), now=3.0), GUARD_FLAME_UNATTENDED)

    def test_上电时刻算起点不会一开机就报(self):
        guards = ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0))
        assert fires(guards.evaluate(metrics(flame_b=1.0), now=1_700_000_000.0),
                     GUARD_FLAME_UNATTENDED) == []

    def test_短时用火不触发(self):
        guards = ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0))
        guards.evaluate(metrics(flame_b=1.0), now=0.0)
        guards.evaluate(metrics(flame_b=1.0), now=600.0)
        guards.evaluate(metrics(flame_b=0.0), now=700.0)
        assert fires(guards.evaluate(metrics(flame_b=1.0), now=800.0),
                     GUARD_FLAME_UNATTENDED) == []


class TestInterlockIntegration:
    def test_触发后登记触发源(self):
        guards = ConservativeGuards()
        supervisor = InterlockSupervisor()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        verdicts = list(fires(guards.evaluate(metrics(gas_level=0.9), now=1.0), GUARD_GAS_SURGE))
        verdicts += list(fires(guards.evaluate(metrics(gas_level=0.9), now=2.0),
                               GUARD_GAS_SURGE))
        frame = supervisor.apply(_Verdicts(verdicts=tuple(verdicts)))
        assert frame.applied == ("gas_valve_close",)
        assert supervisor.controller.channel_state("CH1") == "latched"

    def test_未释放前人工恢复被拦(self):
        guards = ConservativeGuards()
        supervisor = InterlockSupervisor()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        guards.evaluate(metrics(gas_level=0.9), now=1.0)
        supervisor.apply(_Verdicts(verdicts=tuple(
            guards.evaluate(metrics(gas_level=0.9), now=2.0))))
        decision = supervisor.restore("CH1", reviewer="web:李")
        assert decision.ok is False
        assert "触发源" in decision.detail

    def test_释放后可人工恢复(self):
        """保护只在跃迁时产出事件；释放事件丢了就永远恢复不了（见模块 docstring）。"""
        guards = ConservativeGuards()
        supervisor = InterlockSupervisor()
        guards.evaluate(metrics(gas_level=0.05), now=0.0)
        guards.evaluate(metrics(gas_level=0.9), now=1.0)
        supervisor.apply(_Verdicts(verdicts=tuple(
            guards.evaluate(metrics(gas_level=0.9), now=2.0))))
        released = guards.evaluate(metrics(gas_level=0.05), now=3.0)
        supervisor.apply(_Verdicts(verdicts=tuple(released)))
        assert supervisor.restore("CH1", reviewer="web:李").ok is True


class TestPolicy:
    @pytest.mark.parametrize("scale", [0.0, -1.0])
    def test_加速因子必须为正(self, scale):
        with pytest.raises(ValueError):
            GuardPolicy(time_scale=scale)

    def test_比值门限必须大于1(self):
        with pytest.raises(ValueError):
            GuardPolicy(gas_surge_ratio=1.0)

    def test_确认样本数至少一个(self):
        with pytest.raises(ValueError):
            GuardPolicy(gas_confirm_samples=0)


class TestSnapshot:
    def test_三条保护都在快照里(self):
        guards = ConservativeGuards()
        guards.evaluate(metrics(gas_level=0.05, hood_temp=290.0, flame_b=1.0), now=0.0)
        body = guards.snapshot(now=5.0)
        assert body["policy"]["hold_effective_sec"] == 3600.0
        assert {item["guard"] for item in body["guards"]} >= {
            GUARD_GAS_SURGE, GUARD_THERMAL_FAN, GUARD_FLAME_UNATTENDED
        }
        assert body["silent_sec"] == 5.0

    def test_触发状态可查询(self):
        guards = ConservativeGuards()
        guards.evaluate(metrics(hood_temp=290.0), now=0.0)
        assert GUARD_THERMAL_FAN in guards.tripped()
        assert any(state.tripped for state in guards.states())
