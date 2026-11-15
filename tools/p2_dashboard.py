# -*- coding: utf-8 -*-
"""P2 一屏复盘（A2-09 的 CLI 版）：温湿度 · 排烟 · 燃气 · 冷链 · 互锁。

## 这一屏要回答什么

答辩现场最怕的是「每个功能单独讲一遍，评委看不到它们同时在跑」。这一屏把
P2（感知 + 判定 + 联锁）的全部关键量压在一帧里，并且让燃气突变从预警走到
联锁，看它怎么和排烟、温湿度抢同一个排风通道：

```
档位/时间 ─┬─ 燃气浓度（基线带 + 两级门限）
           ├─ 罩温（营业档 280℃ / 10 分钟温升 100℃）
           ├─ 冷链三路（冷藏 / 冷冻 / 留样柜）
           ├─ 三通道互锁状态 + 触发源
           └─ 本段时间内发生的事件
```

Web 版（A7）会把同样的数据画成曲线；这一版是它的**数据口径基线**——先确认
字段和聚合方式对，再谈画得好不好看。

## 用法

```
python tools/p2_dashboard.py              # 默认 15 分钟压缩到 15 帧
python tools/p2_dashboard.py --frames 30
```
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kitchen.rules.config_store import load_builtin          # noqa: E402
from kitchen.runtime import KitchenRuntime                   # noqa: E402
from kitchen.safety import ColdChainGuard, InterlockSupervisor  # noqa: E402

CST = timezone(timedelta(hours=8))

TOTAL_SEC = 900.0          # 15 分钟
GAS_BASELINE = 0.08


class Clock:
    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += float(delta)
        return self.t


def gas_at(t: float) -> float:
    """燃气：300s 起缓慢升高（→ 预警），420s 冲到 0.62（→ 联锁），560s 回落。"""
    if t < 300:
        return GAS_BASELINE
    if t < 420:
        return GAS_BASELINE + (0.50 - GAS_BASELINE) * (t - 300) / 120.0
    if t < 560:
        return 0.62
    if t < 620:
        return 0.62 - (0.62 - GAS_BASELINE) * (t - 560) / 60.0
    return GAS_BASELINE


def hood_at(t: float) -> float:
    """排烟：全程线性升温，50℃ → 200℃（营业档不触发 280℃，只看曲线）。"""
    return 50.0 + 150.0 * t / TOTAL_SEC


def fridge_at(t: float) -> float:
    """冷藏：200~240s 开一次门（4℃ → 9℃ → 回落），演示「开门不报警」。"""
    if t < 200 or t > 280:
        return 4.0
    if t <= 240:
        return 4.0 + 5.0 * (t - 200) / 40.0
    return 9.0 - 5.0 * (t - 240) / 40.0


def sample_at(t: float) -> float:
    """留样柜：600s 起升到 30℃（演示上限 25℃）。"""
    if t < 600:
        return 6.0
    if t < 660:
        return 6.0 + 24.0 * (t - 600) / 60.0
    return 30.0


def bar(value: float, low: float, high: float, width: int = 18) -> str:
    span = max(1e-9, high - low)
    filled = int(round(max(0.0, min(1.0, (value - low) / span)) * width))
    return "█" * filled + "░" * (width - filled)


def panel(
    *, clock: Clock, rt: KitchenRuntime, sup: InterlockSupervisor,
    metrics: dict, events: list[str], filtered: int,
) -> str:
    snap = rt.snapshot()
    lines = []
    when = datetime.fromtimestamp(clock(), CST).strftime("%H:%M:%S")
    lines.append(f"{when}  档位 {snap['profile'] or '-':<8} 口径 {snap['dataset']}"
                 f"  规则 {len(snap['rules'])} 条  过滤 {filtered} 帧")
    lines.append("  燃气  " + bar(metrics.get('gas_level', 0), 0, 0.7)
                 + f" {metrics.get('gas_level', 0):.2f}"
                 + f"（基线 {GAS_BASELINE:.2f}｜预警 0.45/30s｜联锁 0.55）")
    lines.append("  罩温  " + bar(metrics.get('hood_temp', 0), 0, 320)
                 + f" {metrics.get('hood_temp', 0):.0f}℃"
                 "（营业 280℃ / 温升 100℃）")
    cold = "  ".join(
        f"{name} {metrics.get(key, 0):.1f}℃"
        for name, key in (("冷藏", "fridge_temp"), ("冷冻", "freezer_temp"),
                          ("留样", "sample_temp"))
    )
    lines.append(f"  冷链  {bar(metrics.get('fridge_temp', 0), -30, 35)} {cold}"
                 f"｜温湿度 {metrics.get('t', 0):.0f}℃")
    channels = sup.channel_view()
    parts = []
    for name in ("CH1", "CH2"):
        body = channels[name]
        who = "、".join(f"{src}(P{p})" for src, p in
                        sorted((body["sources"] or {}).items(), key=lambda kv: kv[1])) or "-"
        parts.append(f"{name} {body['state']}[{who}]")
    lines.append("  互锁  " + "   ".join(parts))
    if events:
        lines.append("  事件  " + " ｜ ".join(events))
    else:
        lines.append("  事件  ——")
    return "\n".join(lines)


def run(*, frames: int = 15, quiet: bool = False) -> dict:
    start = datetime(2026, 11, 12, 11, 0, 0, tzinfo=CST).timestamp()
    clock = Clock(start)
    config = load_builtin()
    rt = KitchenRuntime(config, device_id="K-DEMO", clock=clock, wall_clock=clock)
    guard = ColdChainGuard(clock=clock)
    sup = InterlockSupervisor()

    per = TOTAL_SEC / frames
    step = 5.0
    records: list[dict] = []
    events: list[str] = []
    filtered = 0
    for _ in range(frames):
        budget = per
        while budget > 0:
            delta = min(step, budget)
            clock.advance(delta)
            budget -= delta
            elapsed = clock() - start
            metrics = {
                "gas_level": gas_at(elapsed),
                "hood_temp": hood_at(elapsed),
                "fridge_temp": fridge_at(elapsed),
                "freezer_temp": -20.0,
                "sample_temp": sample_at(elapsed),
                "t": 26.0,
            }
            frame = guard.observe(metrics, now=clock())
            if frame.held:
                filtered += 1
            for event in frame.events:
                events.append(f"{event.metric} {event.kind}")
            result = rt.tick(frame.passed)
            for verdict in result.verdicts:
                events.append(f"L{verdict.level} {verdict.rule_id} {verdict.event}")
            for ticket in result.tickets:
                events.append(f"开单 {ticket}")
            inter = sup.apply(result)
            for decision in inter.applied:
                events.append(f"→{decision}")

        text = panel(clock=clock, rt=rt, sup=sup, metrics=metrics,
                     events=events, filtered=filtered)
        records.append({"t": clock() - start, "panel": text, "events": list(events)})
        events = []

    if not quiet:
        print("P2 一屏复盘（A2-09 CLI 版）｜设备 K-DEMO｜"
              f"15 分钟压缩成 {frames} 帧，每帧 {per:.0f}s")
        print("=" * 86)
        for record in records:
            print(record["panel"])
            print("-" * 86)
        body = sup.snapshot()
        print("结束：CH1={}  CH2={}  待恢复={}".format(
            body["channels"]["CH1"]["state"],
            body["channels"]["CH2"]["state"],
            list(body["pending_restore"]) or "无",
        ))

    return {"records": records, "snapshot": sup.snapshot()}


def main() -> int:
    parser = argparse.ArgumentParser(description="P2 一屏复盘（CLI 版）")
    parser.add_argument("--frames", type=int, default=15, help="打印多少帧")
    parser.add_argument("--quiet", action="store_true", help="只返回结果不打印")
    args = parser.parse_args()
    run(frames=max(3, args.frames), quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
