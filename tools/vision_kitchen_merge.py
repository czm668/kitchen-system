# -*- coding: utf-8 -*-
"""厨房场景图像补集工具（归档 chef_hat → 目检裁决 → 门禁合并）。

背景：现有视觉数据集全部来自工地 PPE 图（Roboflow 导出），没有厨房实拍；
uniform 类零标注。归档素材里有一套 chef_hat 数据集（Mixkit 免费素材视频
抽帧，413 个视频源、带许可证记录，见归档 `annotations/sources.json`），
单类 chef_hat、框只框帽子实物——与工程 6 类规范的 `hat` 语义一致。

本工具把"补厨房场景"做成可复现、可审计、**带门禁**的三步：

1. **ingest**——把归档 YOLO 导出（243 train + 65 val，135 个 hat 框）
   搬进暂存区 `var/kitchen_merge/`：类别 0(chef_hat) 重映射为 1(hat)，
   文件名加 `chef_` 前缀防碰撞，写审计 JSON（来源、许可证、sha256）。
   **不碰 `datasets/vision/`。**
2. **scan / sheet**——COCO 底座（`yolov8n.pt`）对暂存图普查
   person(0) 与 cell phone(67) 候选，person 裁剪拼对照图供裁决；
   裁决口径（对齐 `fixtures/vision_dataset.yaml`）：
   - person accept = 确认是人（误检/半身残影 reject）；
   - uniform accept = 该人白色工服/围裙在躯干区清晰可见，框取
     person 框躯干区（0.20–0.95，与判定层 `torso_region` 同源推导）；
   - mask **不新增标签**（无法从 person 框诚实导出口罩实物框），
     凡明显戴口罩的人 → 该图整张放弃（drop，避免假阴性监督）；
   - phone accept = 手持/贴脸（放着的手机不标，同 phone 普查口径）。
3. **commit**——按裁决表把暂存图像+标注并入 `datasets/vision/`。
   **门禁**：`var/kitchen_merge/HOLD` 存在时拒绝执行——避免在
   kitchen-v2 phone 评测（读冻结 val）完成前改变 val 集合。
   合并规则：train/val 各回各的 split、test 集永不涉及、坐标去重。

用法：
    python tools/vision_kitchen_merge.py ingest                 # 搬运+重映射
    python tools/vision_kitchen_merge.py scan                   # 教师普查
    python tools/vision_kitchen_merge.py sheet --kind person    # 对照图
    python tools/vision_kitchen_merge.py commit \
        --approvals var/kitchen_merge/approvals.csv             # 门禁合并
    python tools/vision_kitchen_merge.py status                 # 汇总

纯函数（`remap_hat_lines` / `torso_box` / `merge_approved` / `parse_approvals`）
不依赖第三方库，`tests/test_vision_kitchen_merge.py` 直接单测；
scan/sheet 需要训练环境（ultralytics + CUDA，见 `tools/vision_repro.py`）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets" / "vision"
STAGE = ROOT / "var" / "kitchen_merge"
#: 归档 chef_hat 数据集位置（旧工程，未进 git）
ARCHIVE = (ROOT.parent.parent.parent / "毕设项目" / "02_项目代码" / "proj"
           / "datasets" / "chef_hat")
#: 工程数据集 6 类 id（与 src/kitchen/vision/models.py 一致）
HAT_CLASS_ID = 1
PERSON_CLASS_ID = 0
UNIFORM_CLASS_ID = 3
PHONE_CLASS_ID = 5
#: COCO 80 类：person / cell phone
COCO_PERSON, COCO_PHONE = 0, 67
#: uniform 框 = person 框躯干区（与 postprocess.AnalysisPolicy 同源）
TORSO_TOP, TORSO_BOTTOM = 0.20, 0.95

__all__ = [
    "ingest", "scan", "sheet", "commit", "status",
    "remap_hat_lines", "torso_box", "merge_approved", "parse_approvals",
    "dedupe_key",
]


# ------------------------------------------------------------- 纯函数（可单测）
def dedupe_key(image: str, xc: float, yc: float, w: float, h: float) -> tuple:
    """合并去重键：同一张图、坐标 4 位小数相同视为同一条。"""
    return (image, round(xc, 4), round(yc, 4), round(w, 4), round(h, 4))


def remap_hat_lines(text: str) -> str:
    """归档单类标注（class 0 = chef_hat）重映射为工程 hat 类（class 1）。"""
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if not parts:
            continue
        assert parts[0] == "0", f"归档标注应只有 class 0，见 {parts!r}"
        out.append(" ".join([str(HAT_CLASS_ID)] + parts[1:]))
    return "\n".join(out) + ("\n" if out else "")


def torso_box(xc: float, yc: float, w: float, h: float) -> tuple[float, float, float, float]:
    """从 person 框推导 uniform（工服）框：躯干区 0.20–0.95 全宽。

    与 `src/kitchen/vision/postprocess.py::AnalysisPolicy.torso_region`
    同源——判定层按这个区域认工服，训练标签用同一几何，保证分布一致。
    """
    y1, y2 = yc - h / 2, yc + h / 2
    new_top = y1 + h * TORSO_TOP
    new_h = h * (TORSO_BOTTOM - TORSO_TOP)
    new_yc = new_top + new_h / 2
    return (xc, new_yc, w, new_h)


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


def parse_approvals(path: Path) -> dict[tuple, dict]:
    """读裁决表 approvals.csv：image/cand_class/xc/yc/w/h + verdict 列。

    person 行：verdict ∈ {accept, reject}，另有 mask/uniform 列
    （mask=accept 表示该人明显戴口罩 → 整图 drop；uniform=accept 表示
    白色工服清晰可见 → 追加躯干框）。
    phone 行：verdict ∈ {accept, reject}。
    返回 `{(image, class, xc, yc, w, h): row_dict}`。
    """
    verdicts: dict[tuple, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["image"], row["cand_class"],
                   round(float(row["xc"]), 4), round(float(row["yc"]), 4),
                   round(float(row["w"]), 4), round(float(row["h"]), 4))
            verdicts[key] = {k: (row.get(k) or "").strip().lower()
                             for k in ("verdict", "mask", "uniform")}
    return verdicts


# ------------------------------------------------------------- ingest
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest(archive: Path = ARCHIVE) -> dict:
    """归档 YOLO 导出 → 暂存区（图像复制、hat 重映射、审计 JSON）。"""
    yolo = archive / "yolo"
    manifest = json.loads((yolo / "manifest.json").read_text(encoding="utf-8"))
    frames = json.loads((archive / "annotations" / "frames.json")
                        .read_text(encoding="utf-8"))
    (STAGE / "images").mkdir(parents=True, exist_ok=True)
    (STAGE / "labels").mkdir(parents=True, exist_ok=True)
    audit: dict = {"archive": str(archive), "items": [], "hat_boxes": 0,
                   "images": 0, "class_map": {"0": str(HAT_CLASS_ID)}}
    for item in manifest["items"]:
        split, img_id = item["part"], item["id"]
        src_img = yolo / "images" / split / f"{img_id}.jpg"
        if not src_img.exists():  # 导出里可能是 png
            src_img = yolo / "images" / split / f"{img_id}.png"
        if not src_img.exists():
            continue
        src_lbl = yolo / "labels" / split / f"{img_id}.txt"
        new_name = f"chef_{img_id}{src_img.suffix.lower()}"
        shutil.copyfile(src_img, STAGE / "images" / new_name)
        text = src_lbl.read_text(encoding="utf-8") if src_lbl.exists() else ""
        remapped = remap_hat_lines(text)
        n_hat = len([ln for ln in remapped.splitlines() if ln.strip()])
        (STAGE / "labels" / new_name.replace(src_img.suffix.lower(), ".txt")
         ).write_text(remapped, encoding="utf-8")
        meta = frames.get(f"{img_id}.jpg", {})
        audit["items"].append({
            "image": new_name, "split": split,
            "source": meta.get("source"), "license": meta.get("license"),
            "title": meta.get("title"), "image_sha256": _sha256(src_img),
            "hat_boxes": n_hat,
        })
        audit["images"] += 1
        audit["hat_boxes"] += n_hat
    audit_path = STAGE / "ingest_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return {"images": audit["images"], "hat_boxes": audit["hat_boxes"],
            "audit": str(audit_path)}


# ------------------------------------------------------------- scan（需 ultralytics）
def scan(*, chunk: int = 32, conf: float = 0.30, device: int = 0) -> dict:
    """COCO 底座扫暂存图，产出 person/phone 候选 CSV（只写暂存区）。"""
    try:
        from ultralytics import YOLO
    except ImportError as e:  # pragma: no cover
        raise SystemExit(f"scan 需要 ultralytics（训练环境 python）：{e}")
    img_dir = STAGE / "images"
    files = sorted(p for p in img_dir.iterdir()
                   if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not files:
        raise SystemExit("暂存区为空，先跑 ingest")
    model = YOLO(str(ROOT / "yolov8n.pt"))
    assert len(model.names) == 80, "底座不是 COCO 80 类，不能当教师"
    rows, t0 = [], time.time()
    for i in range(0, len(files), chunk):
        batch = [str(p) for p in files[i:i + chunk]]
        for r in model.predict(source=batch, stream=True, imgsz=640, conf=conf,
                               iou=0.70, device=device,
                               classes=[COCO_PERSON, COCO_PHONE], verbose=False):
            name = Path(r.path).name
            for b in r.boxes:
                cid = int(b.cls)
                rows.append({
                    "image": name,
                    "cand_class": "person" if cid == COCO_PERSON else "phone",
                    "conf": round(float(b.conf), 3),
                    "xc": round(float(b.xywhn[0][0]), 5),
                    "yc": round(float(b.xywhn[0][1]), 5),
                    "w": round(float(b.xywhn[0][2]), 5),
                    "h": round(float(b.xywhn[0][3]), 5),
                })
    out = STAGE / "candidates.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.DictWriter(fh, fieldnames=["image", "cand_class", "conf",
                                             "xc", "yc", "w", "h"])
        wtr.writeheader()
        wtr.writerows(rows)
    n_person = sum(1 for r in rows if r["cand_class"] == "person")
    return {"images": len(files), "person_boxes": n_person,
            "phone_boxes": len(rows) - n_person, "csv": str(out),
            "elapsed": round(time.time() - t0, 1)}


# ------------------------------------------------------------- sheet（需 cv2）
def sheet(*, kind: str = "person", per_sheet: int = 24,
          root: Path = ROOT) -> list[Path]:
    """候选裁剪拼对照大图（每格标 序号+conf+所属图名）。返回图片路径列表。"""
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise SystemExit(f"sheet 需要 opencv（训练环境 python）：{e}")
    cand_path = STAGE / "candidates.csv"
    if not cand_path.exists():
        raise SystemExit("还没有候选，先跑 scan")
    with cand_path.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["cand_class"] == kind]
    rows.sort(key=lambda r: (r["image"], -float(r["conf"])))
    if not rows:
        return []
    tile, pad = 240, 6
    out_paths: list[Path] = []
    for si in range(0, len(rows), per_sheet):
        tiles = []
        for gi, r in enumerate(rows[si:si + per_sheet], start=si):
            img_path = STAGE / "images" / r["image"]
            buf = np.fromfile(str(img_path), dtype=np.uint8)  # 中文路径安全
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                continue
            H, W = img.shape[:2]
            xc, yc, w, h = (float(r[k]) for k in ("xc", "yc", "w", "h"))
            x1, y1 = max(0, int((xc - w / 2) * W)), max(0, int((yc - h / 2) * H))
            x2, y2 = min(W, int((xc + w / 2) * W)), min(H, int((yc + h / 2) * H))
            crop = img[max(0, y1 - 10):min(H, y2 + 10),
                       max(0, x1 - 10):min(W, x2 + 10)]
            if crop.size == 0:
                continue
            ch, cw = crop.shape[:2]
            scale = tile / max(ch, cw)
            crop = cv2.resize(crop, (max(1, int(cw * scale)),
                                     max(1, int(ch * scale))))
            canvas = np.zeros((tile, tile, 3), np.uint8)
            oy, ox = (tile - crop.shape[0]) // 2, (tile - crop.shape[1]) // 2
            canvas[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
            cv2.putText(canvas, f'#{gi} {float(r["conf"]):.2f}',
                        (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
            cv2.putText(canvas, r["image"][:26], (4, tile - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1)
            tiles.append(canvas)
        if not tiles:
            continue
        cols = 6
        nrow = (len(tiles) + cols - 1) // cols
        grid = []
        for ri in range(nrow):
            row = tiles[ri * cols:(ri + 1) * cols]
            row += [np.zeros((tile, tile, 3), np.uint8)] * (cols - len(row))
            grid.append(np.vstack([np.pad(t, ((0, 0), (0, pad), (0, 0)),
                                          constant_values=60) for t in row]))
        idx = si // per_sheet
        out_dir = STAGE / "sheets"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{kind}_sheet_{idx}.jpg"
        ok, buf = cv2.imencode(".jpg", np.hstack(grid))
        if ok:
            buf.tofile(str(out_path))
            out_paths.append(out_path)
    return out_paths


# ------------------------------------------------------------- commit（门禁）
def commit(approvals: Path, *, root: Path = ROOT, dry_run: bool = False) -> dict:
    """按裁决表把暂存图像+标注并入 `datasets/vision/`（带 HOLD 门禁）。"""
    hold = root / "var" / "kitchen_merge" / "HOLD"
    if hold.exists() and not dry_run:
        raise SystemExit(
            "门禁生效：var/kitchen_merge/HOLD 存在，拒绝合并。\n"
            "等 kitchen-v2 phone 评测完成（读冻结 val 的协议跑完）后，"
            "确认 v2 结论已落盘并提交，再移除 HOLD 重试。")
    verdicts = parse_approvals(approvals)
    with (STAGE / "candidates.csv").open(encoding="utf-8") as fh:
        cand_rows = list(csv.DictReader(fh))
    audit = {"approvals": str(approvals), "dry_run": dry_run,
             "copied_images": [], "skipped_images": {}, "labels": {}}
    dropped: set[str] = set()
    # 第一遍：mask=accept（明显戴口罩）→ 整图 drop；person 框可见误检 → drop
    person_seen: set[str] = set()
    for r in cand_rows:
        if r["cand_class"] != "person":
            continue
        person_seen.add(r["image"])
    for (image, cls, *_), v in verdicts.items():
        if cls == "person" and v.get("mask") == "accept" and image not in dropped:
            dropped.add(image)
            audit["skipped_images"][image] = "mask_visible_unlabeled"
    merged_lines = 0
    labels_out: dict[str, list[str]] = {}
    for r in cand_rows:
        image = r["image"]
        if image in dropped:
            continue
        key = (image, r["cand_class"], round(float(r["xc"]), 4),
               round(float(r["yc"]), 4), round(float(r["w"]), 4),
               round(float(r["h"]), 4))
        v = verdicts.get(key, {})
        if v.get("verdict") != "accept":
            continue
        lines = labels_out.setdefault(image, [])
        if r["cand_class"] == "person":
            lines.append(f"{PERSON_CLASS_ID} {r['xc']} {r['yc']} "
                         f"{r['w']} {r['h']}")
            if v.get("uniform") == "accept":
                ux, uy, uw, uh = torso_box(float(r["xc"]), float(r["yc"]),
                                           float(r["w"]), float(r["h"]))
                lines.append(f"{UNIFORM_CLASS_ID} {ux:.5f} {uy:.5f} "
                             f"{uw:.5f} {uh:.5f}")
        else:  # phone
            lines.append(f"{PHONE_CLASS_ID} {r['xc']} {r['yc']} "
                         f"{r['w']} {r['h']}")
    for image in sorted(person_seen):
        if image not in dropped:
            audit["skipped_images"].setdefault(image, "person_not_reviewed")
    split_of = {}
    for item in json.loads(
            (STAGE / "ingest_audit.json").read_text(encoding="utf-8"))["items"]:
        split_of[item["image"]] = item["split"]
    for image, lines in sorted(labels_out.items()):
        stem = Path(image).stem
        split = split_of.get(image)
        if split not in ("train", "val"):
            continue
        base_txt = (STAGE / "labels" / f"{stem}.txt").read_text(
            encoding="utf-8") if (STAGE / "labels" / f"{stem}.txt").exists() else ""
        final = merge_approved(base_txt, lines)
        audit["labels"][f"{stem}.txt"] = {
            "split": split, "lines": len(final.splitlines())}
        merged_lines += len(lines)
        if not dry_run:
            target_dir = root / "datasets" / "vision" / "labels" / split
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{stem}.txt").write_text(final, encoding="utf-8")
            src_img = STAGE / "images" / image
            img_dir = root / "datasets" / "vision" / "images" / split
            img_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_img, img_dir / image)
            audit["copied_images"].append(f"{split}/{image}")
    audit["merged_images"] = len(labels_out)
    audit["merged_lines"] = merged_lines
    audit["dropped_images"] = sorted(dropped)
    if not dry_run:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = STAGE / f"commit_{stamp}.json"
        log_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        audit["log"] = str(log_path)
    return audit


# ------------------------------------------------------------- status
def status() -> str:
    """暂存区/候选/裁决汇总（人读）。"""
    lines = []
    ing = STAGE / "ingest_audit.json"
    if ing.exists():
        a = json.loads(ing.read_text(encoding="utf-8"))
        lines.append(f"ingest: 图像 {a['images']}  hat 框 {a['hat_boxes']}")
    cand = STAGE / "candidates.csv"
    if cand.exists():
        with cand.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        n_p = sum(1 for r in rows if r["cand_class"] == "person")
        lines.append(f"scan: person 候选 {n_p}  phone 候选 {len(rows) - n_p}")
    ap = STAGE / "approvals.csv"
    if ap.exists():
        verdicts = parse_approvals(ap)
        c = Counter(v.get("verdict") for v in verdicts.values())
        lines.append(f"approvals: {dict(c)}")
    lines.append(f"HOLD 门禁: {'生效（commit 拒绝）' if (STAGE / 'HOLD').exists() else '已解除'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="厨房场景补集：ingest → scan → sheet → commit")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="归档搬运+重映射（写暂存区）")
    sc = sub.add_parser("scan", help="教师普查 person/phone（需训练环境）")
    sc.add_argument("--device", type=int, default=0)
    sh = sub.add_parser("sheet", help="候选拼对照图（需 cv2）")
    sh.add_argument("--kind", default="person", choices=["person", "phone"])
    cm = sub.add_parser("commit", help="按裁决表门禁合并进 datasets/vision")
    cm.add_argument("--approvals", type=Path, default=STAGE / "approvals.csv")
    cm.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="汇总")
    args = p.parse_args(argv)
    if args.cmd == "ingest":
        print(json.dumps(ingest(), ensure_ascii=False))
    elif args.cmd == "scan":
        print(json.dumps(scan(device=args.device), ensure_ascii=False))
    elif args.cmd == "sheet":
        paths = sheet(kind=args.kind)
        print("\n".join(str(x) for x in paths) or "无候选，先 scan")
    elif args.cmd == "commit":
        print(json.dumps(commit(args.approvals, dry_run=args.dry_run),
                         ensure_ascii=False, indent=2))
    else:
        print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
