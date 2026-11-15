# -*- coding: utf-8 -*-
"""互锁编排测试（A2-08 验收）。

## 验收标准 → 用例

| 验收原文 | 用例 |
|---|---|
| 「三源共用排风」 | `TestSharedFan::test_三路同时触发只开一次风机` |
| 「优先级压制」 | `TestPriority::test_高优先级闩锁期间低优先级不能释放通道` |
| 「全部恢复才关」 | `TestFullRecovery::test_三源全恢复才允许停风机` |

## 为什么这些用例必须驱动真运行时

互锁的坑全在「谁在什么时刻登记了哪个源」这种时序上。用假 `Verdict` 手搓出来
的测试只能证明 `controller.request` 本身没写错——那部分旧工程已经有测试了。
这里的价值在于验证 **规则引擎 → 编排层 → 三通道** 这条链端到端接得起来：
`env_thresholds` 里把 `fan_on` 写成 `fanOn`、把 `source` 记成 `anonymous`、
`released` 时忘了注销源，任何一处出错都只有端到端能抓到。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kitchen.channels import (
    PRIORITY_GAS_LEAK,
    PRIORITY_HOOD_HEAT,
    Channel,
    MisrouteError,
)
from kitchen.rules.config_store import load_builtin
from kitchen.runtime import KitchenRuntime
from kitchen.safety import InterlockSupervisor, priority_of

CST = timezone(timedelta(hours=8))
ROOT_RULES = Path(__file__).resolve().parents[1] / "fixtures" / "kitchen_rules.json"
#: 15:00 属于备餐档：这时 #2 排烟的动作是「开排风 + 关燃气阀」，不是营业档的只播报
PREP_TS = datetime(2026, 11, 12, 15, 0, 0, tzinfo=CST).timestamp()

# 三路触发源的规则 id（env_thresholds.id）
RID_GAS = "1"
RID_HOOD = "5"        # 备餐档的 tier 250℃
RID_AMBIENT = "21"    # #7 温湿度（level=0，只联动排风）


class FakeClock:
    def __init__(self, start: float = PREP_TS) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


def make_pair():
    clock = FakeClock()
    rt = KitchenRuntime(load_builtin(), device_id="K-IL", clock=clock, wall_clock=clock)
    sup = InterlockSupervisor()
    return rt, sup, clock


def step(rt, sup, clock, metrics: dict, seconds: float = 5.0) -> dict:
    """喂一段数据，返回这段时间内**累积**的动作与最后的通道视图。"""
    applied: list[str] = []
    frames = []
    for _ in range(int(round(seconds))):
        clock.advance(1.0)
        frame = sup.apply(rt.tick(metrics))
        applied.extend(frame.applied)
        frames.append(frame)
    last = frames[-1] if frames else None
    return {
        "applied": applied,
        "channels": last.channels if last else {},
        "frame": last,
        "frames": frames,
    }


# ================================================================ 三源共用排风
class TestSharedFan:
    def test_三路同时触发只开一次风机(self):
        """燃气、排烟、温湿度都要开 CH2；同一通道收到三次请求，只应下发一次。"""
        rt, sup, clock = make_pair()
        metrics = {"gas_level": 0.60, "hood_temp": 260.0, "t": 38.0}
        out = step(rt, sup, clock, metrics, seconds=8)

        assert out["applied"].count("fan_on") == 1, f"风机被重复下发：{out['applied']}"
        ch2 = out["channels"]["CH2"]
        assert set(ch2["sources"]) == {RID_GAS, RID_HOOD, RID_AMBIENT}
        assert ch2["state"] == "latched"
        # CH1 只有燃气和排烟关阀，温湿度不碰燃气
        assert set(out["channels"]["CH1"]["sources"]) == {RID_GAS, RID_HOOD}

    def test_闩锁重报不重复下发(self):
        """引擎每 60 秒重报一次 latched，命令不能跟着重发。"""
        rt, sup, clock = make_pair()
        metrics = {"gas_level": 0.60}
        out = step(rt, sup, clock, metrics, seconds=150)
        latched = [v for f in out["frames"] for v in f.decisions if v.duplicate]
        assert latched, "冷却期重报没有被识别为重复"
        assert out["applied"].count("gas_valve_close") == 1

    def test_声光不占通道也不受抑制(self):
        rt, sup, clock = make_pair()
        out = step(rt, sup, clock, {"gas_level": 0.60}, seconds=5)
        assert "alarm_light_on" in out["applied"]
        assert "GPIO_SIGNAL" not in out["channels"]
        assert out["channels"]["CH1"]["sources"] == {RID_GAS: PRIORITY_GAS_LEAK}


# ================================================================ 全部恢复才关
class TestFullRecovery:
    def test_三源全恢复才允许停风机(self):
        rt, sup, clock = make_pair()
        # 1) 三路触发
        step(rt, sup, clock, {"gas_level": 0.60, "hood_temp": 260.0, "t": 38.0}, seconds=8)

        # 2) 燃气回落：CH1 仍被排烟占着，CH2 仍被排烟 + 温湿度占着 → 恢复被拒
        step(rt, sup, clock, {"gas_level": 0.10, "hood_temp": 260.0, "t": 38.0}, seconds=5)
        denied = sup.restore("CH1", reviewer="web:组长")
        assert denied.ok is False
        assert RID_HOOD in denied.detail

        # 3) 排烟也回落：CH1 空了，可以恢复燃气；CH2 还被温湿度占着
        step(rt, sup, clock, {"gas_level": 0.10, "hood_temp": 100.0, "t": 38.0}, seconds=5)
        assert sup.restore("CH1", reviewer="web:组长").ok is True
        denied = sup.restore("CH2", reviewer="web:组长")
        assert denied.ok is False and RID_AMBIENT in denied.detail

        # 4) 温度回落：CH2 全空，才允许停风机
        step(rt, sup, clock, {"gas_level": 0.10, "hood_temp": 100.0, "t": 25.0}, seconds=5)
        assert sup.restore("CH2", reviewer="web:组长").ok is True
        assert sup.controller.channel_state("CH2") == "normal"

    def test_恢复动作必须留复核人(self):
        rt, sup, clock = make_pair()
        step(rt, sup, clock, {"gas_level": 0.60}, seconds=5)
        step(rt, sup, clock, {"gas_level": 0.05}, seconds=5)
        denied = sup.restore("CH1", reviewer="")
        assert denied.ok is False and "复核人" in denied.detail

    def test_未人工确认不得恢复(self):
        rt, sup, clock = make_pair()
        step(rt, sup, clock, {"gas_level": 0.60}, seconds=5)
        step(rt, sup, clock, {"gas_level": 0.05}, seconds=5)
        denied = sup.restore("CH1", reviewer="web:组长", manual_confirmed=False)
        assert denied.ok is False and "人工确认" in denied.detail

    def test_恢复后清空历史触发源(self):
        rt, sup, clock = make_pair()
        step(rt, sup, clock, {"gas_level": 0.60}, seconds=5)
        step(rt, sup, clock, {"gas_level": 0.05}, seconds=5)
        assert sup.snapshot()["pending_restore"]["CH1"] == [RID_GAS]
        assert sup.restore("CH1", reviewer="web:组长").ok is True
        # CH1 已复位；CH2 因为燃气那条规则也带了 fan_on，同样变成「可恢复」
        assert "CH1" not in sup.snapshot()["pending_restore"]


# ================================================================ 优先级压制
class TestPriority:
    def test_优先级按指标登记(self):
        assert priority_of("gas_level") == PRIORITY_GAS_LEAK
        assert priority_of("hood_temp") == PRIORITY_HOOD_HEAT
        assert priority_of("t") > PRIORITY_HOOD_HEAT      # 温湿度排最后
        assert priority_of("unknown_metric") == 99

    def test_高优先级闩锁期间低优先级不能释放通道(self):
        """燃气（P0）还占着 CH2 时，排烟（P2）自己好了也不许停风机。"""
        rt, sup, clock = make_pair()
        step(rt, sup, clock, {"gas_level": 0.60, "hood_temp": 260.0}, seconds=8)

        # 排烟回落，燃气仍在漏
        step(rt, sup, clock, {"gas_level": 0.60, "hood_temp": 100.0}, seconds=5)
        ch2 = sup.controller.active_sources("CH2")
        assert ch2 == {RID_GAS: PRIORITY_GAS_LEAK}, ch2   # 燃气仍是唯一源，且优先级没被降级
        denied = sup.restore("CH2", reviewer="web:组长")
        assert denied.ok is False and RID_GAS in denied.detail

    def test_低优先级触发不改变已有高优先级登记(self):
        """先燃气、后温湿度：CH2 的优先级必须还是 P0，不能被后到的 P5 覆盖。"""
        rt, sup, clock = make_pair()
        step(rt, sup, clock, {"gas_level": 0.60}, seconds=5)
        step(rt, sup, clock, {"gas_level": 0.60, "t": 38.0}, seconds=5)
        sources = sup.controller.active_sources("CH2")
        assert sources[RID_GAS] == PRIORITY_GAS_LEAK
        assert priority_of("t") != PRIORITY_HOOD_HEAT

    def test_断网降级期间禁止恢复(self):
        rt, sup, clock = make_pair()
        step(rt, sup, clock, {"gas_level": 0.60}, seconds=5)
        step(rt, sup, clock, {"gas_level": 0.05}, seconds=5)
        sup.controller.set_degraded(True)
        denied = sup.restore("CH1", reviewer="web:组长")
        assert denied.ok is False and "降级" in denied.detail


# ================================================================ 配置侧护栏
class TestWiring:
    def test_备餐档排烟动作确实是开风机(self):
        """#2 在营业档只播报，若备餐档也「只播报」，三源共用排风根本演不出来。"""
        from kitchen.rules.loader import load_scenario_rules

        extra = load_scenario_rules(ROOT_RULES)["hood_temp"]
        assert "fan_on" in extra["by_profile"]["prep"]["actions"]
        assert extra["by_profile"]["business"]["actions"] == ["voice_play"]

    def test_映射层拒绝跨场景动作(self):
        from kitchen.channels import UnknownActionError, validate_routing

        with pytest.raises(MisrouteError):
            validate_routing("fan_on", scenario="mingchu")
        # mingchu 的 `valve_close` 与 kitchen 的 `gas_valve_close` 不是同一个执行对象
        with pytest.raises(UnknownActionError):
            validate_routing("valve_close")

    def test_CH3_未接不产生触发源(self):
        """A2-08 只接 CH1/CH2；#3 的 power_cut 要等 A5 的真实视觉。"""
        rt, sup, clock = make_pair()
        step(rt, sup, clock, {"gas_level": 0.60, "hood_temp": 260.0, "t": 38.0}, seconds=8)
        assert sup.controller.active_sources(Channel.CH3) == {}
        assert sup.controller.channel_state("CH3") == "normal"
