# -*- coding: utf-8 -*-
"""#2 排烟积热分档调度 —— 离线演练（压缩一天，答辩可直接跑）。

## 演示什么

一条时间线跑完 24 小时，看四件事：

1. **档位自己跟时间走**：10:00 营业 / 14:00 备餐 / 16:30 营业 / 22:00 收档，
   不靠服务端推「当前档位」；
2. **同一档位阈值不同**：同一个 300℃ 在营业档只播语音，在收档档关阀 + 声光；
3. **动作随档位变**：营业档「仅预警不影响出餐」，备餐/收档才动继电器；
4. **收档档带清理工单**：`tickets=("cleanup",)`，落库在 A4。

## 用法

```
python tools/demos/hood_demo.py                 # 完整一天
python tools/demos/hood_demo.py --from 09:00 --to 12:00 --verbose
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

CST = timezone(timedelta(hours=8))

# 这一天的演示脚本：(起始分钟, 结束分钟, 峰值温度, 说明)
SPIKES = (
    (630, 636, 300.0, "营业档：午高峰油烟倒灌"),
    (900, 906, 270.0, "备餐档：灶台空烧忘关"),
    (1380, 1386, 220.0, "收档档：油锅余温 + 排风停机"),
)
# 冲击结束后多久做人工确认（必须等回落后才允许，否则会被引擎拒掉）
ACK_AFTER = 60

# 一天的热负荷基线：(起始分钟, 起始温度, 结束分钟, 结束温度)
BASE_RAMP = (
    (540, 60.0, 600, 45.0),      # 09:00-10:00 开门前余温
    (600, 45.0, 840, 120.0),     # 10:00-14:00 午市爬升
    (840, 120.0, 990, 80.0),     # 14:00-16:30 备餐回落
    (990, 80.0, 1320, 170.0),    # 16:30-22:00 晚市爬升
    (1320, 170.0, 1800, 40.0),   # 22:00-次日 06:00 收档冷却
    (1800, 40.0, 1860, 60.0),
)


def baseline(minute: int) -> float:
    """基线温度：分段线性，每分钟变动 < 1℃，保证 10 分钟窗口内不误触发温升。"""
    for start, temp0, end, temp1 in BASE_RAMP:
        if start <= minute <= end:
            span = max(1, end - start)
            progress = (minute - start) / span
            return temp0 + (temp1 - temp0) * progress
    return 40.0


def hood_temp(minute: int) -> float:
    for start, end, peak, _ in SPIKES:
        if start <= minute <= end:
            span = max(1, end - start)
            low = baseline(start)
            return low + (peak - low) * (minute - start) / span
        if end < minute <= end + 30:
            progress = (minute - end) / 30.0
            return peak - (peak - baseline(minute)) * progress
    return baseline(minute)


class Clock:
    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t


def hhmm(minute: int) -> str:
    return f"{(minute // 60) % 24:02d}:{minute % 60:02d}"


def collapse(timeline: list[dict]) -> list[dict]:
    """把「闩锁期间每分钟重报」折成一行。

    闩锁规则每 `cooldown_sec`（60 秒）重报一次是**设计如此**（持续提醒），
    但打印出来会把真正的关键节点淹掉。答辩现场需要的是「一眼看到发生了什么」，
    所以默认折叠，`--full` 看全量。
    """
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for event in timeline:
        if event["kind"] == "verdict" and "[latched]" in event["text"]:
            rule_id = event["text"].split()[1]
            if rule_id in seen:
                seen[rule_id]["repeats"] = seen[rule_id].get("repeats", 1) + 1
                seen[rule_id]["last"] = event["minute"]
                continue
            entry = dict(event)
            entry["repeats"] = 1
            entry["last"] = event["minute"]
            seen[rule_id] = entry
            out.append(entry)
        else:
            out.append(event)
    for entry in seen.values():
        if entry["repeats"] > 1:
            entry["text"] += (
                f"（闩锁重报共 {entry['repeats']} 次，直至 {hhmm(entry['last'])}）"
            )
    return out


def run(*, start_min: int = 540, end_min: int = 1800, step: int = 1,
        verbose: bool = False, full: bool = False) -> dict:
    """跑一段时间线，返回统计摘要。"""
    day = datetime(2026, 11, 12, tzinfo=CST)
    start_ts = (day + timedelta(minutes=start_min)).timestamp()
    clock = Clock(start_ts)

    config = load_builtin()
    rt = KitchenRuntime(config, device_id="K-DEMO", clock=clock, wall_clock=clock)

    timeline: list[dict] = []
    ack_points = {end + ACK_AFTER for _, end, _, _ in SPIKES}
    acked: set[int] = set()

    minute = start_min
    while minute <= end_min:
        clock.t = (day + timedelta(minutes=minute)).timestamp()
        temp = hood_temp(minute)
        result = rt.tick({"hood_temp": round(temp, 1)})

        if result.profile_changed:
            timeline.append({
                "minute": minute, "kind": "profile",
                "text": f"档位切换 {result.previous_profile} → {result.profile}",
            })
        for verdict in result.verdicts:
            actions = "、".join(verdict.actions) or "—"
            ticket = f" 工单={verdict.ticket}" if verdict.ticket else ""
            timeline.append({
                "minute": minute, "kind": "verdict", "level": verdict.level,
                "text": f"[{verdict.event}] {verdict.rule_id} {verdict.message} "
                        f"动作={actions}{ticket}",
            })
        for ticket in result.tickets:
            timeline.append({
                "minute": minute, "kind": "ticket",
                "text": f"生成工单：{ticket}（清理工单，A4 落 work_orders）",
            })

        # 人工确认：必须落在「已回落」之后，否则引擎会拒绝（防带故障复位）
        if minute in ack_points and minute not in acked:
            acked.add(minute)
            for item in rt.ack_all("E009"):
                timeline.append({
                    "minute": minute, "kind": "ack",
                    "text": f"人工确认 {item.rule_id}：{item.reason}"
                            f"{'' if item.ok else '（被拒绝）'}",
                })

        minute += step

    # ---------------------------------------------------------------- 输出
    print(f"#2 排烟积热分档演练  {hhmm(start_min)} → {hhmm(end_min)}  设备 K-DEMO")
    print(f"配置来源：{config.source}｜取值口径：{config.dataset}｜"
          f"阈值行 {len(config.threshold_rows)} 条｜场景动作 {len(config.scenario_rules)} 组")
    print(f"档位表：{config.schedule.describe()}")
    print("-" * 78)

    for event in (timeline if full else collapse(timeline)):
        tag = {"profile": "档", "verdict": "判", "ticket": "单", "ack": "复"}[event["kind"]]
        temp_here = hood_temp(event["minute"])
        print(f"{hhmm(event['minute'])} [{tag}] {event['text']}"
              f"  (罩温 {temp_here:.0f}℃)")

    print("-" * 78)
    snapshot = rt.snapshot()
    print(f"结束档位：{snapshot['profile']}｜仍闩锁：{snapshot['latched'] or '无'}")
    counts = {}
    for event in timeline:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    print(f"统计：判据事件 {counts.get('verdict', 0)} 条｜"
          f"档位切换 {counts.get('profile', 0)} 次｜"
          f"工单 {counts.get('ticket', 0)} 张｜"
          f"人工复位 {counts.get('ack', 0)} 次")
    if verbose:
        print("\n当前规则集：")
        for rule in rt.engine.get_rules():
            print(f"  {rule.id:>3} {rule.metric:<12} {rule.kind:<9} "
                  f"profile={str(rule.profile or '-'):<9} "
                  f"{rule.describe_bound():<14} L{rule.level} 动作={rule.actions}")
    return {"timeline": timeline, "snapshot": snapshot, "counts": counts}


def main() -> int:
    parser = argparse.ArgumentParser(description="#2 排烟积热分档调度离线演练")
    parser.add_argument("--from", dest="start", default="09:00", help="起始时刻 HH:MM")
    parser.add_argument("--to", dest="end", default="06:00", help="结束时刻 HH:MM（小于起始视为次日）")
    parser.add_argument("--step", type=int, default=1, help="每帧代表的分钟数")
    parser.add_argument("--verbose", action="store_true", help="附带打印规则集")
    parser.add_argument("--full", action="store_true", help="不折叠闩锁重报")
    args = parser.parse_args()

    def to_minute(text: str) -> int:
        hour, minute = text.split(":")
        return int(hour) * 60 + int(minute)

    start_min = to_minute(args.start)
    end_min = to_minute(args.end)
    if end_min <= start_min:
        end_min += 24 * 60          # 跨零点
    run(start_min=start_min, end_min=end_min, step=max(1, args.step),
        verbose=args.verbose, full=args.full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
