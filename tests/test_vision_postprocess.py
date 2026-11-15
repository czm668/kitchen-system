# -*- coding: utf-8 -*-
"""检测框 → 人员状态（A5-02）。"""
from __future__ import annotations

import pytest
from vision_support import (
    P1,
    P2,
    P_PASS,
    ROIS,
    bare,
    cigarette,
    frame,
    full_kit,
    hand_of,
    hat,
    head_of,
    mask,
    mask_of,
    person,
    phone,
    torso_of,
    uniform,
)

from kitchen.vision.models import BBox, Detection, VisionError
from kitchen.vision.postprocess import AnalysisPolicy, analyze, analyze_persons, assign_stations


class TestStationAssignment:
    def test_按脚点分到工位(self):
        view = analyze(bare(P1) + bare(P2), frame(), ROIS)
        assert [p.station_id for p in view.persons] == [1, 2]
        assert view.station_headcount == {1: 1, 2: 1}

    def test_路过的人不属于工位但计入总人数(self):
        view = analyze(bare(P_PASS), frame(), ROIS)
        assert view.persons[0].station_id is None
        assert view.headcount == 1
        assert view.station_headcount == {}

    def test_没有ROI时全部不归属(self):
        view = analyze(bare(P1), frame(), ())
        assert view.persons[0].station_id is None

    def test_assign_stations_纯函数(self):
        assert assign_stations([P1, P2, P_PASS], ROIS) == [1, 2, None]

    def test_同一工位两人(self):
        other = BBox(0.05, 0.30, 0.20, 0.95)
        view = analyze(bare(P1) + bare(other), frame(), ROIS)
        assert view.station_headcount == {1: 2}
        assert view.occupied(1) is True
        assert len(view.at_station(1)) == 2


class TestDress:
    def test_穿戴齐全无缺失(self):
        view = analyze(full_kit(P1), frame(), ROIS)
        state = view.persons[0]
        assert state.missing() == ()
        assert (state.has_hat, state.has_mask, state.has_uniform) == (True, True, True)

    def test_什么都没戴(self):
        state = analyze(bare(P1), frame(), ROIS).persons[0]
        assert set(state.missing()) == {"hat", "mask", "uniform"}

    def test_只缺帽子(self):
        dets = [person(P1), mask(mask_of(P1)), uniform(torso_of(P1))]
        state = analyze(dets, frame(), ROIS).persons[0]
        assert state.missing() == ("hat",)

    def test_帽子框挂在腰上不算戴上(self):
        """帽子在手上/腰间时，框落在躯干而不是头部区域。"""
        belt = BBox(P1.x1 + 0.02, P1.y1 + P1.h * 0.70,
                    P1.x2 - 0.02, P1.y1 + P1.h * 0.80)
        state = analyze([person(P1), hat(belt)], frame(), ROIS).persons[0]
        assert state.has_hat is False

    def test_门槛以下的帽子框不算(self):
        weak = Detection("hat", 0.30, head_of(P1))
        state = analyze([person(P1), weak], frame(), ROIS).persons[0]
        assert state.has_hat is False

    def test_低置信度的人被过滤(self):
        weak = Detection("person", 0.35, P1)      # < person_conf 0.40
        view = analyze([weak], frame(), ROIS)
        assert view.headcount == 0
        assert view.empty is True


class TestPositiveEvidence:
    """正向证据优先：数量够就判合规，不管几何匹配成没成。"""

    def test_三人三帽几何全不匹配也算合规(self):
        # 三个人的帽子框全堆在画面顶部同一个角落，几何上谁都对不上
        corner = BBox(0.02, 0.02, 0.10, 0.08)
        boxes = [
            BBox(0.02, 0.30, 0.12, 0.95),
            BBox(0.16, 0.30, 0.26, 0.95),
            BBox(0.30, 0.30, 0.40, 0.95),
        ]
        dets = [person(b) for b in boxes] + [hat(corner)] * 3
        view = analyze(dets, frame(), ROIS)
        assert view.headcount == 3
        assert all(p.has_hat for p in view.persons), "数量够就该判合规，避免误报"

    def test_三人两帽仍逐人判定(self):
        boxes = [
            BBox(0.02, 0.30, 0.12, 0.95),
            BBox(0.16, 0.30, 0.26, 0.95),
            BBox(0.30, 0.30, 0.40, 0.95),
        ]
        # 只给前两个人帽子
        dets = [person(b) for b in boxes] + [hat(head_of(boxes[0])),
                                             hat(head_of(boxes[1]))]
        view = analyze(dets, frame(), ROIS)
        assert [p.has_hat for p in view.persons] == [True, True, False]

    def test_一顶帽子只能证明一个人(self):
        """两个人的帽子框重合时，高置信度那个先占，另一个仍算没戴。"""
        twin = BBox(P1.x1, P1.y1 + 0.02, P1.x2, 0.88)
        dets = [person(P1), person(twin), hat(head_of(P1))]
        view = analyze(dets, frame(), ROIS)
        assert sum(1 for p in view.persons if p.has_hat) == 1


class TestBehavior:
    def test_手机检出_本人命中(self):
        dets = full_kit(P1) + [phone(hand_of(P1))]
        state = analyze(dets, frame(), ROIS).persons[0]
        assert state.using_phone is True
        assert state.behaviors() == ("phone",)

    def test_手机框置信度不足不认(self):
        dets = full_kit(P1) + [Detection("phone", 0.45, hand_of(P1))]
        state = analyze(dets, frame(), ROIS).persons[0]
        assert state.using_phone is False

    def test_两个人时手机归到最近的那个(self):
        dets = (full_kit(P1) + full_kit(P2) + [phone(hand_of(P2))])
        view = analyze(dets, frame(), ROIS)
        flags = {p.station_id: p.using_phone for p in view.persons}
        assert flags == {1: False, 2: True}

    def test_吸烟与手机同时命中(self):
        dets = full_kit(P1) + [cigarette(hand_of(P1)), phone(hand_of(P1))]
        state = analyze(dets, frame(), ROIS).persons[0]
        assert set(state.behaviors()) == {"smoking", "phone"}

    def test_没有人时行为不成立(self):
        view = analyze([phone(hand_of(P1))], frame(), ROIS)
        assert view.empty is True
        assert view.persons == ()


class TestFrameGuard:
    def test_零尺寸帧拒绝(self):
        with pytest.raises(VisionError):
            analyze([], frame(width=0, height=480), ROIS)

    def test_空检测结果(self):
        view = analyze([], frame(), ROIS)
        assert view.empty is True
        assert view.headcount == 0
        assert "无人" in view.describe()

    def test_描述里有工位人数(self):
        view = analyze(bare(P1), frame(), ROIS)
        assert "ST1:1人" in view.describe()


class TestPolicy:
    def test_head_region_比例(self):
        policy = AnalysisPolicy(head_ratio=0.3)
        region = policy.head_region(P1)
        assert region.y2 == pytest.approx(P1.y1 + P1.h * 0.3)

    def test_torso_region_掐掉头部(self):
        region = AnalysisPolicy().torso_region(P1)
        assert region.y1 > P1.y1 + P1.h * 0.1
        assert region.y2 < P1.y2

    def test_自定义assist阈值(self):
        """帽子框跨在头部区域边界上（约一半在内）：阈值决定算不算命中。"""
        straddle = BBox(P1.x1 + 0.02, P1.y1 + P1.h * 0.30,
                        P1.x2 - 0.02, P1.y1 + P1.h * 0.60)
        dets = [person(P1), hat(straddle)]
        loose = analyze(dets, frame(), ROIS, AnalysisPolicy(attach_overlap=0.40))
        strict = analyze(dets, frame(), ROIS, AnalysisPolicy(attach_overlap=0.60))
        assert loose.persons[0].has_hat is True
        assert strict.persons[0].has_hat is False

    def test_单人场景不走正向优先(self):
        """一个人 + 一顶挂在腰上的帽子：单人没有归属歧义，按几何判。"""
        belt = BBox(P1.x1 + 0.02, P1.y1 + P1.h * 0.70,
                    P1.x2 - 0.02, P1.y1 + P1.h * 0.80)
        state = analyze([person(P1), hat(belt)], frame(), ROIS).persons[0]
        assert state.has_hat is False

    def test_analyze_persons_空输入(self):
        assert analyze_persons([], {"hat": [], "mask": [], "uniform": [],
                                    "cigarette": [], "phone": []},
                               AnalysisPolicy(), ROIS) == []
