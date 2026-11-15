# -*- coding: utf-8 -*-
"""人员准入演练（A3）：一次真实的早班进场流程。

## 演示什么

按时间顺序走一遍后厨进场，七个阶段覆盖 A3 的全部验收点：

1. **开场扫证**：李四的健康证 6 月就过期了，APScheduler 每日扫描把他今天的
   排班置为 blocked——人还没到，系统已经知道他不能上灶；
2. **张三刷卡**：证有效、有排班，但今天没晨检 → TFT 弹问卷（不是干巴巴拒绝）；
3. **填问卷**：体温 36.5，四项全过 → 准入通过，能领任务了；
4. **李四刷卡**：证过期是硬门禁，**连问卷都不给填**——填了也进不去；
5. **张三第二次晨检不合格**（体温 38.2）→ 当日锁定，声光拦截；
6. **组长复核**：先只有刷卡 → 被拒（缺管理端二次确认）；补上 Web 确认 → 放行，
   `channel='tft+web'` 双通道留痕；
7. **视觉发现李四在工位** → 5 秒后未授权上岗告警 + 工单。

## 用法

```
python tools/staff_demo.py
python tools/staff_demo.py --quiet    # 只看结论
```
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kitchen.staff import (                                  # noqa: E402
    AdmissionController,
    CertificateSweeper,
    DutyGuard,
    MemoryStaffRepository,
)
from kitchen.staff.models import MORNING_QUESTIONS, MorningAnswers  # noqa: E402

CST = timezone(timedelta(hours=8))
DAY = date(2026, 11, 12)
AT_0730 = datetime(2026, 11, 12, 7, 30, tzinfo=CST)
AT_0731 = datetime(2026, 11, 12, 7, 31, tzinfo=CST)
AT_0732 = datetime(2026, 11, 12, 7, 32, tzinfo=CST)
AT_0735 = datetime(2026, 11, 12, 7, 35, tzinfo=CST)

ZHANG, LI, LEAD = "A1B2C3D4", "E5F6A7B8", "C9D0E1F2"
STATION = 1


class Mono:
    """单调时钟，只用于未授权上岗的去抖积分。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += float(delta)


def line(kind: str, text: str) -> str:
    marks = {"step": "▶", "ok": "  ✓", "deny": "  ✗", "info": "  ·", "warn": "  !"}
    return f"{marks.get(kind, '  ')} {text}"


def main() -> int:
    parser = argparse.ArgumentParser(description="人员准入演练（A3）")
    parser.add_argument("--quiet", action="store_true", help="只输出阶段标题与结论")
    args = parser.parse_args()
    verbose = not args.quiet

    # 演练自己会把每一步打出来；再把模块日志放出来会有两套并行的叙事。
    # 更麻烦的是 demo 把所有行攒起来最后一次打印，而 logging 是即时刷 stderr，
    # 结果就是预警级的几条（复核放行、未授权上岗）跑到最上面，看起来像时序错乱。
    # 要排查时把级别改成 WARNING / INFO，并把下面的 print 换成逐行输出。
    logging.getLogger("kitchen").setLevel(logging.ERROR)

    repo = MemoryStaffRepository.from_seed()
    # 排班表是提前排好的：李四那条班在证过期之后才被扫描发现——先排班，
    # 才能演出「人还没到、系统已经把他今天拦下」这个效果。
    repo.add_shift(staff_id=2, station_id=1, shift_date=DAY, shift_no=2)
    ctrl = AdmissionController(repo)
    mono = Mono()
    guard = DutyGuard(ctrl, grace_sec=5.0, clock=mono, wall_clock=lambda: AT_0735)

    out: list[str] = []

    # ---------------------------------------------------------- ① 每日扫描
    out.append(line("step", "① 07:30 值班前扫描健康证台账（APScheduler 每日一次）"))
    sweep = CertificateSweeper(repo, warn_days=30).run(DAY)
    out.append(line("warn", sweep.describe()))
    for shift_id in sweep.blocked_shifts:
        out.append(line("info", f"排班 #{shift_id} 已置 blocked（人还没到，系统先拦下）"))
    if verbose:
        for staff, cert, days in sweep.expiring_soon:
            out.append(line("info", f"临期提醒：{staff.name} 的 {cert.cert_no} 还有 {days} 天"))

    # ---------------------------------------------------------- ② 张三刷卡
    out.append(line("step", "② 07:31 张三刷工牌（A1B2C3D4）→ 工位 A"))
    result = ctrl.scan(ZHANG, station_id=STATION, now=AT_0731)
    out.append(line("deny" if not result.ok else "ok", result.describe()))
    if result.needs_morning_check:
        out.append(line("info", "TFT 弹晨检问卷（这一种拒绝当场就能解决）"))
        if verbose:
            for key, label, kind in MORNING_QUESTIONS:
                out.append(line("info", f"    {label}" + ("（数值）" if kind == "number" else "（是/否）")))

    # ---------------------------------------------------------- ③ 填问卷
    out.append(line("step", "③ 07:31 填写晨检：体温 36.5℃，无腹泻/咽炎/伤口"))
    outcome = ctrl.submit_morning(ZHANG, MorningAnswers(temp_c=36.5), now=AT_0731)
    out.append(line("ok" if outcome.ok else "deny", outcome.describe()))
    result = ctrl.scan(ZHANG, station_id=STATION, now=AT_0731)
    out.append(line("ok" if result.ok else "deny", "重新刷卡：" + result.describe()))

    # ---------------------------------------------------------- ④ 李四刷卡
    out.append(line("step", "④ 07:32 李四刷工牌（E5F6A7B8）→ 同一个工位"))
    result = ctrl.scan(LI, station_id=STATION, now=AT_0732)
    out.append(line("deny" if not result.ok else "ok", result.describe()))
    for detail in result.details:
        out.append(line("info", detail))
    if result.masked:
        out.append(line("info", "硬门禁：问卷不弹出——填了也进不去，不给假希望"))
        out.append(line("info", "指引：" + result.reasons[0].next_step))

    # ---------------------------------------------------------- ⑤ 晨检不合格
    out.append(line("step", "⑤ 07:33 张三复测体温 38.2℃（红温枪报警）"))
    outcome = ctrl.submit_morning(ZHANG, MorningAnswers(temp_c=38.2), now=AT_0732)
    out.append(line("deny" if not outcome.ok else "ok", outcome.describe()))
    result = ctrl.scan(ZHANG, station_id=STATION, now=AT_0732)
    out.append(line("deny", "刷卡结果：" + result.describe()))
    out.append(line("info", "动作：语音拦截 + 声光 + 管理端锁定当日操作权限"))

    # ---------------------------------------------------------- ⑥ 组长复核
    out.append(line("step", "⑥ 07:35 王组长复核（TFT 刷工牌 + 管理端二次确认）"))
    single = ctrl.review(1, reviewer_card_uid=LEAD, web_confirmed=False, now=AT_0735)
    out.append(line("deny" if not single.ok else "ok", "只刷卡：" + single.describe()))
    double = ctrl.review(
        1, reviewer_card_uid=LEAD, web_confirmed=True,
        reviewer_label="web:王组长", now=AT_0735,
    )
    out.append(line("ok" if double.ok else "deny", "补上 Web 确认：" + double.describe()))
    if double.ok and verbose:
        saved = repo.morning_check(1, DAY)
        out.append(line("info", f"留痕：channel={saved.channel} confirmed_by={saved.confirmed_by}"))
    result = ctrl.scan(ZHANG, station_id=STATION, now=AT_0735)
    out.append(line("ok" if result.ok else "deny", "复核后刷卡：" + result.describe()))

    # ---------------------------------------------------------- ⑦ 未授权上岗
    out.append(line("step", "⑦ 视觉看到李四站在 A 工位（他没通过准入）"))
    guard.presence(2, STATION)
    alerts = []
    for second in range(1, 9):
        mono.advance(1.0)
        alerts.extend(guard.tick())
        if verbose and second <= 6:
            snap = guard.snapshot()["on_duty"]["2"]
            out.append(line("info", f"  +{second}s 违规累计 {snap['deny_sec']}s，已告警={snap['alerted']}"))
    for alert in alerts:
        out.append(line("warn", alert.describe()))
        out.append(line("info", f"动作 {'+'.join(alert.actions)}；工单类型 {alert.ticket}"))

    # ---------------------------------------------------------- 候选集
    out.append(line("step", "⑧ 派单候选（#10 资质匹配，A4 联调）"))
    for name, requirement in (("灶台 stove", "stove"), ("留样 sample", "sample")):
        staff = ctrl.candidates(requirement, now=AT_0735)
        body = "、".join(f"{s.name}({'/'.join(sorted(s.qualifications))})" for s in staff)
        out.append(line("info", f"{name} 候选：{body or '（无）'}"))

    print("\n".join(out))
    print()
    print(f"状态快照：{repo.snapshot()}")
    print(f"在岗守卫：{guard.snapshot()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
