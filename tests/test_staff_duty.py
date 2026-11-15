# -*- coding: utf-8 -*-
"""#11 未授权上岗告警：在场积分、5 秒去抖、只开一张单。

这组测试盯的是「一上线就被拔喇叭」的形态：

- 没有去抖 → 人正要去填问卷，喇叭已经在响；
- 按分钟重报 → 工单里同一问题刷十条，人开始忽略工单（A2 闩锁踩过）；
- 告警动作里带 `power_cut` → 把整个工位断电，#3 才有资格动的阀被顺手关了。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from kitchen.staff import (
    AdmissionController,
    DutyGuard,
    MemoryStaffRepository,
    TICKET_UNAUTHORIZED,
)
from kitchen.staff.models import CST, DenyReason, MorningAnswers

DAY = date(2026, 11, 12)
AT_9AM = datetime(2026, 11, 12, 9, 0, tzinfo=CST)
ZHANG = 1       # 有排班、证有效
LI = 2          # 证过期、无排班
LEAD = 3        # 有排班、证有效


class Mono:
    """单调时钟：只用来算时长，与「今天」无关。"""

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> float:
        self.t += float(delta)
        return self.t


def make(*, grace: float = 5.0):
    repo = MemoryStaffRepository.from_seed(today=DAY)
    ctrl = AdmissionController(repo)
    mono = Mono()
    guard = DutyGuard(ctrl, grace_sec=grace, clock=mono, wall_clock=lambda: AT_9AM)
    return repo, ctrl, guard, mono


def run(guard, mono, seconds: float, step: float = 1.0):
    """按秒 tick，收集全部告警。"""
    alerts = []
    for _ in range(int(round(seconds / step))):
        mono.advance(step)
        alerts.extend(guard.tick())
    return alerts


# ================================================================ 去抖
class TestDebounce:
    def test_未晨检上工位满五秒才报(self):
        _repo, ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        assert run(guard, mono, 4) == []          # 4 秒：还在去抖窗口内
        alerts = run(guard, mono, 2)              # 第 5 秒起报
        assert len(alerts) == 1
        assert alerts[0].reasons == (DenyReason.MORNING_MISSING,)
        assert alerts[0].staff_name == "张三"

    def test_补了晨检就不报(self):
        _repo, ctrl, guard, mono = make()
        ctrl.submit_morning("A1B2C3D4", MorningAnswers(temp_c=36.4), now=AT_9AM)
        guard.presence(ZHANG, station_id=1)
        assert run(guard, mono, 30) == []

    def test_同一违规只报一次(self):
        """工单里刷十条等于没有工单。"""
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        alerts = run(guard, mono, 60)
        assert len(alerts) == 1

    def test_离开工位后不再计(self):
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        run(guard, mono, 3)
        guard.leave(ZHANG)
        assert run(guard, mono, 20) == []
        assert guard.on_duty() == ()

    def test_回到工位重新计时(self):
        """离开再回来要重新数满 5 秒——否则「闪一下就跑」也能刷出工单。"""
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        run(guard, mono, 3)
        guard.leave(ZHANG)
        guard.presence(ZHANG, station_id=1)
        assert run(guard, mono, 4) == []
        assert len(run(guard, mono, 2)) == 1

    def test_换工位重新计时(self):
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        run(guard, mono, 4)
        guard.presence(ZHANG, station_id=2)        # 换岗：资质要求可能不同，重新判
        assert run(guard, mono, 3) == []

    def test_清空工位(self):
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        guard.presence(LEAD, station_id=1)
        assert sorted(guard.clear_station(1)) == [1, 3]
        assert guard.on_duty() == ()


# ================================================================ 违规来源
class TestReasons:
    def test_资质不符也算未授权上岗(self):
        repo, ctrl, guard, mono = make()
        ctrl.submit_morning("A1B2C3D4", MorningAnswers(temp_c=36.4), now=AT_9AM)
        repo.station_requirements[1] = "sample"
        guard.presence(ZHANG, station_id=1)
        alerts = run(guard, mono, 8)
        assert alerts[0].reasons == (DenyReason.QUALIFICATION_MISS,)

    def test_证过期的人上工位会报(self):
        repo, _ctrl, guard, mono = make()
        repo.add_shift(staff_id=LI, station_id=1, shift_date=DAY, shift_no=2)
        guard.presence(LI, station_id=1)
        alerts = run(guard, mono, 8)
        assert alerts[0].reasons == (DenyReason.CERT_EXPIRED,)

    def test_晨检不合格的人上工位会报(self):
        _repo, ctrl, guard, mono = make()
        ctrl.submit_morning("A1B2C3D4", MorningAnswers(temp_c=38.5), now=AT_9AM)
        guard.presence(ZHANG, station_id=1)
        alerts = run(guard, mono, 8)
        assert alerts[0].reasons == (DenyReason.MORNING_FAILED,)

    def test_复核放行后停止告警(self):
        """组长复核提交的那一刻，缓存可能还是旧值——所以判定每帧都重算。"""
        _repo, ctrl, guard, mono = make()
        ctrl.submit_morning("A1B2C3D4", MorningAnswers(temp_c=38.5), now=AT_9AM)
        guard.presence(ZHANG, station_id=1)
        assert len(run(guard, mono, 8)) == 1
        ctrl.review(ZHANG, reviewer_card_uid="C9D0E1F2", web_confirmed=True, now=AT_9AM)
        guard.tick()
        assert guard.snapshot()["on_duty"]["1"]["alerted"] is False


# ================================================================ 告警内容
class TestAlertShape:
    def test_动作只有声光语音(self):
        """#11 只管任务重排与提醒，#3 才动燃气阀；切电更是 CH3 的专职。"""
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        alert = run(guard, mono, 8)[0]
        assert alert.actions == ("alarm_light_on", "voice_play")
        assert alert.ticket == TICKET_UNAUTHORIZED

    def test_描述带上工位与原因(self):
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        text = run(guard, mono, 8)[0].describe()
        assert "工位1" in text and "张三" in text and "晨检" in text

    def test_快照能看出谁在违规(self):
        _repo, _ctrl, guard, mono = make()
        guard.presence(ZHANG, station_id=1)
        run(guard, mono, 6)
        snap = guard.snapshot()["on_duty"]["1"]
        assert snap["alerted"] is True
        assert snap["reasons"] == ["morning_missing"]

    def test_grace为零时立即报(self):
        _repo, _ctrl, guard, mono = make(grace=0.0)
        guard.presence(ZHANG, station_id=1)
        assert len(guard.tick()) == 1
