# -*- coding: utf-8 -*-
"""#5 冷链两路 + #6 留样柜 —— 离线演练（开门波动 / 分级超限 / 柜温报警）。

## 演示什么

1. **开门不报警**：4℃ → 30 秒冲到 10℃ → 回落，全程零告警（样本被过滤器摘掉）；
2. **缓慢失效照样报**：1℃/min 的爬升不会被当成开门波动，越过 8℃ 后预警，
   持续 2 分钟升级成报警 + `cold_chain` 工单；
3. **留样柜分级**：22℃ 常温水放进去不响，35℃ 温水才响（演示阈值 25℃）；
4. **冷冻同构**：-18℃ 上限、演示 5℃。

## 用法

```
python tools/demos/coldchain_demo.py
python tools/demos/coldchain_demo.py --step 2 --quiet
```
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kitchen.rules.config_store import load_builtin         # noqa: E402
from kitchen.runtime import KitchenRuntime                  # noqa: E402
from kitchen.safety import ColdChainGuard                   # noqa: E402

CST = timezone(timedelta(hours=8))


class Clock:
    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += float(delta)
        return self.t


def mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


# 演示脚本：(标题, 时长秒, 该阶段第 t 秒的遥测)
PHASES = (
    ("冷藏柜稳定 4℃", 120, lambda t: {"fridge_temp": 4.0}),
    ("开门取货：30 秒冲到 10℃", 30, lambda t: {"fridge_temp": 4.0 + 6.0 * t / 30.0}),
    ("关门回落：60 秒回到 4℃", 60, lambda t: {"fridge_temp": 10.0 - 6.0 * t / 60.0}),
    ("压缩机缓慢失效：1℃/min 爬到 12℃", 480,
     lambda t: {"fridge_temp": 4.0 + 8.0 * t / 480.0}),
    ("稳在 12℃：等 120 秒确认", 150, lambda t: {"fridge_temp": 12.0}),
    ("留样柜放入常温水 22℃", 120, lambda t: {"sample_temp": 22.0}),
    ("留样柜放入温水 35℃", 150, lambda t: {"sample_temp": 35.0}),
    ("冷冻柜超温 8℃（演示上限 5℃）", 150, lambda t: {"freezer_temp": 8.0}),
)


def run(*, step: float = 5.0, quiet: bool = False) -> dict:
    start = datetime(2026, 11, 12, 11, 0, 0, tzinfo=CST).timestamp()
    clock = Clock(start)
    config = load_builtin()
    rt = KitchenRuntime(config, device_id="K-DEMO", clock=clock, wall_clock=clock)
    guard = ColdChainGuard(clock=clock)

    timeline: list[dict] = []
    elapsed = 0.0
    filtered = 0
    for title, duration, probe in PHASES:
        if not quiet:
            timeline.append({"t": elapsed, "kind": "phase", "text": f"—— {title} ——"})
        t = 0.0
        while t < duration:
            clock.advance(step)
            t += step
            elapsed += step
            frame = guard.observe(probe(t), now=clock())
            if frame.held:
                filtered += 1
            for event in frame.events:
                timeline.append({"t": elapsed, "kind": "filter",
                                 "text": f"{event.metric} {event.kind}：{event.reason}"})
            result = rt.tick(frame.passed)
            for verdict in result.verdicts:
                timeline.append({
                    "t": elapsed, "kind": "verdict",
                    "text": f"[L{verdict.level}] {verdict.event} {verdict.rule_id} "
                            f"{verdict.metric}={verdict.value:g}｜{verdict.message}",
                })
            for ticket in result.tickets:
                timeline.append({"t": elapsed, "kind": "ticket",
                                 "text": f"开单 {ticket}（落库在 A4）"})

    if not quiet:
        print(f"#5/#6 冷链与留样柜演练  设备 K-DEMO  总时长 {mmss(elapsed)}  步长 {step:g}s")
        print(f"配置：{config.source} / dataset={config.dataset}｜"
              f"规则 {len(rt.engine.get_rules())} 条")
        print(f"开门过滤：{guard.filter.snapshot()['params']}")
        print("-" * 78)
        for event in timeline:
            tag = {"phase": "·", "filter": "滤", "verdict": "判", "ticket": "单"}[event["kind"]]
            print(f"{mmss(event['t'])} [{tag}] {event['text']}")
        print("-" * 78)
        snapshot = rt.snapshot()
        print(f"结束：仍在闩锁的规则 {snapshot['latched'] or '无'}")
        print(f"过滤帧数 {filtered}｜判据事件 "
              f"{sum(1 for e in timeline if e['kind'] == 'verdict')}｜"
              f"工单 {sum(1 for e in timeline if e['kind'] == 'ticket')}")

    return {
        "timeline": timeline,
        "snapshot": rt.snapshot(),
        "filter": guard.snapshot(),
        "filtered_frames": filtered,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="#5/#6 冷链与留样柜离线演练")
    parser.add_argument("--step", type=float, default=5.0, help="每帧代表秒数")
    parser.add_argument("--quiet", action="store_true", help="只返回结果不打印")
    args = parser.parse_args()
    run(step=max(1.0, args.step), quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
