# -*- coding: utf-8 -*-
"""#11 未授权上岗告警：人在工位、但当日不该在工位 → 声光 + 工单。

## 与 #8/#9 的区别：一个是一次事件，一个是持续状态

刷卡被拒是**瞬时**的：人走了就走了，不需要去抖，也不需要「结案」。
「未授权上岗」是**状态**：视觉连续看到工位有人（或工位插座有功率），而这个人
今天没过晨检、证过期、或资质不符。这个状态可能持续整个班次。

所以这里做的是时间积分：`deny_since` 记第一次发现不合法的时间点，满
`grace_sec` 才报。**必须有去抖**，原因很具体：

- 人刚刷完卡、还没做晨检就被视觉识别到 → 现场正要去填问卷，5 秒内就响了；
- 晨检问卷提交与视觉刷新之间有几秒错位（TFT 在 UART 上排队）；
- 组长复核提交的瞬间，缓存可能还是旧值。

没有去抖的版本在联调时表现是「工位一直在响」，然后有人会去把喇叭线拔了。

## 只开一张单

同一人同工位在整个违规期间只报一次（`alerted`）。离开工位或准入恢复 → 清状态，
下次再违规才重新计。**不要按分钟重报**：工单系统里同一个问题刷十条，人就会
开始忽略工单——这正是 A2 阶段闩锁重报踩过的坑。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .admission import AdmissionController
from .models import CST, DenyReason

log = logging.getLogger("kitchen.staff.duty")

__all__ = ["DutyAlert", "DutyGuard", "TICKET_UNAUTHORIZED"]

#: 工单类型（A4 落 `work_orders.type`）
TICKET_UNAUTHORIZED = "unauthorized_on_duty"

#: 违规动作：声光 + 语音。不切电、不关阀——#3 才动燃气阀，离岗/未授权不等于有明火
ALERT_ACTIONS: tuple[str, ...] = ("alarm_light_on", "voice_play")


@dataclass(frozen=True)
class DutyAlert:
    """一次未授权上岗告警。"""

    station_id: int | None
    staff_id: int
    staff_name: str
    reasons: tuple[DenyReason, ...]
    elapsed: float
    details: tuple[str, ...] = ()
    ticket: str = TICKET_UNAUTHORIZED
    actions: tuple[str, ...] = ALERT_ACTIONS

    @property
    def key(self) -> tuple[int | None, int]:
        return (self.station_id, self.staff_id)

    def describe(self) -> str:
        why = "；".join(reason.label for reason in self.reasons) or "准入不通过"
        station = "未知工位" if self.station_id is None else f"工位{self.station_id}"
        return (
            f"{station} 未授权上岗：{self.staff_name}(#{self.staff_id}) "
            f"{why}（已持续 {self.elapsed:.0f}s）"
        )


@dataclass
class _Presence:
    """单个在场记录。"""

    staff_id: int
    station_id: int | None
    since: float                       # 进入工位的单调时刻（统计在岗时长）
    deny_since: float | None = None    # 首次判定不合法的时刻
    alerted: bool = False
    last_reasons: tuple[DenyReason, ...] = ()
    last_details: tuple[str, ...] = ()
    checks: int = 0


@dataclass
class DutyGuard:
    """未授权上岗守卫。接 `AdmissionController`，不自己重算准入条件。"""

    controller: AdmissionController
    #: 连续违规多少秒才报。5 秒是设计文档「5 秒内出工单」的验收值。
    grace_sec: float = 5.0
    #: 去抖时钟（单调秒）。与「今天」无关，只算时长。
    clock: Any = None
    #: 判定「今天」用的真实时间来源（返回 datetime）。
    wall_clock: Any = None
    _presence: dict[int, _Presence] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.grace_sec < 0:
            raise ValueError("grace_sec 不能为负")
        if self.clock is None:
            self.clock = time.monotonic
        if self.wall_clock is None:
            self.wall_clock = lambda: datetime.now(CST)

    # ------------------------------------------------------------ 上报
    def presence(
        self, staff_id: int, station_id: int | None, *, now: float | None = None,
    ) -> None:
        """上报「某人在某工位」。视觉每帧调一次；重复调用幂等。

        只登记在场，不在这里判定——判定集中在 `tick()`，避免「上报路径里做 IO」
        拖慢视觉帧率。
        """
        moment = self._now(now)
        entry = self._presence.get(int(staff_id))
        if entry is None or entry.station_id != station_id:
            # 换工位等于重新计：在新工位上可能是合法的（资质按工位要求）
            self._presence[int(staff_id)] = _Presence(
                staff_id=int(staff_id), station_id=station_id, since=moment,
            )

    def leave(self, staff_id: int) -> None:
        self._presence.pop(int(staff_id), None)

    def clear_station(self, station_id: int | None) -> list[int]:
        """清空某工位（换班/收档）。返回被清掉的 staff_id。"""
        gone = [
            sid for sid, entry in self._presence.items()
            if entry.station_id == station_id
        ]
        for sid in gone:
            self._presence.pop(sid, None)
        return gone

    # ------------------------------------------------------------ 判定
    def tick(self, *, now: float | None = None, wall: datetime | None = None) -> tuple[DutyAlert, ...]:
        """扫描在场人员，返回本次新产生的告警（通常为空元组）。"""
        moment = self._now(now)
        wall_now = wall or self.wall_clock()
        alerts: list[DutyAlert] = []

        for entry in list(self._presence.values()):
            entry.checks += 1
            verdict = self.controller.scan_staff(
                entry.staff_id, station_id=entry.station_id, now=wall_now,
            )
            if verdict.ok:
                # 准入恢复（补了晨检/换了岗）：清违规计时，允许将来再次告警
                entry.deny_since = None
                entry.alerted = False
                entry.last_reasons = ()
                entry.last_details = ()
                continue
            entry.last_reasons = verdict.reasons
            entry.last_details = verdict.details
            if entry.deny_since is None:
                entry.deny_since = moment
            elapsed = moment - entry.deny_since
            if entry.alerted or elapsed < self.grace_sec:
                continue
            entry.alerted = True
            staff = self.controller.repo.staff(entry.staff_id)
            alert = DutyAlert(
                station_id=entry.station_id,
                staff_id=entry.staff_id,
                staff_name=staff.name if staff else f"#{entry.staff_id}",
                reasons=verdict.reasons,
                elapsed=elapsed,
                details=verdict.details,
            )
            alerts.append(alert)
            log.warning("未授权上岗：%s", alert.describe())

        return tuple(alerts)

    # ------------------------------------------------------------ 观测
    def on_duty(self) -> tuple[int, ...]:
        return tuple(sorted(self._presence))

    def snapshot(self) -> dict:
        return {
            "grace_sec": self.grace_sec,
            "on_duty": {
                str(sid): {
                    "station_id": entry.station_id,
                    "deny_sec": (None if entry.deny_since is None
                                 else round(self._now(None) - entry.deny_since, 1)),
                    "alerted": entry.alerted,
                    "reasons": [reason.value for reason in entry.last_reasons],
                    "checks": entry.checks,
                }
                for sid, entry in sorted(self._presence.items())
            },
        }

    def _now(self, now: float | None) -> float:
        return float(self.clock() if now is None else now)
