# -*- coding: utf-8 -*-
"""P1/T1 模拟器注入测试：13 类信号、曲线 / hold / 游走、事件回放、向后兼容。

不连 broker / MySQL：`SimDevice.client` 用 _Recorder 替换，只检查待发布报文。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from kitchen.manifest import load_scenario
from kitchen.channels import MisrouteError
from kitchen.drivers.sensors import (
    AmbientDriver, ColdchainDriver, CurrentDriver, FlameDriver,
    GasDriver, ScaleDriver, ThermalDriver,
)
from kitchen.sim import (
    DEFAULT_PROFILE,
    EXPECTED_SIGNALS,
    TELEMETRY_METRICS,
    KitchenSimProfile,
    ProfileError,
    SimEvent,
)
from kitchen.device import SimDevice, SimState


@pytest.fixture()
def profile() -> KitchenSimProfile:
    instance = KitchenSimProfile.from_file(DEFAULT_PROFILE)
    assert instance.validate(load_scenario("kitchen")) == []
    return instance


class _Recorder:
    """替掉 paho client，只记录 publish 的相对主题与报文体。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload)))


def _kitchen_device(state: SimState, emit_events: bool = False) -> SimDevice:
    drivers = [cls() for cls in (
        AmbientDriver, GasDriver, ThermalDriver, ColdchainDriver,
        FlameDriver, CurrentDriver, ScaleDriver,
    )]
    device = SimDevice(
        "kitchen", "kitchen-node-01", "127.0.0.1", 1883, "", "",
        state, drivers, ("gas_valve_close",), emit_profile_events=emit_events,
    )
    device.client = _Recorder()
    return device


# ---------------------------------------------------------------- 13 类信号
def test_profile_covers_all_13_signals_and_manifest_metrics(profile):
    assert len(EXPECTED_SIGNALS) == 13
    assert set(profile.signal_ids) == set(EXPECTED_SIGNALS)
    assert set(profile.metric_ids) == set(TELEMETRY_METRICS)
    manifest = load_scenario("kitchen")
    assert {item.id for item in manifest.metrics} == set(TELEMETRY_METRICS)
    assert profile.scenario == "kitchen"
    assert len(profile.events) == 8


def test_all_telemetry_metrics_injectable_at_every_tick(profile):
    state = SimState(profile=profile)
    for t in (0.0, 5.0, 15.0, 35.0, 80.0, 200.0):
        values = profile.sample_all(t)
        assert set(values) == set(TELEMETRY_METRICS)
        assert all(isinstance(value, (int, float)) for value in values.values())
        for metric in TELEMETRY_METRICS:
            assert state.value_for(metric, t) is not None
    # 第 13 类：RC522 刷卡事件信号（非遥测，不进 sensor_readings）
    assert "rfid_card" in profile.event_signals
    assert state.value_for("rfid_card", 1.0) is None
    assert any(event.kind == "rfid" for event in profile.events)


def test_curve_heat_hold_cool(profile):
    assert profile.sample_metric("hood_temp", 0) == 32.0     # 升温起点
    assert profile.sample_metric("hood_temp", 15) == 106.0   # 升温中点
    assert profile.sample_metric("hood_temp", 30) == 180.0   # 升温终点
    assert profile.sample_metric("hood_temp", 45) == 180.0   # 保温
    # 降温段起点 50s、时长 30s：t=65 走完一半 → 180 + (40-180)*0.5 = 110
    assert profile.sample_metric("hood_temp", 65) == 110.0   # 降温中点
    assert profile.sample_metric("hood_temp", 80) == 40.0    # 降温终点
    assert profile.sample_metric("hood_temp", 500) == 40.0   # loop=false 保持末值
    assert (profile.sample_metric("hood_temp", 0)
            < profile.sample_metric("hood_temp", 20)
            < profile.sample_metric("hood_temp", 30))


def test_hold_returns_to_baseline(profile):
    assert profile.sample_metric("gas_level", 0) == 0.78
    assert profile.sample_metric("gas_level", 19.9) == 0.78
    assert profile.sample_metric("gas_level", 20.0) == 0.12
    assert profile.sample_metric("flame_a", 0) == 1.0
    assert profile.sample_metric("flame_a", 10.0) == 0.0
    assert profile.sample_metric("sample_temp", 12.0) == 5.0
    assert profile.sample_metric("t", 999) == 26.0  # constant


def test_random_walk_is_deterministic_and_bounded(profile):
    values = [profile.sample_metric("h", t) for t in range(0, 100, 2)]
    assert all(45.0 <= value <= 75.0 for value in values)
    assert any(abs(value - 58.0) > 1e-6 for value in values)  # 真的在游走
    again = [profile.sample_metric("h", t) for t in range(0, 100, 2)]
    assert values == again  # 可回放
    other = KitchenSimProfile.from_file(DEFAULT_PROFILE)
    assert [other.sample_metric("h", t) for t in range(0, 100, 2)] == values


# ---------------------------------------------------------------- 事件回放
def test_events_due_is_left_open_right_closed(profile):
    assert [e.at_s for e in profile.events_due(0.0, 10.0)] == [3.0, 8.0, 9.0]
    assert [e.at_s for e in profile.events_due(0.0, 3.0)] == [3.0]
    assert profile.events_due(3.0, 3.0) == []       # 不重复
    assert len(profile.events_due(0.0, 40.0)) == 8  # 全部可回放
    assert profile.events_due(40.0, 50.0) == []


def test_render_ack_uses_mapping_layer_and_rejects_cross_channel(profile):
    ack = next(event for event in profile.events
               if event.kind == "cmd_ack" and event.data["action"] == "gas_valve_close")
    topic, payload = profile.render_event(ack, ts=1730000000)
    assert topic == "cmd/ack"
    assert payload["cmd_id"] == "kitchen-demo-001"
    assert payload["ok"] is True
    assert payload["action"] == "gas_valve_close"
    assert payload["channel"] == "CH1"
    assert payload["ts"] == 1730000000
    assert "error" not in payload

    forged = SimEvent(
        at_s=1.0, kind="cmd_ack", topic="cmd/ack",
        data={"action": "gas_valve_close", "channel": "CH3",
              "cmd_id": "forged", "ok": True},
    )
    with pytest.raises(MisrouteError):
        profile.render_event(forged, ts=1)

    rfid = next(event for event in profile.events if event.kind == "rfid")
    topic, payload = profile.render_event(rfid, ts=1730000001)
    assert topic == "event/rfid"
    assert payload["card_uid"] == "04A1B2C3"
    assert payload["staff_id"] == "chef-01"
    assert payload["ts"] == 1730000001


def test_validate_rejects_bad_profiles():
    raw = json.loads(Path(DEFAULT_PROFILE).read_text(encoding="utf-8"))

    # ① 跨通道 ack
    bad = copy.deepcopy(raw)
    bad["events"][1]["channel"] = "CH3"
    assert any("跨通道" in item or "非法" in item
               for item in KitchenSimProfile.from_dict(bad).validate())
    # ② 恢复动作没标人工确认
    bad = copy.deepcopy(raw)
    bad["events"][5].pop("manual_confirmed")
    assert any("manual_confirmed" in item
               for item in KitchenSimProfile.from_dict(bad).validate())
    # ③ CH3 恢复缺双通道复核
    bad = copy.deepcopy(raw)
    bad["events"][7].pop("dual_review")
    assert any("dual_review" in item
               for item in KitchenSimProfile.from_dict(bad).validate())
    # ④ 缺一个遥测指标
    bad = copy.deepcopy(raw)
    bad["signals"] = [item for item in bad["signals"] if item["id"] != "weight_g"]
    assert any("缺少遥测指标" in item
               for item in KitchenSimProfile.from_dict(bad).validate())
    # ⑤ 挂到别的场景
    bad = copy.deepcopy(raw)
    bad["scenario"] = "mingchu"
    assert any("只服务 kitchen" in item or "场景不一致" in item
               for item in KitchenSimProfile.from_dict(bad).validate(load_scenario("kitchen")))
    with pytest.raises(ProfileError):
        KitchenSimProfile.from_dict(bad).assert_valid(load_scenario("kitchen"))


# ---------------------------------------------------------------- SimState 兼容
def test_simstate_without_profile_is_backward_compatible():
    """未指定 --sim-profile 时，value_for 与旧 get_override 行为逐点一致。"""
    state = SimState()
    assert state.profile is None
    assert state.value_for("t", 123.0) is None
    state.inject("t", 33.3, hold_s=60)
    assert state.value_for("t", 123.0) == state.get_override("t") == 33.3
    state.inject("t", 0.0, hold_s=-1)  # 已过期
    assert state.value_for("t", 123.0) is None


def test_manual_injection_overrides_profile(profile):
    state = SimState(profile=profile)
    assert state.value_for("t", 0.0) == 26.0
    state.inject("t", 99.0, hold_s=60)
    assert state.value_for("t", 0.0) == 99.0
    state.inject("t", 0.0, hold_s=-1)
    assert state.value_for("t", 0.0) == 26.0


def test_simdevice_tick_publishes_12_metrics_and_replays_events(profile):
    device = _kitchen_device(SimState(profile=profile), emit_events=True)
    device.tick(1.0)
    published = dict(device.client.published)
    tele_keys = set()
    for topic, payload in device.client.published:
        if topic.startswith("kitchen/kitchen-node-01/tele/"):
            tele_keys |= set(payload) - {"ts"}
    assert tele_keys == set(TELEMETRY_METRICS)
    assert published["kitchen/kitchen-node-01/tele/thermal"]["sterilize_temp"] == 112.0
    assert published["kitchen/kitchen-node-01/tele/flame"]["flame_a"] == 1

    device.tick(31.0)  # 一次性回放窗口内全部 8 条事件
    topics = [topic for topic, _ in device.client.published]
    assert topics.count("kitchen/kitchen-node-01/event/rfid") == 2
    assert topics.count("kitchen/kitchen-node-01/cmd/ack") == 6
    # 下一 tick 不应重复回放（左开右闭）
    device.tick(32.0)
    topics_after = [topic for topic, _ in device.client.published]
    assert topics_after.count("kitchen/kitchen-node-01/cmd/ack") == 6


def test_simdevice_without_profile_does_not_emit_events():
    device = _kitchen_device(SimState(), emit_events=False)
    device.tick(31.0)
    topics = [topic for topic, _ in device.client.published]
    assert all("/tele/" in topic for topic in topics)
    assert not any("cmd/ack" in topic or "event/rfid" in topic for topic in topics)


def test_cli_dry_run_is_reproducible(monkeypatch, capsys):
    """在进程内跑一遍离线自检 CLI（等价 `python -m tools.sim_cli --json --seconds 40`）。"""
    from kitchen import sim as kitchen_sim

    monkeypatch.setattr(sys, "argv",
                        ["kitchen.sim", "--json", "--seconds", "40"])
    kitchen_sim.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["signals"] == 13
    assert payload["telemetry_metrics"] == 12
    assert payload["events_total"] == 8
    assert payload["peak_hood_temp"] == 180.0
    assert len(payload["first_sample"]) == 12
