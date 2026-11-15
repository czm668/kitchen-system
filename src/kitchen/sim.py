# -*- coding: utf-8 -*-
"""后厨场景模拟器注入引擎（P1 / T1 可运行实现）。

解决的三个具体问题（对应素材库 `04_系统记录/P1_任务包.md` T1）：
1. **13 类信号可注入**：12 个遥测指标（`kitchen.json` metrics 全量）+ 1 类
   RC522 刷卡事件信号。遥测 id 与 manifest 双向强校验，写错名字在加载时报错。
2. **曲线 / hold / 随机游走 / 常量**：升温→保温→降温曲线、限时保持、
   有界随机游走（确定性可回放）、常量，配置全部在
   本工程 `fixtures/kitchen_sim_profiles.json`，不需要改 Python 代码。
3. **事件回放**：RFID 刷卡与三通道执行 ack 按相对秒 `at_s` 回放；每条 ack
   都经过 `kitchen.channels.validate_routing` 校验「动作 → 通道」，
   伪造跨通道 ack 会被拒绝（与 T2 防误发共用同一套映射）。

两种用法：
- 接模拟器：`kitchen.device.SimDevice(...)` 挂上 `SimState(profile=...)` 回放；
- 离线自检（不连 broker / 不连数据库）：`python -m tools.sim_cli --seconds 40`。

设计约束：纯标准库；不依赖旧工程 kernel/、web/ 与 mingchu 场景。
"""
from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from kitchen.channels import (
    ActuationError,
    resolve_action,
    validate_routing,
)
from kitchen.manifest import (
    EVENT_SIGNALS,
    EXPECTED_SIGNALS,
    TELEMETRY_METRICS,
)

DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "fixtures" / "kitchen_sim_profiles.json"

# 13 类信号的唯一事实源在 kitchen.manifest（12 遥测 + 1 类 RC522 事件）
METRIC_MODES = frozenset({"constant", "hold", "random_walk", "curve"})


class ProfileError(ValueError):
    """注入脚本配置非法。"""


@dataclass(frozen=True)
class MetricSignal:
    """单个遥测指标的信号定义。"""

    id: str
    mode: str
    config: dict[str, Any]
    label: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricSignal":
        signal_id = str(data.get("id") or "").strip()
        mode = str(data.get("mode") or "").strip()
        if not signal_id:
            raise ProfileError("signals[].id 不能为空")
        if mode not in METRIC_MODES:
            raise ProfileError(f"{signal_id} 的 mode={mode!r} 非法，允许 {sorted(METRIC_MODES)}")
        config = {key: value for key, value in data.items()
                  if key not in ("id", "kind", "mode", "label")}
        cls._check(signal_id, mode, config)
        return cls(id=signal_id, mode=mode, config=config, label=str(data.get("label") or ""))

    @staticmethod
    def _check(signal_id: str, mode: str, config: dict[str, Any]) -> None:
        """加载期校验，避免运行到一半才报 KeyError。"""
        if mode == "constant" and "value" not in config:
            raise ProfileError(f"{signal_id}: constant 需要 value")
        if mode == "hold":
            for key in ("value", "hold_s"):
                if key not in config:
                    raise ProfileError(f"{signal_id}: hold 需要 {key}")
            if float(config["hold_s"]) < 0:
                raise ProfileError(f"{signal_id}: hold_s 不能为负")
        if mode == "random_walk":
            for key in ("start", "step", "min", "max"):
                if key not in config:
                    raise ProfileError(f"{signal_id}: random_walk 需要 {key}")
            if float(config["min"]) > float(config["max"]):
                raise ProfileError(f"{signal_id}: min 不能大于 max")
            if float(config.get("interval_s", 1.0)) <= 0:
                raise ProfileError(f"{signal_id}: interval_s 必须 > 0")
        if mode == "curve":
            phases = config.get("phases")
            if not isinstance(phases, list) or not phases:
                raise ProfileError(f"{signal_id}: curve 需要非空 phases")
            for index, phase in enumerate(phases):
                if float(phase.get("duration_s", 0)) <= 0:
                    raise ProfileError(f"{signal_id}: phases[{index}].duration_s 必须 > 0")
                if "from" not in phase or "to" not in phase:
                    raise ProfileError(f"{signal_id}: phases[{index}] 需要 from/to")


@dataclass(frozen=True)
class SimEvent:
    """一条可回放事件（相对秒触发）。"""

    at_s: float
    kind: str  # rfid / cmd_ack
    topic: str  # 相对主题，由 SimDevice.publish 拼上 {scenario}/{device_id}/
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SimEvent":
        try:
            at_s = float(data["at_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileError(f"events[].at_s 非法：{data!r}") from exc
        if at_s < 0:
            raise ProfileError(f"events[].at_s 不能为负：{at_s}")
        kind = str(data.get("kind") or "").strip()
        if kind not in ("rfid", "cmd_ack"):
            raise ProfileError(f"events[].kind={kind!r} 非法")
        topic = str(data.get("topic") or "").strip().strip("/")
        if not topic:
            raise ProfileError(f"events[].topic 不能为空（at_s={at_s}）")
        return cls(at_s=at_s, kind=kind, topic=topic, data=dict(data))


class KitchenSimProfile:
    """13 类信号 + 事件时间线；可离线自检，也可挂到 `SimState` 上跑真模拟器。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = dict(data)
        self.scenario = str(data.get("scenario") or "")
        self.device_id = str(data.get("device_id") or "")
        self.version = str(data.get("version") or "")
        self.metrics: dict[str, MetricSignal] = {}
        self.event_signals: dict[str, dict[str, Any]] = {}
        for item in data.get("signals") or []:
            kind = str(item.get("kind") or "metric")
            signal_id = str(item.get("id") or "")
            if kind == "event":
                self.event_signals[signal_id] = dict(item)
            else:
                signal = MetricSignal.from_dict(item)
                if signal.id in self.metrics:
                    raise ProfileError(f"指标重复定义：{signal.id}")
                self.metrics[signal.id] = signal
        self.events: tuple[SimEvent, ...] = tuple(
            sorted((SimEvent.from_dict(item) for item in data.get("events") or []),
                   key=lambda event: event.at_s)
        )
        self._walks: dict[str, list[float]] = {}  # metric -> 确定性游走序列缓存

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_file(cls, path: str | Path) -> "KitchenSimProfile":
        file_path = Path(path)
        if not file_path.is_file():
            raise ProfileError(f"注入脚本不存在：{file_path}")
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"注入脚本无法读取：{file_path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise ProfileError("注入脚本顶层必须是 JSON 对象")
        return cls(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KitchenSimProfile":
        return cls(data)

    # ------------------------------------------------------------ 校验
    def validate(self, manifest=None) -> list[str]:
        """双向校验；返回问题列表（空列表 = 通过）。

        传 manifest 时额外核对：场景名一致、12 个遥测 id 与
        `scenarios/kitchen.json` 的 metrics 完全一致。
        """
        problems: list[str] = []
        if self.scenario != "kitchen":
            problems.append(f"profile.scenario={self.scenario!r}，本引擎只服务 kitchen 场景")
        if not self.device_id:
            problems.append("profile.device_id 不能为空")

        missing = [name for name in TELEMETRY_METRICS if name not in self.metrics]
        extra = [name for name in self.metrics if name not in TELEMETRY_METRICS]
        if missing:
            problems.append(f"缺少遥测指标：{missing}")
        if extra:
            problems.append(f"未登记的遥测指标：{extra}")
        for name in EVENT_SIGNALS:
            if name not in self.event_signals:
                problems.append(f"缺少事件信号：{name}")
        if len(self.metrics) + len(self.event_signals) != len(EXPECTED_SIGNALS):
            problems.append(
                f"信号总数应为 {len(EXPECTED_SIGNALS)}，实际 "
                f"{len(self.metrics) + len(self.event_signals)}"
            )

        if manifest is not None:
            if getattr(manifest, "scenario", None) != self.scenario:
                problems.append(
                    f"场景不一致：profile={self.scenario} / manifest={getattr(manifest, 'scenario', None)}"
                )
            declared = {item.id for item in manifest.metrics}
            if declared != set(TELEMETRY_METRICS):
                problems.append(
                    f"manifest 指标与 profile 不一致：缺失 {sorted(set(TELEMETRY_METRICS) - declared)}，"
                    f"多出 {sorted(declared - set(TELEMETRY_METRICS))}"
                )

        rfid_topic = str(self.event_signals.get("rfid_card", {}).get("topic") or "")
        for event in self.events:
            if event.kind == "rfid":
                if rfid_topic and event.topic != rfid_topic:
                    problems.append(
                        f"rfid 事件 topic={event.topic} 与信号声明 {rfid_topic} 不一致"
                    )
                if not isinstance(event.data.get("payload"), dict):
                    problems.append(f"rfid 事件缺少 payload（at_s={event.at_s}）")
                continue
            # cmd_ack：动作名、通道、人工确认口径都要过映射层
            action = str(event.data.get("action") or "")
            channel = str(event.data.get("channel") or "")
            try:
                binding = resolve_action(action)
                validate_routing(action, channel=channel)
            except (ActuationError, ValueError) as exc:
                problems.append(f"cmd_ack 动作/通道非法（at_s={event.at_s}）：{exc}")
                continue
            if binding.is_restore and not event.data.get("manual_confirmed"):
                problems.append(
                    f"{action} 是恢复动作，回放脚本必须标注 manual_confirmed=true（at_s={event.at_s}）"
                )
            if action == "power_restore":
                reviewers = set(event.data.get("dual_review") or [])
                if not {"web", "tft"}.issubset(reviewers):
                    problems.append(
                        f"power_restore 回放需 dual_review 含 web+tft（at_s={event.at_s}）"
                    )
            if not str(event.data.get("cmd_id") or ""):
                problems.append(f"cmd_ack 缺少 cmd_id（at_s={event.at_s}）")
        return problems

    def assert_valid(self, manifest=None) -> None:
        problems = self.validate(manifest)
        if problems:
            raise ProfileError("注入脚本校验失败：\n- " + "\n- ".join(problems))

    # ------------------------------------------------------------ 采样
    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(self.metrics)

    @property
    def signal_ids(self) -> tuple[str, ...]:
        return tuple(self.metrics) + tuple(self.event_signals)

    def sample_metric(self, metric: str, t: float) -> float | None:
        """返回 t 秒该指标的注入值；未登记指标返回 None（由驱动走默认基线）。"""
        signal = self.metrics.get(metric)
        if signal is None:
            return None
        return self._value(signal, max(0.0, float(t)))

    def sample_all(self, t: float) -> dict[str, float]:
        """一次性给出全部 12 个遥测指标，供自检 / 演示页使用。"""
        values: dict[str, float] = {}
        for metric in TELEMETRY_METRICS:
            value = self.sample_metric(metric, t)
            if value is not None:
                values[metric] = round(value, 3)
        return values

    def _value(self, signal: MetricSignal, t: float) -> float:
        cfg = signal.config
        mode = signal.mode
        if mode == "constant":
            return float(cfg["value"])
        if mode == "hold":
            # t < hold_s 期间保持注入值，之后回到基线（模拟故障窗口）
            return float(cfg["value"] if t < float(cfg["hold_s"]) else cfg.get("baseline", 0.0))
        if mode == "random_walk":
            interval = float(cfg.get("interval_s", 1.0))
            return self._walk(signal, int(t // interval))
        # curve：按阶段线性插值（升温 → 保温 → 降温），超出总时长后保持末值
        phases = cfg["phases"]
        total = sum(float(phase["duration_s"]) for phase in phases)
        elapsed = t % total if cfg.get("loop") else min(t, total)
        cursor = 0.0
        for phase in phases:
            duration = float(phase["duration_s"])
            start, end = float(phase["from"]), float(phase["to"])
            if elapsed <= cursor + duration or phase is phases[-1]:
                ratio = 0.0 if duration <= 0 else min(1.0, max(0.0, (elapsed - cursor) / duration))
                return start + (end - start) * ratio
            cursor += duration
        return float(phases[-1]["to"])  # pragma: no cover - 上面 for 必然返回

    def _walk(self, signal: MetricSignal, index: int) -> float:
        """有界随机游走：按 (metric, step) 派生独立 RNG，重复调用结果一致、可回放。"""
        cfg = signal.config
        seq = self._walks.setdefault(signal.id, [float(cfg["start"])])
        low, high = float(cfg["min"]), float(cfg["max"])
        while len(seq) <= index:
            rng = random.Random(f"{cfg.get('seed', signal.id)}:{signal.id}:{len(seq)}")
            nxt = seq[-1] + rng.uniform(-float(cfg["step"]), float(cfg["step"]))
            seq.append(min(high, max(low, nxt)))
        return seq[index]

    def iter_samples(self, duration_s: float, step_s: float = 1.0) -> Iterator[tuple[float, dict[str, float]]]:
        """离线回放用：逐秒给出全部 13 类信号中的 12 个遥测值。"""
        if step_s <= 0:
            raise ProfileError("step_s 必须 > 0")
        count = int(duration_s // step_s)
        for index in range(count + 1):
            t = round(index * step_s, 6)
            yield t, self.sample_all(t)

    # ------------------------------------------------------------ 事件
    def events_due(self, prev_t: float, cur_t: float) -> list[SimEvent]:
        """取 (prev_t, cur_t] 区间内到期的事件；左开右闭避免同一事件重复回放。"""
        return [event for event in self.events if prev_t < event.at_s <= cur_t]

    def render_event(self, event: SimEvent, ts: int | None = None) -> tuple[str, dict[str, Any]]:
        """把事件渲染成 (相对 topic, payload)；cmd_ack 再兜一次防误发校验。"""
        stamp = int(time.time()) if ts is None else int(ts)
        if event.kind == "rfid":
            payload = {**dict(event.data.get("payload") or {}), "ts": stamp,
                       "scenario": self.scenario, "device_id": self.device_id}
            return event.topic, payload

        action = str(event.data.get("action") or "")
        channel = str(event.data.get("channel") or "")
        binding = validate_routing(action, channel=channel)  # 跨通道 ack 会抛 MisrouteError
        ok = bool(event.data.get("ok", True))
        payload: dict[str, Any] = {
            "cmd_id": event.data.get("cmd_id"),
            "ok": ok,
            "action": action,
            "channel": binding.channel.value,
            "ts": stamp,
            "scenario": self.scenario,
            "device_id": self.device_id,
        }
        if event.data.get("source"):
            payload["source"] = event.data["source"]
        if not ok:
            payload["error"] = event.data.get("error") or "simulated failure"
        return event.topic, payload


# ---------------------------------------------------------------- CLI（离线自检）
def main() -> None:
    parser = argparse.ArgumentParser(description="后厨模拟器注入脚本离线自检（不连 broker）")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="注入脚本 JSON 路径")
    parser.add_argument("--seconds", type=float, default=40.0, help="回放时长（秒）")
    parser.add_argument("--step", type=float, default=1.0, help="采样步长（秒）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    args = parser.parse_args()

    from kitchen.manifest import load_scenario

    profile = KitchenSimProfile.from_file(args.profile)
    profile.assert_valid(load_scenario("kitchen"))

    samples = list(profile.iter_samples(args.seconds, args.step))
    events = profile.events_due(0.0, args.seconds)
    summary = {
        "profile": str(Path(args.profile).name),
        "scenario": profile.scenario,
        "signals": len(profile.signal_ids),
        "telemetry_metrics": len(profile.metric_ids),
        "events_total": len(profile.events),
        "events_in_window": len(events),
        "first_sample": samples[0][1] if samples else {},
        "peak_hood_temp": max(
            (sample["hood_temp"] for _, sample in samples), default=None
        ),
        "event_timeline": [
            {"at_s": event.at_s, "kind": event.kind,
             "action": event.data.get("action"), "channel": event.data.get("channel")}
            for event in events
        ],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(f"注入脚本 {summary['profile']} 校验通过：{summary['signals']} 类信号"
          f"（{summary['telemetry_metrics']} 遥测 + 1 事件），{summary['events_total']} 条事件")
    print(f"回放 {args.seconds:g}s：排烟罩峰值 {summary['peak_hood_temp']}°C，"
          f"窗口内触发 {summary['events_in_window']} 条事件")
    print("t=0 样本：" + json.dumps(summary["first_sample"], ensure_ascii=False))
    for item in summary["event_timeline"]:
        print(f"  {item['at_s']:>5.1f}s | {item['kind']:<8} | "
              f"{item['action'] or '-'} -> {item['channel'] or '-'}")


if __name__ == "__main__":
    main()
