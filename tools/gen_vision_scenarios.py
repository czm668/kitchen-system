# -*- coding: utf-8 -*-
"""生成视觉时序剧本夹具（可重复运行，坐标来自权威的 tests/vision_support.py）。

剧本是逐帧的检测框序列，喂给 `ScriptedDetector.from_json()` 驱动
`VisionPipeline.tick()` 全链路，覆盖"单帧构造测不到"的时序行为：
去抖、正向证据优先、遮挡、离岗返回。

坐标不手抄：直接调用 vision_support 的 full_kit()/bare()/hand_of() 等，
保证与单测夹具同一套几何口径（过道 0.42~0.58、脚点分工位）。

用法：
    python tools/gen_vision_scenarios.py           # 生成到 fixtures/vision_scenarios/*.json
    python tools/gen_vision_scenarios.py --check   # 只读校验：生成器结果与磁盘一致则退出 0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT / "src"), str(_ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from vision_support import P1, P2, bare, full_kit, hand_of, phone  # noqa: E402

OUT = _ROOT / "fixtures" / "vision_scenarios"


def enc(dets) -> list[dict]:
    return [
        {"cls": d.cls, "conf": d.conf,
         "bbox": [d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2]}
        for d in dets
    ]


def build() -> dict[str, list[list[dict]]]:
    kit_phone = full_kit(P1) + [phone(hand_of(P1))]
    kit_no_hat = [d for d in full_kit(P1) if d.cls != "hat"]
    two_stations_with_phone = kit_phone + full_kit(P2)
    return {
        # 逐步戴帽：3 帧没戴帽 → 3 帧戴齐
        "01_don_hat": [enc(bare(P1)) for _ in range(3)]
                      + [enc(full_kit(P1)) for _ in range(3)],
        # 低头帽子短暂遮挡：正常2 → 缺帽1 → 恢复3（单帧遮挡不应报违规）
        "02_hat_occluded": [enc(full_kit(P1)) for _ in range(2)]
                           + [enc(kit_no_hat)]
                           + [enc(full_kit(P1)) for _ in range(3)],
        # 擦汗误判手机1帧：正常2 → 手部phone 1帧 → 恢复2（1帧不报 phone）
        "03_wipe_sweat": [enc(full_kit(P1)) for _ in range(2)]
                         + [enc(kit_phone)]
                         + [enc(full_kit(P1)) for _ in range(2)],
        # 两人分站：ST1 玩手机（4帧），ST2 穿戴齐全
        "04_two_stations": [enc(two_stations_with_phone) for _ in range(4)],
        # 镜头遮挡：有人2帧 → 连续6帧全空
        "05_camera_blocked": [enc(full_kit(P1)) for _ in range(2)]
                             + [[] for _ in range(6)],
        # 离岗返回：有人3帧 → 空5帧 → 回来2帧
        "06_absence_return": [enc(full_kit(P1)) for _ in range(3)]
                             + [[] for _ in range(5)]
                             + [enc(full_kit(P1)) for _ in range(2)],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成/校验视觉时序剧本夹具")
    ap.add_argument("--check", action="store_true",
                    help="只读校验生成器结果与磁盘 JSON 一致，不写文件")
    args = ap.parse_args(argv)

    rendered = {
        f"{name}.json": json.dumps(frames, ensure_ascii=False, indent=1)
        for name, frames in build().items()
    }
    if args.check:
        problems = []
        for name, content in rendered.items():
            path = OUT / name
            if not path.exists():
                problems.append(f"缺少 {name}")
            elif path.read_text(encoding="utf-8") != content:
                problems.append(f"{name} 与生成器不一致（手改过？请改生成器后重跑）")
        if problems:
            for line in problems:
                print(line, file=sys.stderr)
            return 1
        print(f"gen_vision_scenarios: OK（{len(rendered)} 个剧本与生成器一致）")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        (OUT / name).write_text(content, encoding="utf-8")
        frames = json.loads(content)
        print(f"{name}: {len(frames)} 帧, 首帧 {len(frames[0])} 框")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
