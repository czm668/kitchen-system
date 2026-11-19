# -*- coding: utf-8 -*-
"""phone 类别标注补齐工具（普查 → 人工复核 → 合并）。

背景：`docs/vision_model_metrics.json` 已记录 uniform/phone 两类零标注。
本工具把"补 phone 标注"做成可复现、可审计的三步，不直接编造真值：

1. **scan**——用 COCO 预训练底座（`yolov8n.pt`，80 类，含 `cell phone`）扫图，
   产出候选框：`var/phone_census/<split>_phone.csv`（全部候选）+
   `datasets/vision/labels_proposed/<split>/*.txt`（conf≥keep 的候选）。
   **只写候选区，绝不碰 `labels/` 真值。**
2. **sheet**——把候选裁剪拼成对照大图，人工逐格裁决
   （accept=手持手机 / reject=误检或非手持 / unclear），
   结果写 `var/phone_census/approvals_<split>.csv`。
3. **commit**——按 approvals 把 accept 行并入 `labels/<split>/`。
   合并规则：按 (文件, 坐标 4 位小数) 去重；不动 test 集（保持 truly held-out）。

标注口径见 `fixtures/vision_dataset.yaml`：**phone 只标手持或贴脸状态**，
放着的手机不算违规——sheet 裁决时按此执行。

用法：
    python tools/vision_label_patch.py scan --split val            # 普查
    python tools/vision_label_patch.py sheet --split val --top 30  # 出对照图
    python tools/vision_label_patch.py commit --split val \
        --approvals var/phone_census/approvals_val.csv             # 合并
    python tools/vision_label_patch.py status                      # 汇总

纯函数（`merge_approved` / `parse_approvals` / `dedupe_key`）不依赖任何第三方库，
`tests/test_vision_label_patch.py` 直接单测；scan/sheet 需要训练环境
（ultralytics + CUDA，见 `tools/vision_repro.py` 的环境说明）。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets" / "vision"
CENSUS = ROOT / "var" / "phone_census"
#: 数据集 6 类里 phone 的 id（与 src/kitchen/vision/models.py 的 PHONE 一致）
PHONE_CLASS_ID = 5
#: COCO 80 类里 cell phone 的 id
COCO_PHONE = 67

__all__ = [
    "scan", "sheet", "commit", "status",
    "merge_approved", "parse_approvals", "dedupe_key", "load_census_rows",
]


# ------------------------------------------------------------- 纯函数（可单测）
def dedupe_key(image: str, xc: float, yc: float, w: float, h: float) -> tuple:
    """合并去重键：同一张图、坐标 4 位小数相同视为同一条。"""
    return (image, round(xc, 4), round(yc, 4), round(w, 4), round(h, 4))


def parse_approvals(path: Path) -> dict[tuple, str]:
    """读人工裁决表：`approvals_<split>.csv`（census 列 + verdict 列）。

    verdict ∈ {accept, reject, unclear}；返回 `{dedupe_key: verdict}`。
    """
    verdicts: dict[tuple, str] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            verdicts[dedupe_key(row["image"], float(row["xc"]), float(row["yc"]),
                                float(row["w"]), float(row["h"]))] = \
                row["verdict"].strip().lower()
    return verdicts


def merge_approved(existing: str, accepted_lines: Iterable[str]) -> str:
    """把 accept 的 YOLO 行并入已有标注文本（去重、保持末行换行）。"""
    lines = [ln for ln in existing.splitlines() if ln.strip()]
    seen = {tuple(ln.split()[:5]) for ln in lines}
    for new in accepted_lines:
        parts = tuple(new.split()[:5])
        if parts in seen:
            continue
        seen.add(parts)
        lines.append(new.strip())
    return "\n".join(lines) + ("\n" if lines else "")


def load_census_rows(split: str, root: Path = ROOT) -> list[dict]:
    """读普查 CSV（不存在返回空表）。"""
    path = root / "var" / "phone_census" / f"{split}_phone.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ------------------------------------------------------------- scan（需 ultralytics）
def scan(split: str, *, limit: int | None = None, chunk: int = 32,
         conf: float = 0.30, keep: float = 0.40, device: int = 0) -> dict:
    """用 COCO 底座扫 `<split>` 的全部图，产出候选 CSV 与候选标注。"""
    try:
        from ultralytics import YOLO
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            f"scan 需要 ultralytics（训练环境 python，见 tools/vision_repro.py）：{e}")
    img_dir = DATASET / "images" / split
    files = sorted(p for p in img_dir.iterdir()
                   if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if limit:
        files = files[::max(1, len(files) // limit)][:limit]
    model = YOLO(str(ROOT / "yolov8n.pt"))
    assert len(model.names) == 80 and model.names[COCO_PHONE] == "cell phone", \
        "底座不是 COCO 80 类，不能当 phone 教师"
    CENSUS.mkdir(parents=True, exist_ok=True)
    prop_dir = DATASET / "labels_proposed" / split
    prop_dir.mkdir(parents=True, exist_ok=True)
    rows, kept, t0 = [], 0, time.time()
    for i in range(0, len(files), chunk):
        batch = [str(p) for p in files[i:i + chunk]]
        for r in model.predict(source=batch, stream=True, imgsz=640, conf=conf,
                               iou=0.70, device=device, classes=[COCO_PHONE],
                               verbose=False):
            name = Path(r.path).name
            for b in r.boxes:
                c = float(b.conf)
                x, y, w, h = b.xywhn[0].tolist()
                rows.append({"split": split, "image": name, "conf": round(c, 3),
                             "xc": round(x, 5), "yc": round(y, 5),
                             "w": round(w, 5), "h": round(h, 5)})
                if c >= keep:
                    kept += 1
                    with (prop_dir / f"{Path(name).stem}.txt").open("a") as fh:
                        fh.write(f"{PHONE_CLASS_ID} {x:.5f} {y:.5f} "
                                 f"{w:.5f} {h:.5f}\n")
    out = CENSUS / f"{split}_phone.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["split", "image", "conf",
                                           "xc", "yc", "w", "h"])
        w.writeheader()
        w.writerows(rows)
    return {"split": split, "scanned": len(rows) and len(files), "boxes": len(rows),
            "kept": kept, "images_with_phone": len({r["image"] for r in rows}),
            "csv": str(out), "elapsed": round(time.time() - t0, 1)}


# ------------------------------------------------------------- sheet（需 cv2）
def sheet(split: str, *, top: int = 30, out_name: str | None = None,
          root: Path = ROOT) -> Path | None:
    """候选裁剪拼对照大图，供人工裁决。返回图片路径（无候选返回 None）。"""
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise SystemExit(f"sheet 需要 opencv（训练环境 python）：{e}")
    rows = sorted(load_census_rows(split, root), key=lambda r: -float(r["conf"]))
    if not rows:
        return None
    tile, pad = 240, 6
    tiles = []
    for r in rows[:top]:
        img_path = root / "datasets" / "vision" / "images" / split / r["image"]
        buf = np.fromfile(str(img_path), dtype=np.uint8)  # 中文路径安全
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            continue
        H, W = img.shape[:2]
        xc, yc, w, h = (float(r[k]) for k in ("xc", "yc", "w", "h"))
        x1, y1 = max(0, int((xc - w / 2) * W)), max(0, int((yc - h / 2) * H))
        x2, y2 = min(W, int((xc + w / 2) * W)), min(H, int((yc + h / 2) * H))
        crop = img[max(0, y1 - 8):min(H, y2 + 8), max(0, x1 - 8):min(W, x2 + 8)]
        if crop.size == 0:
            continue
        ch, cw = crop.shape[:2]
        scale = tile / max(ch, cw)
        crop = cv2.resize(crop, (max(1, int(cw * scale)), max(1, int(ch * scale))),
                          interpolation=cv2.INTER_NEAREST)
        canvas = np.zeros((tile, tile, 3), np.uint8)
        oy, ox = (tile - crop.shape[0]) // 2, (tile - crop.shape[1]) // 2
        canvas[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
        cv2.putText(canvas, f'{float(r["conf"]):.2f}', (4, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
        tiles.append(canvas)
    if not tiles:
        return None
    cols = 6
    nrow = (len(tiles) + cols - 1) // cols
    grid = []
    for ri in range(nrow):
        row = tiles[ri * cols:(ri + 1) * cols]
        row += [np.zeros((tile, tile, 3), np.uint8)] * (cols - len(row))
        grid.append(np.hstack([np.pad(t, ((0, 0), (0, pad), (0, 0)),
                                      constant_values=60) for t in row]))
    out_dir = root / "var" / "phone_census"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (out_name or f"{split}_sheet.jpg")
    ok, buf = cv2.imencode(".jpg", np.vstack(grid))
    if ok:
        buf.tofile(str(out_path))
    return out_path


# ------------------------------------------------------------- commit（纯标准库）
def commit(split: str, approvals: Path, *, root: Path = ROOT,
           dry_run: bool = False) -> dict:
    """按裁决表把 accept 候选并入 `labels/<split>/`。

    规则：test 集拒绝合并（保持 truly held-out）；坐标去重；记审计日志。
    """
    if split == "test":
        raise SystemExit("test 集不合并候选（保持 truly held-out），不要动它")
    verdicts = parse_approvals(approvals)
    rows = load_census_rows(split, root)
    accepted = {}
    for r in rows:
        key = dedupe_key(r["image"], float(r["xc"]), float(r["yc"]),
                         float(r["w"]), float(r["h"]))
        if verdicts.get(key) == "accept":
            accepted.setdefault(r["image"], []).append(
                f"{PHONE_CLASS_ID} {r['xc']} {r['yc']} {r['w']} {r['h']}")
    if not accepted:
        raise SystemExit(f"裁决表里没有 accept 行（共 {len(verdicts)} 条裁决）——"
                         "先跑 sheet 并人工裁决，再 commit")
    labels_dir = root / "datasets" / "vision" / "labels" / split
    log = {"split": split, "approvals": str(approvals), "images": {},
           "dry_run": dry_run}
    merged = skipped = 0
    for image, lines in sorted(accepted.items()):
        stem = Path(image).stem
        target = labels_dir / f"{stem}.txt"
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        after = merge_approved(before, lines)
        added = len([ln for ln in after.splitlines()]) - len(
            [ln for ln in before.splitlines() if ln.strip()])
        if added <= 0:
            skipped += len(lines)
            continue
        log["images"][stem] = {"added": added}
        merged += added
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8")
    log["merged_lines"] = merged
    log["skipped_duplicates"] = skipped
    log["merged_images"] = len(log["images"])
    if not dry_run:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = root / "var" / "phone_census" / f"commit_{split}_{stamp}.json"
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        log["log"] = str(log_path)
    return log


# ------------------------------------------------------------- status
def status() -> str:
    """普查/候选/裁决汇总（人读）。"""
    lines = []
    for split in ("val", "test", "train"):
        rows = load_census_rows(split)
        prop = DATASET / "labels_proposed" / split
        n_prop = len(list(prop.glob("*.txt"))) if prop.exists() else 0
        lines.append(f"{split:6s} 候选框={len(rows):4d}  含手机图="
                     f"{len({r['image'] for r in rows}):4d}  候选标注文件={n_prop}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="phone 标注补齐：scan → sheet → commit")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("scan", help="普查候选（需训练环境）")
    sp.add_argument("--split", required=True, choices=["val", "test", "train"])
    sp.add_argument("--limit", type=int, default=None, help="抽样上限（默认全量）")
    sp.add_argument("--device", type=int, default=0)
    sh = sub.add_parser("sheet", help="候选拼图（需 cv2）")
    sh.add_argument("--split", required=True, choices=["val", "test", "train"])
    sh.add_argument("--top", type=int, default=30)
    cm = sub.add_parser("commit", help="按裁决表合并进 labels/")
    cm.add_argument("--split", required=True, choices=["val", "train"])
    cm.add_argument("--approvals", required=True, type=Path)
    cm.add_argument("--dry-run", action="store_true")
    st = sub.add_parser("status", help="汇总")
    args = p.parse_args(argv)
    if args.cmd == "scan":
        print(json.dumps(scan(args.split, limit=args.limit, device=args.device),
                         ensure_ascii=False))
    elif args.cmd == "sheet":
        path = sheet(args.split, top=args.top)
        print(path or "无候选，先 scan")
    elif args.cmd == "commit":
        print(json.dumps(commit(args.split, args.approvals, dry_run=args.dry_run),
                         ensure_ascii=False, indent=2))
    else:
        print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
