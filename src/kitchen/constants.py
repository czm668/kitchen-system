# -*- coding: utf-8 -*-
"""隐式常量的单一来源（审查报告 §3 P2）。

三处"编号一经发布不得改"的常量以前散落在代码里，不看 docs/04、05、08
就改数值，演示会立刻失真：

- TFT 语音编号表（`tft/audio.py::VOICE_TABLE`，001–012）
- 演示阈值换算表（`api/state.py::demo_map()`）
- MQTT 主题根前缀（`mqtt/transport.py::DEFAULT_ROOT`）

现在它们在 `fixtures/kitchen_constants.json` 里各只有一份，代码加载后校验：
编号连续、名字唯一、主题模板含 `{device_id}` 占位。加载失败**启动即报错**，
而不是演示时静默错位——这正是审查报告说的"隐式不变量要有类型/测试保护"。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["constants", "voice_table", "demo_durations", "mqtt_root",
           "vision_thresholds", "DEFAULTS_PATH"]

#: 仓库根（本文件在 src/kitchen/ 下，parents[2] 即仓库根）
DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "kitchen_constants.json"


def _fail(msg: str) -> None:
    raise ValueError(f"kitchen_constants.json 校验失败：{msg}")


@lru_cache(maxsize=1)
def constants() -> dict[str, Any]:
    """读一次 + 全量校验。缓存是因为进程内常量不变，且加载要防止反复读盘。"""
    try:
        data = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"读不了 {DEFAULTS_PATH}: {exc}")

    # ① 语音编号：001–012 连续、名字唯一（真机 SD 卡曲目编号，不能重号）
    voice = data.get("voice_table")
    if not isinstance(voice, dict) or not voice:
        _fail("voice_table 为空")
    nos = sorted(int(v["no"]) for v in voice.values())
    if nos != list(range(1, len(nos) + 1)):
        _fail(f"语音编号不连续：{nos}")
    names = [v["name"] for v in voice.values()]
    if len(set(names)) != len(names):
        _fail("语音名字重复")
    if set(voice) != set(names):
        _fail("语音表的 key 与 name 不一致")

    # ② 演示时长：必须都是正数（换算表里出现 0 会让"演示压缩"失真）
    durations = data.get("demo_durations")
    if not isinstance(durations, (list, tuple)) or not durations:
        _fail("demo_durations 为空")
    for row in durations:
        if float(row["real_sec"]) <= 0:
            _fail(f"演示时长非正：{row.get('label')}")

    # ③ MQTT：根前缀非空，主题模板带 {device_id} 占位
    mqtt = data.get("mqtt")
    if not isinstance(mqtt, dict) or not str(mqtt.get("root", "")).strip():
        _fail("mqtt.root 为空")
    for key, tpl in mqtt.get("topics", {}).items():
        if "{device_id}" not in tpl:
            _fail(f"mqtt.topics.{key} 缺少 {{device_id}} 占位：{tpl}")

    # ④ 视觉阈值台账：五个子表齐全（具体值与代码默认值的一致性由
    #    tests/test_vision_thresholds.py 断言，这里只保证结构完整）
    vision = data.get("vision")
    if not isinstance(vision, dict):
        _fail("vision 段缺失")
    for section in ("model", "analysis", "violation", "absence", "pipeline"):
        if not isinstance(vision.get(section), dict) or not vision[section]:
            _fail(f"vision.{section} 为空")

    return data


def voice_table() -> dict[str, dict[str, Any]]:
    return constants()["voice_table"]


def demo_durations() -> list[dict[str, Any]]:
    return constants()["demo_durations"]


def mqtt_root() -> str:
    return constants()["mqtt"]["root"]


def vision_thresholds() -> dict[str, dict[str, Any]]:
    """视觉阈值登记台账。运行时真值仍是各 Policy dataclass 默认值；
    本函数供文档/测试读取，二者一致性由 tests/test_vision_thresholds.py 守卫。"""
    return constants()["vision"]
