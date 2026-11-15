# -*- coding: utf-8 -*-
"""视觉实体与几何（A5-02 的基础）。"""
from __future__ import annotations

import pytest
from vision_support import P1, P2, P_PASS, ROIS, frame, hat, head_of, person

from kitchen.vision.models import (
    VIOLATION_ACTIONS,
    BBox,
    Detection,
    Frame,
    StationRoi,
    VisionError,
    cuts_power,
    label_of,
    to_payload,
    ViolationKind,
)


class TestBBox:
    def test_宽高与中心(self):
        box = BBox(0.1, 0.2, 0.3, 0.6)
        assert box.w == pytest.approx(0.2)
        assert box.h == pytest.approx(0.4)
        assert box.cx == pytest.approx(0.2)
        assert box.cy == pytest.approx(0.4)
        assert box.area == pytest.approx(0.08)

    def test_脚点取底边中点(self):
        box = BBox(0.1, 0.2, 0.3, 0.9)
        assert box.foot == pytest.approx((0.2, 0.9))

    def test_宽高非正直接拒绝(self):
        with pytest.raises(VisionError):
            BBox(0.5, 0.5, 0.5, 0.6)
        with pytest.raises(VisionError):
            BBox(0.1, 0.6, 0.3, 0.6)

    def test_iou_自身为1(self):
        assert BBox(0.1, 0.1, 0.3, 0.3).iou(BBox(0.1, 0.1, 0.3, 0.3)) == pytest.approx(1.0)

    def test_iou_无交叠为0(self):
        assert BBox(0.1, 0.1, 0.2, 0.2).iou(BBox(0.5, 0.5, 0.6, 0.6)) == 0.0

    def test_覆盖比例与iou的区别(self):
        """小框完全落在大框内：IoU 很小，覆盖比例是 1。"""
        big = BBox(0.1, 0.1, 0.5, 0.9)
        small = BBox(0.2, 0.12, 0.4, 0.20)
        assert big.iou(small) < 0.2
        assert big.overlap_ratio(small) == pytest.approx(1.0)

    def test_覆盖比例零面积保护(self):
        big = BBox(0.1, 0.1, 0.5, 0.9)
        assert big.overlap_ratio(BBox(0.9, 0.9, 0.95, 0.95)) == 0.0

    def test_contains_带边距(self):
        box = BBox(0.2, 0.2, 0.4, 0.4)
        assert box.contains((0.3, 0.3))
        assert not box.contains((0.5, 0.3))
        assert box.contains((0.45, 0.3), margin=0.1)

    def test_像素换算(self):
        box = BBox(0.25, 0.5, 0.75, 1.0)
        assert box.to_pixels(640, 480) == (160, 240, 480, 480)

    def test_序列化保留四位(self):
        assert BBox(0.123456, 0.2, 0.3, 0.4).as_tuple()[0] == 0.1235


class TestDetection:
    def test_未知类别拒绝(self):
        with pytest.raises(VisionError):
            Detection("helmet", 0.9, P1)

    def test_置信度越界拒绝(self):
        with pytest.raises(VisionError):
            Detection("person", 1.2, P1)

    def test_描述带坐标(self):
        assert "person 0.92" in person().describe()


class TestFrame:
    def test_年龄按传入时刻算(self):
        from datetime import timedelta
        f = frame(1, ts=None)
        assert f.age_sec(f.ts + timedelta(seconds=5)) == pytest.approx(5.0)

    def test_零尺寸帧(self):
        # Frame 本身不校验；analyze 会拒绝（见 postprocess 测试）
        assert Frame(seq=1, ts=None or frame().ts, width=0, height=0).width == 0


class TestStationRoi:
    def test_按脚点归属(self):
        assert ROIS[0].owns(P1)
        assert ROIS[1].owns(P2)
        assert not ROIS[0].owns(P2)

    def test_路过的人不属于任何工位(self):
        assert not any(roi.owns(P_PASS) for roi in ROIS)

    def test_描述含名称(self):
        assert "灶台A" in ROIS[0].describe()
        assert StationRoi(3, BBox(0, 0, 1, 1)).describe() == "ST3"


class TestViolationKinds:
    def test_五类违规都有中文名(self):
        for kind in ViolationKind.ALL:
            assert label_of(kind) and label_of(kind) != kind

    def test_着装类不切电_行为类切电(self):
        for kind in (ViolationKind.NO_HAT, ViolationKind.NO_MASK,
                     ViolationKind.NO_UNIFORM):
            assert not cuts_power(kind), f"{kind} 不该断电"
        for kind in (ViolationKind.SMOKING, ViolationKind.PHONE):
            assert cuts_power(kind), f"{kind} 应该断电"

    def test_未知类型按不切电处理(self):
        assert not cuts_power("something_new")

    def test_映射覆盖全部类型(self):
        for kind in ViolationKind.ALL:
            assert kind in VIOLATION_ACTIONS


class TestPayload:
    def test_扁平结构可序列化(self):
        from kitchen.vision.postprocess import analyze
        view = analyze([person(), hat(head_of(P1))], frame(3), ROIS)
        payload = to_payload(view)
        assert payload["frame_seq"] == 3
        assert payload["headcount"] == 1
        assert payload["stations"] == {"1": 1}
        assert payload["persons"][0]["station_id"] == 1
        assert payload["empty"] is False
