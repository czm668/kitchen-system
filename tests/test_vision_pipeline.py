# -*- coding: utf-8 -*-
"""视觉主循环（A5-01~06 串起来）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from vision_support import P1, ROIS, bare, full_kit, hand_of, person, phone

from kitchen.domain import CST
from kitchen.vision.absence import AbsenceMonitor, AbsencePolicy, AbsenceReassigner
from kitchen.vision.detector import ScriptedDetector
from kitchen.vision.disposal import StationPowerBank, ViolationDisposal
from kitchen.vision.pipeline import VisionPipeline
from kitchen.vision.stream import FrameReader, MemoryFrameSource
from kitchen.vision.violations import ViolationPolicy, ViolationTracker

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)

PIXELS = b"\xff\xd8\xff\xe0fake"


def make_pipeline(script, *, step: float = 0.8, blind_frames: int = 5,
                  occupants: dict[int, int] | None = None,
                  with_disposal: bool = True, with_absence: bool = False,
                  policy=None, absence_sec: float = 2.0, evidence=None):
    frames = [
        __import__("kitchen.vision.models", fromlist=["Frame"]).Frame(
            seq=index + 1, ts=T0 + timedelta(seconds=step * index),
            width=640, height=480, uri=f"f{index}",
        )
        for index in range(len(script))
    ]
    source = MemoryFrameSource(frames=[f for f in frames],
                               pixels={f.uri: PIXELS for f in frames})
    reader = FrameReader(source, clock=lambda: T0)
    reader.open()
    detector = ScriptedDetector(script, strict=True)

    seats = dict(occupants or {})
    tracker = ViolationTracker(policy or ViolationPolicy(min_sec=1.0))
    disposal = None
    if with_disposal:
        disposal = ViolationDisposal(
            StationPowerBank(), tracker=tracker, evidence=evidence,
            approvers=lambda n: True, clock=lambda: T0,
        )
    absence = reassigner = None
    if with_absence:
        absence = AbsenceMonitor(occupant_of=lambda sid: seats.get(sid),
                                 policy=AbsencePolicy(absence_sec=absence_sec),
                                 clock=lambda: T0)
        reassigner = AbsenceReassigner(_FakeBoard())
    return VisionPipeline(
        reader, detector, rois=ROIS, tracker=tracker, disposal=disposal,
        absence=absence, reassigner=reassigner,
        occupant_of=lambda sid: seats.get(sid), blind_frames=blind_frames,
    )


class _NoOrders:
    def open_orders(self):
        return ()


class _FakeBoard:
    """离岗转派只用到这几个方法，不必起真订单。"""

    matcher = None
    orders = _NoOrders()
    repo = None

    def station_staff(self, station_id):
        return None

    def assignments_of(self, staff_id):
        return ()

    def release_assignment(self, item_id):
        return None

    def dispatch_pending(self, **_kwargs):
        return ()

    def busy_staff(self):
        return frozenset()

    def assigned_staff(self):
        return {}

    def stations(self):
        return ()


class TestTick:
    def test_没有帧返回None(self):
        pipe = make_pipeline([])
        assert pipe.tick() is None

    def test_单帧只出vision帧(self):
        pipe = make_pipeline([bare(P1)])
        result = pipe.tick()
        assert result.violations == ()
        assert result.vision.headcount == 1

    def test_连续违规触发处置(self):
        script = [full_kit(P1) + [phone(hand_of(P1))]] * 4
        pipe = make_pipeline(script)
        results = [pipe.tick() for _ in range(4)]
        assert results[0].violations == ()
        fired = [r for r in results if r.violations]
        assert fired, "第 3 帧起应该出违规"
        assert fired[0].violations[0].power is True
        assert fired[0].disposals[0].power_cut is True

    def test_处置同时落盘证据包(self, tmp_path):
        from kitchen.vision.evidence import EvidenceStore
        store = EvidenceStore(tmp_path / "ev", model_name="scripted")
        script = [full_kit(P1) + [phone(hand_of(P1))]] * 4
        pipe = make_pipeline(script, evidence=store)
        disposal = None
        for _ in range(4):
            result = pipe.tick()
            if result.disposals:
                disposal = result.disposals[0]
                break
        assert disposal is not None
        assert disposal.evidence_dir, "违规必须留证"
        packages = store.list_day(T0)
        assert len(packages) == 1
        assert store.verify(packages[0]) == []

    def test_run_跑完(self):
        pipe = make_pipeline([full_kit(P1)] * 3)
        assert len(pipe.run()) == 3

    def test_run_限帧(self):
        pipe = make_pipeline([full_kit(P1)] * 5)
        assert len(pipe.run(max_frames=2)) == 2

    def test_on_tick回调(self):
        seen: list = []
        pipe = make_pipeline([full_kit(P1)])
        pipe.on_tick = seen.append
        pipe.tick()
        assert len(seen) == 1

    def test_snapshot结构(self):
        pipe = make_pipeline([full_kit(P1)])
        pipe.tick()
        snap = pipe.snapshot()
        assert snap["frames"] == 1
        assert snap["violations"]["tracks"] == 0


class TestBlind:
    def test_连续空帧且有占用判遮挡(self):
        pipe = make_pipeline([[]] * 6, blind_frames=5, occupants={1: 1})
        results = [pipe.tick() for _ in range(5)]
        assert results[-1].blind == (1,)
        assert "视觉被遮挡" in results[-1].describe()

    def test_没有占用不判遮挡(self):
        pipe = make_pipeline([[]] * 6, blind_frames=5, occupants={})
        results = [pipe.tick() for _ in range(5)]
        assert all(not r.blind for r in results)

    def test_有人后计数归零(self):
        script = [[]] * 3 + [full_kit(P1)] + [[]] * 3
        pipe = make_pipeline(script, blind_frames=5, occupants={1: 1})
        results = [pipe.tick() for _ in range(7)]
        assert all(not r.blind for r in results), "中间出现人应重置空帧计数"


class TestCompose:
    def test_离岗到阈值后产生事件(self):
        seats = {1: 1}
        script = [full_kit(P1)] + [[]] * 4      # 0.8s 一帧，4 帧 = 3.2s > 2s
        pipe = make_pipeline(script, with_absence=True, occupants=seats,
                             absence_sec=2.0)
        results = [pipe.tick() for _ in range(5)]
        fired = [r for r in results if r.absences]
        assert fired, "人走了超过阈值应该出离岗事件"
        assert fired[0].absences[0].station_id == 1

    def test_quiet标记(self):
        pipe = make_pipeline([full_kit(P1)])
        assert pipe.tick().quiet is True

    def test_一帧里多工位(self):
        from vision_support import P2
        pipe = make_pipeline([bare(P1) + bare(P2)])
        result = pipe.tick()
        assert result.vision.station_headcount == {1: 1, 2: 1}
