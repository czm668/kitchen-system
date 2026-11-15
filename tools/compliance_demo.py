# -*- coding: utf-8 -*-
"""食安合规闭环演练（A4-05~A4-09）：留样 → 48h 销毁 → 消毒/复消 → 溯源标签 → 派单改派。

## 演示什么

六段，每段都走「真实会出错的分支」，不是只跑通顺路：

1. **应留样清单**：按当日批次自动列出该留的品种（漏留是稽查最常见的失分项）；
2. **扫码称重**：扫溯源码 → 118g 被拒（差 7g）→ 补到 132g 达标落库；
3. **48h 销毁**：`time_scale=2880` 把两天压成一分钟，到期自动开单 + 推送；
4. **消毒门控 + 曲线**：非消毒时段喂 25℃ **不报警**（这条是 #15 的命门）；
   开会话后 120℃ 累计 10 分钟（演示 30 秒）自动达标关单；
5. **不达标 → 复消**：160℃ 立即异常超温 → 禁出库 + 复消工单 → 复消达标关单解禁；
6. **溯源与标签**：扫码一屏（批次/供应商/检测记录）+ 二维码字符预览；
7. **派单改派**：指定一个「资质在册但健康证过期」的人 → 自动改派。

## 用法

```
python tools/compliance_demo.py
python tools/compliance_demo.py --quiet          # 只打印关键结论
python tools/compliance_demo.py --png out.png    # 把溯源码标签写到文件
```
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kitchen.domain import CST                                      # noqa: E402
from kitchen.process import (                                       # noqa: E402
    DEMO_TIME_SCALE,
    DispatchBoard,
    MemoryProcessRepository,
    OrderService,
    RetentionService,
    TraceService,
)
from kitchen.process.disinfection import DisinfectionPolicy, DisinfectionService  # noqa: E402
from kitchen.staff import (                                          # noqa: E402
    AdmissionController,
    MemoryStaffRepository,
    StaffMatcherAdapter,
)

T0 = datetime(2026, 11, 12, 20, 0, tzinfo=CST)      # 晚餐后：消毒时段的开始
TRACE_CODE = "TC-DEMO-0001"


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def title(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser(description="食安合规闭环演练（#14/#15/#6/溯源/派单）")
    parser.add_argument("--quiet", action="store_true", help="只打印关键结论")
    parser.add_argument("--png", default="", help="把溯源码标签 PNG 写到该路径")
    args = parser.parse_args()
    verbose = not args.quiet
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    clock = Clock()
    repo = MemoryProcessRepository.from_seed()

    # ---------------------------------------------------------------- ① 应留样
    title("① #14 应留样清单（按当日批次自动生成）")
    retention = RetentionService(repo, clock=clock, time_scale=2880)
    for target in retention.plan():
        print(f"  应留样：{target.describe()}")
    print(f"  未完成：{[item.batch_no for item in retention.pending()]}")

    # ---------------------------------------------------------------- ② 扫码称重
    title("② #14 扫码 → 称重 125g 门槛")
    scanned = retention.resolve(TRACE_CODE)
    print(f"  扫码 {TRACE_CODE} → {scanned.describe()}")
    short = retention.weigh(scanned, 118, staff_id=1)
    print(f"  118g：{short.describe()}")
    ok = retention.weigh(scanned, 132, staff_id=1)
    print(f"  132g：{ok.describe()}")
    sample = retention.sample_of(1)
    print(f"  落库：{sample.describe()}")

    # ---------------------------------------------------------------- ③ 48h 销毁
    title("③ #6 留样满 48 小时 → 销毁任务（演示加速 48h→1min）")
    clock.advance(30)
    print(f"  30 秒后：销毁任务 {len(retention.due_for_destroy())} 张（未到期）")
    clock.advance(32)
    tasks = retention.due_for_destroy()
    for task in tasks:
        print(f"  到期：{task.describe()}")
        for notice in task.notices:
            print(f"  推送[{notice['channel']}]：{notice['title']}｜{notice['body']}")
    retention.destroy(tasks[0].id, staff_id=3)
    print(f"  销毁登记后：{retention.sample_of(1).describe()}")

    # ---------------------------------------------------------------- ④ 消毒曲线
    title("④ #15 消毒会话门控 + 达标曲线（600s 压成 30 秒）")
    disinfection = DisinfectionService(
        repo, policy=DisinfectionPolicy(time_scale=DEMO_TIME_SCALE), clock=clock)
    print(f"  非消毒时段喂 25℃：{disinfection.feed(25.0)}（被门控丢弃，不报警）")
    print(f"  已丢弃读数：{disinfection.ignored}")
    disinfection.start(batch_id=1, staff_id=3)
    for _ in range(15):                      # 15 × 2s × 20 = 600 虚拟秒
        clock.advance(2)
        reading = disinfection.feed(120.0, staff_id=3)
    print(f"  120℃ 累计：{reading.describe()}")
    print(f"  会话状态：{disinfection.records[0].describe()}")

    # ---------------------------------------------------------------- ⑤ 不达标与复消
    title("⑤ #15 异常超温 → 复消工单 → 禁出库 → 复消达标关单")
    disinfection.start(batch_id=1, session_uid="DI-BAD", staff_id=3)
    clock.advance(2)
    bad = disinfection.feed(160.0, staff_id=3)
    print(f"  160℃：{bad.describe()}")
    ticket = disinfection.tickets[0]
    print(f"  工单：{ticket.describe()}")
    print(f"  该批禁止出库：{disinfection.forbid_outbound(batch_id=1)}")
    disinfection.start_rework(ticket.id, staff_id=3)
    for _ in range(15):
        clock.advance(2)
        disinfection.feed(118.0, staff_id=3)
    print(f"  复消后：{disinfection.ticket(ticket.id).describe()}")
    print(f"  该批禁止出库：{disinfection.forbid_outbound(batch_id=1)}")

    # ---------------------------------------------------------------- ⑥ 溯源标签
    title("⑥ A4-08 扫码溯源 + 二维码标签")
    trace = TraceService(repo)
    view = trace.view(TRACE_CODE)
    print(f"  溯源：{view.describe()}")
    print(f"  检测记录：{view.inspection}")
    print(f"  出库门禁：{trace.outbound_blockers(TRACE_CODE) or '（无阻塞，可出库）'}")
    print(f"  二维码内容：{view.qr_url}")
    if verbose:
        label = __import__("kitchen.process.qr", fromlist=["qr"]).encode(view.qr_url)
        print(f"  版本 v{label.version} / 掩码 {label.mask} / {label.size}×{label.size} 模块")
        print(trace.label_ascii(TRACE_CODE))
    if args.png:
        path = Path(args.png)
        path.write_bytes(trace.label_png(TRACE_CODE))
        print(f"  标签已写出：{path}（{path.stat().st_size} 字节）")

    # ---------------------------------------------------------------- ⑦ 派单改派
    title("⑦ A4-09 派单与资质联调（资质在册但证过期 → 自动改派）")
    orders = OrderService(repo, clock=clock)
    staff_repo = MemoryStaffRepository.from_seed()
    admission = AdmissionController(staff_repo)
    for card in ("A1B2C3D4", "C9D0E1F2"):           # 张三 / 王组长晨检通过
        admission.submit_morning(card, {
            "temp_c": 36.4, "diarrhea": False,
            "pharyngitis": False, "hand_wound": False,
        }, now=clock())
    board = DispatchBoard(repo, orders, clock=clock,
                          matcher=StaffMatcherAdapter(admission, clock=clock))
    order = orders.place_order(table_id=1, items=[("D-QJRS", 1)])
    item = order.items[0]
    decisions = board.dispatch_pending(staff_plan={item.id: 2})   # 指定李四（证过期）
    name = {staff.id: staff.name for staff in staff_repo.all_staff()}
    decision = decisions[0]
    who = name.get(decision.staff_id, "?") if decision.staff_id else "未指派"
    moved = (f"（原定 {name.get(decision.reassigned_from, decision.reassigned_from)} "
             f"被改派）" if decision.reassigned else "")
    print(f"  {decision.describe()}")
    print(f"  结论：{decision.order_no}/{decision.dish} → ST{decision.station_id} "
          f"由 {who} 负责{moved}")
    print(f"  指派表：{board.assigned_staff()}")
    if verbose:
        print(f"  候选集：{[staff.name for staff in admission.candidates('stove', now=clock())]}")

    print("\n演练完成：留样、销毁、消毒、复消、溯源、派单六条链均可复现。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
