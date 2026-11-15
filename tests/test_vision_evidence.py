# -*- coding: utf-8 -*-
"""抓拍与证据链（A5-03）。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from vision_support import P1, ROIS, bare, frame

from kitchen.vision.evidence import (
    MAX_EVIDENCE_FILES,
    EvidenceError,
    EvidenceStore,
    sha256_of,
)
from kitchen.vision.models import VisionError
from kitchen.vision.postprocess import analyze
from kitchen.vision.violations import Violation, ViolationKind

from kitchen.domain import CST
from datetime import datetime

T0 = datetime(2026, 11, 12, 11, 32, 45, tzinfo=CST)


def make_violation(kind: str = ViolationKind.SMOKING, station_id: int = 1) -> Violation:
    state = analyze(bare(P1), frame(5, ts=T0), ROIS).persons[0]
    return Violation(kind=kind, station_id=station_id, since=T0,
                     last=T0 + timedelta(seconds=2), frames=4, person=state,
                     evidence_seq=(1, 2, 3, 4, 5))


def make_store(tmp_path, **kwargs) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence", **kwargs)


JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body" + b"x" * 100


class TestCapture:
    def test_生成目录与文件(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0), (2, T0 + timedelta(seconds=1))],
                                lambda seq: JPEG)
        assert package.path.is_dir()
        assert (package.path / "meta.json").is_file()
        assert len(package.files) == 2
        assert package.event_id == "20261112T113245-ST1-smoking"

    def test_目录按日期与工位分层(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(station_id=2), [(1, T0)], lambda s: JPEG)
        assert package.path.parent.name == "ST2"
        assert package.path.parent.parent.name == "20261112"

    def test_meta_含哈希与模型版本(self, tmp_path):
        store = make_store(tmp_path, model_name="yolov8", model_version="kitchen-v3",
                           policy={"min_frames": 3})
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        meta = package.meta
        assert meta["model"] == {"name": "yolov8", "version": "kitchen-v3"}
        assert meta["policy"] == {"min_frames": 3}
        assert meta["files"][0]["sha256"] == sha256_of(JPEG)
        assert meta["power_cut"] is True

    def test_文件名带序号与时刻(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(7, T0)], lambda s: JPEG)
        assert package.files[0].name.startswith("0007-113245")

    def test_primary_是最后一张(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(),
                                [(1, T0), (2, T0 + timedelta(seconds=1))], lambda s: JPEG)
        assert package.primary().frame_seq == 2

    def test_不留part临时文件(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        assert not list(package.path.glob("*.part"))

    def test_空帧列表拒绝(self, tmp_path):
        with pytest.raises(EvidenceError):
            make_store(tmp_path).capture(make_violation(), [], lambda s: JPEG)

    def test_全部读不到拒绝生成空包(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(EvidenceError, match="拒绝生成空证据包"):
            store.capture(make_violation(), [(1, T0)], lambda s: b"")

    def test_部分帧读不到跳过(self, tmp_path):
        store = make_store(tmp_path)

        def reader(seq):
            if seq == 2:
                raise OSError("文件没了")
            return JPEG

        package = store.capture(make_violation(),
                                [(1, T0), (2, T0), (3, T0)], reader)
        assert len(package.files) == 2
        assert package.meta["skipped_frames"] == [2]

    def test_帧数超上限只留最后几张(self, tmp_path):
        store = make_store(tmp_path)
        frames = [(i, T0 + timedelta(seconds=i)) for i in range(1, MAX_EVIDENCE_FILES + 6)]
        package = store.capture(make_violation(), frames, lambda s: JPEG)
        assert len(package.files) == MAX_EVIDENCE_FILES
        assert package.files[-1].frame_seq == MAX_EVIDENCE_FILES + 5

    def test_extra字段落到meta(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG,
                                extra={"order_no": "A001"})
        assert package.meta["extra"]["order_no"] == "A001"


class TestVerify:
    def test_完好返回空列表(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        assert store.verify(package) == []

    def test_改一个字节就报哈希不符(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        target = package.path / package.files[0].name
        target.write_bytes(JPEG + b"tampered")
        problems = store.verify(package.path)
        assert any("大小不符" in p or "哈希不符" in p for p in problems)

    def test_删掉图报缺文件(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        (package.path / package.files[0].name).unlink()
        assert any("缺文件" in p for p in store.verify(package.path))

    def test_缺meta报错(self, tmp_path):
        store = make_store(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(EvidenceError, match="meta.json"):
            store.verify(empty)

    def test_meta损坏报错(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        (package.path / "meta.json").write_text("{ 坏 json", encoding="utf-8")
        with pytest.raises(EvidenceError, match="无法解析"):
            store.verify(package.path)


class TestQuery:
    def test_load_读回(self, tmp_path):
        store = make_store(tmp_path)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        loaded = store.load(package.path)
        assert loaded.event_id == package.event_id
        assert loaded.kind == ViolationKind.SMOKING
        assert loaded.station_id == 1
        assert len(loaded.files) == 1

    def test_list_day_当日全部(self, tmp_path):
        store = make_store(tmp_path)
        store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        store.capture(make_violation(station_id=2), [(1, T0)], lambda s: JPEG)
        rows = store.list_day(T0)
        assert len(rows) == 2
        assert {r.station_id for r in rows} == {1, 2}

    def test_list_day_字符串日期(self, tmp_path):
        store = make_store(tmp_path)
        store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        assert len(store.list_day("2026-11-12")) == 1

    def test_list_day_空目录(self, tmp_path):
        assert make_store(tmp_path).list_day("20250101") == []

    def test_load_不存在报错(self, tmp_path):
        with pytest.raises(EvidenceError, match="不存在"):
            make_store(tmp_path).load(tmp_path / "nope")


class TestNoTempLeak:
    def test_证据写到指定根目录下(self, tmp_path):
        root = tmp_path / "custom"
        store = EvidenceStore(root)
        package = store.capture(make_violation(), [(1, T0)], lambda s: JPEG)
        assert root in package.path.parents


class TestFrameErrorPropagation:
    def test_构造违规需要人状态(self):
        with pytest.raises(VisionError):
            analyze(bare(P1), frame(width=-1), ROIS)
