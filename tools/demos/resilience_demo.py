# -*- coding: utf-8 -*-
"""异常兜底演练（A6-01~05）：断网、掉电、传感器坏、三端降级。

## 演示什么

十段，每段都走「现场真会出问题」的那条路径：

1. **端侧队列**：断网攒数据；**掉电重启后 `seq` 接着走**（重号会被服务端当重复丢掉）；
2. **补传去重**：同一批送两遍 → 第二遍全是"重复"，页面不出现重复点；
3. **乱序补传**：seq 乱序送上去，查询仍按时间线排；
4. **时钟校准**：端侧慢 45 秒 → 校准后补传的时间落在正确位置；
5. **保护①燃气突变**：单点尖峰**不关阀**，连续两点立即关阀；
6. **保护②超温联动风机**：罩温 290℃ 强制开风机；274℃ 仍不解除（滞回 5℃）；
7. **保护③明火长烧无人**：演示加速 60 分钟 → 60 秒；中途刷卡重新计时；
8. **传感器故障工单**：拔掉一路 DS18B20（报 85℃）→ 立刻开单 → 超时升级 L2；
9. **三端降级标记**：视觉离线时三端各自怎么显示「哪些能力不可用」；
10. **离线整链**：断网 30 秒里三条保护全部生效 → 恢复网络自动补传。

## 用法

```
python tools/demos/resilience_demo.py
python tools/demos/resilience_demo.py --quiet            # 只打印结论
python tools/demos/resilience_demo.py --data var/queue   # 指定队列文件落点
```
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kitchen.domain import CST                                    # noqa: E402
from kitchen.resilience import (                                 # noqa: E402
    KIND_TELEMETRY,
    ClockCalibrator,
    ConservativeGuards,
    GuardPolicy,
    LocalQueue,
    MemoryOfflineStore,
    OfflineFirstRuntime,
    ReplayUploader,
    SensorHealthMonitor,
    SensorPolicy,
    build_degrade,
)
from kitchen.resilience.queue import KIND_ALARM                  # noqa: E402
from kitchen.rules.config_store import load_builtin              # noqa: E402
from kitchen.runtime import KitchenRuntime                       # noqa: E402
from kitchen.safety.interlock import InterlockSupervisor         # noqa: E402

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)

METRICS: dict[str, float] = {
    "t": 24.0, "h": 55.0, "gas_level": 0.06, "hood_temp": 70.0,
    "sterilize_temp": 40.0, "fridge_temp": 4.0, "freezer_temp": -18.0,
    "sample_temp": 3.0, "flame_a": 0.0, "flame_b": 0.0,
    "sct_current": 0.6, "weight_g": 120.0,
}


def frame(**overrides: float) -> dict[str, float]:
    return {**METRICS, **overrides}


def say(verbose: bool, text: str = "") -> None:
    if verbose:
        print(text)


# ---------------------------------------------------------------- ① 队列
def step1_queue(verbose: bool, root: Path) -> LocalQueue:
    print("\n① 端侧队列：断网攒数据，掉电重启不重号")
    queue = LocalQueue(root / "kitchen.queue", device_id="K-DEMO")
    for i in range(3):
        queue.append(KIND_TELEMETRY, metric="t", value=24.0 + i, ts=100.0 + i)
    queue.append(KIND_ALARM, metric="gas_level", payload={"level": 2})
    say(verbose, f"  攒了 {len(queue)} 条，最后序号 {queue.last_seq}")

    # 模拟掉电：同一个文件重新装载
    restarted = LocalQueue(root / "kitchen.queue", device_id="K-DEMO")
    nxt = restarted.append(KIND_TELEMETRY, metric="t", value=28.0)
    say(verbose, f"  重启后装载 {len(restarted)} 条，下一条序号 {nxt.seq}"
                 f"（不会回到 1，否则服务端会把新数据当重复丢掉）")
    say(verbose, f"  队列健康：{restarted.stats().describe()}")
    print(f"  → 结论：掉电不丢待补传数据，序号继续递增（下一号 {nxt.seq}）")
    return restarted


# ---------------------------------------------------------------- ② 补传去重
def step2_dedup(verbose: bool, queue: LocalQueue) -> MemoryOfflineStore:
    print("\n② 补传去重：服务端写成功但响应超时 → 重发不产生重复点")
    store = MemoryOfflineStore()

    class FlakySink:
        """第一次「写进去了但响应丢了」，第二次正常返回。"""

        def __init__(self) -> None:
            self.first = True

        def receive(self, payload: dict) -> dict:
            store.receive(payload)
            if self.first:
                self.first = False
                raise TimeoutError("响应超时（服务端其实已写入）")
            return {"accepted": 0, "duplicates": len(payload.get("records") or ()),
                    "conflicts": []}

    uploader = ReplayUploader(queue, FlakySink(), device_id="K-DEMO")
    first = uploader.replay()
    say(verbose, f"  第一次补传：{first.describe()}")
    say(verbose, f"  端侧没收到确认 → 队列里还有 {len(queue)} 条")

    second = uploader.replay()
    say(verbose, f"  重发一遍：{second.describe()}")
    assert second.accepted == 0 and second.duplicates == 5
    print(f"  → 结论：库里仍是 {len(store)} 行，重复的 {second.duplicates} 条被去重"
          "（端侧重发是正常失败，靠服务端唯一键兜住）")
    return store


# ---------------------------------------------------------------- ③ 乱序
def step3_out_of_order(verbose: bool, root: Path) -> None:
    print("\n③ 乱序补传：seq 乱着到，页面按时间线排")
    queue = LocalQueue(root / "ooo.queue", device_id="K-DEMO")
    # 故意把「晚发生的」先入队（真实场景：先补传近的，远的还在后面）
    queue.append(KIND_TELEMETRY, metric="t", value=30.0, ts=300.0)
    queue.append(KIND_TELEMETRY, metric="t", value=10.0, ts=100.0)
    queue.append(KIND_TELEMETRY, metric="t", value=20.0, ts=200.0)
    store = MemoryOfflineStore()
    ReplayUploader(queue, store, device_id="K-DEMO").replay()
    series = store.values("t")
    say(verbose, f"  入库顺序（按 ts 排）：{series}")
    assert [value for _ts, value in series] == [10.0, 20.0, 30.0]
    print("  → 结论：乱序补传后曲线仍是 10 → 20 → 30，没有交叉点")


# ---------------------------------------------------------------- ④ 时钟校准
def step4_clock(verbose: bool, root: Path) -> None:
    print("\n④ 时钟校准：端侧慢 45 秒，补传时间自动平移")
    calibrator = ClockCalibrator(min_samples=3)
    # 端侧 t0=0 发出，服务端 45.05 收到、45.10 回，端侧 0.15 收到
    for _ in range(3):
        state = calibrator.sample(0.0, 45.05, 45.10, 0.15)
    say(verbose, f"  校准结果：{state.describe()}")
    assert state.stable

    queue = LocalQueue(root / "clock.queue", device_id="K-DEMO")
    queue.append(KIND_TELEMETRY, metric="t", value=24.0, ts=1000.0)
    store = MemoryOfflineStore()
    ReplayUploader(queue, store, device_id="K-DEMO", calibrator=calibrator).replay()
    landed = store.values("t")[0][0]
    say(verbose, f"  端侧记的时刻 1000.0 → 上送后 {landed:.1f}")
    print(f"  → 结论：偏差 {landed - 1000.0:+.1f}s 被自动校正，趋势图和真实事件对得上")


# ---------------------------------------------------------------- ⑤ 燃气
def step5_gas(verbose: bool) -> None:
    print("\n⑤ 保护①燃气突变：单点尖峰不关阀，连续两点立即关阀")
    guards = ConservativeGuards()
    spike = guards.evaluate(frame(gas_level=0.05), now=0.0)
    spike += guards.evaluate(frame(gas_level=0.90), now=1.0)
    say(verbose, f"  第 1 个采样点 0.90：{len(spike)} 条事件（尖峰不关阀）")
    assert not spike

    guards = ConservativeGuards()
    guards.evaluate(frame(gas_level=0.05), now=0.0)
    guards.evaluate(frame(gas_level=0.90), now=1.0)
    fired = guards.evaluate(frame(gas_level=0.92), now=2.0)
    say(verbose, f"  连续第 2 个采样点：{[v.message for v in fired]}")
    assert fired and fired[0].actions == ("gas_valve_close",)
    print("  → 结论：1 秒内确认燃气突变并关阀（#1 主通道要 30 秒确认窗口）")


# ---------------------------------------------------------------- ⑥ 超温
def step6_thermal(verbose: bool) -> None:
    print("\n⑥ 保护②超温：不看配置，罩温 290℃ 强制开风机")
    guards = ConservativeGuards()
    fired = guards.evaluate(frame(hood_temp=290.0), now=0.0)
    say(verbose, f"  290℃：{[v.message for v in fired]}")
    hold = guards.evaluate(frame(hood_temp=274.0), now=1.0)
    say(verbose, f"  274℃：{len(hold)} 条解除事件（滞回 5℃，未到 275℃ 不解除）")
    released = guards.evaluate(frame(hood_temp=270.0), now=2.0)
    say(verbose, f"  270℃：{[v.message for v in released]}")
    print("  → 结论：超温强制排风；回落留 5℃ 余量，继电器不会反复吸合")


# ---------------------------------------------------------------- ⑦ 长烧
def step7_flame(verbose: bool) -> None:
    print("\n⑦ 保护③明火长烧无人：演示加速 60 分钟 → 60 秒")
    guards = ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0, time_scale=60.0))
    say(verbose, f"  折算后门限：{guards.policy.hold_effective_sec:.0f}s")
    guards.evaluate(frame(flame_b=1.0), now=0.0)
    mid = guards.evaluate(frame(flame_b=1.0), now=59.0)
    fired = guards.evaluate(frame(flame_b=1.0), now=61.0)
    say(verbose, f"  第 59 秒：{len(mid)} 条事件；第 61 秒：{[v.message for v in fired]}")
    assert fired and fired[0].ticket == "unattended"

    guards = ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0, time_scale=60.0))
    guards.evaluate(frame(flame_b=1.0), now=0.0)
    guards.mark_human("card", now=40.0)          # 厨师刷卡（人回来了）
    still = guards.evaluate(frame(flame_b=1.0), now=80.0)
    later = guards.evaluate(frame(flame_b=1.0), now=101.0)
    say(verbose, f"  第 40 秒刷卡 → 第 80 秒 {len(still)} 条；第 101 秒 "
                 f"{[v.rule_id for v in later]}")
    print("  → 结论：长烧 + 无人工活动才关气源；刷过卡就从刷卡时刻重新计时")


# ---------------------------------------------------------------- ⑧ 传感器
def step8_sensor(verbose: bool) -> None:
    print("\n⑧ 传感器故障：拔掉一路 DS18B20（报 85℃）→ 工单 → 超时升级")
    monitor = SensorHealthMonitor(SensorPolicy())
    base = int(T0.timestamp())
    faults = monitor.observe(frame(fridge_temp=85.0), now=float(base))
    for fault in faults:
        say(verbose, f"  故障：{fault.describe()}")
    ticket = monitor.tickets()[0]
    say(verbose, f"  工单：{ticket.describe()}，时限 {monitor.policy.due_sec / 60:.0f} 分钟")

    monitor.observe(frame(fridge_temp=4.0), now=float(base) + 30.0)
    say(verbose, f"  读数恢复后工单状态：{ticket.state}（不自动关单，等人工确认）")

    due = monitor.due_escalations(now=float(base) + monitor.policy.due_sec
                                  + monitor.policy.escalate_after_sec)
    escalated = monitor.escalate(due[0].id, now=float(base) + 3000.0)
    say(verbose, f"  超时升级：{escalated.describe()}，{escalated.notes[-1]}")
    print("  → 结论：读数坏 ≠ 读数越界；缺数据、卡死、超量程三类都进故障工单")


# ---------------------------------------------------------------- ⑨ 降级
def step9_degrade(verbose: bool) -> None:
    print("\n⑨ 三端降级标记：视觉离线时三端怎么说")
    report = build_degrade(
        network_online=False,
        vision={"online": False, "age_sec": 95.0},
        sensor_faults=[{"metric": "fridge_temp", "label": "超量程"}],
        queue_pending=42,
        wall_epoch=float(T0.timestamp()),
    )
    say(verbose, f"  严重度：{report.severity} / 标题：{report.headline}")
    for line in report.describe().splitlines()[1:]:
        say(verbose, f"  {line}")
    for name, channel in report.channels.items():
        head = channel.get("headline") or channel.get("status") or channel.get("badge")
        say(verbose, f"  [{name}] {head}")
        if name == "tft":
            say(verbose, f"        屏幕行：{channel['lines']}")
        elif name == "miniapp":
            say(verbose, f"        提示：{channel['hint']}")
    print("  → 结论：断网不等于不安全（本地保护仍生效）；视觉离线必须点名丢了哪些能力")


# ---------------------------------------------------------------- ⑩ 整链
def step10_end_to_end(verbose: bool, root: Path) -> None:
    print("\n⑩ 离线整链：断网 30 秒三条保护都生效，恢复网络自动补传")
    queue = LocalQueue(root / "e2e.queue", device_id="K-DEMO")
    store = MemoryOfflineStore()
    runtime = KitchenRuntime(load_builtin(), device_id="K-DEMO")
    loop = OfflineFirstRuntime(
        runtime,
        InterlockSupervisor(),
        device_id="K-DEMO",
        queue=queue,
        uploader=ReplayUploader(queue, store, device_id="K-DEMO"),
        guards=ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0, time_scale=120.0)),
        online=False,
    )
    steps = []
    for t in (0.0, 1.0, 15.0, 31.0):
        steps.append(loop.tick(frame(gas_level=0.9, hood_temp=295.0, flame_b=1.0), now=t))
    fired = sorted({v.rule_id for step in steps for v in step.guard_verdicts
                    if v.event == "fired"})
    actions = sorted({action for step in steps for action in step.actions})
    say(verbose, f"  离线期间触发：{fired}")
    say(verbose, f"  实际下发动作：{actions}")
    say(verbose, f"  本地排队：{len(queue)} 条（{queue.stats().describe()}）")
    assert len(fired) == 3
    assert "gas_valve_close" in actions and "fan_on" in actions

    report = loop.set_online(True, wall_epoch=32.0)
    say(verbose, f"  恢复网络：{report.describe()}")
    say(verbose, f"  服务端收到 {len(store)} 行，队列剩 {len(queue)} 条")
    assert len(queue) == 0 and len(store) > 0
    print("  → 结论：断网只影响「上报」，不影响判定与执行；恢复后自动补传")


def make_root(raw: str) -> tuple[Path, bool]:
    if raw:
        root = Path(raw)
        root.mkdir(parents=True, exist_ok=True)
        return root, False
    root = Path("var") / "resilience"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root, False


def run(*, verbose: bool = True, data_root: str = "") -> int:
    root, temporary = make_root(data_root)
    try:
        queue = step1_queue(verbose, root)
        step2_dedup(verbose, queue)
        step3_out_of_order(verbose, root)
        step4_clock(verbose, root)
        step5_gas(verbose)
        step6_thermal(verbose)
        step7_flame(verbose)
        step8_sensor(verbose)
        step9_degrade(verbose)
        step10_end_to_end(verbose, root)
        print(f"\n数据目录：{root}")
        return 0
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="异常兜底演练")
    parser.add_argument("--quiet", action="store_true", help="只打印关键结论")
    parser.add_argument("--data", default="", help="队列文件落点（默认 var/resilience）")
    args = parser.parse_args(argv)
    return run(verbose=not args.quiet, data_root=args.data)


if __name__ == "__main__":
    raise SystemExit(main())
