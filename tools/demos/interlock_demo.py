# -*- coding: utf-8 -*-
"""互锁编排演练（A2-08）：三源共用排风 / 优先级压制 / 全部恢复才关。

## 演示什么

一排三通道状态表，配上每次动作裁决，看四件事：

1. **任一触发即开**：燃气、排烟、温湿度三路都要开 CH2，第二次、第三次请求
   被识别成「通道已在目标状态」，不再重复下发；
2. **全部恢复才关**：三个源都回落之前，`fan_off` 一律被拒，拒绝理由里写明
   还有谁占着通道；
3. **优先级压制**：燃气（P0）还占着 CH1/CH2 时，排烟（P2）自己好了也不许
   释放通道——所以恢复动作不携带优先级，只认「通道还有没有源」；
4. **恢复必须人工确认 + 留痕**：引擎永远不会自动开阀/停风机。

## 用法

```
python tools/demos/interlock_demo.py
python tools/demos/interlock_demo.py --step 2
```
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kitchen.rules.config_store import load_builtin          # noqa: E402
from kitchen.runtime import KitchenRuntime                   # noqa: E402
from kitchen.safety import InterlockSupervisor               # noqa: E402

CST = timezone(timedelta(hours=8))

# 备餐档（14:00-16:30）：这时 #2 排烟的动作是「开排风 + 关燃气阀」，
# 营业档只播语音，演不出三源共用排风。
PREP_TS = datetime(2026, 11, 12, 15, 0, 0, tzinfo=CST).timestamp()

# 演示脚本：(标题, 时长秒, 遥测, 本阶段结束时尝试人工恢复的通道)
# 恢复尝试是演示的重点：每一次被拒都在解释一条硬规则。
PHASES = (
    ("燃气浓度 0.60（超泄漏门限 0.55）", 10, {"gas_level": 0.60}, ("CH1",)),
    ("排烟罩 260℃（备餐档阈值 250℃）", 10,
     {"gas_level": 0.60, "hood_temp": 260.0}, ("CH1",)),
    ("后厨温度 38℃（#7 联动排风）", 10,
     {"gas_level": 0.60, "hood_temp": 260.0, "t": 38.0}, ("CH2",)),
    ("燃气回落 0.10：排烟仍占着通道", 10,
     {"gas_level": 0.10, "hood_temp": 260.0, "t": 38.0}, ("CH1",)),
    ("排烟回落 100℃：CH1 可恢复，CH2 还被温湿度占着", 10,
     {"gas_level": 0.10, "hood_temp": 100.0, "t": 38.0}, ("CH1", "CH2")),
    ("温度回落 25℃：三源全清", 10,
     {"gas_level": 0.10, "hood_temp": 100.0, "t": 25.0}, ()),
)


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


def channel_line(channels: dict) -> str:
    parts = []
    for name in ("CH1", "CH2", "CH3"):
        body = channels.get(name) or {}
        sources = body.get("sources") or {}
        if sources:
            who = "、".join(f"{src}(P{prio})" for src, prio in
                            sorted(sources.items(), key=lambda item: item[1]))
        else:
            who = "-"
        parts.append(f"{name}[{body.get('state', '?')}] {who}")
    return "  ".join(parts)


def run(*, step: float = 1.0, quiet: bool = False) -> dict:
    clock = Clock(PREP_TS)
    config = load_builtin()
    rt = KitchenRuntime(config, device_id="K-DEMO", clock=clock, wall_clock=clock)
    sup = InterlockSupervisor()

    timeline: list[dict] = []
    elapsed = 0.0
    for title, duration, metrics, attempts in PHASES:
        timeline.append({"t": elapsed, "kind": "phase", "text": f"—— {title} ——"})
        t = 0.0
        while t < duration:
            clock.advance(step)
            t += step
            elapsed += step
            frame = sup.apply(rt.tick(metrics))
            for decision in frame.decisions:
                if decision.duplicate:
                    continue  # 重复下发不上屏，避免刷屏（统计里仍有记录）
                timeline.append({"t": elapsed, "kind": "action",
                                 "text": decision.describe()})
            for channel, source in frame.recovered:
                timeline.append({"t": elapsed, "kind": "recover",
                                 "text": f"源 {source} 已从 {channel} 注销"})
        timeline.append({"t": elapsed, "kind": "state",
                         "text": "通道：" + channel_line(sup.channel_view())})
        for name in attempts:
            decision = sup.restore(name, reviewer="web:王组长")
            timeline.append({"t": elapsed,
                             "kind": "restore" if decision.ok else "denied",
                             "text": decision.describe()})

    # 全部恢复之后的收尾：三源都清干净了，才允许人工把通道恢复到常态
    for name in ("CH1", "CH2"):
        decision = sup.restore(name, reviewer="web:王组长")
        timeline.append({"t": elapsed,
                         "kind": "restore" if decision.ok else "denied",
                         "text": decision.describe()})
    timeline.append({"t": elapsed, "kind": "state",
                     "text": "恢复后：" + channel_line(sup.channel_view())})

    if not quiet:
        print(f"互锁演练  设备 K-DEMO  备餐档 15:00 起｜规则 "
              f"{len(rt.engine.get_rules())} 条｜步长 {step:g}s")
        print("=" * 86)
        for event in timeline:
            tag = {"phase": "·", "action": "动", "recover": "销",
                   "state": "态", "restore": "复", "denied": "拒"}[event["kind"]]
            print(f"{mmss(event['t'])} [{tag}] {event['text']}")
        print("=" * 86)
        body = sup.snapshot()
        print(f"结束：CH1={body['channels']['CH1']['state']}  "
              f"CH2={body['channels']['CH2']['state']}  "
              f"CH3={body['channels']['CH3']['state']}  "
              f"待恢复={list(body['pending_restore']) or '无'}")

    return {"timeline": timeline, "snapshot": sup.snapshot()}


def main() -> int:
    parser = argparse.ArgumentParser(description="互锁编排离线演练")
    parser.add_argument("--step", type=float, default=1.0, help="每帧代表秒数")
    parser.add_argument("--quiet", action="store_true", help="只返回结果不打印")
    args = parser.parse_args()
    run(step=max(0.5, args.step), quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
