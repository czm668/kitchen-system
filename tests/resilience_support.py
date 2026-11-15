# -*- coding: utf-8 -*-
"""异常兜底测试的公共夹具（A6）。

刻意不用 `MagicMock`：兜底层的输入是「读数」和「时间」，用假时钟 + 假运行时
能写出可读的时间线（`now=0/10/11`），而 mock 出来的调用断言看不出
「30 秒之后才该触发」这类语义。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (_ROOT, _ROOT / "src"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from kitchen.resilience.queue import LocalQueue  # noqa: E402
from kitchen.runtime import TickResult  # noqa: E402

#: 一帧「什么都没事」的遥测（12 类契约里的 11 个数值量 + 火焰）
BASE_METRICS: dict[str, float] = {
    "t": 24.0,
    "h": 55.0,
    "gas_level": 0.06,
    "hood_temp": 60.0,
    "sterilize_temp": 40.0,
    "fridge_temp": 4.0,
    "freezer_temp": -18.0,
    "sample_temp": 3.0,
    "flame_a": 0.0,
    "flame_b": 0.0,
    "sct_current": 0.5,
    "weight_g": 100.0,
}


def metrics(**overrides: float) -> dict[str, float]:
    """一键造一帧遥测。`metrics(gas_level=0.9)` 只改想改的那项。"""
    return {**BASE_METRICS, **overrides}


class FakeClock:
    """可推进的墙钟（测试里所有时间都是「第几秒」）。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += float(delta)
        return self.t

    def set(self, value: float) -> float:
        self.t = float(value)
        return self.t


def make_queue(tmp_path, name: str = "device.queue", **kwargs) -> LocalQueue:
    return LocalQueue(tmp_path / name, device_id=kwargs.pop("device_id", "K-TEST"), **kwargs)


@dataclass
class StubRuntime:
    """只回固定判定的假运行时，用来把「保护层的动作」从「引擎的动作」里隔离出来。

    真 `KitchenRuntime` 会在燃气 0.9 时自己触发 `#1`，两条路径发出同一个
    `gas_valve_close`，于是「保护到底有没有起作用」在断言里看不出来。
    """

    device_id: str = "K-STUB"
    config: Any = field(default_factory=lambda: _Config())
    verdicts: tuple = ()
    calls: int = 0

    def tick(self, frame=None, now=None, *, raw_metrics=None, **kwargs) -> TickResult:
        self.calls += 1
        return TickResult(
            profile="business",
            previous_profile=None,
            profile_changed=False,
            verdicts=tuple(self.verdicts),
        )


@dataclass
class _Config:
    degraded: bool = False


@dataclass
class CountingSink:
    """统计被调用次数的接收端，用来断言「tick 里没有网络 IO」。"""

    inner: Any
    calls: int = 0

    def receive(self, payload: dict) -> dict:
        self.calls += 1
        return self.inner.receive(payload)


def collect_actions(steps) -> tuple[str, ...]:
    """把多帧的 `applied` 合并成一个集合（去重保序）。"""
    out: list[str] = []
    for step in steps:
        for action in step.actions:
            if action not in out:
                out.append(action)
    return tuple(out)


def collect_guard_events(steps) -> list[tuple[str, str]]:
    """(保护名, 事件) 序列，便于断言「先 fired 后 released」。"""
    return [
        (verdict.rule_id, verdict.event)
        for step in steps
        for verdict in step.guard_verdicts
    ]
