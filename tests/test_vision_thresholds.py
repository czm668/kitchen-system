# -*- coding: utf-8 -*-
"""视觉阈值台账 ↔ 代码默认值的一致性守卫（审查报告 §3 隐式常量单一来源）。

阈值散落在 AnalysisPolicy / ViolationPolicy / AbsencePolicy / BLIND_FRAMES /
YoloDetector 五处，改一处漏一处是典型静默故障（比如把 person_conf 调高却忘了
同步设计文档，答辩口径对不上）。

做法：**不改运行时代码**——各 dataclass 默认值仍是真值、现有测试零回归；
本测试断言 `fixtures/kitchen_constants.json` 的 vision 台账与这些默认值逐字一致。
谁改了一边忘了另一边，CI 第一关就会红。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from kitchen.constants import vision_thresholds  # noqa: E402
from kitchen.vision.absence import ABSENCE_SEC, AbsencePolicy  # noqa: E402
from kitchen.vision.detector import YoloDetector  # noqa: E402
from kitchen.vision.pipeline import BLIND_FRAMES  # noqa: E402
from kitchen.vision.postprocess import AnalysisPolicy  # noqa: E402
from kitchen.vision.violations import ACTIVE_GRACE_SEC, ViolationPolicy  # noqa: E402


def test_台账与AnalysisPolicy默认值一致():
    t = vision_thresholds()["analysis"]
    p = AnalysisPolicy()
    for key in ("min_conf", "person_conf", "behavior_conf", "head_ratio",
                "mask_ratio", "torso_top", "torso_bottom", "attach_overlap"):
        assert t[key] == getattr(p, key), f"analysis.{key} 台账={t[key]} 代码={getattr(p, key)}"


def test_台账与ViolationPolicy默认值一致():
    t = vision_thresholds()["violation"]
    p = ViolationPolicy()
    for key in ("min_frames", "min_sec", "cooldown_sec", "clear_misses", "evidence_window"):
        assert t[key] == getattr(p, key), f"violation.{key} 台账={t[key]} 代码={getattr(p, key)}"
    assert t["active_grace_sec"] == ACTIVE_GRACE_SEC


def test_台账与AbsencePolicy默认值一致():
    t = vision_thresholds()["absence"]
    p = AbsencePolicy()
    assert t["absence_sec"] == p.absence_sec == ABSENCE_SEC
    assert t["return_sec"] == p.return_sec
    assert t["cooldown_sec"] == p.cooldown_sec


def test_台账与pipeline遮挡阈值一致():
    assert vision_thresholds()["pipeline"]["blind_frames"] == BLIND_FRAMES


def test_台账与模型推理默认一致():
    t = vision_thresholds()["model"]
    det = YoloDetector("weights/kitchen.pt")
    assert t["conf"] == det.conf
    assert t["imgsz"] == det.imgsz
    # iou 只存在于训练日志（ultralytics 内部 NMS），台账必须记录该来源
    assert t["iou"] == 0.7
