# -*- coding: utf-8 -*-
"""kitchen-only 模拟器测试：状态注入优先级、遥测发布、事件左开右闭回放。"""
from __future__ import annotations

import json

import pytest

from kitchen.device import SimDevice, SimState
from kitchen.sim import DEFAULT_PROFILE, TELEMETRY_METRICS, KitchenSimProfile


@pytest.fixture()
def profile() -> KitchenSimProfile:
    instance = KitchenSimProfile.from_file(DEFAULT_PROFILE)
    instance.assert_valid()
    return instance


class _Recorder:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload)))


def test_state_without_profile_is_backward_compatible():
    state = SimState()
    assert state.profile is None
    assert state.value_for("t", 1.0) is None
    state.inject("t", 33.3, hold_s=60)
    assert state.value_for("t", 1.0) == state.get_override("t") == 33.3
    assert state.active() == ["t"]
    state.inject("t", 0.0, hold_s=-1)
    assert state.value_for("t", 1.0) is None


def test_manual_injection_overrides_profile(profile):
    state = SimState(profile=profile)
    assert state.value_for("t", 0.0) == 26.0
    state.inject("t", 99.0, hold_s=60)
    assert state.value_for("t", 0.0) == 99.0
    state.inject("t", 0.0, hold_s=-1)
    assert state.value_for("t", 0.0) == 26.0


def test_device_rejects_non_kitchen_scenario():
    with pytest.raises(ValueError, match="只服务 kitchen"):
        SimDevice("mingchu", "node-01")


def test_default_drivers_publish_all_12_metrics(profile):
    device = SimDevice("kitchen", "kitchen-node-01", state=SimState(profile=profile))
    assert len(device.drivers) == 7
    device.tick(1.0)
    keys: set[str] = set()
    for topic, payload in device.published:
        assert topic.startswith("kitchen/kitchen-node-01/tele/")
        keys |= set(payload) - {"ts"}
    assert keys == set(TELEMETRY_METRICS)
    assert device.published[0][1]["ts"] > 0
    assert device.snapshot()["published"] == len(device.published)


def test_injected_client_receives_json_payloads(profile):
    device = SimDevice("kitchen", "kitchen-node-01", state=SimState(profile=profile),
                       emit_profile_events=True)
    recorder = _Recorder()
    device.client = recorder
    device.tick(1.0)
    assert recorder.published
    assert all(isinstance(item, dict) for _, item in recorder.published)


def test_events_replayed_once_with_left_open_right_closed(profile):
    device = SimDevice("kitchen", "kitchen-node-01", state=SimState(profile=profile),
                       emit_profile_events=True)
    device.tick(1.0)
    assert not any("event/rfid" in topic or "cmd/ack" in topic
                   for topic, _ in device.published)
    device.tick(31.0)
    topics = [topic for topic, _ in device.published]
    assert topics.count("kitchen/kitchen-node-01/event/rfid") == 2
    assert topics.count("kitchen/kitchen-node-01/cmd/ack") == 6
    device.tick(32.0)
    assert [t for t, _ in device.published].count("kitchen/kitchen-node-01/cmd/ack") == 6


def test_without_profile_no_events_are_emitted():
    device = SimDevice("kitchen", "kitchen-node-01", emit_profile_events=True)
    device.tick(31.0)
    assert all("/tele/" in topic for topic, _ in device.published)
    assert not any("cmd/ack" in topic or "event/rfid" in topic
                   for topic, _ in device.published)
