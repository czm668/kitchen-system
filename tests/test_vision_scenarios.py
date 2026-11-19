# -*- coding: utf-8 -*-
"""视觉时序剧本闭环测试（fixtures/vision_scenarios/*.json）。

单测多是"单帧造一个 Detection 列表"，覆盖不到去抖/遮挡/离岗这些**时间维度**
的行为。本文件用 `ScriptedDetector.from_json()` 逐帧喂真实剧本，驱动
`VisionPipeline.tick()` 全链路，验证：
  01 逐步戴帽：先报 no_hat，戴齐后停止
  02 帽子短暂遮挡：单帧遮挡不触发违规（去抖生效）
  03 擦汗误判手机 1 帧：不触发 phone 违规
  04 两人分站：按工位算人数，玩手机的 ST1 报违规、ST2 不报
  05 镜头连续遮挡：有占用却连续空帧 → 判 blind
  06 离岗返回：有人→离开→回来，离岗事件只报一次

剧本由 tools/gen_vision_scenarios.py 从权威 vision_support 几何生成，坐标不手抄。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT / "src"), str(_ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from kitchen.domain import CST  # noqa: E402
from kitchen.vision.absence import AbsenceMonitor, AbsencePolicy, AbsenceReassigner  # noqa: E402
from kitchen.vision.detector import ScriptedDetector  # noqa: E402
from kitchen.vision.disposal import StationPowerBank, ViolationDisposal  # noqa: E402
from kitchen.vision.models import Frame  # noqa: E402
from kitchen.vision.pipeline import VisionPipeline  # noqa: E402
from kitchen.vision.stream import FrameReader, MemoryFrameSource  # noqa: E402
from kitchen.vision.violations import ViolationPolicy, ViolationTracker  # noqa: E402
from vision_support import ROIS  # noqa: E402

SCENARIOS = _ROOT / "fixtures" / "vision_scenarios"
T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)
PIXELS = b"\xff\xd8\xff\xe0fake"


def pipeline_from(scenario: str, *, step: float = 0.8, blind_frames: int = 5,
                  occupants: dict[int, int] | None = None,
                  absence_sec: float = 3.0, min_sec: float = 1.0):
    script = ScriptedDetector.from_json(SCENARIOS / f"{scenario}.json", strict=True)
    n = len(script.script)
    frames = [Frame(seq=i + 1, ts=T0 + timedelta(seconds=step * i),
                    width=640, height=480, uri=f"s{i}")
              for i in range(n)]
    source = MemoryFrameSource(frames=list(frames),
                               pixels={f.uri: PIXELS for f in frames})
    reader = FrameReader(source, clock=lambda: T0)
    reader.open()
    seats = dict(occupants or {})
    tracker = ViolationTracker(ViolationPolicy(min_sec=min_sec))
    disposal = ViolationDisposal(
        StationPowerBank(), tracker=tracker, evidence=None,
        approvers=lambda n: True, clock=lambda: T0,
    )
    absence = AbsenceMonitor(
        occupant_of=lambda sid: seats.get(sid),
        policy=AbsencePolicy(absence_sec=absence_sec), clock=lambda: T0,
    )
    reassigner = AbsenceReassigner(_FakeBoard())
    pipe = VisionPipeline(
        reader, script, rois=ROIS, tracker=tracker, disposal=disposal,
        absence=absence, reassigner=reassigner,
        occupant_of=lambda sid: seats.get(sid), blind_frames=blind_frames,
    )
    return pipe, n


class _NoOrders:
    def open_orders(self):
        return ()


class _FakeBoard:
    matcher = None
    orders = _NoOrders()
    repo = None

    def station_staff(self, station_id):
        return 101 if station_id == 1 else None

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


class TestScenarios:
    def test_01_逐步戴帽_报后清零(self):
        pipe, n = pipeline_from("01_don_hat")
        results = [pipe.tick() for _ in range(n)]
        fired = [(i, r) for i, r in enumerate(results) if r.violations]
        assert fired, "连续 3 帧没戴帽应触发 no_hat"
        kinds = {v.kind for _, r in fired for v in r.violations}
        assert "no_hat" in kinds
        # 戴齐之后（后 3 帧）不应再新增违规
        tail_fired = [i for i, r in fired if i >= 3]
        assert not tail_fired, "戴齐帽子后不应继续报违规"

    def test_02_帽子短暂遮挡_去抖不误报(self):
        pipe, n = pipeline_from("02_hat_occluded")
        results = [pipe.tick() for _ in range(n)]
        # 只有第 3 帧缺帽，去抖需要连续 3 帧，全程不应报 no_hat
        assert all(not r.violations for r in results), "单帧遮挡不应触发违规"

    def test_03_擦汗误判手机一帧_不报(self):
        pipe, n = pipeline_from("03_wipe_sweat")
        results = [pipe.tick() for _ in range(n)]
        assert all(not r.violations for r in results), "手机只出现 1 帧不应触发 phone"

    def test_04_两人分站_只报玩手机那站(self):
        pipe, n = pipeline_from("04_two_stations")
        results = [pipe.tick() for _ in range(n)]
        assert results[0].vision.headcount == 2
        assert results[0].vision.station_headcount[1] == 1
        assert results[0].vision.station_headcount[2] == 1
        fired = [(i, r) for i, r in enumerate(results) if r.violations]
        assert fired, "ST1 连续玩手机应触发"
        kinds = {v.kind for _, r in fired for v in r.violations}
        assert "phone" in kinds
        # 违规归属 ST1，不是穿戴齐全的 ST2
        stations = {v.station_id for _, r in fired for v in r.violations}
        assert stations == {1}

    def test_05_镜头连续遮挡_判blind(self):
        pipe, n = pipeline_from("05_camera_blocked", occupants={1: 101}, blind_frames=5)
        results = [pipe.tick() for _ in range(n)]
        # 前 2 帧有人不盲；后面连续空帧超过阈值，应判 ST1 视觉被遮挡
        assert not results[0].blind
        assert any(1 in r.blind for r in results), "有占用却连续空帧应判遮挡"

    def test_06_离岗返回_只报一次(self):
        pipe, n = pipeline_from("06_absence_return", occupants={1: 101}, absence_sec=2.0)
        results = [pipe.tick() for _ in range(n)]
        absences = [a for r in results for a in r.absences]
        assert absences, "离开超过阈值应报离岗"
        # 同工位冷却期内只报一次
        assert len(absences) == 1, f"离岗应只报一次，实际 {len(absences)} 次"


class TestScenarioIntegrity:
    def test_六个剧本齐全(self):
        names = {p.stem for p in SCENARIOS.glob("*.json")}
        assert names == {
            "01_don_hat", "02_hat_occluded", "03_wipe_sweat",
            "04_two_stations", "05_camera_blocked", "06_absence_return",
        }

    def test_生成器可复现_只读校验(self):
        """--check 只读比对生成器输出与磁盘 JSON（不写文件，避免测试副作用）。"""
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "gen_vision_scenarios.py"), "--check"],
            capture_output=True, text=True)
        assert proc.returncode == 0, (
            "剧本 JSON 与生成器不一致：请改生成器后重跑，别手改 JSON\n" + proc.stderr)
