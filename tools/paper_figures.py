# -*- coding: utf-8 -*-
"""论文第 5 章图表一键重生成（A14）。

## 为什么写这个

训练/实验/BOM 的**原始数字**早就有，但论文里要的是中文标注、可重跑的图：
手工画一次，改一个数据就得重画一轮。本脚本只读固定输入、输出固定文件，
数据变了重跑一条命令即可，且每张图都带数据源标注，不凭空造数。

## 数据源（全部实码/实测文件，缺一即报错，不静默兜底）

| 输入 | 用途 |
|---|---|
| `runs/detect/runs/vision/kitchen-v1/results.csv` | 80 epoch 训练曲线（loss / mAP） |
| `var/train.log` | 验证集分项 AP、Ultralytics 官方单次测速 |
| `runs/.../BoxPR_curve.png` | Ultralytics 原始 PR 曲线（真实数据，本脚本只加中文标注与出处） |
| `05_系统记录/硬件采购BOM_立创导入版.xlsx` | 硬件成本构成（按「类别」汇总金额） |
| `05_系统记录/20261112_对比实验报告.md` | 三组对比实验的处置延迟 |
| `datasets/vision/images/val/` + `weights/kitchen.pt` | `--bench` 实测端到端单帧延迟分布 |

## 用法（工程根 02_项目代码/kitchen）

    python tools/paper_figures.py                       # 用已有数据重出全部图
    python tools/paper_figures.py --bench 200          # 先真跑 200 帧测延迟，再出图
    python tools/paper_figures.py --out 05_系统记录/论文图表_v2
    python tools/paper_figures.py --check              # 只校验数据源，不写文件

产出目录默认 `../../05_系统记录/论文图表_v1/`，同时写 `figures_manifest.json`
（源文件 sha256 + 生成时间 + 口径说明），改数据后可核对图是否过期。

## 诚实标注（别删）

- 分项 AP 只有 4 类：uniform 无公开集、phone 吃 COCO 底子，**未列入的类别不补 0**。
- 延迟分布是本机 RTX 4060 / 640px 输入下的**演示估值**，换机器数值会变。
- BOM 是**估算单价**，不是采购发票；对比实验全部标注「仿真」。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 02_项目代码/kitchen
PROJECT = ROOT.parent.parent                            # kitchen-system
RUN = ROOT / "runs/detect/runs/vision/kitchen-v1"
WEIGHTS = ROOT / "weights/kitchen.pt"
VAL_IMAGES = ROOT / "datasets/vision/images/val"
TRAIN_LOG = ROOT / "var/train.log"
RESULTS_CSV = RUN / "results.csv"
PR_PNG = RUN / "BoxPR_curve.png"
BOM_XLSX = PROJECT / "05_系统记录/硬件采购BOM_立创导入版.xlsx"
EXPERIMENT_MD = PROJECT / "05_系统记录/20261112_对比实验报告.md"
DEFAULT_OUT = PROJECT / "05_系统记录/论文图表_v1"

# matplotlib 默认缓存目录在本机不可写（AppData 权限），改到工程内
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".local/mplcache"))
os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / ".local/ultralytics"))
(ROOT / ".local/mplcache").mkdir(parents=True, exist_ok=True)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
import numpy as np
from PIL import Image

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
]
FONT_PATH = next((p for p in FONT_CANDIDATES if p.exists()), None)
FP = fm.FontProperties(fname=str(FONT_PATH)) if FONT_PATH else fm.FontProperties()
plt.rcParams["axes.unicode_minus"] = False

CLASS_NAMES = ("all", "person", "hat", "mask", "uniform", "cigarette", "phone")
CLASS_ROW = re.compile(
    r"^\s*(all|person|hat|mask|uniform|cigarette|phone)\s+(\d+)\s+(\d+)\s+"
    r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$"
)
SPEED_ROW = re.compile(
    r"Speed:\s*([\d.]+)ms preprocess,\s*([\d.]+)ms inference,\s*"
    r"([\d.]+)ms loss,\s*([\d.]+)ms postprocess", re.I
)
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def require(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"数据源缺失：{path}")
    return path


def parse_results() -> list[dict]:
    with require(RESULTS_CSV).open(encoding="utf-8-sig", newline="") as f:
        rows = [{k: (float(v) if k != "epoch" else int(v)) for k, v in r.items()}
                for r in csv.DictReader(f)]
    if len(rows) < 2:
        raise ValueError("results.csv 行数异常")
    return rows


def parse_train_log() -> dict:
    """从最终一次验证输出取分项 AP 与官方测速；进度条行不匹配，自动跳过。"""
    classes: dict[str, dict] = {}
    speed: dict[str, float] = {}
    equipment = ""
    text = require(TRAIN_LOG).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        m = CLASS_ROW.match(line)
        if m:
            name = m.group(1)
            classes[name] = {
                "images": int(m.group(2)), "instances": int(m.group(3)),
                "P": float(m.group(4)), "R": float(m.group(5)),
                "mAP50": float(m.group(6)), "mAP50_95": float(m.group(7)),
            }  # 后出现的行覆盖前面（日志里含多轮验证，取最后一轮）
        m = SPEED_ROW.search(line)
        if m:
            speed = dict(zip(("preprocess", "inference", "loss", "postprocess"),
                             (float(x) for x in m.groups())))
        if "CUDA:0" in line and "Ultralytics" in line:
            equipment = line.strip()
    if "all" not in classes:
        raise ValueError("train.log 里找不到最终验证表")
    return {"classes": classes, "speed_ms": speed, "equipment": equipment}


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_bom() -> dict[str, float]:
    """纯标准库读 xlsx（本机无 openpyxl）；sheet1 的 B 列=类别，J 列=金额。"""
    path = require(BOM_XLSX)
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(XLSX_NS + "t"))
                      for si in root.findall(XLSX_NS + "si")]
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        totals: dict[str, float] = {}
        for row in root.findall(".//" + XLSX_NS + "row"):
            cells: dict[int, str] = {}
            for c in row.findall(XLSX_NS + "c"):
                v = c.find(XLSX_NS + "v")
                val = "" if v is None else (v.text or "")
                if c.get("t") == "s" and val.isdigit():
                    val = shared[int(val)]
                elif c.get("t") == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(XLSX_NS + "t"))
                cells[_col_index(c.get("r", "A1"))] = val
            category = cells.get(1, "").strip()
            amount = cells.get(9, "").strip()
            if not category or category == "类别" or not amount:
                continue
            try:
                value = float(amount)
            except ValueError:
                continue
            if value > 0:
                totals[category] = totals.get(category, 0.0) + value
    if not totals:
        raise ValueError("BOM 里没解析出任何金额")
    return totals


def parse_experiment() -> dict:
    text = require(EXPERIMENT_MD).read_text(encoding="utf-8")
    scenarios: list[tuple[str, float]] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        m = re.search(r"\*\*([\d.]+)\s*秒\*\*", line)
        if not m:
            continue
        label = line.split("|")[1].strip()
        label = re.sub(r"[（(].*", "", label).strip()
        label = re.sub(r"\s*[\d.]+\s*秒\s*$", "", label).strip()  # 场景名里别把「12 秒」带进标签
        if not label:
            continue
        scenarios.append((label, float(m.group(1))))
    false_alarm = None
    m = re.search(r"([\d.]+)\s*次\s*/\s*小时|误报.*?([\d.]+)\s*次", text)
    if m:
        false_alarm = float(m.group(1) or m.group(2))
    if not scenarios:
        raise ValueError("对比实验报告里没解析出延迟数据")
    return {"scenarios": scenarios, "false_alarm_per_hour": false_alarm}


def bench_latency(count: int, out: Path) -> dict:
    """真跑模型测端到端单帧延迟（含预处理/推理/后处理），不编造分布。"""
    import torch
    from ultralytics import YOLO

    if not require(WEIGHTS).exists():
        raise FileNotFoundError("缺 weights/kitchen.pt")
    images = sorted(VAL_IMAGES.glob("*.jpg")) + sorted(VAL_IMAGES.glob("*.png"))
    if not images:
        raise FileNotFoundError(f"验证图片为空：{VAL_IMAGES}")
    images = images[:count]
    device = 0 if torch.cuda.is_available() else "cpu"
    device_name = torch.cuda.get_device_name(0) if device == 0 else "CPU"
    model = YOLO(str(WEIGHTS))
    for p in images[:3]:  # 预热，避免首帧 CUDA 初始化混进分布
        model.predict(str(p), imgsz=640, device=device, verbose=False)
    samples = []
    for p in images:
        t0 = time.perf_counter()
        model.predict(str(p), imgsz=640, device=device, verbose=False)
        samples.append(round((time.perf_counter() - t0) * 1000, 3))
    record = {
        "mode": "measured",
        "note": "端到端单帧耗时（预处理+推理+后处理，含 Python 调用开销）；演示估值，换机器需重测",
        "source": f"{WEIGHTS.relative_to(ROOT).as_posix()} + datasets/vision/images/val",
        "device": device_name, "imgsz": 640, "count": len(samples),
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "samples_ms": samples,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "latency_samples.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def load_latency(out: Path, log: dict) -> dict:
    path = out / "latency_samples.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "mode": "reference",
        "note": "未跑 --bench：只有 Ultralytics 验证日志的单次测速，不是分布，禁止画成直方图",
        "source": "var/train.log 的 Speed 行",
        "speed_ms": log["speed_ms"],
        "equipment": log["equipment"],
    }


def _label(ax, title: str, xlabel: str, ylabel: str, footnote: str,
           x: float = 0.01) -> None:
    ax.set_title(title, fontproperties=FP, fontsize=14, pad=12)
    ax.set_xlabel(xlabel, fontproperties=FP, fontsize=11)
    ax.set_ylabel(ylabel, fontproperties=FP, fontsize=11)
    for t in list(ax.get_xticklabels()) + list(ax.get_yticklabels()):
        t.set_fontproperties(FP)
    ax.grid(alpha=0.3, linestyle="--")
    if footnote:
        ax.figure.text(x, 0.01, footnote, fontproperties=FP, fontsize=8, color="#666")


def fig_training(rows: list[dict], out: Path, dpi: int) -> str:
    ep = [r["epoch"] for r in rows]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 4.8))
    for key, label, style in [("train/box_loss", "训练 box", "-"),
                              ("train/cls_loss", "训练 cls", "-"),
                              ("train/dfl_loss", "训练 dfl", "-"),
                              ("val/box_loss", "验证 box", "--"),
                              ("val/cls_loss", "验证 cls", "--"),
                              ("val/dfl_loss", "验证 dfl", "--")]:
        ax1.plot(ep, [r[key] for r in rows], style, label=label, linewidth=1.6)
    ax1.legend(prop=FP, ncol=2, fontsize=9)
    _label(ax1, "图 5-1 训练损失曲线（80 epochs）", "训练轮次", "损失值", "")
    ax2.plot(ep, [r["metrics/mAP50(B)"] for r in rows], label="mAP50", linewidth=2)
    ax2.plot(ep, [r["metrics/mAP50-95(B)"] for r in rows], label="mAP50-95", linewidth=2)
    ax2.plot(ep, [r["metrics/precision(B)"] for r in rows], label="精确率 P", linewidth=1.2, alpha=0.7)
    ax2.plot(ep, [r["metrics/recall(B)"] for r in rows], label="召回率 R", linewidth=1.2, alpha=0.7)
    last = rows[-1]
    ax2.annotate(f"最终 {last['metrics/mAP50(B)']:.3f} / {last['metrics/mAP50-95(B)']:.3f}",
                 xy=(ep[-1], last["metrics/mAP50(B)"]), xytext=(-120, -28),
                 textcoords="offset points", fontproperties=FP, fontsize=9,
                 arrowprops=dict(arrowstyle="->", color="#555"))
    ax2.set_ylim(0, 1.05)
    ax2.legend(prop=FP, fontsize=9, loc="lower right")
    _label(ax2, "图 5-2 验证集精度收敛", "训练轮次", "指标值", "")
    fig.text(0.5, 0.012,
             "数据源：runs/detect/runs/vision/kitchen-v1/results.csv（80 epochs，3484 图 / 9893 实例） · "
             "mAP50=0.929 / mAP50-95=0.643 · 生成：tools/paper_figures.py",
             ha="center", fontproperties=FP, fontsize=8, color="#666")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    name = "01_训练曲线.png"
    fig.savefig(out / name, dpi=dpi)
    plt.close(fig)
    return name


def fig_ap(log: dict, out: Path, dpi: int) -> str:
    cls = log["classes"]
    names = [k for k in ("person", "hat", "mask", "cigarette") if k in cls]
    x = np.arange(len(names))
    w = 0.36
    fig, ax = plt.subplots(figsize=(8.4, 5))
    b1 = ax.bar(x - w / 2, [cls[n]["mAP50"] for n in names], w, label="mAP50", color="#3b7dd8")
    b2 = ax.bar(x + w / 2, [cls[n]["mAP50_95"] for n in names], w, label="mAP50-95", color="#e39a3b")
    for bars in (b1, b2):
        for b in bars:
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.012,
                    f"{b.get_height():.3f}", ha="center", fontproperties=FP, fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(names, fontproperties=FP)
    ax.set_ylim(0, 1.12)
    ax.legend(prop=FP)
    _label(ax, "图 5-3 各目标类别检测精度（分项 AP）", "类别", "AP 值",
           "数据源：var/train.log 最终验证表 · uniform 无公开集、phone 吃 COCO 底子，未列入的类别不补 0")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    name = "02_分项AP.png"
    fig.savefig(out / name, dpi=dpi)
    plt.close(fig)
    return name


def fig_pr(out: Path, dpi: int) -> str:
    """Ultralytics 原始 PR 曲线是真实数据，只补中文标题与出处，不重画曲线。"""
    img = Image.open(require(PR_PNG)).convert("RGB")
    fig = plt.figure(figsize=(9.2, 7.2))
    ax = fig.add_axes((0.06, 0.14, 0.88, 0.76))
    ax.imshow(img)
    ax.axis("off")
    fig.text(0.5, 0.955, "图 5-4 精确率—召回率（PR）曲线", ha="center",
             fontproperties=FP, fontsize=15)
    fig.text(0.5, 0.075,
             "横轴：召回率 Recall ｜ 纵轴：精确率 Precision ｜ 曲线：各类别与总体 all\n"
             "原图由 Ultralytics 验证阶段生成（best.pt，3484 图 / 9893 实例），本脚本仅补中文标注",
             ha="center", fontproperties=FP, fontsize=9, color="#555")
    fig.text(0.01, 0.01, f"数据源：{PR_PNG.relative_to(ROOT).as_posix()} · 生成：tools/paper_figures.py",
             fontproperties=FP, fontsize=8, color="#666")
    name = "03_PR曲线.png"
    fig.savefig(out / name, dpi=dpi)
    plt.close(fig)
    return name


def fig_latency(lat: dict, out: Path, dpi: int) -> str:
    fig, ax = plt.subplots(figsize=(8.4, 5))
    if lat["mode"] == "measured":
        s = np.array(lat["samples_ms"], dtype=float)
        ax.hist(s, bins=30, color="#3b7dd8", alpha=0.85, edgecolor="white")
        p50, p95 = np.percentile(s, [50, 95])
        ax.axvline(p50, color="#1f6f3b", linewidth=2, label=f"P50 {p50:.1f} ms")
        ax.axvline(p95, color="#c0392b", linewidth=2, label=f"P95 {p95:.1f} ms")
        pure = lat.get("reference_speed_ms", {}).get("inference")
        pure_note = (f"\nUltralytics 日志纯推理 {pure:.1f} ms，差值来自 Python/IO 开销"
                     if pure is not None else "")
        note = (f"实测 {lat['count']} 帧 · {lat['device']} · imgsz=640\n"
                f"均值 {s.mean():.1f} ms / 最小 {s.min():.1f} / 最大 {s.max():.1f}\n"
                "含预处理+推理+后处理与 Python 调用开销；演示估值，换机器需重测"
                + pure_note)
        _label(ax, "图 5-5 单帧推理延迟分布（端到端）", "单帧耗时（ms）", "帧数",
               "数据源：latency_samples.json（--bench 真跑生成） · 生成：tools/paper_figures.py")
        ax.legend(prop=FP)
        ax.text(0.98, 0.95, note, transform=ax.transAxes, ha="right", va="top",
                fontproperties=FP, fontsize=9,
                bbox=dict(boxstyle="round", facecolor="white", alpha=0.9))
    else:
        # 没跑 bench 就只有一个测速点：如实画成阶段耗时条形，绝不伪造成分布
        sp = lat["speed_ms"]
        stages = [("预处理", sp.get("preprocess", 0)), ("推理", sp.get("inference", 0)),
                  ("损失", sp.get("loss", 0)), ("后处理", sp.get("postprocess", 0))]
        ax.bar([s[0] for s in stages], [s[1] for s in stages], color="#3b7dd8")
        for i, (_, v) in enumerate(stages):
            ax.text(i, v + 0.02, f"{v:.1f} ms", ha="center", fontproperties=FP, fontsize=9)
        ax.set_ylim(0, max(v for _, v in stages) * 1.5 + 0.1)
        _label(ax, "图 5-5 单帧推理各阶段耗时（测速参考值）", "阶段", "耗时（ms）",
               "数据源：var/train.log 的 Speed 行 · 生成：tools/paper_figures.py")
        ax.text(0.98, 0.95,
                "尚未执行 --bench：这是 Ultralytics 单次测速记录，\n"
                "不是分布，勿当作统计结果引用。跑 --bench 200 可出真分布。",
                transform=ax.transAxes, ha="right", va="top", fontproperties=FP, fontsize=9,
                bbox=dict(boxstyle="round", facecolor="#fff6e5", alpha=0.95))
        for t in ax.get_xticklabels():
            t.set_fontproperties(FP)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    name = "04_推理延迟.png"
    fig.savefig(out / name, dpi=dpi)
    plt.close(fig)
    return name


def fig_cost(bom: dict[str, float], out: Path, dpi: int) -> str:
    items = sorted(bom.items(), key=lambda kv: -kv[1])
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    total = sum(values)
    fig, ax = plt.subplots(figsize=(8.6, 6))
    wedges, _, autotexts = ax.pie(
        values, labels=None, autopct=lambda p: f"{p:.1f}%" if p >= 3 else "",
        startangle=110, textprops={"fontproperties": FP, "fontsize": 9},
        colors=plt.cm.Set2(np.linspace(0, 1, len(items))))
    ax.legend(wedges, [f"{k}  {v:.2f} 元" for k, v in items],
              prop=FP, loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=9)
    ax.set_title(f"图 5-6 硬件成本构成（合计 {total:.2f} 元）", fontproperties=FP, fontsize=14)
    ax.text(0, -1.28, "数据源：05_系统记录/硬件采购BOM_立创导入版.xlsx（估算单价，非采购发票）\n"
            f"逐行求和 {total:.2f} 元；BOM 合计行写 257.70 元（差 2.00 元，待核对）\n"
            "演示估值；燃气电磁阀改用 MG90S 舵机替代表，答辩口径见采购说明",
            ha="center", fontproperties=FP, fontsize=8.5, color="#666")
    fig.subplots_adjust(right=0.72, bottom=0.16, top=0.9)
    name = "05_成本构成.png"
    fig.savefig(out / name, dpi=dpi)
    plt.close(fig)
    return name


def fig_experiment(exp: dict, out: Path, dpi: int) -> str:
    items = exp["scenarios"]
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.barh(labels, values, color="#3b7dd8")
    for b, v in zip(bars, values):
        ax.text(v + max(values) * 0.02, b.get_y() + b.get_height() / 2,
                f"{v:.1f} s", va="center", fontproperties=FP, fontsize=9)
    ax.invert_yaxis()
    for t in ax.get_yticklabels():
        t.set_fontproperties(FP)
    fa = exp.get("false_alarm_per_hour")
    note = "正常态误报 %s 次/h" % (f"{fa:.2f}" if fa is not None else "（报告未解析出）")
    ax.text(0.98, 0.05, note + " · 全部为仿真注入数据，非真机实测",
            transform=ax.transAxes, ha="right", fontproperties=FP, fontsize=9,
            bbox=dict(boxstyle="round", facecolor="#fff6e5", alpha=0.95))
    ax.set_xlim(0, max(values) * 1.25 + 0.5)
    _label(ax, "图 5-7 报警处置延迟对比实验", "发现→动作延迟（秒）", "场景",
           "数据源：05_系统记录/20261112_对比实验报告.md · 规则层含设计确认窗口（防误报，不是系统慢）")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    name = "06_对比实验.png"
    fig.savefig(out / name, dpi=dpi)
    plt.close(fig)
    return name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="论文第 5 章图表一键重生成")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="产出目录")
    ap.add_argument("--bench", type=int, default=0, metavar="N",
                    help="先真跑 N 帧测端到端延迟，再出分布图")
    ap.add_argument("--dpi", type=int, default=160)
    ap.add_argument("--check", action="store_true", help="只校验数据源，不写文件")
    args = ap.parse_args(argv)
    out: Path = args.out.resolve()

    rows = parse_results()
    log = parse_train_log()
    bom = parse_bom()
    exp = parse_experiment()
    require(PR_PNG)
    print(f"数据源校验通过：{len(rows)} epochs / {len(log['classes'])} 类指标 / "
          f"{len(bom)} 类成本 / {len(exp['scenarios'])} 组对比实验")
    if args.check:
        return 0

    out.mkdir(parents=True, exist_ok=True)
    lat = bench_latency(args.bench, out) if args.bench > 0 else load_latency(out, log)
    lat.setdefault("reference_speed_ms", log["speed_ms"])
    made = [
        fig_training(rows, out, args.dpi),
        fig_ap(log, out, args.dpi),
        fig_pr(out, args.dpi),
        fig_latency(lat, out, args.dpi),
        fig_cost(bom, out, args.dpi),
        fig_experiment(exp, out, args.dpi),
    ]
    sources = [RESULTS_CSV, TRAIN_LOG, PR_PNG, BOM_XLSX, EXPERIMENT_MD]
    if lat["mode"] == "measured":
        sources += [WEIGHTS, out / "latency_samples.json"]
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": "tools/paper_figures.py",
        "dpi": args.dpi,
        "figures": made,
        "latency": {k: v for k, v in lat.items() if k != "samples_ms"},
        "sources": [{"path": str(p), "sha256": sha256(p)} for p in sources],
        "disclaimer": "仿真/估值数据；分项 AP 缺类不补 0，延迟为演示机实测估值，BOM 为估算单价",
    }
    (out / "figures_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for name in made:
        print(f"  ✓ {name}")
    print(f"完成：{len(made)} 张图 + figures_manifest.json → {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 缺数据源直接失败，不产出半套图
        print(f"失败：{exc}", file=sys.stderr)
        sys.exit(1)
