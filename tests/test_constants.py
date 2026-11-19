# -*- coding: utf-8 -*-
"""隐式常量单一来源守卫（审查报告 §3 P2）。

钉死三件事：JSON 与代码读出一致、校验真能拦住错误配置、三处消费者都从
单一来源读而不是各自硬编码。
"""
from __future__ import annotations

from pathlib import Path

from kitchen.constants import constants, voice_table
from kitchen.mqtt.transport import DEFAULT_ROOT, topic_telemetry
from kitchen.tft.audio import VOICE_TABLE


ROOT = Path(__file__).resolve().parents[1]


def test_voice_table_matches_single_source():
    src = voice_table()
    assert len(VOICE_TABLE) == 12
    assert sorted(t.no for t in VOICE_TABLE.values()) == list(range(1, 13))
    for key, track in VOICE_TABLE.items():
        assert track.no == src[key]["no"]
        assert track.text == src[key]["text"]
    assert VOICE_TABLE["alarm"].text == "请注意，设备报警"


def test_demo_durations_keep_published_values():
    rows = {r["label"]: r["real_sec"] for r in constants()["demo_durations"]}
    assert rows["#3 明火离人确认"] == 12.0
    assert rows["#1 燃气确认窗口"] == 30.0
    assert rows["#15 消毒达标累计"] == 3600.0
    assert rows["#6 留样时限"] == 172800.0


def test_mqtt_topics_from_single_source():
    assert DEFAULT_ROOT == "kitchen"
    assert topic_telemetry("node-01") == "kitchen/node-01/telemetry"


def test_loader_rejects_broken_voice_numbering(tmp_path, monkeypatch):
    # 复制一份改动过的 JSON，指向临时路径，确认校验会拒绝重号
    import json
    from kitchen import constants as cmod

    data = json.loads(cmod.DEFAULTS_PATH.read_text(encoding="utf-8"))
    data["voice_table"]["boot"]["no"] = 2   # 与 new_order 重号
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(cmod, "DEFAULTS_PATH", bad)
    cmod.constants.cache_clear()
    try:
        cmod.constants()
    except ValueError as exc:
        assert "编号不连续" in str(exc)
    else:
        raise AssertionError("重号时应抛 ValueError")
    finally:
        cmod.constants.cache_clear()


def test_loader_rejects_missing_device_id_placeholder(tmp_path, monkeypatch):
    import json
    from kitchen import constants as cmod

    data = json.loads(cmod.DEFAULTS_PATH.read_text(encoding="utf-8"))
    data["mqtt"]["topics"]["telemetry"] = "kitchen/all/telemetry"
    bad = tmp_path / "bad2.json"
    bad.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(cmod, "DEFAULTS_PATH", bad)
    cmod.constants.cache_clear()
    try:
        cmod.constants()
    except ValueError as exc:
        assert "device_id" in str(exc)
    else:
        raise AssertionError("缺 device_id 占位时应抛 ValueError")
    finally:
        cmod.constants.cache_clear()
