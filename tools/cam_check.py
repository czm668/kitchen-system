# -*- coding: utf-8 -*-
"""摄像头节点接入自检（A5-01 验收脚本）。

## 它验的是什么

不是「能不能连上」，而是**帧质量够不够判定层用**：

1. **帧间隔稳定性**：抖动超过 ±60ms，#3 的「持续 12 秒」就会算错，
   因为它是按帧时间戳积分的；
2. **单帧大小**：< 12KB 多半是镜头被挡/全黑，> 120KB 说明质量配太高，
   2.4G 下会堆延迟；
3. **时间戳单调**：`FrameReader.health.clock_fixes > 0` 说明设备时钟在跳，
   排查起来极难（症状是「功能偶尔不触发」）；
4. **JPEG 头尾完整**：切帧函数按 FFD8/FFD9 工作，头尾不全会切出半帧。

## 用法

```
python tools/cam_check.py --url http://192.168.1.60/stream --frames 100
python tools/cam_check.py --snapshot http://192.168.1.60/snapshot --out var/shot.jpg
```

无网络/无相机时直接退出码 2 并说明，不假装通过。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kitchen.vision.stream import FrameReader, MjpegFrameSource     # noqa: E402

#: 帧间隔允许的抖动（毫秒）。5fps → 200ms
INTERVAL_TOLERANCE_MS = 60
#: 单帧大小的合理区间（字节）
MIN_BYTES = 12 * 1024
MAX_BYTES = 120 * 1024


def check_snapshot(url: str, out: Path) -> int:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            data = response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"✗ 取单帧失败：{exc}")
        return 2
    if not data.startswith(b"\xff\xd8"):
        print(f"✗ 返回内容不是 JPEG（前 8 字节 {data[:8]!r}）")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    ok = MIN_BYTES <= len(data) <= MAX_BYTES
    print(f"{'✓' if ok else '!'} 单帧 {len(data) / 1024:.1f}KB → {out}")
    if not ok:
        print(f"    参考区间 {MIN_BYTES / 1024:.0f}~{MAX_BYTES / 1024:.0f}KB："
              f"过小多为镜头被挡/全黑，过大则推流会堆延迟")
    return 0 if ok else 1


def check_stream(url: str, count: int) -> int:
    source = MjpegFrameSource(url, device_id="cam-check")
    reader = FrameReader(source, stale_after_sec=10)
    try:
        reader.open()
    except Exception as exc:                            # noqa: BLE001
        print(f"✗ 连不上帧源：{exc}")
        return 2

    print(f"开始收帧：{url}（目标 {count} 帧）")
    stamps: list[float] = []
    sizes: list[int] = []
    bad_frames = 0
    deadline = time.monotonic() + max(10.0, count * 0.6)

    try:
        while len(stamps) < count and time.monotonic() < deadline:
            frame = reader.read()
            if frame is None:
                time.sleep(0.05)
                continue
            data = reader.read_bytes(frame)
            if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
                bad_frames += 1
            stamps.append(frame.ts.timestamp() * 1000)
            sizes.append(len(data))
    finally:
        reader.close()

    if len(stamps) < 2:
        print(f"✗ 只收到 {len(stamps)} 帧，无法评估（检查供电与 /stream 端点）")
        return 2

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    mean = statistics.fmean(gaps)
    stdev = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
    size_mean = statistics.fmean(sizes) / 1024

    print(f"\n== 结果 ==")
    print(f"  收到帧数       {len(stamps)}")
    print(f"  帧间隔 均值    {mean:.0f}ms（目标 200ms）")
    print(f"  帧间隔 抖动    ±{stdev:.0f}ms（上限 {INTERVAL_TOLERANCE_MS}ms）")
    print(f"  单帧 均值      {size_mean:.1f}KB（参考 {MIN_BYTES // 1024}~{MAX_BYTES // 1024}KB）")
    print(f"  头尾不完整     {bad_frames} 帧")
    print(f"  时钟被修正     {reader.health.clock_fixes} 次")

    ok = True
    if abs(mean - 200) > INTERVAL_TOLERANCE_MS:
        print(f"  ! 帧率偏离目标（实际约 {1000 / mean:.1f}fps），"
              f"要同步改 ViolationPolicy.for_fps()")
        ok = False
    if stdev > INTERVAL_TOLERANCE_MS:
        print("  ! 帧间隔抖动过大：弱信号或供电不足（优先查 5V/2A）")
        ok = False
    if not MIN_BYTES / 1024 <= size_mean <= MAX_BYTES / 1024:
        print("  ! 单帧大小超出参考区间")
        ok = False
    if bad_frames:
        print("  ! 有头尾不完整的帧：切帧会出半张图，检查内存带宽与 fb_count")
        ok = False
    if reader.health.clock_fixes:
        print("  ! 时间戳出现过倒退：设备时钟在跳，#3 的时间积分会算错")
        ok = False

    print("\n结论：" + ("通过，可接判定层" if ok else "不通过，按上面提示逐条处理"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="摄像头节点自检")
    parser.add_argument("--url", default="", help="MJPEG 流地址，如 http://ip/stream")
    parser.add_argument("--snapshot", default="", help="单帧端点，如 http://ip/snapshot")
    parser.add_argument("--out", default="var/snapshot.jpg", help="单帧保存路径")
    parser.add_argument("--frames", type=int, default=100, help="采样帧数")
    args = parser.parse_args(argv)

    if not args.url and not args.snapshot:
        print("用法：python tools/cam_check.py --url http://<ip>/stream")
        print("     或 --snapshot http://<ip>/snapshot --out var/shot.jpg")
        return 2

    code = 0
    if args.snapshot:
        code |= check_snapshot(args.snapshot, Path(args.out))
    if args.url:
        code |= check_stream(args.url, args.frames)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
