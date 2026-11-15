# -*- coding: utf-8 -*-
"""#16 违规处置闭环（A5-04）：切电 → 语音整改 → 双通道复核 → 恢复 → 工单。

## 闭环长什么样

```
视觉违规（连续 N 帧成立）
   ├─ 着装类（帽子/口罩/工服）→ 语音提醒 + 工单，**不切电**
   └─ 行为类（吸烟/玩手机）   → 切该工位插座 + 语音 + 工单
                                  ↓
                    组长 TFT 刷工牌  +  Web 端二次确认
                                  ↓
                    power_restore（两通道齐了才允许）+ 清视觉状态 + 关单
```

## 五条硬口径

1. **着装类不切电。** 没戴帽子戴回来只要 3 秒，切断插座既不影响他戴帽子，
   又让整条线停摆。一刀切全断电的版本，现场的处理方式是**把摄像头遮住**。
   吸烟在明火厨房是火灾隐患，才值得停设备。

2. **切电请求必须去重。** `channels.InterlockController` 的 trip 分支每次都
   `reset_reviewers.clear()`——重复切电会把已经完成的复核记录冲掉，表现为
   「组长确认了两次、按钮一直变回未确认」。所以已断电的工位不再重复 request。

3. **复核不能由本人完成，也不能由非组长完成。** 视觉不知道画面里是谁，所以
   靠工位当前刷卡在岗人来判（`on_duty` 注入）。这条挡的是「自己刷自己的卡
   把电送回来」——那是整个闭环唯一能被绕开的地方。

4. **断电到恢复有最短间隔（`min_cut_sec=10`）。** 立刻恢复等于没断过电：
   人还没去戴帽子，画面还没变，下一轮判定又是违规，于是形成「断电-恢复」
   的循环，继电器触点会被打坏（这是真实损耗，不是理论问题）。

5. **恢复失败要能在工单上看见。** 两通道确认齐了但 `power_restore` 被互锁
   拒绝（比如 CH3 还有别的源、或断网降级），单子不能关——否则表现为
   「系统说整改完成，设备还是黑的」。拒绝原因写进 `blocked` 与工单。

## 硬件前提：一路继电器一个工位

`StationPowerBank(capacity=2)` 对应 2 路继电器模块。两工位同时违规时两路各
断各的；只有 1 路硬件却要切第二个工位时抛 `PowerBusyError`，**不静默错切**。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..channels import (
    PRIORITY_VISION_VIOLATION,
    RELAY_DEVICE_ID,
    Channel,
    InterlockBlocked,
    InterlockController,
)
from ..domain import CST
from .models import VIOLATION_LABELS, VisionError
from .violations import Violation

log = logging.getLogger("kitchen.vision.disposal")

__all__ = [
    "StationPowerBank",
    "ViolationDisposal",
    "DisposalTicket",
    "DisposalResult",
    "ReviewResult",
    "DisposalError",
    "ReviewerNotAllowed",
    "TooSoonError",
    "PowerBusyError",
    "REVIEW_CHANNELS",
    "MIN_CUT_SEC",
    "TICKET_TYPE",
    "voice_text",
]

#: 复核通道。与契约 §2.4 一致：Web + TFT 两处，缺一不可
REVIEW_CHANNELS: tuple[str, ...] = ("tft", "web")

#: 断电到允许恢复的最短间隔（秒）
MIN_CUT_SEC = 10.0

#: 工单类型（A7 工单中心按此过滤）
TICKET_TYPE = "vision_violation"

#: 语音整改话术。**必须带工位号和具体缺什么**——只播「违规」的话，
#: 厨房里三个人同时抬头，谁都不知道该干什么
VOICE_TEMPLATES: dict[str, str] = {
    "no_hat": "ST{station} 请戴好厨师帽",
    "no_mask": "ST{station} 请戴好口罩",
    "no_uniform": "ST{station} 请穿好工服",
    "smoking": "ST{station} 工位禁止吸烟，设备已断电，请立即整改",
    "phone": "ST{station} 工位禁止使用手机，设备已断电，请立即整改",
}


def voice_text(kind: str, station_id: int) -> str:
    template = VOICE_TEMPLATES.get(kind, "ST{station} 请立即整改")
    return template.format(station=station_id)


class DisposalError(VisionError):
    """违规处置相关错误。"""


class ReviewerNotAllowed(DisposalError):
    """复核人无权限（不是组长，或者就是违规本人）。"""


class TooSoonError(DisposalError):
    """断电时间太短，还不允许恢复供电。"""


class PowerBusyError(DisposalError):
    """继电器路数不够（工位数 > 实际路数）。"""


# ------------------------------------------------------------------ 断电控制
class StationPowerBank:
    """按工位管理 CH3 断电锁存。**一路继电器一个工位**。

    为什么要按工位分实例：`InterlockController` 的闩锁是「每通道一套」，
    两个工位共用一套时，ST1 断电会被当成 ST2 也断了，ST2 的人就被劝离
    工位——而他的插座其实有电。分实例是最小改动就能正确的东西。
    """

    def __init__(self, *, capacity: int = 2,
                 device_id: str = RELAY_DEVICE_ID) -> None:
        if capacity < 1:
            raise DisposalError(f"继电器路数必须 ≥1，收到 {capacity}")
        self.capacity = int(capacity)
        self.device_id = device_id
        self._ctl: dict[int, InterlockController] = {}
        self._cut_at: dict[int, datetime] = {}

    # -------------------------------------------------------------- 访问
    def controller(self, station_id: int) -> InterlockController:
        sid = int(station_id)
        ctl = self._ctl.get(sid)
        if ctl is not None:
            return ctl
        if len(self._ctl) >= self.capacity:
            raise PowerBusyError(
                f"继电器只有 {self.capacity} 路，已占用工位 "
                f"{sorted(self._ctl)}；要处理 ST{sid} 需要增加一路继电器"
            )
        ctl = InterlockController(device_id=self.device_id)
        self._ctl[sid] = ctl
        return ctl

    @property
    def used(self) -> int:
        return len(self._ctl)

    def is_cut(self, station_id: int) -> bool:
        ctl = self._ctl.get(int(station_id))
        if ctl is None:
            return False
        return bool(ctl.active_sources(Channel.CH3))

    def cut_at(self, station_id: int) -> datetime | None:
        return self._cut_at.get(int(station_id))

    # -------------------------------------------------------------- 切 / 恢复
    def cut(self, station_id: int, *, source: str, now: datetime) -> str:
        """切电。**已断电时直接返回 'already'，不重复 request**（口径 2）。"""
        sid = int(station_id)
        if self.is_cut(sid):
            return "already"
        ctl = self.controller(sid)
        ctl.request("power_cut", channel=Channel.CH3,
                    source=source or TICKET_TYPE,
                    priority=PRIORITY_VISION_VIOLATION)
        self._cut_at[sid] = now
        log.warning("ST%s 工位插座已断电（来源 %s）", sid, source or TICKET_TYPE)
        return "cut"

    def confirm(self, station_id: int, channel: str,
                reviewer: str) -> set[str]:
        """登记一次复核。返回当前已确认的**通道**集合。

        `channels.InterlockController.confirm_manual_reset()` 的参数名叫
        `reviewer`，但它存进 `reset_reviewers` 的值会被拿去跟 `{"web","tft"}`
        比对——所以那里必须传**通道名**，不是人名。人名记在工单上（`ticket.reviewers`）。
        一开始按字面传人名，结果是「两个人都确认了、电还是送不上」，而
        `power_restore` 报的是「缺少 ['tft', 'web']」——看着像通道没确认，
        其实是值填错了字段。
        """
        name = str(channel).strip().lower()
        if name not in REVIEW_CHANNELS:
            raise DisposalError(f"未知复核通道 {channel!r}，只有 {REVIEW_CHANNELS}")
        if not str(reviewer).strip():
            raise ReviewerNotAllowed("复核人不能为空")
        return self.controller(station_id).confirm_manual_reset(Channel.CH3, name)

    def reviewers(self, station_id: int) -> set[str]:
        ctl = self._ctl.get(int(station_id))
        if ctl is None:
            return set()
        return set(ctl.snapshot()[Channel.CH3.value]["reset_reviewers"])

    def restore(self, station_id: int) -> str:
        """恢复供电。两通道齐了才允许（`InterlockController` 会再校验一次）。"""
        sid = int(station_id)
        ctl = self.controller(sid)
        ctl.note_recovered(Channel.CH3, TICKET_TYPE)
        ctl.request("power_restore", channel=Channel.CH3, manual_confirmed=True)
        self._cut_at.pop(sid, None)
        log.info("ST%s 工位插座已恢复供电", sid)
        return "restored"

    def set_degraded(self, degraded: bool) -> None:
        """断网降级：期间禁止自动恢复（`request` 会拒），这里只是同步状态。"""
        for ctl in self._ctl.values():
            ctl.set_degraded(degraded)

    def snapshot(self) -> dict:
        return {
            "capacity": self.capacity,
            "used": self.used,
            "stations": {
                str(sid): {
                    "cut": self.is_cut(sid),
                    "reviewers": sorted(self.reviewers(sid)),
                    "cut_at": self._cut_at[sid].isoformat() if sid in self._cut_at else None,
                }
                for sid in sorted(self._ctl)
            },
        }


# ------------------------------------------------------------------ 工单
@dataclass
class DisposalTicket:
    """违规工单（内存版；A7 工单中心落 `work_orders`）。"""

    id: int
    station_id: int
    event_id: str
    kind: str
    state: str = "open"
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    power_cut: bool = False
    reviewers: dict[str, str] = field(default_factory=dict)
    evidence_dir: str = ""
    note: str = ""
    repeated: bool = False

    @property
    def label(self) -> str:
        return VIOLATION_LABELS.get(self.kind, self.kind)

    def describe(self) -> str:
        state = "已关单" if self.state == "closed" else "处理中"
        cut = "已断电" if self.power_cut else "未断电"
        return (f"工单#{self.id} ST{self.station_id} {self.label}"
                f"（{cut}，{state}{'，重报' if self.repeated else ''}）")

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "type": TICKET_TYPE,
            "station_id": self.station_id,
            "event_id": self.event_id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "power_cut": self.power_cut,
            "reviewers": dict(self.reviewers),
            "evidence_dir": self.evidence_dir,
            "opened_at": int(self.opened_at.timestamp() * 1000) if self.opened_at else None,
            "closed_at": int(self.closed_at.timestamp() * 1000) if self.closed_at else None,
        }


@dataclass(frozen=True)
class DisposalResult:
    """一次处置的下发结果。"""

    ticket: DisposalTicket
    power_cut: bool
    voice: str
    actions: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    evidence_dir: str = ""
    reason: str = ""

    def describe(self) -> str:
        bits = [self.ticket.describe()]
        if self.actions:
            bits.append("下发 " + "、".join(self.actions))
        if self.blocked:
            bits.append("受阻 " + "；".join(self.blocked))
        if self.reason:
            bits.append(self.reason)
        return "｜".join(bits)


@dataclass(frozen=True)
class ReviewResult:
    """一次复核确认的结果。"""

    station_id: int
    channel: str
    reviewer: str
    reviewers: tuple[str, ...]
    restored: bool
    ticket: DisposalTicket | None = None
    blocked: str = ""

    @property
    def complete(self) -> bool:
        return set(REVIEW_CHANNELS).issubset(self.reviewers)

    def describe(self) -> str:
        mark = "已恢复供电" if self.restored else (
            f"待 {sorted(set(REVIEW_CHANNELS) - set(self.reviewers))} 确认"
            if not self.complete else "通道已齐但恢复受阻"
        )
        tail = f"（{self.blocked}）" if self.blocked else ""
        return f"复核 ST{self.station_id} ← {self.channel}/{self.reviewer}：{mark}{tail}"


# ------------------------------------------------------------------ 处置编排
class ViolationDisposal:
    """把违规事件推进到「已整改、已恢复、已关单」。

    `approvers` 与 `on_duty` 两个回调是本模块与人员域的**唯一接口**：
    视觉不知道画面里是谁，硬要 import `staff` 就会让视觉域依赖刷卡域，
    而两侧的时钟/候选集口径一旦分叉，就会出现「派单说有人、视觉说没人」。
    """

    def __init__(
        self,
        bank: StationPowerBank,
        *,
        tracker=None,
        evidence=None,
        clock: Callable[[], datetime] | None = None,
        approvers: Callable[[str], bool] | None = None,
        on_duty: Callable[[int], object | None] | None = None,
        min_cut_sec: float = MIN_CUT_SEC,
        voice: Callable[[str], None] | None = None,
        notify: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.bank = bank
        self.tracker = tracker
        self.evidence = evidence
        self._clock = clock or (lambda: datetime.now(CST))
        self.approvers = approvers
        self.on_duty = on_duty
        self.min_cut_sec = float(min_cut_sec)
        self.voice = voice
        self.notify = notify
        self._tickets: dict[int, DisposalTicket] = {}
        self._open_by_station: dict[int, int] = {}
        self._seq = 0

    # -------------------------------------------------------------- 处置
    def dispose(
        self,
        violation: Violation,
        *,
        frames: Sequence[tuple[int, datetime]] = (),
        read_bytes: Callable[[int], bytes] | None = None,
        now: datetime | None = None,
    ) -> DisposalResult:
        """登记一条违规：抓拍 → 切电（仅行为类）→ 语音 → 开单。"""
        moment = now or self._clock()
        actions: list[str] = []
        blocked: list[str] = []
        evidence_dir = ""

        # ① 抓拍存证。**先抓拍再切电**：切电之后如果摄像头不是独立供电，
        # 画面就没了（见 stream.py 硬口径 1）。顺序反了在真机上必踩。
        if self.evidence is not None and frames and read_bytes is not None:
            try:
                package = self.evidence.capture(violation, frames, read_bytes)
                evidence_dir = str(package.path)
            except VisionError as exc:
                # 存证失败不阻断处置：安全处置优先于留证，但必须记进工单
                blocked.append(f"抓拍失败：{exc}")

        # ② 切电（仅行为类）
        power_cut = False
        if violation.power:
            try:
                outcome = self.bank.cut(violation.station_id,
                                        source=TICKET_TYPE, now=moment)
                power_cut = True
                actions.append("power_cut" if outcome == "cut"
                               else "power_cut(已断电，跳过)")
            except (InterlockBlocked, PowerBusyError, DisposalError) as exc:
                blocked.append(f"断电失败：{exc}")

        # ③ 语音整改要求（事件型动作，每次都播）
        text = voice_text(violation.kind, violation.station_id)
        try:
            if self.voice is not None:
                self.voice(text)
            actions.append("voice_play")
        except Exception as exc:                        # noqa: BLE001
            blocked.append(f"语音失败：{exc}")

        # ④ 开单
        ticket = self._open_ticket(violation, moment, power_cut, evidence_dir)

        result = DisposalResult(
            ticket=ticket, power_cut=power_cut, voice=text,
            actions=tuple(actions), blocked=tuple(blocked),
            evidence_dir=evidence_dir,
            reason="行为类违规已切断工位插座" if power_cut else "着装类违规仅语音提醒",
        )
        if self.notify is not None:
            try:
                self.notify("violation_disposal", result.ticket.to_payload())
            except Exception as exc:                    # noqa: BLE001
                log.warning("推送违规工单失败：%s", exc)
        log.warning("%s", result.describe())
        return result

    def _open_ticket(self, violation: Violation, moment: datetime,
                     power_cut: bool, evidence_dir: str) -> DisposalTicket:
        """同一工位已有未关单时**复用**那张单（口径：一件事一张单）。

        否则冷却期一过就开新单，一天下来工单中心里同一个灶挂着十几条，
        值班的人第一件事就是把通知关掉。
        """
        existing_id = self._open_by_station.get(violation.station_id)
        ticket = self._tickets.get(existing_id) if existing_id is not None else None
        if ticket is not None and ticket.state == "open":
            ticket.repeated = True
            ticket.kind = violation.kind
            ticket.power_cut = ticket.power_cut or power_cut
            if evidence_dir:
                ticket.evidence_dir = evidence_dir
            ticket.note = f"{ticket.note}｜再次违规 {moment:%H:%M:%S}".strip("｜")
            return ticket

        self._seq += 1
        ticket = DisposalTicket(
            id=self._seq,
            station_id=violation.station_id,
            event_id=f"{moment:%Y%m%dT%H%M%S}-ST{violation.station_id}-{violation.kind}",
            kind=violation.kind,
            opened_at=moment,
            power_cut=power_cut,
            evidence_dir=evidence_dir,
        )
        self._tickets[ticket.id] = ticket
        self._open_by_station[violation.station_id] = ticket.id
        return ticket

    # -------------------------------------------------------------- 复核
    def confirm(self, station_id: int, channel: str, reviewer: str,
                *, note: str = "", now: datetime | None = None) -> ReviewResult:
        """组长复核一个通道。两通道齐了才尝试恢复供电。

        拒绝的三种情况都会抛异常并说明原因，**不静默忽略**：
        非组长、违规本人、未知通道。静默忽略的表现是「组长刷了卡没反应」，
        现场会以为是读卡器坏了。
        """
        moment = now or self._clock()
        sid = int(station_id)
        name = str(reviewer).strip()

        if self.approvers is not None and not self.approvers(name):
            raise ReviewerNotAllowed(f"{name} 不具备组长资质，不能复核违规整改")
        occupant = self.on_duty(sid) if self.on_duty is not None else None
        if occupant is not None and str(getattr(occupant, "name", occupant)) == name:
            raise ReviewerNotAllowed(
                f"{name} 是本工位当前在岗人员，不能复核自己的违规"
            )

        reviewers = sorted(self.bank.confirm(sid, channel, name))
        ticket = self._ticket_of(sid)
        if ticket is not None:
            ticket.reviewers[str(channel).strip().lower()] = name
            if note:
                ticket.note = f"{ticket.note}｜{note}".strip("｜")

        complete = set(REVIEW_CHANNELS).issubset(reviewers)
        restored = False
        blocked = ""
        if complete:
            if self.bank.is_cut(sid):
                cut_at = self.bank.cut_at(sid)
                waited = (moment - cut_at).total_seconds() if cut_at else None
                if waited is not None and waited < self.min_cut_sec:
                    blocked = (f"距断电仅 {waited:.0f}s（要求 ≥{self.min_cut_sec:.0f}s），"
                               f"稍后再试")
                else:
                    try:
                        self.bank.restore(sid)
                        restored = True
                        self._close(ticket, moment)
                    except InterlockBlocked as exc:
                        blocked = f"恢复被互锁拒绝：{exc}"
            else:
                # 没切过电（着装类）：两通道确认即关单
                restored = True
                self._close(ticket, moment)

        result = ReviewResult(station_id=sid, channel=str(channel),
                              reviewer=name, reviewers=tuple(reviewers),
                              restored=restored, ticket=ticket, blocked=blocked)
        log.info("%s", result.describe())
        return result

    def _close(self, ticket: DisposalTicket | None, moment: datetime) -> None:
        """关单 + 清视觉状态。**两件事必须一起做**。

        只关单不清状态：复核页面上的「当前违规」一直挂着；
        只清状态不关单：工单中心里永远有一条处理中的单。
        """
        if ticket is not None and ticket.state == "open":
            ticket.state = "closed"
            ticket.closed_at = moment
            self._open_by_station.pop(ticket.station_id, None)
            station_id = ticket.station_id
        else:
            station_id = -1
        if self.tracker is not None and station_id >= 0:
            self.tracker.clear_station(station_id)

    # -------------------------------------------------------------- 查询
    def _ticket_of(self, station_id: int) -> DisposalTicket | None:
        tid = self._open_by_station.get(int(station_id))
        return self._tickets.get(tid) if tid is not None else None

    def tickets(self, *, state: str | None = None) -> list[DisposalTicket]:
        rows = list(self._tickets.values())
        if state:
            rows = [t for t in rows if t.state == state]
        return sorted(rows, key=lambda t: t.id, reverse=True)

    def open_ticket(self, station_id: int) -> DisposalTicket | None:
        return self._ticket_of(station_id)

    def snapshot(self) -> dict:
        return {
            "power_bank": self.bank.snapshot(),
            "open_tickets": [t.to_payload() for t in self.tickets(state="open")],
            "closed": len([t for t in self._tickets.values() if t.state == "closed"]),
        }
