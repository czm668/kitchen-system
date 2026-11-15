# -*- coding: utf-8 -*-
"""#3 明火离人 —— 离线演练（一次完整的「离人 → 联锁 → 返岗刷卡 → 视觉失联」）。

## 演示什么

1. **有火有人不报警**：正常炒菜每天都在发生，判据必须有这两下；
2. **有火无人 12 秒才触发**：太短会被「转身取料」打到，太长则错过处置窗口；
3. **联锁动作**：关燃气阀 + 切断灶台电源 + 声光 + 语音；
4. **刷卡即复位**：人回来就是判据失效的直接原因，不必等传感器；
5. **视觉失联单独告警**：不伪造成「有人」就算了，还要主动说「这条防线瞎了」。

## 用法

```
python tools/flame_demo.py
python tools/flame_demo.py --step 1 --quiet
```
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kitchen.rules.config_store import load_builtin         # noqa: E402
from kitchen.runtime import KitchenRuntime                  # noqa: E402
from kitchen.safety import FlameGuard, StaffCard            # noqa: E402

CST = timezone(timedelta(hours=8))

# 演示名册：张三有效，李四健康证过期（#9）
CARDS = {
    "04A2B3C1": StaffCard("04A2B3C1", "1", "张三"),
    "04A2B3C2": StaffCard("04A2B3C2", "2", "李四", expired=True),
}


class Clock:
    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t


def mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def run(*, step: float = 3.0, total: float = 240.0, quiet: bool = False) -> dict:
    start = datetime(2026, 11, 12, 11, 0, 0, tzinfo=CST).timestamp()
    clock = Clock(start)
    config = load_builtin()
    rt = KitchenRuntime(config, device_id="K-DEMO", clock=clock, wall_clock=clock)
    guard = FlameGuard(rt.engine, card_lookup=CARDS.get, clock=clock,
                       vision_timeout_sec=30.0)

    timeline: list[dict] = []
    state = "normal"

    def note(second: float, kind: str, text: str) -> None:
        timeline.append({"t": second, "kind": kind, "text": text})

    t = 0.0
    while t < total:
        clock.t = start + t

        # ------------------------------------------------ 现场剧本
        if t < 60:
            flame, no_person = 1.0, False          # 正常炒菜
        elif t < 96:
            flame, no_person = 1.0, True           # 厨师转身去拿货
        elif t < 150:
            flame, no_person = 1.0, False          # 刷卡回来了
        else:
            flame, no_person = 1.0, False          # 但视觉服务已经断连

        # 150 秒后不再推视觉信号，模拟 ai_service 掉线
        if t < 150:
            guard.observe_vision(no_person=no_person, headcount=0 if no_person else 2)

        result = rt.tick({"flame_b": flame, **guard.vision_frame()})

        for verdict in result.verdicts:
            actions = "、".join(verdict.actions) or "—"
            note(t, "verdict", f"[{verdict.event}] {verdict.message} 动作={actions}")

        # 96 秒：厨师返岗刷卡
        if t >= 96 and state == "away":
            state = "returned"
            ack = guard.ack_by_card("04A2B3C1")
            note(t, "ack", f"刷卡复位 {ack.describe()}")

        # 视觉离线告警：只在刚过期的那个时刻报一次
        health = guard.vision_health()
        if health.stale and state == "returned" and t >= 180:
            state = "vision_lost"
            note(t, "vision", f"⚠ {health.message}")

        if t < 96 and state == "normal":
            pass
        if t >= 60 and t < 96:
            state = "away" if state == "normal" else state

        t += step

    # ---------------------------------------------------------------- 输出
    if not quiet:
        print(f"#3 明火离人演练  设备 K-DEMO  时长 {mmss(total)}  步长 {step:g}s")
        print(f"判据：flame_b > 0.5 持续 12s  AND  视觉判定无人（requires）")
        print(f"配置：{config.source} / {config.dataset}｜规则 {len(rt.engine.get_rules())} 条")
        print("-" * 76)
        for event in timeline:
            tag = {"verdict": "判", "ack": "卡", "vision": "视"}[event["kind"]]
            print(f"{mmss(event['t'])} [{tag}] {event['text']}")
        print("-" * 76)
        snapshot = guard.snapshot()
        print(f"结束：工位锁定={snapshot['locked']}｜视觉在线={snapshot['vision']['online']}")
        print(f"视觉状态：{snapshot['vision']['message'] or '正常'}")
        print(f"事件统计：判据 {sum(1 for e in timeline if e['kind'] == 'verdict')}｜"
              f"刷卡 {sum(1 for e in timeline if e['kind'] == 'ack')}｜"
              f"视觉告警 {sum(1 for e in timeline if e['kind'] == 'vision')}")

    return {"timeline": timeline, "snapshot": guard.snapshot()}


def main() -> int:
    parser = argparse.ArgumentParser(description="#3 明火离人离线演练")
    parser.add_argument("--step", type=float, default=3.0, help="每帧代表秒数")
    parser.add_argument("--total", type=float, default=240.0, help="总时长（秒）")
    parser.add_argument("--quiet", action="store_true", help="只返回结果不打印")
    args = parser.parse_args()
    run(step=max(0.5, args.step), total=args.total, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
