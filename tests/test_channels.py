# -*- coding: utf-8 -*-
"""P1/T2 执行对象映射层测试：三通道防串路、互锁优先级、CH3 失效安全。

全部纯内存，不连 broker / MySQL / 真机。依据素材库 docs/kitchen-contract.md §2.1–§2.6。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kitchen.manifest import LEGACY_ACTION_NAMES, load_scenario
from kitchen.channels import (
    ACTION_BINDINGS,
    ALL_CHANNELS,
    LATCHED,
    MANUAL_RESET_REQUIRED,
    NORMAL,
    PRIORITY_GAS_LEAK,
    PRIORITY_HOOD_HEAT,
    PRIORITY_VISION_VIOLATION,
    RELAY_ACTIONS,
    RELAY_CHANNELS,
    RELEASING,
    SIGNAL_ACTIONS,
    Channel,
    InterlockBlocked,
    InterlockController,
    MisrouteError,
    UnknownActionError,
    crosscheck_manifest,
    resolve_action,
    validate_routing,
)


# ---------------------------------------------------------------- 映射唯一性
def test_six_relay_actions_bind_unique_channel():
    assert len(RELAY_ACTIONS) == 6
    assert set(RELAY_ACTIONS) == {
        "gas_valve_close", "gas_valve_open", "fan_on", "fan_off",
        "power_cut", "power_restore",
    }
    expected = {
        "gas_valve_close": Channel.CH1, "gas_valve_open": Channel.CH1,
        "fan_on": Channel.CH2, "fan_off": Channel.CH2,
        "power_cut": Channel.CH3, "power_restore": Channel.CH3,
    }
    for action, channel in expected.items():
        assert resolve_action(action).channel is channel
    # 每通道恰好 2 个动作（trip + restore）
    for channel in RELAY_CHANNELS:
        bound = [a for a in RELAY_ACTIONS if resolve_action(a).channel is channel]
        assert len(bound) == 2


def test_cross_channel_misroute_all_rejected():
    """核心防误发：6 动作 × 错误通道全部拦截，正确通道才放行。"""
    for action in RELAY_ACTIONS:
        binding = resolve_action(action)
        for channel in ALL_CHANNELS:
            if channel is binding.channel:
                assert validate_routing(action, channel=channel) is binding
            else:
                with pytest.raises(MisrouteError, match="跨通道下发已被拦截"):
                    validate_routing(action, channel=channel)
    # 点名最容易出事的一条：关燃气阀绝不允许落到 CH3 工位插座
    with pytest.raises(MisrouteError):
        validate_routing("gas_valve_close", channel=Channel.CH3)
    assert validate_routing("power_cut", channel="CH3").channel is Channel.CH3


def test_wrong_device_and_wrong_scenario_rejected():
    with pytest.raises(MisrouteError, match="归属设备"):
        validate_routing("gas_valve_close", device_id="esp32-01")
    # mingchu/lab_safety 也有 fan_on，但不得复用 kitchen 的 CH2 映射
    with pytest.raises(MisrouteError, match="只服务 kitchen"):
        validate_routing("fan_on", scenario="mingchu")
    with pytest.raises(MisrouteError, match="只服务 kitchen"):
        InterlockController().request("fan_on", scenario="lab_safety")


def test_unknown_action_and_legacy_mingchu_name_rejected():
    for action in ("valve_close", "valve_open", "power_restore_typo", ""):
        with pytest.raises(UnknownActionError):
            resolve_action(action)
    # 旧场景动作名口径由 manifest 统一维护，不依赖旧工程场景文件
    assert set(LEGACY_ACTION_NAMES) == {"valve_close", "valve_open"}
    # 旧动作名不能混进 kitchen 映射层（kitchen 有 fan_on，但不是同一执行对象）
    assert "valve_close" not in ACTION_BINDINGS
    assert "valve_open" not in ACTION_BINDINGS


def test_state_effects_match_kitchen_manifest():
    manifest = load_scenario("kitchen")
    declared = {}
    for driver in manifest.drivers:
        for action in driver.actions:
            declared[action] = dict(driver.states.get(action) or {})
    for action, binding in ACTION_BINDINGS.items():
        assert declared[action] == binding.state_effect()
    assert crosscheck_manifest(manifest) == []


def test_crosscheck_detects_tampered_manifest():
    """配置侧兜底：states 被改错 / 动作漏登记都要报出来。"""
    driver = SimpleNamespace(
        id="relay3ch",
        actions=("gas_valve_close",),
        states={"gas_valve_close": {"station_power": "cut"}},  # 故意串到 CH3
    )
    problems = crosscheck_manifest(SimpleNamespace(drivers=(driver,)))
    assert any("states 不一致" in item for item in problems)
    assert any("未在 manifest 声明" in item for item in problems)


# ---------------------------------------------------------------- 互锁
def test_high_priority_occupancy_blocks_low_priority_restore():
    """契约 §2.3 ①：高优先级占用期间，低优先级触发源恢复不得释放通道。"""
    ctrl = InterlockController()
    ctrl.request("gas_valve_close", source="gas_leak", priority=PRIORITY_GAS_LEAK)
    ctrl.request("fan_on", source="gas_leak", priority=PRIORITY_GAS_LEAK)
    ctrl.note_trigger(Channel.CH2, "hood_heat", PRIORITY_HOOD_HEAT)

    ctrl.note_recovered(Channel.CH2, "hood_heat")  # 低优先级已恢复
    assert ctrl.is_latched(Channel.CH2)
    assert ctrl.active_sources(Channel.CH2) == {"gas_leak": PRIORITY_GAS_LEAK}
    with pytest.raises(InterlockBlocked, match="gas_leak"):
        ctrl.request("fan_off", manual_confirmed=True)

    ctrl.note_recovered(Channel.CH2, "gas_leak")
    assert ctrl.channel_state(Channel.CH2) == RELEASING
    assert ctrl.request("fan_off", manual_confirmed=True).state_effect() == {"fan": "off"}


def test_restore_requires_manual_confirm_even_after_all_recovered():
    """契约 §2.3 ③：规则引擎不得自动下发恢复动作。"""
    ctrl = InterlockController()
    ctrl.request("gas_valve_close", source="gas_leak")
    ctrl.note_recovered(Channel.CH1, "gas_leak")
    with pytest.raises(InterlockBlocked, match="必须人工确认"):
        ctrl.request("gas_valve_open")
    assert ctrl.request("gas_valve_open", manual_confirmed=True).action == "gas_valve_open"
    assert ctrl.channel_state(Channel.CH1) == NORMAL


def test_ch3_fail_safe_requires_dual_review():
    """契约 §2.4：断电即断，恢复需 Web + TFT 双通道复核 + 人工确认。"""
    ctrl = InterlockController()
    ctrl.request("power_cut", source="vision_violation",
                 priority=PRIORITY_VISION_VIOLATION)
    assert ctrl.channel_state(Channel.CH3) == MANUAL_RESET_REQUIRED
    ctrl.confirm_manual_reset(Channel.CH3, "web")
    ctrl.note_recovered(Channel.CH3, "vision_violation")

    with pytest.raises(InterlockBlocked, match="缺少"):
        ctrl.request("power_restore", manual_confirmed=True)
    ctrl.confirm_manual_reset(Channel.CH3, "tft")
    assert ctrl.request("power_restore", manual_confirmed=True).state_effect() == {
        "station_power": "on"
    }
    assert ctrl.channel_state(Channel.CH3) == NORMAL
    # 每次重新断电都必须重新复核
    ctrl.request("power_cut", source="vision_violation")
    ctrl.note_recovered(Channel.CH3, "vision_violation")
    with pytest.raises(InterlockBlocked, match="双通道"):
        ctrl.request("power_restore", manual_confirmed=True)


def test_signal_gpio_never_occupies_relay_channels_and_is_not_suppressed():
    ctrl = InterlockController()
    ctrl.request("gas_valve_close", source="gas_leak")
    ctrl.set_degraded(True)
    for action in SIGNAL_ACTIONS:
        binding = ctrl.request(action)  # 优先级/降级都不抑制声光
        assert binding.channel is Channel.GPIO_SIGNAL
        assert binding.channel not in RELAY_CHANNELS
    assert ctrl.snapshot()[Channel.CH1.value]["state"] == "degraded"


def test_degraded_blocks_automatic_restore():
    ctrl = InterlockController()
    ctrl.set_degraded(True)
    with pytest.raises(InterlockBlocked, match="断网降级"):
        ctrl.request("gas_valve_open", manual_confirmed=True)
    ctrl.set_degraded(False)
    assert ctrl.request("gas_valve_open", manual_confirmed=True).action == "gas_valve_open"


def test_trip_latches_and_snapshot_shape():
    ctrl = InterlockController()
    ctrl.request("fan_on", source="hood_heat", priority=PRIORITY_HOOD_HEAT)
    assert ctrl.channel_state(Channel.CH2) == LATCHED
    snap = ctrl.snapshot()
    assert set(snap) == {channel.value for channel in RELAY_CHANNELS}
    assert snap["CH2"]["sources"] == {"hood_heat": PRIORITY_HOOD_HEAT}
    ctrl.reset_channel(Channel.CH2)
    assert ctrl.channel_state(Channel.CH2) == NORMAL
