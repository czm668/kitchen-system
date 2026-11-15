# -*- coding: utf-8 -*-
"""跨帧去抖 → 违规事件（A5-02 / A5-04）。"""
from __future__ import annotations

from datetime import timedelta

from vision_support import P1, P2, P_PASS, ROIS, bare, frame, full_kit, hand_of, phone

from kitchen.vision.models import ViolationKind
from kitchen.vision.postprocess import analyze
from kitchen.vision.violations import ViolationPolicy, ViolationTracker

from kitchen.domain import CST
from datetime import datetime

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


def feed(tracker: ViolationTracker, dets, *, seconds: int, step: float = 0.8,
         start_seq: int = 1, start_at: float = 0.0, rois=ROIS) -> list:
    """按固定间隔喂 N 帧同样的检测结果，收集所有产出的违规。

    `start_at` 是相对 T0 的起始秒数——冷却重报的用例必须把时间往后推，
    否则第二段的时间戳比第一段还早，`last - fired_at` 是负数。
    """
    out = []
    for index in range(seconds):
        seq = start_seq + index
        ts = T0 + timedelta(seconds=start_at + step * index)
        out.extend(tracker.observe(analyze(dets, frame(seq, ts=ts), rois)))
    return out


class TestDebounce:
    def test_单帧不报(self):
        tracker = ViolationTracker()
        assert feed(tracker, bare(P1), seconds=1) == []

    def test_两帧不报(self):
        tracker = ViolationTracker()
        assert feed(tracker, bare(P1), seconds=2) == []

    def test_三帧且够时长才报(self):
        tracker = ViolationTracker()
        out = feed(tracker, bare(P1), seconds=3)
        assert len(out) == 3, "三种缺失各报一条"
        assert {v.kind for v in out} == {
            ViolationKind.NO_HAT, ViolationKind.NO_MASK, ViolationKind.NO_UNIFORM}
        assert all(v.station_id == 1 for v in out)

    def test_帧数够但时长不够不报(self):
        tracker = ViolationTracker(ViolationPolicy(min_frames=3, min_sec=5.0))
        assert feed(tracker, bare(P1), seconds=3, step=0.8) == []

    def test_穿戴齐全不报(self):
        tracker = ViolationTracker()
        assert feed(tracker, full_kit(P1), seconds=5) == []

    def test_只缺一项只报一条(self):
        from vision_support import hat, head_of, mask, mask_of, person, torso_of, uniform
        tracker = ViolationTracker()
        dets = [person(P1), mask(mask_of(P1)), uniform(torso_of(P1))]
        out = feed(tracker, dets, seconds=4)
        assert [v.kind for v in out] == [ViolationKind.NO_HAT]


class TestClearRules:
    def test_人离开工位立即清零_回来不立刻报(self):
        """人走了状态还在的话，回来的第一帧就满足「持续 3 秒」，现场表现是刚站回去就被报警。"""
        tracker = ViolationTracker()
        feed(tracker, bare(P1), seconds=2)                      # 累计两帧
        assert tracker.observe(analyze([], frame(9, ts=T0 + timedelta(seconds=5)), ROIS)) == []
        out = feed(tracker, bare(P1), seconds=2, start_seq=10)  # 回来两帧
        assert out == [], "回来后必须重新积分，不能凭残留状态立刻报"

    def test_人在但合规_隔帧抖动仍能积分(self):
        """检出一帧帽子成功不清零，否则 3 帧规则永远凑不满。"""
        tracker = ViolationTracker(ViolationPolicy(clear_misses=3))
        from vision_support import hat, head_of, mask, mask_of, person, torso_of, uniform
        with_hat = [person(P1), hat(head_of(P1)), mask(mask_of(P1)), uniform(torso_of(P1))]
        without = [person(P1), mask(mask_of(P1)), uniform(torso_of(P1))]
        tracker.observe(analyze(without, frame(1, ts=T0), ROIS))
        tracker.observe(analyze(with_hat, frame(2, ts=T0 + timedelta(seconds=0.8)), ROIS))
        tracker.observe(analyze(without, frame(3, ts=T0 + timedelta(seconds=1.6)), ROIS))
        out = tracker.observe(analyze(without, frame(4, ts=T0 + timedelta(seconds=2.4)), ROIS))
        assert [v.kind for v in out] == [ViolationKind.NO_HAT]

    def test_连续合规满次数才清状态(self):
        tracker = ViolationTracker(ViolationPolicy(clear_misses=3))
        feed(tracker, bare(P1), seconds=2)
        for index in range(3):
            tracker.observe(analyze(full_kit(P1), frame(20 + index,
                              ts=T0 + timedelta(seconds=3 + index * 0.8)), ROIS))
        assert tracker.snapshot()["tracks"] == 0


class TestRepeat:
    def test_持续违规只报一次(self):
        tracker = ViolationTracker(ViolationPolicy(cooldown_sec=999))
        out = feed(tracker, bare(P1), seconds=10)
        assert len(out) == 3, "三类各报一次，不按帧重报"

    def test_冷却后重报并标记repeat(self):
        tracker = ViolationTracker(ViolationPolicy(min_sec=1.0, cooldown_sec=2.0))
        first = feed(tracker, bare(P1), seconds=4)
        later = feed(tracker, bare(P1), seconds=4, start_seq=20, start_at=10.0)
        assert first and not any(v.repeat for v in first)
        assert later and all(v.repeat for v in later)

    def test_重复报不会清空人工复核(self):
        """切电去重的前置条件：报过之后不再产生新事件。"""
        tracker = ViolationTracker(ViolationPolicy(cooldown_sec=999))
        out = feed(tracker, bare(P1), seconds=15)
        assert len([v for v in out if v.kind == ViolationKind.NO_HAT]) == 1


class TestMultiPerson:
    def test_同工位多人按最差报一条(self):
        other = P1.__class__(0.05, 0.30, 0.20, 0.95)
        tracker = ViolationTracker()
        dets = full_kit(P1) + bare(other)     # 一人合规、一人裸
        out = feed(tracker, dets, seconds=4)
        assert len(out) == 3, "报三条（三类），不是按人头六条"
        assert all(v.station_id == 1 for v in out)

    def test_两个工位各自报(self):
        tracker = ViolationTracker()
        out = feed(tracker, bare(P1) + bare(P2), seconds=4)
        stations = {v.station_id for v in out}
        assert stations == {1, 2}

    def test_路过的人不产生违规(self):
        tracker = ViolationTracker()
        assert feed(tracker, bare(P_PASS), seconds=6) == []


class TestActions:
    def test_着装类不切电(self):
        tracker = ViolationTracker()
        out = feed(tracker, bare(P1), seconds=4)
        assert all(not v.power for v in out)

    def test_行为类切电(self):
        tracker = ViolationTracker()
        out = feed(tracker, full_kit(P1) + [phone(hand_of(P1))], seconds=4)
        assert [v.kind for v in out] == [ViolationKind.PHONE]
        assert out[0].power is True

    def test_证据帧窗口(self):
        tracker = ViolationTracker(ViolationPolicy(evidence_window=3))
        out = feed(tracker, bare(P1), seconds=5)
        target = next(v for v in out if v.kind == ViolationKind.NO_HAT)
        assert len(target.evidence_seq) == 3
        assert target.evidence_seq[-1] == 3


class TestQueries:
    def test_active_含已报事件(self):
        tracker = ViolationTracker()
        feed(tracker, bare(P1), seconds=4)
        assert len(tracker.active()) == 3

    def test_active_超过宽限被过滤(self):
        tracker = ViolationTracker()
        feed(tracker, bare(P1), seconds=4)
        later = T0 + timedelta(seconds=60)
        assert tracker.active(now=later) == []

    def test_clear_station(self):
        tracker = ViolationTracker()
        feed(tracker, bare(P1) + bare(P2), seconds=4)
        removed = tracker.clear_station(1)
        assert removed == 3
        assert all(v.station_id == 2 for v in tracker.active())

    def test_snapshot_结构(self):
        tracker = ViolationTracker()
        feed(tracker, bare(P1), seconds=4)
        snap = tracker.snapshot()
        assert snap["tracks"] == 3
        assert len(snap["active"]) == 3
        assert snap["policy"]["min_frames"] == 3

    def test_describe_含工位与标签(self):
        tracker = ViolationTracker()
        out = feed(tracker, bare(P1), seconds=4)
        text = next(v for v in out if v.kind == ViolationKind.NO_HAT).describe()
        assert "ST1" in text and "未戴厨师帽" in text


class TestPolicyForFps:
    def test_帧率越高所需帧数越多(self):
        """5fps 下 1.5 秒约 9 帧，1fps 下 1.5 秒只有 1~2 帧；换算后两者都覆盖 ~1.5s。"""
        assert ViolationPolicy.for_fps(10.0).min_frames > ViolationPolicy.for_fps(1.0).min_frames

    def test_帧数不低于2(self):
        assert ViolationPolicy.for_fps(0.2).min_frames >= 2
