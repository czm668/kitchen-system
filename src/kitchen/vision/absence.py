# -*- coding: utf-8 -*-
"""#11 离岗检测与任务动态重排（A5-05）。

## 与 #3、与 `staff.duty` 的分工

| 现象 | 归属 | 做什么 |
|---|---|---|
| 工位**没人**，火还开着 | #3 `safety/flame.py` | 关燃气阀 + 锁定工位（安全） |
| 工位**没人**，任务没做完 | #11 本模块 | 把未开火的任务转派（排产） |
| 工位**有人**，但今天不该在岗 | `staff/duty.py` | 声光告警 + 工单（合规） |

三件事的判据都来自视觉，但**动作完全不同**，混在一起写就会在离岗时
顺手关阀，或者在未授权上岗时顺手转派——后者特别危险：转派会给一个
「今天没过晨检的人」派单，等于系统认同他上岗。所以模块边界必须清楚。

## 一条最重要的口径：**烹饪中的菜不转派**

离岗时工位上可能有两类任务：

- `DISPATCHED`（已派单、还没上灶）：换个人接手，零成本；
- `COOKING`（已经开火）：**不能换人**。炒到一半换人等于重做，而且锅里
  的东西没人认账，出了食安问题查不到责任人。

所以 `COOKING` 的子项不进转派列表，而是作为 `blocked` 报到界面上——
「ST1 有一道菜在烧，需组长接手」。这条如果做错（把 cooking 也改派走），
演示时看不出来，真跑一天就会看到同一道菜被两个人各自做了一遍。

## 转派不硬塞

找不到「在岗 + 资质合格 + 不忙」的接手人就**撤销预约、把单子放回队列**，
报「无人可接」。硬转给一个资质不符的人，会重演 A4-09 那个坑：他刷卡被拒，
单子已经挂灶上了。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..domain import CST, DomainError, InvalidTransition
from ..process.models import ItemState
from .models import VisionFrame

log = logging.getLogger("kitchen.vision.absence")

__all__ = [
    "AbsenceEvent",
    "AbsencePolicy",
    "AbsenceMonitor",
    "AbsenceReassigner",
    "ReassignResult",
    "ABSENCE_SEC",
]

#: 连续离岗阈值（秒）。文档 #11 是「10~15 秒可配」，取中值 12。
#: 取 12 而不是 10 的理由：视觉在岗判定本身有 1~2 秒延迟，取 10 秒等于
#: 实际只给了 8 秒，蹲下取个料就误报；取 15 秒则离岗 14 秒的人刚好漏过。
ABSENCE_SEC = 12.0


@dataclass(frozen=True)
class AbsencePolicy:
    absence_sec: float = ABSENCE_SEC
    #: 人回来之后多久才算「真的回来了」（防视觉抖动）。回来判定要求连续
    #: 有人在这么多帧——但这里按秒更实际：抓拍模式 0.2fps 时 3 帧要 15 秒
    return_sec: float = 2.0
    #: 同一工位两次离岗告警的最小间隔
    cooldown_sec: float = 300.0


@dataclass
class AbsenceEvent:
    """一次离岗。"""

    station_id: int
    staff_id: int | None
    since: datetime
    last: datetime
    seconds: float
    #: 离岗期间该工位是否还有明火（由调用方填；#11 不据此动作，只是提示）
    flame_on: bool = False

    def describe(self) -> str:
        who = f"{self.staff_id} 号" if self.staff_id else "未知人员"
        fire = "，⚠ 灶上还有火" if self.flame_on else ""
        return f"ST{self.station_id} {who}已离岗 {self.seconds:.0f}s{fire}"

    def to_payload(self) -> dict:
        return {
            "station_id": self.station_id,
            "staff_id": self.staff_id,
            "since": int(self.since.timestamp() * 1000),
            "seconds": round(self.seconds, 1),
            "flame_on": self.flame_on,
        }


@dataclass
class _Seat:
    """单个工位的在场积分。"""

    absent_since: datetime | None = None
    last_seen: datetime | None = None
    reported_at: datetime | None = None
    present_since: datetime | None = None


class AbsenceMonitor:
    """逐帧喂视觉结果，出离岗事件。

    只有「有活动占用会话」的工位才看离岗——没人上灶的工位本来就没人在，
    那不是离岗。这条判断靠 `occupant_of` 注入（来自 `DispatchBoard.station_staff`），
    而不是在本模块里猜。
    """

    def __init__(
        self,
        *,
        occupant_of: Callable[[int], int | None] | None = None,
        policy: AbsencePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.occupant_of = occupant_of
        self.policy = policy or AbsencePolicy()
        self._clock = clock or (lambda: datetime.now(CST))
        self._seats: dict[int, _Seat] = {}
        #: 上一帧「工位有人」的集合，用来判「人回来了」
        self._last_present: set[int] = set()
        #: 最后一帧的时刻。`absent_now()` 默认用它而不是墙钟——
        #: 回放/单测里帧时间与墙钟差着几小时，用墙钟会把所有离岗都算成
        #: 「已经离了很久」，也会把刚发生的离岗算成「还没到阈值」
        self._last_ts: datetime | None = None

    # ---------------------------------------------------------------- 主入口
    def observe(self, frame: VisionFrame) -> list[AbsenceEvent]:
        """返回本帧**新产生**的离岗事件（同一段离岗只报一次）。"""
        moment = frame.frame.ts
        self._last_ts = moment
        events: list[AbsenceEvent] = []
        present = {sid for sid, count in frame.station_headcount.items() if count > 0}

        # 有人 → 记为「刚回来」，并清掉离岗积分
        for sid in present:
            seat = self._seats.setdefault(sid, _Seat())
            if seat.absent_since is not None:
                gap = (moment - seat.absent_since).total_seconds()
                if gap >= self.policy.absence_sec:
                    log.info("ST%s 离岗结束（本次 %.0fs）", sid, gap)
            seat.absent_since = None
            seat.last_seen = moment
            seat.present_since = seat.present_since or moment

        # 无人 → 检查是不是「该有人却没人的工位」
        for sid in self._watched_stations(frame):
            if sid in present:
                continue
            seat = self._seats.setdefault(sid, _Seat())
            seat.present_since = None
            if seat.last_seen is None:
                # 从未见人：说明这个工位从来没人上过灶，不判离岗
                continue
            seat.absent_since = seat.absent_since or seat.last_seen
            elapsed = (moment - seat.absent_since).total_seconds()
            if elapsed < self.policy.absence_sec:
                continue
            if seat.reported_at is not None:
                cooling = (moment - seat.reported_at).total_seconds()
                if cooling < self.policy.cooldown_sec:
                    continue
            seat.reported_at = moment
            events.append(AbsenceEvent(
                station_id=int(sid),
                staff_id=self.occupant_of(int(sid)) if self.occupant_of else None,
                since=seat.absent_since,
                last=moment,
                seconds=elapsed,
            ))

        self._last_present = present
        return events

    def _watched_stations(self, frame: VisionFrame) -> set[int]:
        """要看离岗的工位：有活动占用会话的那些。"""
        if self.occupant_of is None:
            return set(frame.station_headcount) | set(self._seats)
        out: set[int] = set()
        for sid in set(frame.station_headcount) | set(self._seats):
            if self.occupant_of(int(sid)) is not None:
                out.add(int(sid))
        return out

    def mark_present(self, station_id: int, moment: datetime) -> None:
        """外部证据说明该工位有人（刷卡、按键）——**视觉漏检时用它救回来**。

        蹲在灶下修东西时视觉可能连续十几秒看不到人，#3 会关阀。刷卡/按键
        是比视觉更硬的证据，应该能压过视觉。
        """
        seat = self._seats.setdefault(int(station_id), _Seat())
        seat.absent_since = None
        seat.last_seen = moment
        seat.present_since = moment

    def absent_now(self, moment: datetime | None = None) -> list[int]:
        """当前处于离岗中的工位（未达阈值的不算）。"""
        now = moment or self._last_ts or self._clock()
        out = []
        for sid, seat in self._seats.items():
            if seat.absent_since is None:
                continue
            if (now - seat.absent_since).total_seconds() >= self.policy.absence_sec:
                out.append(sid)
        return sorted(out)

    def reset(self) -> None:
        self._seats.clear()
        self._last_present.clear()
        self._last_ts = None

    def snapshot(self) -> dict:
        return {
            "absent": self.absent_now(),
            "seats": {
                str(sid): {
                    "absent_since": seat.absent_since.isoformat() if seat.absent_since else None,
                    "last_seen": seat.last_seen.isoformat() if seat.last_seen else None,
                }
                for sid, seat in sorted(self._seats.items())
            },
            "policy": {"absence_sec": self.policy.absence_sec},
        }


@dataclass(frozen=True)
class ReassignResult:
    """一次离岗重排的结果。"""

    station_id: int
    absent_staff: int | None
    moved: tuple[dict, ...] = ()
    blocked: tuple[str, ...] = ()
    released: tuple[int, ...] = ()
    available: tuple[int, ...] = ()
    #: 灶腾出来之后重新派出去的排队单（离岗不只是「转走他的活儿」，
    #: 更是「把腾出来的产能用起来」）
    redispatched: tuple[dict, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.released or self.redispatched)

    def describe(self) -> str:
        bits = [f"ST{self.station_id} 离岗重排"]
        if self.moved:
            bits.append("转派 " + "；".join(
                f"#{m['item_id']}{m['dish']} → {m['to']}号" for m in self.moved))
        if self.released:
            bits.append("退回队列 " + "、".join(f"#{i}" for i in self.released))
        if self.redispatched:
            bits.append("重新派单 " + "；".join(
                f"#{d['item_id']} → ST{d['station_id']}" for d in self.redispatched))
        if self.blocked:
            bits.append("未处理 " + "；".join(self.blocked))
        if not self.changed and not self.blocked:
            bits.append("无需重排")
        return "｜".join(bits)


class AbsenceReassigner:
    """把离岗事件变成一次任务重排。

    依赖全部通过构造注入（`board` / `matcher` 都是鸭子类型），所以单测里
    可以只给一个记录调用的假板子——本模块不需要真的起订单与人员域。
    """

    def __init__(
        self,
        board,
        *,
        notify: Callable[[str, dict], None] | None = None,
        require_on_duty: Callable[[int], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.board = board
        self.notify = notify
        #: 「这个人此刻在不在岗」——由人员域提供（晨检 + 排班 + 刷卡在场）。
        #: 不给就退回「候选集里挑第一个」，那是 A4-09 的口径，够用但不够严。
        self.require_on_duty = require_on_duty
        self._clock = clock or (lambda: datetime.now(CST))
        self.history: list[ReassignResult] = []

    # ---------------------------------------------------------------- 主入口
    def handle(self, event: AbsenceEvent) -> ReassignResult:
        sid = int(event.station_id)
        absent = event.staff_id if event.staff_id is not None else self.board.station_staff(sid)

        # ① 该工位上、已派出还没上灶的子项
        candidates = list(self.board.assignments_of(absent)) if absent is not None else []
        moved: list[dict] = []
        blocked: list[str] = []
        released: list[int] = []

        # ② 工位上正在烧的菜：不转派，只报
        for item_id in self._cooking_at(sid):
            dish = self._dish_name(item_id)
            blocked.append(f"#{item_id}{dish} 已开火不能换人，需组长接手看火")

        # ③ 逐个转派
        for item_id in candidates:
            requirement = self._required_kind(item_id)
            pick = self._pick(requirement, exclude=absent)
            if pick is None:
                self.board.release_assignment(item_id)
                released.append(int(item_id))
                blocked.append(
                    f"#{item_id} 无人在岗可接（「{requirement}」资质），已退回队列")
                continue
            try:
                moved.append(self.board.takeover(
                    item_id, pick,
                    reason=f"原 {absent} 号离岗 {event.seconds:.0f}s"))
            except (DomainError, InvalidTransition) as exc:
                blocked.append(f"#{item_id} 转派失败：{exc}")

        # ④ 灶已经腾出来（没有活动占用会话）→ 立刻重新派单。
        #    离岗的价值一半在「转走他的活儿」，另一半在「别让腾出来的灶空着」：
        #    只做前者的话，排队单会一直等到下一轮派单（`cooldown` 分钟级），
        #    现场看到的是「一个灶空着、一堆单在等」「系统没反应」。
        redispatched: list[dict] = []
        if self.board.station_staff(sid) is None and hasattr(self.board, "dispatch_pending"):
            try:
                for decision in self.board.dispatch_pending():
                    if not decision.ok:
                        continue
                    redispatched.append({
                        "item_id": decision.item_id,
                        "station_id": decision.station_id,
                        "staff_id": decision.staff_id,
                    })
            except DomainError as exc:
                blocked.append(f"重新派单失败：{exc}")

        result = ReassignResult(
            station_id=sid, absent_staff=absent, moved=tuple(moved),
            blocked=tuple(blocked), released=tuple(released),
            available=tuple(self._available(absent)),
            redispatched=tuple(redispatched),
        )
        self.history.append(result)
        log.warning("%s", result.describe())

        if self.notify is not None and (result.changed or result.blocked):
            try:
                self.notify("absence_reassign", {
                    "station_id": sid,
                    "absent_staff": absent,
                    "seconds": round(event.seconds, 1),
                    "moved": [dict(m) for m in result.moved],
                    "released": list(result.released),
                    "redispatched": [dict(d) for d in result.redispatched],
                    "blocked": list(result.blocked),
                })
            except Exception as exc:                    # noqa: BLE001
                log.warning("离岗重排推送失败：%s", exc)
        return result

    # ---------------------------------------------------------------- 内部
    def _pick(self, requirement: str, *, exclude: int | None) -> int | None:
        """挑接手人：在岗 + 资质合格 + 不忙。**从候选集里挑，不另立一套。**"""
        matcher = getattr(self.board, "matcher", None)
        if matcher is None:
            return None
        busy = self.board.busy_staff()
        taken = set(self.board.assigned_staff().values())
        for candidate in matcher.candidates(requirement):
            cid = int(candidate)
            if cid == exclude or cid in busy or cid in taken:
                continue
            if self.require_on_duty is not None and not self.require_on_duty(cid):
                continue
            return cid
        return None

    def _available(self, exclude: int | None) -> list[int]:
        matcher = getattr(self.board, "matcher", None)
        if matcher is None:
            return []
        busy = self.board.busy_staff()
        out: list[int] = []
        for station_kind in self._kinds():
            for candidate in matcher.candidates(station_kind):
                cid = int(candidate)
                if cid == exclude or cid in busy or cid in out:
                    continue
                if self.require_on_duty is not None and not self.require_on_duty(cid):
                    continue
                out.append(cid)
        return sorted(out)

    def _kinds(self) -> tuple[str, ...]:
        kinds: list[str] = []
        for station in self.board.stations():
            if station.kind and station.kind not in kinds:
                kinds.append(station.kind)
        return tuple(kinds) or ("stove",)

    def _cooking_at(self, station_id: int) -> list[int]:
        """该工位上处于 COOKING 的子项。"""
        out: list[int] = []
        for order in self.board.orders.open_orders():
            for item in order.items:
                if item.state is not ItemState.COOKING:
                    continue
                if getattr(item, "station_id", None) == int(station_id):
                    out.append(int(item.id))
        return out

    def _required_kind(self, item_id: int) -> str:
        """该子项需要的工位资质。取 `dishes.station_kind`，与派单同一事实源。"""
        try:
            item = self.board.orders.item(int(item_id))[1]
            return str(self.board.repo.dish(item.dish_id).station_kind)
        except Exception:                               # noqa: BLE001
            return "stove"

    def _dish_name(self, item_id: int) -> str:
        try:
            return self.board.orders.item(int(item_id))[1].dish_name
        except Exception:                               # noqa: BLE001
            return ""
