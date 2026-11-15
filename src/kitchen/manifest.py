# -*- coding: utf-8 -*-
"""场景契约加载器 + 13 类信号的唯一事实源。

新工程不依赖素材库的 `kernel.plugins`：只用标准库解析 `scenarios/<name>.json`，
在**加载期**把指标、驱动、动作、状态回写全部校验完，避免运行到一半才炸。

校验内容包括：
- `drivers[].class` 真实可导入，且类的 `id/topic/metrics/actions` 与声明一致；
- 指标 id 唯一、驱动 id 唯一、驱动引用的指标都已声明；
- 动作名不含旧场景的 `valve_close/valve_open`（防「关燃气阀」串到「切工位插座」）；
- `states` 的 key 必须落在 `actions` 内；`devices[].drivers` 必须都已声明；
- kitchen 场景额外强校验：12 个遥测 id 与 `TELEMETRY_METRICS` 完全一致。

这一层是薄适配层，只负责契约；接入平台底座（MQTT / 命令 / 数据库）后，
由 `tools/legacy.py fetch platform-core` 取用素材并按需替换，业务代码不改。
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# src/kitchen/manifest.py -> [0]=kitchen, [1]=src, [2]=工程根
ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = ROOT / "scenarios"

# 13 类信号 = 12 个数值遥测 + 1 类 RC522 刷卡事件（事件不进 sensor_readings）
TELEMETRY_METRICS: tuple[str, ...] = (
    "t", "h", "gas_level", "hood_temp", "sterilize_temp", "fridge_temp",
    "freezer_temp", "sample_temp", "flame_a", "flame_b", "sct_current", "weight_g",
)
EVENT_SIGNALS: tuple[str, ...] = ("rfid_card",)
EXPECTED_SIGNALS: tuple[str, ...] = TELEMETRY_METRICS + EVENT_SIGNALS

# 旧场景口径：仅用于防串味校验，禁止出现在 kitchen 的动作表里
LEGACY_ACTION_NAMES: frozenset[str] = frozenset({"valve_close", "valve_open"})
LEGACY_SCENARIOS: frozenset[str] = frozenset({"mingchu", "lab_safety"})


class ManifestError(ValueError):
    """manifest 非法配置；`field` 给出出错的字段路径，便于定位。"""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(f"{field}: {message}" if field else message)
        self.field = field


@dataclass(frozen=True)
class MetricDef:
    id: str
    label: str = ""
    unit: str = ""
    aliases: tuple[str, ...] = ()
    kind: str = "number"


@dataclass(frozen=True)
class DriverDef:
    id: str
    class_path: str
    topic: str = ""
    metrics: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    states: dict[str, dict[str, str]] | None = None


@dataclass(frozen=True)
class DeviceDef:
    id: str
    name: str = ""
    dtype: str = ""
    drivers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioManifest:
    scenario: str
    name: str
    version: str
    metrics: tuple[MetricDef, ...]
    devices: tuple[DeviceDef, ...]
    drivers: tuple[DriverDef, ...]
    raw: dict[str, Any]

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.metrics)

    @property
    def action_names(self) -> tuple[str, ...]:
        return tuple(action for driver in self.drivers for action in driver.actions)


def resolve_driver_class(class_path: str) -> type:
    """把 `module:Attr` 解析成真实类；导入失败给出可定位的报错。"""
    module_name, _, attr = str(class_path).partition(":")
    if not module_name or not attr:
        raise ManifestError(f"驱动类路径格式应为 'module:Attr'，收到 {class_path!r}", "drivers[].class")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ManifestError(f"无法导入驱动模块 {module_name!r}：{exc}", "drivers[].class") from exc
    obj = getattr(module, attr, None)
    if not isinstance(obj, type):
        raise ManifestError(f"{class_path} 不是类", "drivers[].class")
    return obj


def _validate(data: dict[str, Any], name: str) -> ScenarioManifest:
    if data.get("scenario") != name:
        raise ManifestError(
            f"scenario={data.get('scenario')!r} 与请求 {name!r} 不一致", "scenario"
        )
    if name in LEGACY_SCENARIOS:
        raise ManifestError(f"旧场景 {name!r} 不属于新工程，请从素材库取用", "scenario")

    raw_metrics = data.get("metrics") or []
    metrics: list[MetricDef] = []
    seen_metrics: set[str] = set()
    for index, item in enumerate(raw_metrics):
        metric_id = str(item.get("id") or "")
        field = f"metrics[{index}]"
        if not metric_id:
            raise ManifestError("id 不能为空", field)
        if metric_id in seen_metrics:
            raise ManifestError(f"指标 id 重复：{metric_id}", field)
        seen_metrics.add(metric_id)
        metrics.append(MetricDef(
            id=metric_id,
            label=str(item.get("label") or ""),
            unit=str(item.get("unit") or ""),
            aliases=tuple(str(a) for a in item.get("aliases") or ()),
            kind=str(item.get("kind") or "number"),
        ))

    raw_drivers = data.get("drivers") or []
    drivers: list[DriverDef] = []
    seen_drivers: set[str] = set()
    seen_actions: set[str] = set()
    for index, item in enumerate(raw_drivers):
        field = f"drivers[{index}]"
        driver_id = str(item.get("id") or "")
        if not driver_id or driver_id in seen_drivers:
            raise ManifestError(f"驱动 id 为空或重复：{driver_id!r}", field)
        seen_drivers.add(driver_id)

        class_path = str(item.get("class") or "")
        declared_metrics = tuple(str(m) for m in item.get("metrics") or ())
        declared_actions = tuple(str(a) for a in item.get("actions") or ())
        topic = str(item.get("topic") or "")

        # 防串味优先于实现一致性：旧动作名绝不进入 kitchen 契约
        for action in declared_actions:
            if action in LEGACY_ACTION_NAMES:
                raise ManifestError(
                    f"{driver_id} 混入了旧场景动作名 {action!r}（防串路硬拦截）", field
                )
            if action in seen_actions:
                raise ManifestError(f"动作名重复：{action!r}", field)
            seen_actions.add(action)

        cls = resolve_driver_class(class_path)

        # 声明与实现双向核对，防止 manifest 与驱动代码脱节
        if getattr(cls, "id", None) != driver_id:
            raise ManifestError(f"{class_path}.id={getattr(cls, 'id', None)!r} != {driver_id!r}", field)
        if tuple(getattr(cls, "metrics", ())) != declared_metrics:
            raise ManifestError(
                f"{driver_id} 的 metrics 声明 {declared_metrics} 与实现 "
                f"{tuple(getattr(cls, 'metrics', ()))} 不一致", field
            )
        if tuple(getattr(cls, "actions", ())) != declared_actions:
            raise ManifestError(
                f"{driver_id} 的 actions 声明 {declared_actions} 与实现 "
                f"{tuple(getattr(cls, 'actions', ()))} 不一致", field
            )
        if declared_metrics and getattr(cls, "topic", "") != topic:
            raise ManifestError(f"{driver_id} 的 topic 与实现不一致", field)

        for metric in declared_metrics:
            if metric not in seen_metrics:
                raise ManifestError(f"{driver_id} 引用了未声明指标 {metric!r}", field)

        states = {
            str(key): {str(k): str(v) for k, v in dict(value or {}).items()}
            for key, value in (item.get("states") or {}).items()
        }
        unknown_states = sorted(set(states) - set(declared_actions))
        if unknown_states:
            raise ManifestError(f"states 含未声明动作：{unknown_states}", field)
        drivers.append(DriverDef(
            id=driver_id, class_path=class_path, topic=topic,
            metrics=declared_metrics, actions=declared_actions, states=states,
        ))

    devices = tuple(
        DeviceDef(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            dtype=str(item.get("dtype") or ""),
            drivers=tuple(str(d) for d in item.get("drivers") or ()),
        )
        for item in data.get("devices") or []
    )
    for index, device in enumerate(devices):
        missing = [d for d in device.drivers if d not in seen_drivers]
        if missing:
            raise ManifestError(f"设备 {device.id} 引用了未声明驱动 {missing}", f"devices[{index}]")

    # kitchen 专属强校验：13 类信号口径不能漂移
    if name == "kitchen" and seen_metrics != set(TELEMETRY_METRICS):
        raise ManifestError(
            f"kitchen 遥测指标必须是 12 个且完全一致；"
            f"缺 {sorted(set(TELEMETRY_METRICS) - seen_metrics)}，"
            f"多 {sorted(seen_metrics - set(TELEMETRY_METRICS))}", "metrics"
        )

    return ScenarioManifest(
        scenario=name,
        name=str(data.get("name") or ""),
        version=str(data.get("version") or ""),
        metrics=tuple(metrics),
        devices=devices,
        drivers=tuple(drivers),
        raw=dict(data),
    )


@lru_cache(maxsize=8)
def load_scenario(name: str, scenarios_dir: str | Path | None = None) -> ScenarioManifest:
    """加载并校验场景契约；结果缓存，非法配置直接抛 `ManifestError`。

    `scenarios_dir` 仅供测试注入临时目录使用，正常调用不要传。
    """
    if name in LEGACY_SCENARIOS:
        raise ManifestError(
            f"旧场景 {name!r} 不属于新工程，请从素材库取用", "scenario"
        )
    directory = Path(scenarios_dir) if scenarios_dir is not None else SCENARIOS_DIR
    path = directory / f"{name}.json"
    if not path.is_file():
        raise ManifestError(f"场景文件不存在：{path}", "scenario")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"场景文件无法读取：{exc}", "scenario") from exc
    if not isinstance(data, dict):
        raise ManifestError("场景文件顶层必须是 JSON 对象", "scenario")
    return _validate(data, name)
