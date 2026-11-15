# -*- coding: utf-8 -*-
"""视觉域（A5）：帧源 → 检测 → 判定 → 处置。

| 模块 | 职责 | 功能编号 |
|---|---|---|
| `stream` | 帧源契约（MJPEG 真机 / 目录回放 / 内存 / 合成）+ 时间戳单调化 + 看门狗 | A5-01 |
| `detector` | 检测器契约（YOLOv8 真机 / 脚本假检测） | A5-02 |
| `models` | 框、检测、人员状态、违规类型与「哪类切电」 | A5-02 |
| `postprocess` | 检测框 → 人员状态（ROI 归属、穿戴匹配、正向证据优先） | A5-02 |
| `violations` | 跨帧去抖 → 违规事件 | A5-02 / A5-04 |
| `evidence` | 抓拍多图 + 哈希 + 元数据（可验签） | A5-03 |
| `disposal` | 切工位插座 → 语音 → 双通道复核 → 恢复 → 工单 | A5-04 |
| `absence` | 连续离岗 → 未开火任务转派 | A5-05 |
| `link` | VisionFrame → `FlameGuard`（#3） | A5-06 |
| `pipeline` | 一帧一调的主循环 + 视觉被遮挡判定 | A5-01~06 |

## 用哪一层

- 只想验证「判定对不对」→ 直接 `analyze(detections, frame, rois)`，纯函数；
- 想跑完整闭环 → `VisionPipeline(reader, detector, ...)`，逐帧 `tick()`；
- 想接真机 → 把 `detector` 换成 `YoloDetector`，`reader` 换成 MJPEG 帧源，
  其余不动。

## 一条不要越过的线

**视觉域不 import `staff`，也不 import `process`。** 需要人员资质、在岗状态、
工位占用这些事实时，一律通过构造参数注入回调（`on_duty` / `approvers` /
`occupant_of` / `board`）。理由：这是第五个域，它的输入是异步的帧流，
一旦反向依赖业务域，时钟与口径就会分叉——派单说有人在、视觉说没人，
两边都有日志、都对，就是合不起来。
"""
from __future__ import annotations

from kitchen.vision.absence import (
    ABSENCE_SEC,
    AbsenceEvent,
    AbsenceMonitor,
    AbsencePolicy,
    AbsenceReassigner,
    ReassignResult,
)
from kitchen.vision.detector import (
    Detector,
    ScriptedDetector,
    YoloDetector,
    from_yolo_rows,
)
from kitchen.vision.disposal import (
    MIN_CUT_SEC,
    REVIEW_CHANNELS,
    TICKET_TYPE,
    DisposalError,
    DisposalResult,
    DisposalTicket,
    PowerBusyError,
    ReviewerNotAllowed,
    ReviewResult,
    StationPowerBank,
    TooSoonError,
    ViolationDisposal,
    voice_text,
)
from kitchen.vision.evidence import (
    EvidenceError,
    EvidenceFile,
    EvidencePackage,
    EvidenceStore,
    sha256_of,
)
from kitchen.vision.link import FlameVisionLink, LinkResult
from kitchen.vision.models import (
    CIGARETTE,
    CLASSES,
    HAT,
    MASK,
    PERSON,
    PHONE,
    UNIFORM,
    VIOLATION_ACTIONS,
    VIOLATION_LABELS,
    BBox,
    Detection,
    Frame,
    FrameStaleError,
    PersonState,
    StationRoi,
    VisionError,
    VisionFrame,
    ViolationKind,
    cuts_power,
    label_of,
    to_payload,
)
from kitchen.vision.pipeline import BLIND_FRAMES, TickResult, VisionPipeline
from kitchen.vision.postprocess import AnalysisPolicy, analyze, assign_stations
from kitchen.vision.stream import (
    DirectoryFrameSource,
    FrameReader,
    FrameSource,
    MIN_FRAME_INTERVAL_SEC,
    MemoryFrameSource,
    MjpegFrameSource,
    StreamHealth,
    SyntheticFrameSource,
    build_replay,
    extract_jpegs,
)
from kitchen.vision.violations import (
    DEFAULT_POLICY,
    Violation,
    ViolationPolicy,
    ViolationTracker,
)

__all__ = [
    # 帧源（A5-01）
    "FrameSource", "MemoryFrameSource", "DirectoryFrameSource",
    "SyntheticFrameSource", "MjpegFrameSource", "FrameReader", "StreamHealth",
    "build_replay", "extract_jpegs", "MIN_FRAME_INTERVAL_SEC",
    # 检测（A5-02）
    "Detector", "ScriptedDetector", "YoloDetector", "from_yolo_rows",
    "CLASSES", "PERSON", "HAT", "MASK", "UNIFORM", "CIGARETTE", "PHONE",
    # 实体
    "BBox", "Detection", "Frame", "PersonState", "StationRoi", "VisionFrame",
    "ViolationKind", "VIOLATION_LABELS", "VIOLATION_ACTIONS",
    "label_of", "cuts_power", "to_payload",
    # 判定
    "AnalysisPolicy", "analyze", "assign_stations",
    "ViolationTracker", "ViolationPolicy", "Violation", "DEFAULT_POLICY",
    # 证据链（A5-03）
    "EvidenceStore", "EvidencePackage", "EvidenceFile", "EvidenceError", "sha256_of",
    # 处置（A5-04）
    "ViolationDisposal", "StationPowerBank", "DisposalTicket", "DisposalResult",
    "ReviewResult", "DisposalError", "ReviewerNotAllowed", "TooSoonError",
    "PowerBusyError", "REVIEW_CHANNELS", "MIN_CUT_SEC", "TICKET_TYPE", "voice_text",
    # 离岗转派（A5-05）
    "AbsenceMonitor", "AbsencePolicy", "AbsenceEvent", "AbsenceReassigner",
    "ReassignResult", "ABSENCE_SEC",
    # 接 #3（A5-06）
    "FlameVisionLink", "LinkResult",
    # 主循环
    "VisionPipeline", "TickResult", "BLIND_FRAMES",
    # 异常
    "VisionError", "FrameStaleError",
]
