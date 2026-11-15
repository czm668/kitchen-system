# -*- coding: utf-8 -*-
"""帧源契约与三种实现（A5-01）。

## 只定义「怎么拿到帧」，不定义「怎么解码」

ESP32-CAM 用 MJPEG over HTTP 推流（`multipart/x-mixed-replace`），开发机用
图片目录回放，单测用内存帧。三者行为一样：**给一个帧的元数据 + 能按需取到
像素**。判定层只吃元数据，于是三层可以分开测。

## 硬口径 1：摄像头必须独立 5V/2A 供电，绝不接工位插座

这条不是选型建议，是 #16 能不能闭环的前提。`power_cut` 切的是工位插座，
如果摄像头跟着工位取电：

1. 切电 → 摄像头黑掉；
2. 黑掉 → 拍不到整改后的画面；
3. 拍不到 → 「双通道复核」只能凭人嘴说改好了，闭环失效；
4. 更糟：断开瞬间的电涌会让 CAM 反复重启，串口日志刷屏，看起来像 WiFi 问题。

同理路由器/中继也不能接工位插座。接线图上要单独标一路常电。

## 硬口径 2：时间戳只进不退

#3 的 `duration_sec` 和 #16 的去抖都按帧时间戳算差值。回放跨越午夜、
设备时钟被 NTP 校正、多路摄像头时间不同步时，差值会变成负数，于是
「持续 12 秒」永远不成立——**表现为联调时功能完全不触发，日志里一切正常**。
`FrameReader` 在出口统一夹住：新帧时间戳不早于上一帧 + 一个最小间隔。

## 硬口径 3：看门狗判「离线」，不判「无人」

连续读不到帧时唯一正确的结论是「这条防线瞎了」，不是「工位没人」。
把超时帧喂成 headcount=0 会让 #3 关掉全店燃气阀。所以 `read()` 返回 `None`，
由 `StreamHealth` 单独产离线告警。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain import CST
from .models import Frame, FrameStaleError, VisionError

log = logging.getLogger("kitchen.vision.stream")

__all__ = [
    "FrameSource",
    "MemoryFrameSource",
    "DirectoryFrameSource",
    "SyntheticFrameSource",
    "MjpegFrameSource",
    "FrameReader",
    "StreamHealth",
    "MIN_FRAME_INTERVAL_SEC",
    "extract_jpegs",
]

#: 两帧之间允许的最小间隔。时间戳倒退时按这个值往前推，保证严格单调
MIN_FRAME_INTERVAL_SEC = 1e-3

#: 单个帧文件的大小上限。ESP32-CAM 的 JPEG 一般 20~60KB；超过 2MB 说明
#: 有人在目录里丢了别的东西（或帧源写坏了），读进来只会把内存吃光
MAX_FRAME_BYTES = 2 * 1024 * 1024


@runtime_checkable
class FrameSource(Protocol):
    """帧源。`read()` 无帧时返回 `None`（不是抛异常）。"""

    device_id: str

    def open(self) -> None: ...

    def read(self) -> Frame | None: ...

    def read_bytes(self, frame: Frame) -> bytes: ...

    def close(self) -> None: ...


# ------------------------------------------------------------------ 内存帧源
@dataclass
class MemoryFrameSource:
    """测试 / 回放用：给定一串帧，按序吐出。像素用字典按 `uri` 查。"""

    frames: list[Frame] = field(default_factory=list)
    pixels: dict[str, bytes] = field(default_factory=dict)
    device_id: str = "esp32cam-01"
    _cursor: int = 0
    _opened: bool = False

    def open(self) -> None:
        self._cursor = 0
        self._opened = True

    def read(self) -> Frame | None:
        if not self._opened:
            raise VisionError("帧源未打开（先调 open()）")
        if self._cursor >= len(self.frames):
            return None
        frame = self.frames[self._cursor]
        self._cursor += 1
        return frame

    def read_bytes(self, frame: Frame) -> bytes:
        return self.pixels.get(frame.uri, b"")

    def close(self) -> None:
        self._opened = False

    def rewind(self) -> None:
        self._cursor = 0


# ------------------------------------------------------------------ 目录帧源
@dataclass
class DirectoryFrameSource:
    """把本地图片目录当推流用（离线开发 / 答辩兜底）。

    `loop=True` 时循环播放：答辩现场没有硬件，靠一段录好的视频逐帧回放
    比「黑屏 + 一句网络不好」强得多。回放与真机走的是同一条判定链路，
    所以演示出来的结论对真机同样成立。
    """

    directory: Path | str
    device_id: str = "esp32cam-01"
    loop: bool = False
    #: 回放时每帧的虚拟间隔（秒）。0 表示按文件修改时间
    frame_interval_sec: float = 0.2
    width: int = 640
    height: int = 480
    _files: list[Path] = field(default_factory=list, init=False, repr=False)
    _cursor: int = 0
    _seq: int = 0
    _clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(CST), repr=False
    )
    _last_ts: datetime | None = field(default=None, init=False, repr=False)

    SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp")

    def open(self) -> None:
        root = Path(self.directory)
        if not root.is_dir():
            raise VisionError(f"帧目录不存在：{root}")
        self._files = sorted(
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in self.SUFFIXES
        )
        if not self._files:
            raise VisionError(f"帧目录里没有图片（支持 {self.SUFFIXES}）：{root}")
        self._cursor = 0
        self._seq = 0
        self._last_ts = None
        log.info("目录帧源就绪：%d 帧 ← %s", len(self._files), root)

    def read(self) -> Frame | None:
        if not self._files:
            raise VisionError("帧源未打开（先调 open()）")
        if self._cursor >= len(self._files):
            if not self.loop:
                return None
            self._cursor = 0
        path = self._files[self._cursor]
        self._cursor += 1
        self._seq += 1

        if self.frame_interval_sec > 0:
            base = self._last_ts or self._clock()
            self._last_ts = base + timedelta(seconds=self.frame_interval_sec)
            ts = self._last_ts
        else:
            ts = datetime.fromtimestamp(path.stat().st_mtime, CST)
        return Frame(seq=self._seq, ts=ts, width=self.width, height=self.height,
                     uri=str(path), device_id=self.device_id)

    def read_bytes(self, frame: Frame) -> bytes:
        path = Path(frame.uri)
        if not path.is_file():
            raise VisionError(f"帧文件已消失：{path}")
        size = path.stat().st_size
        if size > MAX_FRAME_BYTES:
            raise VisionError(f"帧文件过大（{size} 字节 > {MAX_FRAME_BYTES}）：{path}")
        return path.read_bytes()

    def close(self) -> None:
        self._files = []

    @property
    def total(self) -> int:
        return len(self._files)


# ------------------------------------------------------------------ 合成帧源
#: 1×1 灰色 PNG。不引 PIL——离线便携 Python 装不上，而「合成帧」存在的意义
#: 就是让抓拍/存证链路在没有任何图片素材时也能跑通。
_ONE_PX_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c636060606000000005000156bd5a1a"
    "0000000049454e44ae426082"
)


@dataclass
class SyntheticFrameSource:
    """按帧率产空白帧。用于「只看时序逻辑」的测试与演示。"""

    fps: float = 5.0
    count: int | None = None
    device_id: str = "synthetic-01"
    width: int = 640
    height: int = 480
    _seq: int = 0
    _ts: datetime | None = None
    _clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(CST), repr=False
    )

    def open(self) -> None:
        self._seq = 0
        self._ts = self._clock()

    def read(self) -> Frame | None:
        if self.count is not None and self._seq >= self.count:
            return None
        self._seq += 1
        step = 1.0 / self.fps if self.fps > 0 else 0.2
        self._ts = (self._ts or self._clock()) + timedelta(seconds=step)
        return Frame(seq=self._seq, ts=self._ts, width=self.width,
                     height=self.height, uri=f"synth:{self._seq}",
                     device_id=self.device_id)

    def read_bytes(self, frame: Frame) -> bytes:
        return _ONE_PX_PNG

    def close(self) -> None:
        self._seq = 0


# ------------------------------------------------------------------ MJPEG 帧源
#: JPEG 的起始/结束标记。**按标记切帧，不按 multipart boundary 切**：
#: ESP32-CAM 的 boundary 是随机串，各家固件写法不一样；而 FFD8/FFD9 是
#: JPEG 规范强制的，只要有 JPEG 就能切。按 boundary 解析的版本在换一块
#: CAM 板子之后就会静默失效（切出来的「帧」解码失败，日志里全是 None）。
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


def extract_jpegs(buffer: bytes) -> tuple[list[bytes], bytes]:
    """从累积缓冲区切出完整 JPEG。返回 `(完整帧列表, 剩余未完成字节)`。

    纯函数，可单测——真机连不上时也能验证切帧逻辑，这是它单独提出来的理由。
    """
    frames: list[bytes] = []
    data = buffer
    while True:
        start = data.find(_SOI)
        if start < 0:
            # 没有起始标记：保留最后 1 字节（可能是被切开的 FFD8 前半）
            return frames, data[-1:]
        end = data.find(_EOI, start + 2)
        if end < 0:
            return frames, data[start:]
        frames.append(data[start:end + 2])
        data = data[end + 2:]


@dataclass
class MjpegFrameSource:
    """ESP32-CAM 的 MJPEG over HTTP 帧源（真机路径）。

    固件端点：`http://<ip>/stream`（`multipart/x-mixed-replace`）。
    本类只依赖标准库 `urllib`，不引 requests / opencv——服务端可能跑在
    只有标准库的环境里，而这里要的只是「连着 TCP 读字节」。

    内存里只留最近 `keep` 帧的像素：抓拍窗口 5 帧、证据包上限 12 张，
    留 20 帧足够；全留的话 640×480@5fps 一小时就是 5GB。
    """

    url: str
    device_id: str = "esp32cam-01"
    width: int = 640
    height: int = 480
    timeout_sec: float = 5.0
    keep: int = 20
    _conn: object = field(default=None, init=False, repr=False)
    _buffer: bytes = field(default=b"", init=False, repr=False)
    _pixels: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)
    _order: list[str] = field(default_factory=list, init=False, repr=False)
    _seq: int = 0
    _clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(CST), repr=False
    )

    def open(self) -> None:
        import urllib.request                              # noqa: PLC0415
        try:
            self._conn = urllib.request.urlopen(self.url, timeout=self.timeout_sec)
        except OSError as exc:
            raise VisionError(f"连不上帧源 {self.url}：{exc}") from exc
        self._buffer = b""
        log.info("MJPEG 帧源已连接：%s", self.url)

    def read(self) -> Frame | None:
        if self._conn is None:
            raise VisionError("帧源未打开（先调 open()）")
        # 最多读 8 个 chunk 找一帧；再多说明流卡住了，交回上层走看门狗
        for _ in range(8):
            frames, self._buffer = extract_jpegs(self._buffer)
            if frames:
                data = frames[-1]
                self._seq += 1
                uri = f"mjpeg:{self._seq}"
                self._pixels[uri] = data
                self._order.append(uri)
                while len(self._order) > max(1, self.keep):
                    self._pixels.pop(self._order.pop(0), None)
                return Frame(seq=self._seq, ts=self._clock(), width=self.width,
                             height=self.height, uri=uri, device_id=self.device_id)
            chunk = self._conn.read(4096)                   # type: ignore[union-attr]
            if not chunk:
                return None
            self._buffer = (self._buffer + chunk)[-4 * 1024 * 1024:]
        return None

    def read_bytes(self, frame: Frame) -> bytes:
        return self._pixels.get(frame.uri, b"")

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()                                # type: ignore[union-attr]
            except OSError:
                pass


# ------------------------------------------------------------------ 读取器
@dataclass
class StreamHealth:
    """帧流健康状况。#11 与 A6-05 的「视觉离线」标记都读它。"""

    device_id: str = ""
    online: bool = True
    last_ts: datetime | None = None
    #: 已读帧数 / 空读次数
    frames: int = 0
    misses: int = 0
    #: 时间戳被夹正的次数（时钟倒退 / 回放跨午夜）
    clock_fixes: int = 0
    detail: str = ""

    def describe(self) -> str:
        if self.online:
            return (f"{self.device_id} 在线，{self.frames} 帧"
                    f"{f'，修正时钟 {self.clock_fixes} 次' if self.clock_fixes else ''}")
        return f"{self.device_id} 离线：{self.detail or '无帧'}"


class FrameReader:
    """帧源外面包一层：时间戳单调化 + 看门狗 + 计数。

    上层（`VisionPipeline`）只用这个，不直接用帧源——否则时间戳单调性
    这条约束就得在每个调用点各写一遍，总有一处忘了。
    """

    def __init__(self, source: FrameSource, *, stale_after_sec: float = 30.0,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.source = source
        self.stale_after_sec = float(stale_after_sec)
        self._clock = clock or (lambda: datetime.now(CST))
        self._last: datetime | None = None
        self.health = StreamHealth(device_id=getattr(source, "device_id", ""))

    # -------------------------------------------------------------- 生命周期
    def open(self) -> None:
        self.source.open()
        self.health.online = True
        self.health.misses = 0
        self.health.detail = ""

    def close(self) -> None:
        self.source.close()

    # -------------------------------------------------------------- 读帧
    def read(self) -> Frame | None:
        frame = self.source.read()
        if frame is None:
            self.health.misses += 1
            self._mark_stale("帧源无新帧")
            return None
        if self._last is not None and frame.ts <= self._last:
            # 不抛异常：一帧时钟抖动不该让整条链路停摆，但必须记数（时钟坏了
            # 是运维要处理的事，症状是「功能偶尔不触发」）
            bumped = self._last + timedelta(seconds=MIN_FRAME_INTERVAL_SEC)
            self.health.clock_fixes += 1
            log.debug("帧时间戳倒退：%s → %s，夹到 %s", self._last, frame.ts, bumped)
            frame = Frame(seq=frame.seq, ts=bumped, width=frame.width,
                          height=frame.height, uri=frame.uri,
                          device_id=frame.device_id)
        self._last = frame.ts
        self.health.frames += 1
        self.health.last_ts = frame.ts
        self.health.online = True
        self.health.detail = ""
        return frame

    def read_bytes(self, frame: Frame) -> bytes:
        return self.source.read_bytes(frame)

    def drain(self, *, limit: int = 1000) -> list[Frame]:
        """一直读到没有帧为止（回放/批量补处理用）。"""
        out: list[Frame] = []
        while len(out) < limit:
            frame = self.read()
            if frame is None:
                break
            out.append(frame)
        return out

    # -------------------------------------------------------------- 健康
    def stale(self, now: datetime | None = None) -> bool:
        if self.health.last_ts is None:
            return True
        age = ((now or self._clock()) - self.health.last_ts).total_seconds()
        return age > self.stale_after_sec

    def _mark_stale(self, detail: str) -> None:
        if self.stale():
            self.health.online = False
            self.health.detail = detail

    def check(self, now: datetime | None = None) -> StreamHealth:
        """主动探活。上层定时调（不是每帧调），结果给三端降级标记用。"""
        if self.health.last_ts is None:
            self.health.online = False
            self.health.detail = "从未收到帧"
            return self.health
        age = ((now or self._clock()) - self.health.last_ts).total_seconds()
        if age > self.stale_after_sec:
            self.health.online = False
            self.health.detail = (f"最近一帧已 {age:.0f}s（阈值 "
                                 f"{self.stale_after_sec:.0f}s）")
        else:
            self.health.online = True
            self.health.detail = ""
        return self.health


def build_replay(paths: Iterable[Path | str], *, device_id: str = "esp32cam-01",
                 interval_sec: float = 0.2) -> MemoryFrameSource:
    """把一串图片路径做成内存帧源（单测与 demo 用）。"""
    base = datetime(2026, 11, 12, 11, 30, tzinfo=CST)
    frames: list[Frame] = []
    pixels: dict[str, bytes] = {}
    for index, raw in enumerate(paths, start=1):
        path = Path(raw)
        uri = str(path)
        frames.append(Frame(
            seq=index, ts=base + timedelta(seconds=interval_sec * (index - 1)),
            width=640, height=480, uri=uri, device_id=device_id,
        ))
        if path.is_file():
            pixels[uri] = path.read_bytes()
        else:
            pixels[uri] = _ONE_PX_PNG
    return MemoryFrameSource(frames=frames, pixels=pixels, device_id=device_id)


def ensure_dir(path: Path | str) -> Path:
    root = Path(path)
    os.makedirs(root, exist_ok=True)
    return root
