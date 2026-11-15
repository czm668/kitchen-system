# -*- coding: utf-8 -*-
"""视觉主循环（把帧源、检测、判定、处置串起来）。

```
FrameReader.read()           一帧（时间戳已单调化）
   ↓ detector.detect()       Detection 列表（模型原样输出）
   ↓ postprocess.analyze()   VisionFrame（谁在哪个工位、戴没戴）
   ├→ violations.observe()   违规事件 → disposal.dispose()（#16 切电/语音/工单）
   ├→ absence.observe()      离岗事件 → reassigner.handle()（#11 转派）
   └→ link.push()            → FlameGuard（#3 明火离人）
```

## 「视觉被遮挡」单独报

连续 `blind_frames` 帧一个人都检不出，而某个工位的占用会话还开着——这不是
「厨师离岗」，是**摄像头被挡了或坏了**。两者在数据上完全一样（都是 0 人），
但处置差得很远：离岗要转派，被挡要喊人来修。

现实中「把镜头转开」是逃避监管最省事的办法，所以这条必须报，而且要和
离岗分开展示（`TickResult.blind`）。判据用「有活动占用 + 连续空帧」，
比看亮度稳：不用碰像素，也就不依赖帧源给什么。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .absence import AbsenceEvent, AbsenceMonitor, AbsenceReassigner, ReassignResult
from .detector import Detector
from .disposal import DisposalResult, ViolationDisposal
from .link import FlameVisionLink, LinkResult
from .models import Frame, StationRoi, VisionFrame
from .postprocess import AnalysisPolicy, analyze
from .stream import FrameReader, StreamHealth
from .violations import Violation, ViolationPolicy, ViolationTracker

log = logging.getLogger("kitchen.vision.pipeline")

__all__ = ["VisionPipeline", "TickResult", "BLIND_FRAMES"]

#: 连续这么多空帧才判「视觉被遮挡」。5fps 下约 4 秒
BLIND_FRAMES = 20


@dataclass
class TickResult:
    """一帧处理完的全部产出。**空字段一律留空，不要塞 None 占位。**"""

    frame: Frame
    vision: VisionFrame
    violations: tuple[Violation, ...] = ()
    disposals: tuple[DisposalResult, ...] = ()
    absences: tuple[AbsenceEvent, ...] = ()
    reassigns: tuple[ReassignResult, ...] = ()
    link: LinkResult | None = None
    #: 本帧被判定为「视觉被遮挡」的工位
    blind: tuple[int, ...] = ()
    health: StreamHealth | None = None

    @property
    def quiet(self) -> bool:
        return not (self.violations or self.absences or self.blind)

    def describe(self) -> str:
        bits = [self.vision.describe()]
        for item in self.violations:
            bits.append("违规 " + item.describe())
        for event in self.absences:
            bits.append("离岗 " + event.describe())
        if self.blind:
            bits.append("⚠ 视觉被遮挡：" + "、".join(f"ST{s}" for s in self.blind))
        return "｜".join(bits)


class VisionPipeline:
    """一帧一调。**不自己起线程/定时器**——调度交给 APScheduler（A6），
    这样单测可以逐帧驱动，不用 sleep。
    """

    def __init__(
        self,
        reader: FrameReader,
        detector: Detector,
        *,
        rois: Sequence[StationRoi] = (),
        policy: AnalysisPolicy | None = None,
        tracker: ViolationTracker | None = None,
        violation_policy: ViolationPolicy | None = None,
        disposal: ViolationDisposal | None = None,
        absence: AbsenceMonitor | None = None,
        reassigner: AbsenceReassigner | None = None,
        link: FlameVisionLink | None = None,
        occupant_of: Callable[[int], int | None] | None = None,
        blind_frames: int = BLIND_FRAMES,
        on_tick: Callable[[TickResult], None] | None = None,
    ) -> None:
        self.reader = reader
        self.detector = detector
        self.rois = tuple(rois)
        self.policy = policy or AnalysisPolicy()
        self.tracker = tracker or ViolationTracker(violation_policy)
        self.disposal = disposal
        self.absence = absence
        self.reassigner = reassigner
        self.link = link
        self.occupant_of = occupant_of
        self.blind_frames = int(blind_frames)
        self.on_tick = on_tick
        self._empty_streak = 0

    # ---------------------------------------------------------------- 单帧
    def tick(self) -> TickResult | None:
        """处理一帧。没有新帧返回 None（不是异常）。"""
        frame = self.reader.read()
        if frame is None:
            return None
        payload = self.reader.read_bytes(frame)
        detections = self.detector.detect(frame, payload)
        vision = analyze(detections, frame, self.rois, self.policy)

        # 违规 → 处置
        violations = self.tracker.observe(vision)
        disposals: list[DisposalResult] = []
        if self.disposal is not None:
            for violation in violations:
                frames = self._evidence_frames(violation, frame)
                disposals.append(self.disposal.dispose(
                    violation, frames=frames,
                    read_bytes=self._read_seq,
                    now=frame.ts,
                ))

        # 离岗 → 转派
        absences: list[AbsenceEvent] = []
        reassigns: list[ReassignResult] = []
        if self.absence is not None:
            absences = self.absence.observe(vision)
            if self.reassigner is not None:
                for event in absences:
                    reassigns.append(self.reassigner.handle(event))

        # 视觉 → #3
        link_result = None
        if self.link is not None:
            link_result = self.link.push(vision, online=self._stream_online())

        blind = self._blind_stations(vision)

        result = TickResult(
            frame=frame, vision=vision,
            violations=tuple(violations), disposals=tuple(disposals),
            absences=tuple(absences), reassigns=tuple(reassigns),
            link=link_result, blind=blind, health=self.reader.health,
        )
        if self.on_tick is not None:
            self.on_tick(result)
        return result

    def run(self, *, max_frames: int | None = None) -> list[TickResult]:
        """一直跑到没有帧为止。回放/演示用。"""
        out: list[TickResult] = []
        while max_frames is None or len(out) < max_frames:
            result = self.tick()
            if result is None:
                break
            out.append(result)
        return out

    # ---------------------------------------------------------------- 遮挡
    def _blind_stations(self, vision: VisionFrame) -> tuple[int, ...]:
        if not vision.empty:
            self._empty_streak = 0
            return ()
        self._empty_streak += 1
        if self._empty_streak < self.blind_frames or self.occupant_of is None:
            return ()
        # 占用会话还开着却一个都看不见 → 镜头大概率被挡/被转开
        stations: list[int] = []
        for roi in self.rois:
            if self.occupant_of(roi.station_id) is not None:
                stations.append(roi.station_id)
        if stations:
            log.warning("连续 %d 帧无人但工位仍有占用，疑似视觉被遮挡：%s",
                        self._empty_streak, stations)
        return tuple(sorted(stations))

    # ---------------------------------------------------------------- 抓拍
    def _evidence_frames(self, violation: Violation,
                         current: Frame) -> list[tuple[int, datetime]]:
        """取违规窗口里的帧序号。**当前帧一定在最后**（它不是从历史里取的）。"""
        seqs = [s for s in violation.evidence_seq if s != current.seq]
        out = [(seq, current.ts) for seq in seqs]
        out.append((current.seq, current.ts))
        return out

    def _read_seq(self, seq: int) -> bytes:
        """按帧序号取像素。回放场景（`MemoryFrameSource`）能直接取；真机只能
        取到最后 N 帧的缓存，取不到就返回空字节（证据层会跳过）。"""
        source = self.reader.source
        frames = getattr(source, "frames", None)
        if frames is None:
            return b""
        for frame in frames:
            if frame.seq == seq:
                return self.reader.read_bytes(frame)
        return b""

    def _stream_online(self) -> bool:
        return bool(self.reader.health.online)

    # ---------------------------------------------------------------- 状态
    def snapshot(self) -> dict:
        return {
            "frames": self.reader.health.frames,
            "online": self.reader.health.online,
            "empty_streak": self._empty_streak,
            "violations": self.tracker.snapshot(),
            "absence": self.absence.snapshot() if self.absence else None,
            "disposal": self.disposal.snapshot() if self.disposal else None,
            "link": self.link.snapshot() if self.link else None,
        }
