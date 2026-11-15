# -*- coding: utf-8 -*-
"""#1 燃气泄漏分级处置的离线演示（不连 broker / 不连 MySQL）。

一次跑完四段，输出时间线：

1. **0–300 秒**：开机宽限期，只建基线不判定（对应 04 文档 #1「开机前 5 分钟只建基线」）
2. **300–600 秒**：基线就绪，正常波动 → 无事件
3. **600–900 秒**：浓度缓升到预警区 → 只播报，不动继电器
4. **900 秒后**：冲过绝对门限 → 联锁（关阀 + 排风 + 声光 + 语音），闩锁
5. **回落后**：仍闩锁，直到人工确认才释放

`GasLeakMonitor` 负责领域判定（基线 / 突变率 / 明火放宽），`RuleEngine` 负责
框架能力（持续时长 / 闩锁 / 人工复位 / NVS）。两层的分工在这个脚本里看得最清楚。

用法::

    python tools/gas_demo.py                # 文本时间线
    python tools/gas_demo.py --json         # 机器可读，便于写进系统记录
    python tools/gas_demo.py --speed 60     # 60 倍速（默认 300 倍，2Hz 采样按秒聚合）
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

_CODE_ROOT = Path(__file__).resolve().parents[1]
if str(_CODE_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT / "src"))

from kitchen.channels import InterlockController  # noqa: E402
from kitchen.manifest import load_scenario  # noqa: E402
from kitchen.rules import (  # noqa: E402
    EVENT_FIRED,
    EVENT_RELEASED,
    Rule,
    RuleEngine,
    load_rows,
    load_scenario_rules,
)
from kitchen.safety import (  # noqa: E402
    LEVEL_ALARM,
    LEVEL_WARN,
    GasBaselineTracker,
    GasLeakMonitor,
)

FIXTURES = _CODE_ROOT / "fixtures"


def build_curve(total_seconds: float, step: float) -> list[tuple[float, float, bool]]:
    """合成一条演示曲线：(秒, gas_level, 是否有明火)。

    真实设备上这三个量来自 MQ-2 与火焰传感器；这里用确定性随机数造出
    「干净 → 波动 → 缓升 → 泄漏 → 回落」的形状，便于复现。
    """
    rng = random.Random(20261112)
    points: list[tuple[float, float, bool]] = []
    t = 0.0
    while t <= total_seconds:
        flame = 700.0 <= t < 1500.0          # 炒菜时段有明火
        if t < 300:
            value = 0.08 + rng.uniform(-0.005, 0.005)      # 开机期：基线来源
        elif t < 600:
            value = 0.08 + rng.uniform(-0.01, 0.01)        # 稳定期
        elif t < 900:
            # 缓慢上升但仍低于预警线 0.45：这段是「正常波动」，不该有任何事件
            progress = (t - 600) / 300.0
            value = 0.08 + 0.17 * progress + rng.uniform(-0.005, 0.005)
        elif t < 1150:
            # 越过 0.45 预警线并停留 250 秒 → 预警级（level=1，只播报）能真正演出来
            progress = (t - 900) / 250.0
            value = 0.25 + 0.27 * progress + rng.uniform(-0.008, 0.008)
        elif t < 1230:
            # 冲破泄漏门限 0.55 → 联锁级（level=2，动继电器）
            progress = (t - 1150) / 80.0
            value = 0.52 + 0.20 * progress + rng.uniform(-0.01, 0.01)
        elif t < 1400:
            value = 0.72 + rng.uniform(-0.02, 0.02)        # 关阀后管道残留
        else:
            # 排风 + 关阀后的回落
            progress = min(1.0, (t - 1400) / 200.0)
            value = 0.72 - 0.64 * progress + rng.uniform(-0.01, 0.01)
        points.append((t, max(0.0, min(1.0, value)), flame))
        t += step
    return points


def run(total_seconds: float = 1600.0, step: float = 5.0, ack_at: float = 1560.0,
        seed_profile: str = "demo") -> dict:
    manifest = load_scenario("kitchen")
    scenario_rules = load_scenario_rules(FIXTURES / "kitchen_rules.json")

    # 只取燃气的两条阈值（绝对门限 + 预警持续），其余判定点在别的演示脚本里
    rows = _load_gas_rows()
    active, _ = load_rows(rows, scenario_rules=scenario_rules)
    rules = [rule for rule in active if rule.metric == "gas_level"]

    clock = _Clock()
    engine = RuleEngine(
        device_id="kitchen-node-01",
        default_duration_sec=3.0,
        cooldown_sec=60.0,
        boot_grace_sec=300.0,     # #1：开机 5 分钟只建基线
        dataset=seed_profile,
        clock=clock,
        wall_clock=clock,
    )
    engine.set_rules(rules)
    engine.boot(now=clock())

    tracker = GasBaselineTracker(window_sec=300.0, min_samples=40)
    monitor = GasLeakMonitor()
    interlock = InterlockController()

    timeline: list[dict] = []
    acked = False
    frozen_at: float | None = None
    last_monitor_level = 0        # monitor 自己不做去抖，只在级别变化时记一条
    for moment, value, flame in build_curve(total_seconds, step):
        clock.t = moment
        tracker.update(moment, value)
        # 周更固化：必须在稳定期做。放到演示末尾做的话，窗口里全是泄漏高峰
        # 数据，固化出来的基线会是 0.55——等于把泄漏浓度当成「平时水平」。
        if frozen_at is None and moment >= 650.0:
            frozen = tracker.freeze(wall_now=moment)
            frozen_at = moment
            timeline.append({
                "t": moment, "kind": "baseline", "event": "weekly_freeze",
                "value": round(frozen.value, 4),
                "message": f"基线周更固化 {frozen.value:.4f} 并写 NVS（重启免等 5 分钟）",
            })
        baseline = tracker.baseline(wall_now=moment)
        assessment = monitor.assess(value, baseline, flame_active=flame)

        verdicts = engine.evaluate(
            {"gas_level": value, "flame_b": 1.0 if flame else 0.0, "t": 26.0},
            now=moment,
        )
        for verdict in verdicts:
            item = {
                "t": moment,
                "kind": "engine",
                "event": verdict.event,
                "rule_id": verdict.rule_id,
                "value": round(verdict.value, 4),
                "level": verdict.level,
                "latched": verdict.latched,
                "actions": list(verdict.actions),
                "message": verdict.message,
            }
            timeline.append(item)
            if verdict.event == EVENT_FIRED:
                _dispatch(interlock, verdict.actions, source=f"rule-{verdict.rule_id}")

        # monitor 只做领域判定，不做去抖/冷却（那是规则引擎的活）。演示里按
        # 「级别变化」记一条，否则每一帧都会刷一行。
        if assessment.level != last_monitor_level:
            last_monitor_level = assessment.level
            # 闩锁期间不再报低级别提示：已经在「关阀 + 声光」了，再提示「浓度偏高」
            # 属于重复告警，和规则引擎的等级压制同理（见 _suppress_lower_levels）
            if assessment.level == LEVEL_WARN and not engine.snapshot()["latched"]:
                timeline.append({
                    "t": moment, "kind": "monitor", "event": "warn",
                    "value": round(value, 4), "level": assessment.level,
                    "ratio": round(assessment.ratio, 2),
                    "duration_required": assessment.duration_required,
                    "message": f"{assessment.reason}（基线 {assessment.baseline:.3f}，"
                               f"处置门槛 {assessment.duration_required:g}s）→ 只播报不动作",
                })

        # 人工确认复位：只在值回落且指定时刻之后
        if not acked and moment >= ack_at and assessment.level == 0:
            results = engine.ack_all("E009")
            acked = any(item.ok for item in results)
            for result in results:
                timeline.append({
                    "t": moment, "kind": "ack", "event": "manual_reset",
                    "rule_id": result.rule_id, "ok": result.ok,
                    "reviewer": result.reviewer, "message": result.reason,
                })
                if result.ok:
                    timeline.extend(_release_channels(interlock, result.rule_id, moment))

    frozen = tracker.baseline(wall_now=total_seconds)

    summary = {
        "steps": len(build_curve(total_seconds, step)),
        "step_sec": step,
        "boot_grace_sec": engine.boot_grace_sec,
        "rules": [rule.id for rule in rules],
        "events": timeline,
        "final": {
            "baseline": round(frozen.value, 4),
            "baseline_ready": frozen.ready,
            "latched": engine.snapshot()["latched"],
            "interlock": interlock.snapshot(),
        },
        "nvs_blob": engine.persist(),
    }
    assert manifest.scenario == "kitchen"  # 防止误挂到别的场景
    return summary


class _Clock:
    """演示脚本自己的时间轴：判定与墙钟都按脚本秒推进，与真实时间解耦。"""

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t


def _release_channels(interlock: InterlockController, rule_id: str, moment: float) -> list[dict]:
    """人工确认后释放通道：先标触发源恢复，再下发恢复动作。

    这两步的顺序是硬要求（契约 §2.3 ④）：`note_recovered` 之前通道仍有触发源，
    `request("gas_valve_open")` 会被 InterlockBlocked 拦下来——这正是
    「高优先级闩锁期间低优先级恢复不得释放同一通道」的落地。
    """
    from kitchen.channels import ACTION_BINDINGS, Channel, RELAY_CHANNELS

    source = f"rule-{rule_id}"
    released: list[dict] = []
    for channel in RELAY_CHANNELS:
        if source not in interlock.active_sources(channel):
            continue
        fully = interlock.note_recovered(channel, source)
        if not fully:
            continue  # 同通道还有别的触发源，不能恢复
        restore = next(
            (binding.action for binding in ACTION_BINDINGS.values()
             if binding.channel is channel and binding.is_restore),
            None,
        )
        if restore is None:
            continue
        interlock.request(restore, manual_confirmed=True)
        released.append({
            "t": moment, "kind": "interlock", "event": "channel_released",
            "channel": channel.value, "action": restore,
            "message": f"人工确认后释放 {channel.value}，下发 {restore}",
        })
    return released


def _dispatch(interlock: InterlockController, actions: list[str] | tuple[str, ...],
              source: str) -> None:
    """把规则给出的动作交给互锁层裁决；被拦的记日志但不中断演示。"""
    from kitchen.channels import ActuationError, PRIORITY_GAS_LEAK

    for action in actions:
        try:
            interlock.request(action, source=source, priority=PRIORITY_GAS_LEAK)
        except ActuationError:
            # 恢复类动作需要人工确认，规则引擎本来就不该自动下发——拦下是对的
            pass


def _load_gas_rows() -> list[dict]:
    """从种子 SQL 里抠出燃气那两行（与单测共用同一份真实数据）。"""
    import re

    sql_text = (FIXTURES / "seed_kitchen.sql").read_text(encoding="utf-8")
    rows: list[dict] = []
    columns = ("id", "point_id", "kind", "profile", "lower_bound", "upper_bound",
               "duration_sec", "window_sec", "aggregate", "level", "enabled")
    for body in re.findall(
        r"INSERT\s+IGNORE\s+INTO\s+env_thresholds\s*\([^)]*\)\s*VALUES(.*?);",
        sql_text, re.IGNORECASE | re.DOTALL,
    ):
        for match in re.finditer(r"\(([^()]*)\)", body):
            cells = [item.strip() for item in match.group(1).split(",")]
            row = {}
            for name, cell in zip(columns, cells):
                row[name] = _value(cell)
            if row.get("point_id") == 1:
                row["metric"] = "gas_level"
                rows.append(row)
    return rows


def _value(cell: str):
    text = cell.strip()
    if text.upper() == "NULL":
        return None
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def main() -> None:
    parser = argparse.ArgumentParser(description="#1 燃气分级处置离线演示")
    parser.add_argument("--seconds", type=float, default=1600.0, help="演示总时长（脚本秒）")
    parser.add_argument("--step", type=float, default=5.0, help="采样步长（脚本秒）")
    parser.add_argument("--ack-at", type=float, default=1560.0, help="人工确认复位的时刻")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    summary = run(args.seconds, args.step, args.ack_at)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    print(f"#1 燃气分级处置演示：{summary['steps']} 帧 / 步长 {summary['step_sec']:g}s"
          f" / 开机宽限 {summary['boot_grace_sec']:g}s / 规则 {summary['rules']}")
    print("-" * 88)
    print(f"{'秒':>6} | {'来源':<7} | {'事件':<12} | {'值':>5} | 说明")
    print("-" * 88)
    for item in summary["events"]:
        value = item.get("value", "")
        value_text = f"{value:.3f}" if isinstance(value, (int, float)) else "-"
        print(f"{item['t']:>6.0f} | {item['kind']:<7} | {item['event']:<12} | "
              f"{value_text:>5} | {item.get('message', '')}")
    print("-" * 88)
    final = summary["final"]
    print(f"基线：{final['baseline']}（就绪={final['baseline_ready']}）")
    print(f"仍处闩锁：{final['latched'] or '无'}")
    for channel, state in final["interlock"].items():
        print(f"  {channel}: {state['state']}  触发源={state['sources']}")
    print(f"NVS blob：{summary['nvs_blob']!r}")


if __name__ == "__main__":
    main()
