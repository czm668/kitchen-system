# -*- coding: utf-8 -*-
"""跨帧聚合与去抖：#16 违规事件（A5-02 / A5-04 的判定入口）。

单帧检测结果**永远不能直接生成违规**。原因不是「模型不准」这么笼统：

- 厨师低头取料，帽子被烟罩挡住 2 帧 → 报「未戴帽」→ 切电停线；
- 抬手擦汗，手部框被判成手机 1 帧 → 报「玩手机」→ 切电停线；
- 人刚要戴帽子，帽子还在手上（框在腰部）→ 报「未戴帽」。

以上三种在真实画面里都是几帧内自愈的。所以要求「连续 N 帧且持续 T 秒」，
两个条件**同时**满足：只看帧数会在高帧率下退化成「0.4 秒就报」，只看
时长会在低帧率（1fps 的抓拍模式）下退化成「2 帧就报」。

## 三条必守的口径

1. **人离开工位立即清零，人还在只是合规了要延迟清零。**
   人走了还留着积分，等他回来时 `last-first` 早已超过阈值，**回来的第一帧
   就报警**——现场表现是「刚站回灶前就被切电」。反过来，人还在、只是某一帧
   帽子检出成功，就立刻清零，那 3 帧规则永远凑不满（检测本来就是隔帧抖的）。

2. **一个工位多人时按最差的那个人判一条，不按人头各报一条。**
   3 人里 1 人没戴帽 → 1 条违规。报 3 条会让工单里同一件事刷三遍。

3. **报过就不再报，但允许冷却后重报。**
   `fired` 之后持续违规不重复产出（否则每帧一条，切电请求把已完成的复核
   记录冲掉——`channels.py` 的 trip 分支会 `reset_reviewers.clear()`）。
   冷却的意义是「整改失败后能再报一次」，不是「按秒重报」。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import (
    VIOLATION_LABELS,
    PersonState,
    VisionFrame,
    ViolationKind,
    cuts_power,
)

log = logging.getLogger("kitchen.vision.violations")

__all__ = [
    "Violation",
    "ViolationPolicy",
    "ViolationTracker",
    "DEFAULT_POLICY",
    "ACTIVE_GRACE_SEC",
]


@dataclass(frozen=True)
class ViolationPolicy:
    """去抖参数。默认值对应「5fps 推流、3 帧、1.5 秒」。"""

    #: 最少连续帧数
    min_frames: int = 3
    #: 最少持续秒数
    min_sec: float = 1.5
    #: 同一工位同一类型的重报冷却
    cooldown_sec: float = 120.0
    #: 人在工位、但本帧合规：连续这么多帧才清零（防检测抖动）
    clear_misses: int = 3
    #: 参与抓拍的帧窗口大小（违规前若干帧一起存，答辩能看出「不是一帧巧合」）
    evidence_window: int = 5

    @classmethod
    def for_fps(cls, fps: float, **overrides) -> ViolationPolicy:
        """按帧率折算：5fps 时 3 帧≈0.6 秒，1fps 抓拍模式要 3 秒才够 3 帧。

        用帧率折算而不是写死帧数，是因为真机推流会掉帧——配置写 3 帧、
        实际拿到 1fps，规则就从 1.5 秒悄悄变成 4.5 秒。
        """
        rate = max(0.2, float(fps))
        frames = max(2, int(round(rate * (overrides.get("min_sec") or 1.5) * 0.6)))
        base = {"min_frames": frames}
        base.update(overrides)
        return cls(**base)


DEFAULT_POLICY = ViolationPolicy()

#: 判定为「当前仍在违规」的宽限：最后一次命中之后这么久内算活跃
ACTIVE_GRACE_SEC = 10.0


@dataclass
class Violation:
    """一条违规事件。这是要写库、要切电、要开单的那个东西。"""

    kind: str
    station_id: int
    since: datetime
    last: datetime
    frames: int
    #: 触发该违规的人的框（用于抓拍时裁剪，也用于复核时看「是不是他」）
    person: PersonState | None = None
    #: 参与判定的最近帧序号（抓拍多图用）
    evidence_seq: tuple[int, ...] = ()
    #: 冷却期结束后重报时置 True
    repeat: bool = False

    @property
    def label(self) -> str:
        return VIOLATION_LABELS.get(self.kind, self.kind)

    @property
    def power(self) -> bool:
        """是否要切工位插座。着装类不切电（见 `models.VIOLATION_ACTIONS`）。"""
        return cuts_power(self.kind)

    @property
    def elapsed(self) -> float:
        return (self.last - self.since).total_seconds()

    def describe(self) -> str:
        tail = f"{self.elapsed:.1f}s/{self.frames}帧"
        return f"ST{self.station_id} {self.label}（{tail}{'，重报' if self.repeat else ''}）"

    def to_payload(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "station_id": self.station_id,
            "since": int(self.since.timestamp() * 1000),
            "last": int(self.last.timestamp() * 1000),
            "frames": self.frames,
            "power": self.power,
        }


@dataclass
class _Track:
    """单个 (工位, 违规类型) 的积分状态。"""

    since: datetime
    last: datetime
    hits: int = 1
    fired_at: datetime | None = None
    recent: list[int] = field(default_factory=list)
    missing: int = 0

    def hits_window(self, size: int) -> tuple[int, ...]:
        return tuple(self.recent[-size:]) if size > 0 else ()


def _cases(state: PersonState) -> Iterable[str]:
    """一个人身上有哪些违规。"""
    for cls in state.missing():
        yield {
            "hat": ViolationKind.NO_HAT,
            "mask": ViolationKind.NO_MASK,
            "uniform": ViolationKind.NO_UNIFORM,
        }[cls]
    if state.smoking:
        yield ViolationKind.SMOKING
    if state.using_phone:
        yield ViolationKind.PHONE


class ViolationTracker:
    """逐帧喂 `VisionFrame`，出来 `Violation` 列表（每次 observe 的增量）。"""

    def __init__(self, policy: ViolationPolicy | None = None) -> None:
        self.policy = policy or DEFAULT_POLICY
        self._tracks: dict[tuple[int, str], _Track] = {}
        #: 已产出但仍在违规中的事件（复核前要用它判断「现在还有几条在挂着」）
        self._active: dict[tuple[int, str], Violation] = {}
        self.last_frame: VisionFrame | None = None

    # ---------------------------------------------------------------- 主入口
    def observe(self, frame: VisionFrame) -> list[Violation]:
        self.last_frame = frame
        moment = frame.frame.ts
        out: list[Violation] = []

        # 每帧先把「该工位有没有人」算出来：没人 → 立即清零（口径 1）
        present: dict[tuple[int, str], PersonState] = {}
        for state in frame.persons:
            if state.station_id is None:
                continue
            for kind in _cases(state):
                key = (state.station_id, kind)
                # 同一工位同类多人违规时保留置信度最高的那个作为「代表」
                current = present.get(key)
                if current is None or state.conf > current.conf:
                    present[key] = state

        # 工位无人 → 删掉该工位的所有状态
        empty_stations = {
            sid for sid in {k[0] for k in self._tracks}
            if not frame.occupied(sid)
        }
        for key in [k for k in self._tracks if k[0] in empty_stations]:
            del self._tracks[key]
            self._active.pop(key, None)

        # 有新命中：积分
        for key, state in present.items():
            track = self._tracks.get(key)
            if track is None:
                track = _Track(since=moment, last=moment)
                self._tracks[key] = track
            else:
                track.last = moment
                track.hits += 1
                track.missing = 0
            track.recent.append(frame.frame.seq)
            if len(track.recent) > 50:
                del track.recent[:-50]

            if self._ready(key, track):
                out.append(self._fire(key, track, state, moment, frame))

        # 人在工位但本帧合规：延迟清零（口径 1 的后半）
        for key, track in list(self._tracks.items()):
            if key in present:
                continue
            if key[0] in empty_stations:
                continue                       # 上面已删
            track.missing += 1
            if track.missing >= self.policy.clear_misses:
                del self._tracks[key]
                self._active.pop(key, None)

        return out

    def _ready(self, key: tuple[int, str], track: _Track) -> bool:
        if track.hits < self.policy.min_frames:
            return False
        if (track.last - track.since).total_seconds() < self.policy.min_sec:
            return False
        if track.fired_at is None:
            return True
        # 冷却期内不重报
        return (track.last - track.fired_at).total_seconds() >= self.policy.cooldown_sec

    def _fire(self, key: tuple[int, str], track: _Track, state: PersonState,
              moment: datetime, frame: VisionFrame) -> Violation:
        repeat = track.fired_at is not None
        track.fired_at = moment
        violation = Violation(
            kind=key[1],
            station_id=key[0],
            since=track.since,
            last=moment,
            frames=track.hits,
            person=state,
            evidence_seq=track.hits_window(self.policy.evidence_window),
            repeat=repeat,
        )
        self._active[key] = violation
        log.info("违规：%s", violation.describe())
        return violation

    # ---------------------------------------------------------------- 查询
    def active(self, now: datetime | None = None) -> list[Violation]:
        """当前仍在成立的违规（按最后一次命中时间倒序）。

        `now` 给了就按「最后一帧 + 宽限」过滤：断电后人走了，视觉不再命中，
        但状态要留一小会儿，复核页面才不会瞬间变空。
        """
        moment = now or (self.last_frame.frame.ts if self.last_frame else None)
        out = []
        for violation in self._active.values():
            if moment is None:
                out.append(violation)
                continue
            if (moment - violation.last).total_seconds() <= ACTIVE_GRACE_SEC:
                out.append(violation)
        return sorted(out, key=lambda v: v.last, reverse=True)

    def clear_station(self, station_id: int) -> int:
        """人工处置完成后清该工位状态（否则复核完还在「活跃违规」里挂着）。"""
        keys = [k for k in self._tracks if k[0] == int(station_id)]
        for key in keys:
            del self._tracks[key]
        self._active = {k: v for k, v in self._active.items() if k[0] != int(station_id)}
        return len(keys)

    def reset(self) -> None:
        self._tracks.clear()
        self._active.clear()

    def snapshot(self) -> dict:
        return {
            "tracks": len(self._tracks),
            "active": [v.to_payload() for v in self.active()],
            "policy": {
                "min_frames": self.policy.min_frames,
                "min_sec": self.policy.min_sec,
                "cooldown_sec": self.policy.cooldown_sec,
            },
        }
