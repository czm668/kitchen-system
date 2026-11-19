# -*- coding: utf-8 -*-
"""视觉模型「现场可复现」脚本（11-19 实证沉淀）。

## 它回答答辩的三个追问

1. **指标怎么复现？** —— `python tools/vision_repro.py baseline` 用训练环境
   （torch+cu124 的 python）在同一 val 集上重算，期望复现 mAP50-95≈0.643。
2. **为什么不靠 TTA 提点？** —— `... tta` 实测 TTA 反而下降（0.638 < 0.644），
   这条负面结论同样可复现，省得评审现场瞎试。
3. **为什么不直接重训换更大模型？** —— 见 `--help` 末尾的「边界」段：数据
   uniform/phone 零标注、下载受限、单轮 6.5 分钟（80 轮 8+ 小时），三条硬约束。

## 运行环境（重要）

**必须用带 CUDA 的训练环境**，不是系统默认 python（默认是 torch CPU 版）：

    PY="D:/liulan1/毕设/.local/python311/python.exe"   # torch 2.6.0+cu124
    cd 02_项目代码/kitchen                             # yaml 的 path 是相对 cwd
    "$PY" tools/vision_repro.py baseline               # 或 tta

若本机字体缓存目录被拒（WinError 5），脚本会自动把 MPLCONFIGDIR 指到可写临时目录。

## 它不做什么

不改权重、不改指标单一源、不训练。只做「把已发生的事实当场再算一遍」，
输出与 docs/vision_model_metrics.json 对齐的口径。离线/CI 环境不需要它。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 期望值（来自 docs/vision_model_metrics.json，答辩口径）
EXPECTED_MAP5095 = 0.64269
TOLERANCE = 0.005
VAL_SPLIT = "val"
IMGSZ = 416  # 必须与判定层一致（见 detector.py / vision_train.py）


def _ensure_font_cache() -> None:
    """Windows 上 %LOCALAPPDATA%\\matplotlib 常被拒；预置可写缓存避免联网下载字体。"""
    if os.name != "nt" or os.environ.get("MPLCONFIGDIR"):
        return
    cache = Path(tempfile.gettempdir()) / "kitchen-mplconfig"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(cache)
    except OSError:
        pass
    # Ultralytics 从 USER_CONFIG_DIR 找 Arial.ttf，找不到就联网（代理会 502）。
    # 有系统字体就复制过去，纯本地绕过网络。
    cfg = Path(os.environ.get("APPDATA", "")) / "Ultralytics"
    font = cfg / "Arial.ttf"
    system = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    if not font.exists() and system.exists():
        try:
            cfg.mkdir(parents=True, exist_ok=True)
            font.write_bytes(system.read_bytes())
        except OSError:
            pass


def _validate(*, augment: bool) -> dict[str, float]:
    from ultralytics import YOLO  # 惰性导入：无 torch 的环境 import 本文件不报错

    _ensure_font_cache()
    model = YOLO(str(ROOT / "weights" / "kitchen.pt"))
    result = model.val(
        data=str(ROOT / "fixtures" / "vision_dataset.yaml"),
        split=VAL_SPLIT, imgsz=IMGSZ, device=0, verbose=False,
        plots=False, augment=augment,
    )
    box = result.box
    return {
        "mAP50-95": float(box.map),
        "mAP50": float(box.map50),
        "precision": float(box.mp),
        "recall": float(box.mr),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="视觉模型现场复现（需 CUDA 训练环境）",
        epilog=("边界：uniform/phone 两类 val 真值框为 0，无法算 AP；"
                "当前 4 类口径已是数据上限。TTA 实测下降、更大模型下载受限、"
                "全量重训 8+ 小时——提升需先补数据，见 docs/vision_model_metrics.json。"),
    )
    parser.add_argument("mode", choices=("baseline", "tta"),
                        help="baseline=常规推理复现；tta=测试时增强对照（预期下降）")
    args = parser.parse_args(argv)

    try:
        metrics = _validate(augment=(args.mode == "tta"))
    except ImportError as exc:
        print(f"需要带 ultralytics/torch 的训练环境（非系统默认 python）：{exc}",
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 现场工具，任何失败都要给出可读信息
        print(f"复现失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"[{args.mode}] " + "  ".join(f"{k}={v:.5f}" for k, v in metrics.items()))
    if args.mode == "baseline":
        delta = abs(metrics["mAP50-95"] - EXPECTED_MAP5095)
        verdict = "PASS" if delta <= TOLERANCE else "DRIFT"
        print(f"期望 mAP50-95≈{EXPECTED_MAP5095:.5f}（±{TOLERANCE}），"
              f"偏差 {delta:.5f} → {verdict}")
        return 0 if verdict == "PASS" else 1
    print("TTA 对照：若 mAP50-95 低于 baseline，则印证‘不靠 TTA 提点’的结论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
