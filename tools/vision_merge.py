# -*- coding: utf-8 -*-
"""公开数据集拼装工具：把多个 YOLO 格式数据集重映射合并进 `datasets/vision`。

## 为什么需要它

六类目标（person/hat/mask/uniform/cigarette/phone）没有任何一个公开集能
单独覆盖，实际路线是「多个公开集各取所需 + 少量自标」。但各公开集的
class id 定义互不相同（SHWD 的 0 是 helmet，COCO 的 0 是 person），直接
把标注文件拷进来 = 标签错位，模型会把帽子认成手机——而且代码里完全
看不出来，只有现场能发现。

这个工具做三件事：
1. **重映射**：按调用方给的映射表，把源集的 class id 翻译成本工程 6 类；
2. **合并**：统一拷进 `datasets/vision`，按 8:1:1 切 train/val/test，
   文件名加来源前缀防重名覆盖；
3. **审计**：输出每个源集的映射结果（丢弃/保留/各类框数），映射不到
   本工程 6 类的类别默认丢弃——宁缺毋滥，脏标签比少数据更伤。

源目录只读不改（copy 语义），输出目录存在内容时拒绝执行（--force 覆盖）。

## 用法示例（SHWD 安全帽数据集，helmet→hat、head→person）

    python tools/vision_merge.py \
      --source D:/data/SHWD --map "0:hat,1:person" --prefix shwd
    python tools/vision_merge.py --check   # 合并后校验各类框数
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUT = _ROOT / "datasets" / "vision"
CLASSES = ["person", "hat", "mask", "uniform", "cigarette", "phone"]
SPLIT = {"train": 0.8, "val": 0.1, "test": 0.1}


def parse_mapping(spec: str, source: Path) -> dict[int, str]:
    """`"0:hat,1:person"` → {0: 'hat', 1: 'person'}；键也接受源类别名。"""
    names_file = source / "classes.txt"
    source_names: dict[int, str] = {}
    if names_file.exists():
        source_names = {i: line.strip()
                        for i, line in enumerate(names_file.read_text(encoding="utf-8").splitlines())
                        if line.strip()}
    mapping: dict[int, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item or ":" not in item:
            raise ValueError(f"映射项格式错误：{item!r}（应为 源类别:目标类别）")
        left, right = (part.strip() for part in item.split(":", 1))
        if right not in CLASSES:
            raise ValueError(f"目标类别 {right!r} 不在本工程 6 类里：{CLASSES}")
        if left.isdigit():
            key = int(left)
        elif left in source_names.values():
            key = next(k for k, v in source_names.items() if v == left)
        else:
            raise ValueError(f"源类别 {left!r} 既不是 id 也找不到 classes.txt 对应行")
        mapping[key] = right
    return mapping


def merge_source(source: Path, mapping: dict[int, str], prefix: str,
                 stats: Counter, dropped: Counter) -> tuple[int, int]:
    """拷一个源集，返回 (保留图数, 丢弃图数)。标注重写 class id。"""
    src_img = source / "images"
    src_lbl = source / "labels"
    if not src_img.is_dir():
        raise FileNotFoundError(f"{source} 下没有 images/ 目录（需 YOLO 格式）")

    images = sorted(p for p in src_img.iterdir()
                    if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    rng = random.Random(20261114)  # 固定种子：同样的输入永远得到同样的切分
    kept = dropped_n = 0
    for img in images:
        lbl = src_lbl / (img.stem + ".txt")
        if not lbl.exists():
            dropped[f"{prefix}:无标注"] += 1
            dropped_n += 1
            continue
        rewritten: list[str] = []
        for line in lbl.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) != 5:
                continue
            src_cls = int(parts[0])
            target = mapping.get(src_cls)
            if target is None:
                dropped[f"{prefix}:类别{src_cls}未映射"] += 1
                continue
            rewritten.append(f"{CLASSES.index(target)} " + " ".join(parts[1:]))
            stats[target] += 1
        if not rewritten:
            dropped_n += 1  # 整图没有一个可映射框 → 跳过（负样本请走自标通道）
            continue
        ratio = rng.random()
        split = ("train" if ratio < SPLIT["train"]
                 else "val" if ratio < SPLIT["train"] + SPLIT["val"] else "test")
        dst_img = _OUT / "images" / split
        dst_lbl = _OUT / "labels" / split
        dst_img.mkdir(parents=True, exist_ok=True)
        dst_lbl.mkdir(parents=True, exist_ok=True)
        dst_name = f"{prefix}_{img.stem}{img.suffix.lower()}"
        shutil.copy2(img, dst_img / dst_name)
        (dst_lbl / (Path(dst_name).stem + ".txt")).write_text(
            "\n".join(rewritten) + "\n", encoding="utf-8")
        kept += 1
    return kept, dropped_n


def check_output() -> None:
    """合并后的体检：各类框数 + 图/标对应性。"""
    if not _OUT.exists():
        print("! datasets/vision 还不存在，先跑 --source 合并")
        return
    stats: Counter = Counter()
    orphan = 0
    for lbl_file in _OUT.glob("labels/*/*.txt"):
        img = _OUT / "images" / lbl_file.parent.name / (lbl_file.stem + ".jpg")
        if not img.exists() and not (_OUT / "images" / lbl_file.parent.name /
                                     (lbl_file.stem + ".png")).exists():
            orphan += 1
        for line in lbl_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                stats[CLASSES[int(line.split()[0])]] += 1
    print(f"图-标不配对（有标无图）: {orphan}")
    for cls in CLASSES:
        n = stats.get(cls, 0)
        mark = "✓" if n >= 200 else "! 偏少（<200 框，微调后该类召回可能不稳）"
        print(f"  {cls:<10} {n:>6} 框  {mark}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开集重映射合并进 datasets/vision")
    parser.add_argument("--source", type=Path, help="源数据集目录（YOLO 格式：images/ + labels/）")
    parser.add_argument("--map", dest="mapping", help='类别映射，如 "0:hat,1:person"')
    parser.add_argument("--prefix", default="src", help="文件名前缀（防多源重名）")
    parser.add_argument("--force", action="store_true", help="输出目录已有内容时仍执行")
    parser.add_argument("--check", action="store_true", help="只体检现有输出，不合并")
    args = parser.parse_args(argv)

    if args.check:
        check_output()
        return 0
    if not args.source or not args.mapping:
        parser.error("合并需要 --source 与 --map（或用 --check 只体检）")

    if _OUT.exists() and any(_OUT.iterdir()) and not args.force:
        print(f"! {_OUT} 已有内容。确认覆盖/追加请加 --force（重名会被前缀区分）")
        return 1
    mapping = parse_mapping(args.mapping, args.source)
    stats: Counter = Counter()
    dropped: Counter = Counter()
    kept, dropped_n = merge_source(args.source, mapping, args.prefix, stats, dropped)
    print(f"[{args.prefix}] 保留 {kept} 图，丢弃 {dropped_n} 图")
    for cls in CLASSES:
        if stats.get(cls):
            print(f"  {cls:<10} {stats[cls]:>6} 框")
    for reason, n in dropped.most_common():
        print(f"  丢弃 {reason}: {n}")
    print("\n合并后体检：")
    check_output()
    print("\n下一步：python tools/vision_train.py --check  → 通过后按其打印的命令训练")
    return 0


if __name__ == "__main__":
    sys.exit(main())
