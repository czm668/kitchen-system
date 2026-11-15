# -*- coding: utf-8 -*-
"""Pascal VOC XML 标注 → YOLO txt 转换（vision_merge 的前置步骤）。

MAFA 系口罩集（hmnshudhmn24/face-mask-detection，即 maksssksksss）是
images/*.png + annotations/*.xml 的 VOC 格式。本工具把它翻成
vision_merge.py 认的 YOLO 布局：

    源/
    ├── images/*.png
    └── annotations/*.xml
        ↓ 本工具
    源/labels/*.txt   （cx cy w h 归一化，类别 id 原样保留）

类别 id 原样保留，语义翻译交给 vision_merge --map。

## 用法

    python tools/vision_voc_to_yolo.py --source D:/data/mask2-repo
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def convert(source: Path) -> tuple[int, int, int]:
    """转换一个源目录，返回 (图数, 框数, 无标注图数)。"""
    ann_dir = source / "annotations"
    if not ann_dir.is_dir():
        raise FileNotFoundError(f"{ann_dir} 不存在")
    labels_dir = source / "labels"
    labels_dir.mkdir(exist_ok=True)
    n_img = n_box = n_empty = 0
    for xml_file in sorted(ann_dir.glob("*.xml")):
        root = ET.parse(xml_file).getroot()
        size = root.find("size")
        w = float(size.findtext("width", "0"))
        h = float(size.findtext("height", "0"))
        lines: list[str] = []
        if w > 0 and h > 0:
            for obj in root.findall("object"):
                name = obj.findtext("name", "").strip()
                bnd = obj.find("bndbox")
                if bnd is None or not name:
                    continue
                xmin = float(bnd.findtext("xmin", "0"))
                ymin = float(bnd.findtext("ymin", "0"))
                xmax = float(bnd.findtext("xmax", "0"))
                ymax = float(bnd.findtext("ymax", "0"))
                bw, bh = xmax - xmin, ymax - ymin
                if bw <= 0 or bh <= 0:
                    continue
                # 本工具不做类别语义翻译，id 由调用方对齐：
                # VOC 没有数字 id，这里按 name→序号映射，映射表由调用方传入
                cls_id = _CLASS_IDS.get(name)
                if cls_id is None:
                    continue
                cx = min(max((xmin + bw / 2) / w, 0.0), 1.0)
                cy = min(max((ymin + bh / 2) / h, 0.0), 1.0)
                lines.append(f"{cls_id} {cx:.6f} {cy:.6f} "
                             f"{min(bw / w, 1.0):.6f} {min(bh / h, 1.0):.6f}")
        stem = Path(root.findtext("filename", xml_file.stem)).stem
        (labels_dir / f"{stem}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        n_img += 1
        n_box += len(lines)
        if not lines:
            n_empty += 1
    return n_img, n_box, n_empty


# MAFA 口罩集的三类（与 annotations/*.xml 里的 <name> 一致）
_CLASS_IDS = {"with_mask": 0, "without_mask": 1, "mask_weared_incorrect": 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VOC XML → YOLO txt")
    parser.add_argument("--source", type=Path, required=True,
                        help="源目录（含 images/ 与 annotations/*.xml）")
    args = parser.parse_args(argv)
    n_img, n_box, n_empty = convert(args.source)
    print(f"[{args.source.name}] {n_img} 图 / {n_box} 框 / {n_empty} 图无标注"
          f" → labels/ 已生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
