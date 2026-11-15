# -*- coding: utf-8 -*-
"""场景契约加载器测试：13 类信号口径、驱动双向核对、加载期硬校验。"""
from __future__ import annotations

import copy
import json

import pytest

from kitchen.manifest import (
    EVENT_SIGNALS,
    EXPECTED_SIGNALS,
    LEGACY_ACTION_NAMES,
    TELEMETRY_METRICS,
    ManifestError,
    load_scenario,
    resolve_driver_class,
)


def test_13_signals_are_single_source_of_truth():
    assert len(TELEMETRY_METRICS) == 12
    assert EVENT_SIGNALS == ("rfid_card",)
    assert len(EXPECTED_SIGNALS) == 13
    assert set(LEGACY_ACTION_NAMES) == {"valve_close", "valve_open"}


def test_load_kitchen_manifest_is_fully_consistent():
    manifest = load_scenario("kitchen")
    assert manifest.scenario == "kitchen"
    assert manifest.version == "0.1.0"
    assert set(manifest.metric_ids) == set(TELEMETRY_METRICS)
    assert len(manifest.metrics) == 12
    # 7 个遥测驱动 + 2 个执行驱动
    assert len(manifest.drivers) == 9
    assert len(manifest.action_names) == 9
    assert not set(manifest.action_names) & set(LEGACY_ACTION_NAMES)
    assert manifest.devices[0].id == "kitchen-node-01"
    assert set(manifest.devices[0].drivers) == {d.id for d in manifest.drivers}


def test_load_scenario_is_cached():
    assert load_scenario("kitchen") is load_scenario("kitchen")


def test_driver_classes_are_real_and_match_declaration():
    for driver in load_scenario("kitchen").drivers:
        cls = resolve_driver_class(driver.class_path)
        assert cls.id == driver.id
        assert tuple(cls.metrics) == driver.metrics
        assert tuple(cls.actions) == driver.actions


def test_legacy_and_missing_scenarios_are_rejected_explicitly():
    for name in ("mingchu", "lab_safety"):
        with pytest.raises(ManifestError, match="不属于新工程"):
            load_scenario(name)
    with pytest.raises(ManifestError, match="不存在"):
        load_scenario("no_such_scenario")
    with pytest.raises(ManifestError, match="'module:Attr'"):
        resolve_driver_class("kitchen.drivers.sensors")
    with pytest.raises(ManifestError, match="无法导入"):
        resolve_driver_class("no_such_module:X")


def _write_case(tmp_path, index, mutate):
    """在独立目录写一份 kitchen.json 变体；目录名不同可绕开 lru_cache。"""
    source = copy.deepcopy(load_scenario("kitchen").raw)
    mutate(source)
    directory = tmp_path / f"case{index}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "kitchen.json").write_text(
        json.dumps(source, ensure_ascii=False), encoding="utf-8"
    )
    return directory


@pytest.mark.parametrize("index,mutate,expected", [
    (0, lambda d: d["metrics"].append({"id": "ghost", "label": "多出来的指标"}),
     "遥测指标必须是 12 个"),
    (1, lambda d: d["drivers"][-2]["actions"].append("valve_close"),
     "旧场景动作名"),
    (2, lambda d: d["drivers"][0].update({"id": "wrong_id"}), "wrong_id"),
    (3, lambda d: d["drivers"][-2]["states"].update({"ghost": {"x": "y"}}),
     "未声明动作"),
    (4, lambda d: d["devices"][0]["drivers"].append("ghost_driver"), "未声明驱动"),
    (5, lambda d: d["drivers"][0].update({"class": "kitchen.drivers.sensors:Nope"}),
     "不是类"),
    (6, lambda d: d["metrics"].pop(), "未声明指标"),
    (7, lambda d: d["drivers"][0]["metrics"].append("ghost_metric"), "不一致"),
])
def test_manifest_rejects_tampering_at_load_time(tmp_path, index, mutate, expected):
    directory = _write_case(tmp_path, index, mutate)
    with pytest.raises(ManifestError, match=expected):
        load_scenario("kitchen", scenarios_dir=directory)


def test_manifest_rejects_non_object_json(tmp_path):
    directory = tmp_path / "non_object"
    directory.mkdir()
    (directory / "kitchen.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ManifestError, match="JSON 对象"):
        load_scenario("kitchen", scenarios_dir=directory)
