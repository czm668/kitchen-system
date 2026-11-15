# -*- coding: utf-8 -*-
"""帧源与看门狗（A5-01）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from vision_support import bare, frame

from kitchen.domain import CST
from kitchen.vision.stream import (
    DirectoryFrameSource,
    FrameReader,
    MemoryFrameSource,
    MjpegFrameSource,
    StreamHealth,
    SyntheticFrameSource,
    build_replay,
    extract_jpegs,
)

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make_dir(tmp_path, count: int = 3):
    folder = tmp_path / "frames"
    folder.mkdir()
    for index in range(count):
        (folder / f"{index:03d}.jpg").write_bytes(b"jpeg" + bytes([index]) * 10)
    return folder


class TestDirectorySource:
    def test_读图并给序号(self, tmp_path):
        source = DirectoryFrameSource(make_dir(tmp_path), frame_interval_sec=0.2)
        source.open()
        first = source.read()
        assert first.seq == 1
        assert first.device_id == "esp32cam-01"
        assert first.uri.endswith("000.jpg")
        assert first.width == 640 and first.height == 480

    def test_读完返回None(self, tmp_path):
        source = DirectoryFrameSource(make_dir(tmp_path, 2))
        source.open()
        assert source.read() is not None
        assert source.read() is not None
        assert source.read() is None

    def test_循环播放(self, tmp_path):
        source = DirectoryFrameSource(make_dir(tmp_path, 2), loop=True)
        source.open()
        seqs = [source.read().seq for _ in range(6)]
        assert seqs == [1, 2, 3, 4, 5, 6]

    def test_时间戳按间隔推进(self, tmp_path):
        clock = FakeClock()
        source = DirectoryFrameSource(make_dir(tmp_path, 3), frame_interval_sec=0.5,
                                      )
        source._clock = clock
        source.open()
        a, b = source.read(), source.read()
        assert (b.ts - a.ts).total_seconds() == pytest.approx(0.5)

    def test_空目录报错(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(Exception, match="没有图片"):
            DirectoryFrameSource(empty).open()

    def test_目录不存在报错(self, tmp_path):
        with pytest.raises(Exception, match="帧目录不存在"):
            DirectoryFrameSource(tmp_path / "nope").open()

    def test_读像素(self, tmp_path):
        source = DirectoryFrameSource(make_dir(tmp_path, 1))
        source.open()
        f = source.read()
        assert source.read_bytes(f).startswith(b"jpeg")

    def test_超大文件拒绝(self, tmp_path):
        folder = tmp_path / "big"
        folder.mkdir()
        (folder / "000.jpg").write_bytes(b"x" * (3 * 1024 * 1024))
        source = DirectoryFrameSource(folder)
        source.open()
        f = source.read()
        with pytest.raises(Exception, match="过大"):
            source.read_bytes(f)

    def test_总帧数(self, tmp_path):
        source = DirectoryFrameSource(make_dir(tmp_path, 5))
        source.open()
        assert source.total == 5


class TestSynthetic:
    def test_按帧率产帧(self):
        source = SyntheticFrameSource(fps=5, count=3)
        source.open()
        rows = [source.read() for _ in range(3)]
        assert [r.seq for r in rows] == [1, 2, 3]
        assert (rows[1].ts - rows[0].ts).total_seconds() == pytest.approx(0.2)
        assert source.read() is None

    def test_像素是合法PNG头(self):
        source = SyntheticFrameSource(count=1)
        source.open()
        assert source.read_bytes(source.read()).startswith(b"\x89PNG")


class TestFrameReader:
    def test_时间戳倒退被夹正(self):
        base = T0
        frames = [
            frame(1, ts=base),
            frame(2, ts=base - timedelta(seconds=5)),      # 时钟倒退
        ]
        reader = FrameReader(MemoryFrameSource(frames))
        reader.open()
        first = reader.read()
        second = reader.read()
        assert second.ts > first.ts
        assert reader.health.clock_fixes == 1

    def test_重复时间戳也夹正(self):
        frames = [frame(1, ts=T0), frame(2, ts=T0)]
        reader = FrameReader(MemoryFrameSource(frames))
        reader.open()
        reader.read()
        reader.read()
        assert reader.health.clock_fixes == 1

    def test_计数与健康(self):
        frames = [frame(i + 1, ts=T0 + timedelta(seconds=i)) for i in range(3)]
        reader = FrameReader(MemoryFrameSource(frames), clock=lambda: T0)
        reader.open()
        reader.drain()
        assert reader.health.frames == 3
        assert reader.health.online is True
        assert reader.health.misses == 1

    def test_超时判离线(self):
        frames = [frame(1, ts=T0)]
        reader = FrameReader(MemoryFrameSource(frames), stale_after_sec=10,
                             clock=lambda: T0 + timedelta(seconds=30))
        reader.open()
        reader.read()
        assert reader.stale() is True
        assert reader.check().online is False
        assert "已 30s" in reader.check().detail

    def test_从未收到帧判离线(self):
        reader = FrameReader(MemoryFrameSource([]))
        reader.open()
        assert reader.check().online is False
        assert reader.check().detail == "从未收到帧"

    def test_未打开直接读报错(self):
        reader = FrameReader(MemoryFrameSource([frame(1)]))
        with pytest.raises(Exception, match="未打开"):
            reader.read()

    def test_健康描述(self):
        health = StreamHealth(device_id="cam-1", frames=10)
        assert "cam-1" in health.describe()
        health.online = False
        health.detail = "无帧"
        assert "离线" in health.describe()


class TestBuildReplay:
    def test_不存在的路径用占位像素(self, tmp_path):
        source = build_replay([tmp_path / "a.jpg", tmp_path / "b.jpg"])
        source.open()
        f = source.read()
        assert source.read_bytes(f).startswith(b"\x89PNG")

    def test_真实文件读真实内容(self, tmp_path):
        target = tmp_path / "real.jpg"
        target.write_bytes(b"real-jpeg")
        source = build_replay([target])
        source.open()
        f = source.read()
        assert source.read_bytes(f) == b"real-jpeg"

    def test_间隔推进时间戳(self, tmp_path):
        source = build_replay([tmp_path / f"{i}.jpg" for i in range(3)], interval_sec=0.5)
        source.open()
        a, b = source.read(), source.read()
        assert (b.ts - a.ts).total_seconds() == pytest.approx(0.5)


class TestExtractJpegs:
    """MJPEG 切帧：按 FFD8/FFD9 而不是 multipart boundary。"""

    def test_从multipart里切出一帧(self):
        buffer = (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                  b"\xff\xd8JPG1\xff\xd9\r\n")
        frames, rest = extract_jpegs(buffer)
        assert frames == [b"\xff\xd8JPG1\xff\xd9"]
        assert rest == b"\n"

    def test_一次读进两帧(self):
        buffer = b"\xff\xd8A\xff\xd9xx\xff\xd8B\xff\xd9"
        frames, rest = extract_jpegs(buffer)
        assert frames == [b"\xff\xd8A\xff\xd9", b"\xff\xd8B\xff\xd9"]
        assert rest == b""

    def test_半帧留在缓冲区(self):
        frames, rest = extract_jpegs(b"\xff\xd8JPG")
        assert frames == []
        assert rest == b"\xff\xd8JPG"

    def test_起始标记被切断(self):
        frames, rest = extract_jpegs(b"garbage\xff")
        assert frames == []
        assert rest == b"\xff", "最后一个字节可能是被切开的 FFD8 前半，必须留住"

    def test_纯垃圾丢弃(self):
        frames, rest = extract_jpegs(b"not-a-jpeg-at-all")
        assert frames == []
        assert rest == b"l"

    def test_空缓冲区(self):
        assert extract_jpegs(b"") == ([], b"")


class TestMjpegSource:
    def test_未打开直接读报错(self):
        with pytest.raises(Exception, match="未打开"):
            MjpegFrameSource("http://127.0.0.1:1/stream").read()

    def test_连不上报错(self):
        source = MjpegFrameSource("http://127.0.0.1:1/stream", timeout_sec=0.2)
        with pytest.raises(Exception, match="连不上帧源"):
            source.open()
