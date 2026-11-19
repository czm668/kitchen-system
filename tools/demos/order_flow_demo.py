# -*- coding: utf-8 -*-
"""后厨一条单的全链路演练（A4）：下单 → 派单 → 上灶 → 称重 → SOP → 出餐。

## 演示什么

九段，覆盖 A4 已落地的四个验收点，每一段都刻意留一个「真实会出错的分支」：

1. **下单**：点三道菜，其中「清炒时蔬」今天已沽清 → 整单拒绝并**列出全部**沽清项；
2. **建单**：改点两道热菜，交期按最慢那道菜算（13 分钟，不是平均）；
3. **派单**：两单抢两个灶，交期近的先派；第三单**排队**并给出位置；
4. **上灶**：A 灶刷卡上灶并点火 → 确认；B 灶刷了卡但**没点火** → 30 秒后报出来；
5. **称重**：猪里脊多放 10% → 锁下一步；补足 150g → 解锁（`process_events` 留痕）；
6. **SOP**：跳到第 3 步被拒（下一步是第 2 步）；按序刷完六步；
7. **出餐门禁**：故意只确认到第 5 步就点出餐 → 被拦，且**一次列全**原因；
8. **出餐 + 释放**：补完最后一步 → 出餐到 `ready`，出餐释放灶、上桌完成；
9. **排风**：SCT-013 电流判「在转 / 停转 / 无读数」，并说明为什么不参与派单。

## 用法

```
python tools/demos/order_flow_demo.py
python tools/demos/order_flow_demo.py --quiet     # 只看关键结论
```
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kitchen.domain import CST                                       # noqa: E402
from kitchen.process import (                                       # noqa: E402
    CookingSession,
    DispatchBoard,
    MemoryProcessRepository,
    NotReadyError,
    OrderService,
    SoldOutError,
    StepLockedError,
    WeighError,
)
from kitchen.process.orders import replay_events                    # noqa: E402

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


class Clock:
    """演练时钟：秒级推进，交期与事件时间都从它来。"""

    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def stamp(self) -> str:
        return self._now.strftime("%H:%M:%S")


def line(kind: str, text: str) -> str:
    marks = {"step": "▶", "ok": "  ✓", "deny": "  ✗", "info": "  ·", "warn": "  !"}
    return f"{marks.get(kind, '  ')} {text}"


def main() -> int:
    parser = argparse.ArgumentParser(description="后厨一条单的全链路演练（A4）")
    parser.add_argument("--quiet", action="store_true", help="只输出阶段标题与结论")
    args = parser.parse_args()
    verbose = not args.quiet

    # 演练自己会把每一步打出来；模块日志再放出来就是两套并行叙事，
    # 而且 logging 即时刷 stderr、demo 攒到最后一次 print，行会跑到最上面。
    logging.getLogger("kitchen").setLevel(logging.ERROR)

    clock = Clock()
    repo = MemoryProcessRepository.from_seed()
    svc = OrderService(repo, clock=clock)
    board = DispatchBoard(repo, svc, clock=clock)

    out: list[str] = []
    out.append(f"后厨过程演练 · {T0:%Y-%m-%d} · 工位 ST-A / ST-B（同一采集节点）")

    # ---------------------------------------------------------- ① 下单命中沽清
    out.append(line("step", "① 11:30 服务员下单：青椒肉丝 + 清炒时蔬"))
    try:
        svc.place_order(table_id=1, items=[("D-QJRS", 1), ("D-QGD", 1)])
    except SoldOutError as exc:
        out.append(line("deny", str(exc)))
        out.append(line("info", "整单拒绝并列出全部沽清项——只报第一道会让顾客改一轮再被拒一轮"))
    out.append(line("info", "「清炒时蔬」种子数据里 sold_out=1，且 enabled 仍为 1："
                           "沽清不等于停售，退款流程还要查得到这个菜名"))

    # ---------------------------------------------------------- ② 建单
    out.append(line("step", "② 11:30 改点青椒肉丝 + 宫保鸡丁"))
    order = svc.place_order(table_id=1, items=[("D-QJRS", 1), ("D-GBJD", 1)])
    lead = (order.promised_at - order.created_at).total_seconds() / 60
    out.append(line("ok", f"{order.describe()}"))
    out.append(line("info", f"交期 {order.promised_at:%H:%M}（按最慢那道菜 {lead:.0f} 分钟，"
                           f"不按平均——一桌菜要一起上）"))
    out.append(line("info", f"配方已快照：子项 recipe_id={order.items[0].recipe_id}"
                           f"（之后改配方不影响在做的单）"))

    # ---------------------------------------------------------- ③ 派单
    clock.advance(60)
    out.append(line("step", "③ 11:31 再进两桌：B 桌青椒肉丝、C 桌宫保鸡丁"))
    order_b = svc.place_order(table_id=2, items=[("D-QJRS", 1), ("D-GBJD", 1)])
    clock.advance(60)
    order_c = svc.place_order(table_id=3, items=[("D-GBJD", 1)])
    out.append(line("info", f"A 桌交期 {order.promised_at:%H:%M}、"
                           f"B 桌 {order_b.promised_at:%H:%M}、C 桌 {order_c.promised_at:%H:%M}"))
    for decision in board.dispatch_pending():
        out.append(line("ok" if decision.ok else "warn", decision.describe()))

    # ---------------------------------------------------------- ④ 上灶 + 点火
    clock.advance(20)
    out.append(line("step", "④ 11:32 张三在 A 灶刷工牌上灶并开火；王组长在 B 灶刷了卡但没点火"))
    # A 桌两道菜正好占满两个灶；B、C 桌排队——营业高峰这是常态，不是错误
    first, second = order.items
    board.occupy(1, staff_id=1, item_id=first.id)
    board.occupy(2, staff_id=3, item_id=second.id)
    board.observe_flame(1, 0.93)
    out.append(line("ok", board.flame_check(1).describe()))
    out.append(line("info", board.flame_check(2).describe() + "（窗口期内，暂不告警）"))

    for _ in range(3):
        clock.advance(10)
    for verdict in board.pending_flame_checks():
        out.append(line("warn", verdict.describe()))
        out.append(line("info", "这种灶在系统里显示 busy、实际是空的——后面的单被白白排队"))

    # ---------------------------------------------------------- ⑤ 称重
    clock.advance(5)
    out.append(line("step", "⑤ 11:33 配菜：A 灶这单先称猪里脊（目标 150g）"))
    session = CookingSession(repo, svc, first.id, session_uid="S-0001", clock=clock)
    check = session.weigh("猪里脊", 165.0, staff_id=1)
    out.append(line("deny" if not check.ok else "ok", check.describe() + " —— " + check.detail))
    try:
        session.confirm(1, staff_id=1)
    except StepLockedError as exc:
        out.append(line("deny", f"下一步被锁：{exc}"))
    fixed = session.weigh("猪里脊", 150.0, staff_id=1)
    out.append(line("ok", "补足后重称：" + fixed.describe()))
    out.append(line("info", "生抽只有 8g，5% 容差是 0.4g——实际按 max(百分比, 1g) 判，"
                           "否则 HX711 抖一下就锁，现场只能把容差改成 50%"))
    # 称重要照做，quiet 只影响打印：跳过称重会让后面的出餐门禁直接拦住，
    # 于是 --quiet 模式跑出一个「不该出现的失败」。
    for name, grams in (("青椒", 100.0), ("食用油", 20.0), ("生抽", 8.0)):
        row = session.weigh(name, grams, staff_id=1)
        if verbose:
            out.append(line("ok" if row.ok else "deny", row.describe()))

    # ---------------------------------------------------------- ⑥ SOP
    out.append(line("step", "⑥ 11:34 SOP 分步确认（6 步，其中 3 个关键控制点）"))
    try:
        session.confirm(3, staff_id=1)
    except WeighError as exc:
        out.append(line("deny", f"跳步被拒：{exc}"))
    for step in session.steps[:5]:
        result = session.confirm(step.step_no, staff_id=1)
        if verbose or result.ccp:
            out.append(line("ok", result.describe()))

    # ---------------------------------------------------------- ⑦ 出餐门禁
    out.append(line("step", "⑦ 11:35 师傅直接点「出餐」（第 6 步还没确认）"))
    try:
        session.serve(staff_id=1)
    except NotReadyError as exc:
        out.append(line("deny", str(exc)))
        out.append(line("info", "一次列全而不是遇到第一个就抛——逐条抛会让师傅"
                               "「补一样、被拒一次」循环"))

    # ---------------------------------------------------------- ⑧ 出餐 + 释放
    out.append(line("step", "⑧ 11:36 补确认第 6 步（出锅中心温度 ≥75℃）后出餐"))
    last = session.steps[-1]
    out.append(line("ok", session.confirm(last.step_no, staff_id=1).describe()))
    session.serve(staff_id=1)
    out.append(line("ok", f"出餐：子项 → {session.item.state.value}"
                         f"（只推到 ready，上桌是传菜的动作）"))
    board.close(1, reason="出餐释放")
    svc.serve(first.id)
    out.append(line("ok", f"A 灶已释放，子项 {first.state.value}，"
                         f"占用的 station_id={first.station_id}"))
    for decision in board.dispatch_pending():
        out.append(line("ok" if decision.ok else "warn",
                        "空出灶后再扫一轮：" + decision.describe()))
    replayed = replay_events(svc.events(order.id))
    out.append(line("info", f"事件流重放 {len(svc.events(order.id))} 条 → "
                           f"{replayed['state'].value}，与内存状态一致："
                           f"{replayed['items'] == {i.id: i.state for i in order.items}}"))

    # ---------------------------------------------------------- ⑨ 排风
    out.append(line("step", "⑨ 排风监测（SCT-013 采集风机回路电流）"))
    out.append(line("info", board.exhaust_state().describe() + "（没读数就说不知道）"))
    board.observe_exhaust(0.62)
    out.append(line("ok", board.exhaust_state().describe()))
    board.observe_exhaust(0.03)
    out.append(line("warn", board.exhaust_state().describe()))
    out.append(line("info", "排风停转是 #2 排烟积热的判据，走声光与联锁；"
                           "派单层再拦一次会让整条线派不出单，那比积热更危险"))

    print("\n".join(out))
    print()
    print(f"工位视图：{board.dispatch_summary()['stations']}")
    print(f"待派队列：{[item.describe() for item in board.queue()]}")
    print(f"process_events：{len(session.events)} 条（kind="
          f"{sorted({event.kind for event in session.events})}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
