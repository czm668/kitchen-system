# -*- coding: utf-8 -*-
"""视觉数据集校验与训练指令生成（A5-02）。

## 这个脚本**不训练**

本机没有 GPU、没有数据集，也没有 `ultralytics`。假装训练出来的「结果」比
没有结果更糟——论文里会写上一句「模型 mAP 0.87」，答辩被问「怎么复现」时
答不上来。所以这里只做两件真能做的事：

1. **校验类别顺序**。`fixtures/vision_dataset.yaml` 的 `names` 与
   `src/kitchen/vision/models.py` 的 `CLASSES` 必须逐位相同。顺序错了模型会把
   帽子认成手机，而代码里完全看不出来——只有现场能看到「有人拿手机」报警
   但其实是没戴帽，排查一整天。
2. **生成可复制的训练命令**，把 `imgsz / conf / 类别数 / 权重路径` 对齐到
   判定层实际期望的值。人工敲一遍十有八九把 `imgsz` 写成 640，而后处理的
   归一化按 416 算过，于是「框都在画面外」。

## 用法

```
python tools/vision_train.py --check          # 校验 + 打印训练命令
python tools/vision_train.py --check --legacy # 顺带比对归档里的旧数据集
```

退出码：类别不一致 → 1；其余情况 0（数据集没采集齐只报警告，不阻断 CI）。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kitchen.vision.models import CLASSES                            # noqa: E402

DATASET_YAML = ROOT / "fixtures" / "vision_dataset.yaml"
#: 归档素材里的旧数据集（只读，不搬）。存在就比对类别定义。
#: 走 `proj/annotations/`（数据集 yaml 在那里，`proj/datasets/` 只有图片目录）
LEGACY_ROOT = (ROOT.parents[2] / "毕设项目" / "02_项目代码" / "proj" / "annotations")

#: 判定层按 416 归一化，训练尺寸必须一致
IMGSZ = 416
#: 模型侧低门槛，后处理按类别分档过滤（见 `detector.YoloDetector`）
MODEL_CONF = 0.25


@dataclass
class CheckReport:
    names: dict[int, str]
    problems: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def parse_names(text: str) -> dict[int, str]:
    """只解析 `names:` 段。**不引 PyYAML**：便携 Python 装不上包，
    而这个文件的结构是我们自己定的，解析 6 行不值得一个依赖。"""
    names: dict[int, str] = {}
    in_names = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.strip() == "names:":
            in_names = True
            continue
        if in_names:
            if not line.startswith((" ", "\t")):
                break                       # 回到顶层键，names 段结束
            key, _, value = line.strip().partition(":")
            try:
                index = int(key.strip())
            except ValueError:
                continue
            names[index] = value.strip()
    return names


def parse_scalar(text: str, key: str) -> str:
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if line.strip().startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def check(path: Path = DATASET_YAML) -> CheckReport:
    problems: list[str] = []
    warnings: list[str] = []
    if not path.is_file():
        return CheckReport({}, [f"数据集配置不存在：{path}"], warnings)

    text = path.read_text(encoding="utf-8")
    names = parse_names(text)
    if not names:
        problems.append("names 段为空或解析失败")
        return CheckReport({}, problems, warnings)

    # ① 类别顺序：这一条是本脚本存在的理由
    expected = {index: cls for index, cls in enumerate(CLASSES)}
    if names != expected:
        problems.append(
            f"类别顺序与 models.CLASSES 不一致：\n"
            f"    数据集 {names}\n"
            f"    代码   {expected}\n"
            f"    顺序错位会让「帽子」被认成别的类，且代码侧完全看不出来"
        )

    # ② 数据集目录
    base = parse_scalar(text, "path")
    if not base:
        warnings.append("未配置 path，跳过数据统计")
    else:
        root = (ROOT / base).resolve()
        if not root.is_dir():
            warnings.append(
                f"数据集目录不存在：{root}（采集完再来跑一次；不影响判定层单测）")
        else:
            for split in ("train", "val"):
                folder = root / "images" / split
                count = len(list(folder.glob("*.jpg"))) + len(list(folder.glob("*.png"))) \
                    if folder.is_dir() else 0
                if count == 0:
                    warnings.append(f"{split} 集为空：{folder}")
                else:
                    labels = root / "labels" / split
                    label_count = len(list(labels.glob("*.txt"))) if labels.is_dir() else 0
                    if label_count != count:
                        warnings.append(
                            f"{split}：图片 {count} 张 / 标注 {label_count} 份，数量不一致")

    return CheckReport(names, problems, warnings)


def check_legacy(root: Path = LEGACY_ROOT) -> list[str]:
    """比对归档里的旧数据集类别定义（只读，不做任何搬移）。"""
    notes: list[str] = []
    if not root.is_dir():
        return [f"归档数据集不存在（跳过）：{root}"]
    for yaml in sorted(root.glob("**/*.yaml"))[:5]:
        text = yaml.read_text(encoding="utf-8", errors="replace")
        names = parse_names(text)
        if not names:
            continue
        notes.append(f"{yaml.relative_to(root)}：{len(names)} 类 → "
                     f"{[names[i] for i in sorted(names)]}")
    if not notes:
        notes.append(f"{root} 下没有可解析的数据集 yaml")
    notes.append("旧集类别与本工程的 6 类不同，直接混用会让 names 与标注错位；"
                 "要用请人工写映射表，不要整包复制")
    return notes


def train_command(names_count: int, *, data: Path = DATASET_YAML,
                  epochs: int = 80) -> str:
    """打印可复制的训练命令。绑定 `imgsz` 与 `conf`，避免手敲错。"""
    return "\n".join([
        f"# 类别数 {names_count}，imgsz={IMGSZ}（必须与判定层一致）",
        "yolo detect train \\",
        f"  data={data} \\",
        "  model=yolov8n.pt \\",
        f"  imgsz={IMGSZ} \\",
        f"  epochs={epochs} \\",
        "  batch=16 \\",
        "  patience=20 \\",
        "  project=runs/vision \\",
        "  name=kitchen-v1",
        "",
        "# 导出给端侧/服务端推理用（ONNX 更省依赖）",
        "yolo export model=runs/vision/kitchen-v1/weights/best.pt format=onnx opset=12",
        "",
        f"# 推理门槛写 {MODEL_CONF}（后处理再按类别分档），权重放到 weights/kitchen-v1.pt",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="视觉数据集校验与训练指令")
    parser.add_argument("--check", action="store_true", help="校验数据集配置（默认行为）")
    parser.add_argument("--legacy", action="store_true", help="比对归档里的旧数据集")
    parser.add_argument("--epochs", type=int, default=80, help="写进训练命令的轮数")
    args = parser.parse_args(argv)

    report = check()
    print("== 类别顺序 ==")
    for index in sorted(report.names):
        mark = "✓" if CLASSES[index:index + 1] == (report.names[index],) else "✗"
        print(f"  {index}: {report.names[index]:10} {mark}")

    if report.problems:
        print("\n== 问题 ==")
        for item in report.problems:
            print(f"  ✗ {item}")
    if report.warnings:
        print("\n== 待办 / 警告 ==")
        for item in report.warnings:
            print(f"  ! {item}")

    if args.legacy:
        print("\n== 归档数据集 ==")
        for line in check_legacy():
            print(f"  · {line}")

    print("\n== 训练命令（本机不执行）==")
    print(train_command(len(report.names) or len(CLASSES), epochs=args.epochs))

    print("\n结论：" + ("配置一致，可进入采集/训练" if report.ok else "类别定义不一致，必须先修"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
