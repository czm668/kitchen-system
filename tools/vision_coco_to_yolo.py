# -*- coding: utf-8 -*-
"""COCO JSON 标注 → YOLO txt 转换（vision_merge 的前置步骤）。

## 为什么需要它

Roboflow/HF 上的不少检测集（如 keremberke/hard-hat-detection）图片平铺 +
`_annotations.coco.json`，而 `vision_merge.py` 只吃 YOLO 格式
（images/ + labels/）。本工具在两者之间搭桥：

    数据集/
    ├── images/                  ← 已就位（或从 zip 解出）
    └── _annotations.coco.json   ← COCO 格式
        ↓ 本工具
    数据集/labels/*.txt          ← 生成 YOLO txt（cx cy w h 归一化）

类别 id **原样保留**——翻译到本工程 6 类是 vision_merge 的职责（--map），
这里不做语义判断。

## 用法

    python tools/vision_coco_to_yolo.py --source D:/data/hardhat-train
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def convert(source: Path) -> tuple[int, int, int]:
    """转换一个源目录，返回 (图数, 框数, 无标注图数)。"""
    json_path = source / "_annotations.coco.json"
    images_dir = source / "images"
    if not json_path.exists():
        raise FileNotFoundError(f"{json_path} 不存在（COCO 标注应在源目录根部）")
    if not images_dir.is_dir():
        raise FileNotFoundError(f"{images_dir} 不存在（先把图片解包到 images/）")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    img_info = {im["id"]: im for im in data.get("images", [])}
    boxes: dict[int, list[str]] = {iid: [] for iid in img_info}

    for ann in data.get("annotations", []):
        iid = ann["image_id"]
        info = img_info.get(iid)
        if info is None:
            continue
        x, y, w, h = ann["bbox"]
        iw, ih = info["width"], info["height"]
        if iw <= 0 or ih <= 0 or w <= 0 or h <= 0:
            continue
        # COCO 绝对像素 [x,y,w,h] → YOLO 归一化 [cx,cy,w,h]，并夹取到图内
        cx = min(max((x + w / 2) / iw, 0.0), 1.0)
        cy = min(max((y + h / 2) / ih, 0.0), 1.0)
        nw = min(w / iw, 1.0)
        nh = min(h / ih, 1.0)
        boxes[iid].append(f"{ann['category_id']} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

    labels_dir = source / "labels"
    labels_dir.mkdir(exist_ok=True)
    n_img = n_empty = 0
    total = 0
    for iid, info in img_info.items():
        name = Path(info["file_name"]).stem + ".txt"
        lines = boxes.get(iid, [])
        (labels_dir / name).write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        n_img += 1
        total += len(lines)
        if not lines:
            n_empty += 1
    return n_img, total, n_empty


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="COCO JSON → YOLO txt")
    parser.add_argument("--source", type=Path, required=True,
                        help="源目录（含 images/ 与 _annotations.coco.json）")
    args = parser.parse_args(argv)
    n_img, total, n_empty = convert(args.source)
    print(f"[{args.source.name}] {n_img} 图 / {total} 框 / {n_empty} 图无标注"
          f" → labels/ 已生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
