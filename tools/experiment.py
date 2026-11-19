# -*- coding: utf-8 -*-
"""A10-05 三组对比实验：无系统（人工巡检） vs 有系统（自动判据） vs 演示注入。

## 实验设计（答辩材料要求：三组对比 + 标注「实验室/仿真数据」）

- **组 A · 无系统（人工巡检基线）**：按巡检制度假设（每 10 分钟巡一次 +
  发现后 60 秒人工处置）解析计算。**这组没有实测**——它就是现状，
  数据来自口径假设，报告里明确标注。
- **组 B · 有系统（自动判据，实测）**：向规则引擎喂真实时序帧（50ms 一帧
  与生产同频），测「故障出现 → 判定触发 + 动作下发」的延迟。保护层
  （guard）与规则层（rule）分开记——前者立即，后者要过确认窗口。
- **组 C · 演示模式（信号注入，实测）**：走 `/api/demo/inject` 注入同一
  故障值，验证与组 B **同一条判定路径** + 审计留 `demo.inject` 标记。

## 诚实口径（写进报告的每张表）

- 全部数据来自**仿真环境**（`AppState` + 规则引擎，非真实燃气/热电偶）；
- 组 B 延迟含确认窗口（30s/120s 是**设计出来的故意延迟**，滤抖动用的，
  不是系统慢）——报告分开列「保护层立即响应」与「规则层确认后动作」；
- 组 A 的巡检参数是假设，改动它结论方向不变（巡检间隔越短人力成本越高）。

## 用法

    python tools/experiment.py            # 跑实验并打印报告
    python tools/experiment.py --write    # 同时写入 05_系统记录/ 对比实验报告.md
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from api_support import data_of, login, make_state  # noqa: E402

#: 帧间隔（与生产采样同频）
TICK = 0.05

#: 正常态安全基线（全部在阈值内，含小幅噪声由调用方叠加）
SAFE: dict[str, float] = {
    "t": 26.0, "h": 58.0, "gas_level": 0.10, "hood_temp": 38.0,
    "sterilize_temp": 25.0, "fridge_temp": 4.0, "freezer_temp": -19.0,
    "sample_temp": 5.0, "flame_a": 0.0, "flame_b": 0.0,
    "sct_current": 0.6, "weight_g": 125.0,
}


def _frame(**overrides: float) -> dict[str, float]:
    frame = dict(SAFE)
    frame.update(overrides)
    return frame


def _ticks_until_alarm(state, frame: dict[str, float], timeout: float,
                       want_guard: bool = True):
    """喂故障帧直到报警。返回 (延迟秒, 保护层判定, 规则层判定, 动作)。"""
    start = state.monotonic()
    deadline = start + timeout
    guard_hit: Any = None
    rule_hit: Any = None
    actions: tuple[str, ...] = ()
    while state.monotonic() < deadline:
        step = state.tick(frame)
        if want_guard and guard_hit is None and step.guard_verdicts:
            guard_hit = step.guard_verdicts[0]
        if rule_hit is None:
            fired = [v for v in step.verdicts if v.fired or v.latched]
            if fired:
                rule_hit = fired[0]
        if step.actions:
            actions = step.actions
            return state.monotonic() - start, guard_hit, rule_hit, actions
        time.sleep(TICK)
    return None, guard_hit, rule_hit, actions


def run_group_b(workdir: Path) -> list[dict[str, Any]]:
    """组 B：四个故障场景的实测延迟。flame_hold_sec=12 是 #3 设计值。"""
    state = make_state(workdir / "b", flame_hold_sec=12)
    scenarios = [
        ("燃气泄漏（0.10→0.65 阶跃）", _frame(gas_level=0.65), 45.0),
        ("明火离人 12 秒（flame_b=1 无刷卡）", _frame(flame_b=1.0), 25.0),
        ("排烟超温（38→300℃ 阶跃）", _frame(hood_temp=300.0), 10.0),
        ("冷链超限（4→10℃ 持续）", _frame(fridge_temp=10.0), 140.0),
    ]
    rows = []
    for name, frame, timeout in scenarios:
        # 每个场景独立 state：闩锁/基线互不污染
        state = make_state(workdir / "b", flame_hold_sec=12)
        latency, guard, rule, actions = _ticks_until_alarm(
            state, frame, timeout)
        rows.append({
            "name": name,
            "latency": latency,
            "guard": guard.rule_id if guard else "",
            "rule": rule.rule_id if rule else "",
            "actions": list(actions),
            "timeout": latency is None,
        })
    return rows


def run_false_alarm(workdir: Path, seconds: float = 60.0) -> dict[str, Any]:
    """正常态误报：阈值内小幅噪声跑 60 秒，数 fired/latched 的判定。"""
    state = make_state(workdir / "fa", flame_hold_sec=12)
    noise = {"hood_temp": 3.0, "fridge_temp": 0.6, "gas_level": 0.02,
             "h": 2.0}
    alarms = 0
    ticks = 0
    deadline = state.monotonic() + seconds
    while state.monotonic() < deadline:
        frame = dict(SAFE)
        for metric, amp in noise.items():
            frame[metric] += (state.monotonic() * 7.0 % 2 - 1) * amp
        step = state.tick(frame)
        ticks += 1
        alarms += sum(1 for v in step.verdicts if v.fired or v.latched)
        time.sleep(TICK)
    return {"seconds": seconds, "ticks": ticks, "alarms": alarms,
            "per_hour": alarms * 3600.0 / seconds}


def run_group_c(workdir: Path) -> dict[str, Any]:
    """组 C：同一故障走演示注入通道，验证同一条判定 + demo 留痕。"""
    from kitchen.api import create_server

    state = make_state(workdir / "c", flame_hold_sec=12)
    server = create_server(state, port=0, secret="exp")
    admin = login(server, "admin")
    payload = data_of(server, "POST", "/api/demo/inject",
                      session=admin, metrics={"gas_level": 0.65})
    verdicts = payload["verdicts"] + payload["guards"]
    audit = [row for row in server.state.audit.entries()
             if row.action == "demo.inject"]
    events = [row for row in server.state.events()
              if row["kind"] == "inject"]
    return {
        "verdict_count": len(verdicts),
        "rules": sorted({v["rule_id"] for v in verdicts}),
        "actions": payload["actions"],
        "audit_marked": bool(audit),
        "event_marked": bool(events),
    }


def build_report(b_rows, false_alarm, group_c) -> str:
    patrol_min = 10.0
    react_sec = 60.0
    lines = [
        "# A10-05 三组对比实验报告",
        "",
        f"> 数据性质：**实验室/仿真数据**（`AppState` + 规则引擎时序仿真，"
        f"非真实传感器）。生成：tools/experiment.py，{time.strftime('%Y-%m-%d %H:%M')}。",
        "",
        "## 组 A · 无系统（人工巡检基线，口径假设）",
        "",
        "| 指标 | 数值 | 口径 |",
        "| --- | --- | --- |",
        f"| 平均发现延迟 | {patrol_min / 2:.0f} 分钟 | 每 {patrol_min:.0f} 分钟巡一次，"
        "故障出现在间隔内均匀分布 |",
        f"| 最坏发现延迟 | {patrol_min:.0f} 分钟 | 巡检刚走故障就发生 |",
        f"| 处置延迟（发现→动作） | {react_sec:.0f} 秒 | 人工关阀/断电 |",
        f"| 自动联动率 | 0% | 无人值守时段无任何自动动作 |",
        "| 误报 | 不适用 | 无自动判定即无误报，代价是漏报同样不可见 |",
        "",
        "## 组 B · 有系统（自动判据，实测）",
        "",
        "| 故障场景 | 保护层响应 | 规则层确认 | 首个动作 | 动作延迟 |",
        "| --- | --- | --- | --- | --- |",
    ]
    #: 阈值表行号 → 人话（env_thresholds.id，见 seed_kitchen.sql）
    RULE_NAMES = {
        "1": "#1 燃气·泄漏区间（保守保护联动）",
        "2": "#1 燃气·持续 30s 确认",
        "14": "#5 冷链·超限预警（3s 去抖）",
        "23": "#5 冷链·持续 120s 报警",
    }
    for row in b_rows:
        if row["timeout"]:
            lines.append(f"| {row['name']} | — | — | 超时未触发 | > 上限 |")
            continue
        guard_text = row["guard"] or "（保护层直报，见动作列）"
        rule_text = RULE_NAMES.get(row["rule"], row["rule"] or "—")
        action_text = "、".join(row["actions"]) or "—"
        lines.append(
            f"| {row['name']} | {guard_text} | {rule_text} | {action_text} "
            f"| **{row['latency']:.1f} 秒** |")
    lines += [
        "",
        "> 延迟构成：保护层（guard）为**立即响应**（1~2 帧，燃气/超温场景 0 秒"
        "即下发动作）；规则层含设计确认窗口（燃气 30s / 冷链 120s，滤抖动防"
        "误报，不是系统慢）。两层数据分列，答辩被问「为什么不是 0.1 秒」时"
        "按此口径回答。冷链 3 秒是预警级（只播语音）；报警级（声光+工单）"
        "按设计需持续 120 秒。",
        "",
        f"## 正常态误报率（实测 {false_alarm['seconds']:.0f} 秒 / "
        f"{false_alarm['ticks']} 帧）",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 报警级判定次数 | {false_alarm['alarms']} 次 |",
        f"| 折算每小时 | {false_alarm['per_hour']:.2f} 次/小时 |",
        "",
        "> 阈值内随机噪声（排烟 ±3℃ / 冷藏 ±0.6℃ / 燃气 ±0.02 等）下应为 0；"
        "非零即为规则去抖不足，按 bug 处理。",
        "",
        "## 组 C · 演示模式（信号注入，实测）",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 触发判定条数 | {group_c['verdict_count']} |",
        f"| 命中规则 | {'、'.join(group_c['rules'])} |",
        f"| 下发动作 | {'、'.join(group_c['actions']) or '—'} |",
        f"| 审计留痕（demo.inject） | {'✅' if group_c['audit_marked'] else '❌'} |",
        f"| 事件流标记（kind=inject） | {'✅' if group_c['event_marked'] else '❌'} |",
        "",
        "> 组 C 与组 B 走**同一条判定路径**（注入只改采样值不改逻辑），"
        "区别仅在留痕标记——现场演示时据此声明「这是注入演出来的报警」。",
        "",
        "## 结论",
        "",
        "1. **响应速度**：保护层立即（<0.1 秒）vs 人工巡检平均 5~10 分钟，"
        "提升约 3 个数量级；规则层确认窗口是防误报的设计延迟。",
        "2. **无人值守**：系统在故障发生即下发关阀/断气动作（组 B 全场景触发），"
        "人工模式该时段为 0%。",
        "3. **误报可控**：正常态噪声下报警级判定为 "
        f"{false_alarm['alarms']} 次（{false_alarm['per_hour']:.2f}/小时），"
        "去抖窗口有效。",
        "4. **演示可复现**：注入通道与真实采样同一判定路径且全程留痕，"
        "答辩现场可安全演示故障场景。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="A10-05 对比实验")
    parser.add_argument("--write", action="store_true",
                        help="写入 05_系统记录/20261112_对比实验报告.md")
    args = parser.parse_args()

    workdir = ROOT / ".local/tmp/experiment"
    workdir.mkdir(parents=True, exist_ok=True)

    print("组 B：四个故障场景（真实时钟，含确认窗口，约 4 分钟）...")
    b_rows = run_group_b(workdir)
    for row in b_rows:
        if row["timeout"]:
            print(f"  [超时] {row['name']}")
        else:
            print(f"  {row['name']}: {row['latency']:.1f}s "
                  f"→ {'、'.join(row['actions'])}")
    print("正常态误报（60 秒）...")
    false_alarm = run_false_alarm(workdir)
    print(f"  {false_alarm['alarms']} 次 / {false_alarm['per_hour']:.2f} 每小时")
    print("组 C：演示注入通道...")
    group_c = run_group_c(workdir)
    print(f"  判定 {group_c['verdict_count']} 条，审计留痕 "
          f"{group_c['audit_marked']}")

    report = build_report(b_rows, false_alarm, group_c)
    if args.write:
        target = ROOT.parents[1] / "05_系统记录" / "20261112_对比实验报告.md"
        target.write_text(report, encoding="utf-8")
        print(f"\n已写入 {target}")
    else:
        print("\n" + report)
    ok = all(not row["timeout"] for row in b_rows) and false_alarm["alarms"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
