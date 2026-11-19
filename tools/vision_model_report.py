# -*- coding: utf-8 -*-
"""视觉模型质量报告（审查报告 §5.1 缺陷#15 的可辩护出口）。

## 为什么需要它

模型指标以前只活在 `var/train.log`（11MB、满屏进度条）里，答辩要引用一个 AP
得翻日志；更要命的是"6 类模型"与"只有 4 类有验证集"两个说法散落在 README、
矩阵、论文图表三处，口径一漂移就会被问穿帮（审查报告 §1.1 高严重度项）。

现在指标只有一份机器可读源：`docs/vision_model_metrics.json`，数值逐字来自
`var/train.log` 的 `best.pt` 验证结果。本工具：
  1. `check()` 校验 JSON 自洽（类别顺序 = models.CLASSES、ap_class_index 与
     validated 一致、overall 的 P/R/mAP 在各类别范围内、verified 类数对得上）；
  2. `report()` 生成人读 Markdown（含 uniform/phone 缺口的可辩护说明）；
  3. CLI `--check` 供 CI/单测调用，默认打印报告。

## 它不做什么

不重新训练、不 import ultralytics、不碰权重。它是"把已发生的事实讲清楚"的
工具，不是"证明模型更好"的工具——后者需要真实验证集，属于数据缺口（见
JSON 的 boundary.improvement_path）。

用法：
    python tools/vision_model_report.py            # 打印 Markdown 报告
    python tools/vision_model_report.py --check    # 只校验自洽，失败退出 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# tools/ 在仓库根下，src/ 也在根下：把根加进 sys.path 才能 import kitchen
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from kitchen.vision.models import CLASSES  # noqa: E402

METRICS_PATH = _ROOT / "docs" / "vision_model_metrics.json"

__all__ = ["load_metrics", "check", "report", "METRICS_PATH"]


def load_metrics(path: Path | str = METRICS_PATH) -> dict[str, Any]:
    """读 JSON；格式错误直接抛，不降级（指标文件必须可信）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check(data: dict[str, Any], *, classes: tuple[str, ...] = CLASSES) -> list[str]:
    """返回问题列表；空列表表示自洽。纯函数，便于单测注入假数据。"""
    issues: list[str] = []
    per = data.get("per_class")
    if not isinstance(per, list) or len(per) != len(classes):
        return [f"per_class 数量 {0 if not per else len(per)} != 类别数 {len(classes)}"]

    # ① 类别顺序必须与模型 class id 一致——顺序错了帽子会被认成手机且看不出来
    names = [row["name"] for row in per]
    if tuple(names) != classes:
        issues.append(f"类别顺序不符：JSON={names}，models.CLASSES={list(classes)}")
    for idx, row in enumerate(per):
        if row.get("id") != idx:
            issues.append(f"per_class[{idx}].id={row.get('id')}，应等于下标 {idx}")

    # ② ap_class_index 必须恰好等于 validated 的类别 id 集合
    validated_ids = [row["id"] for row in per if row.get("validated")]
    if sorted(data.get("ap_class_index", [])) != sorted(validated_ids):
        issues.append(
            f"ap_class_index={data.get('ap_class_index')} 与 validated 类 {validated_ids} 不一致")

    # ③ nt_per_class / nt_per_image 与 instances / val_images 对齐
    for i, row in enumerate(per):
        nt = data.get("nt_per_class", [])
        ni = data.get("nt_per_image", [])
        if i < len(nt) and nt[i] != row.get("instances"):
            issues.append(f"{row['name']}: nt_per_class[{i}]={nt[i]} != instances={row.get('instances')}")
        if i < len(ni) and ni[i] != row.get("val_images"):
            issues.append(f"{row['name']}: nt_per_image[{i}]={ni[i]} != val_images={row.get('val_images')}")

    # ④ 无验证样本的类不得编造 AP；有样本的类指标必须在 0~1 且非空
    for row in per:
        name, ok = row["name"], row.get("validated")
        metrics = ("precision", "recall", "map50", "map50_95")
        if not ok:
            if row.get("instances", 0) != 0:
                issues.append(f"{name}: validated=false 但 instances={row['instances']} != 0")
            if any(row.get(m) is not None for m in metrics):
                issues.append(f"{name}: 无验证样本却填了 AP 指标（不许编造）")
        else:
            if row.get("instances", 0) <= 0:
                issues.append(f"{name}: validated=true 但 instances<=0")
            for m in metrics:
                v = row.get(m)
                if not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
                    issues.append(f"{name}: {m}={v!r} 不是 0~1 的数")

    # ⑤ boundary 声明与事实一致
    b = data.get("boundary", {})
    if b.get("validated_classes") != len(validated_ids):
        issues.append(f"boundary.validated_classes={b.get('validated_classes')} != {len(validated_ids)}")
    unvalidated = [row["name"] for row in per if not row.get("validated")]
    if sorted(b.get("unvalidated_classes", [])) != sorted(unvalidated):
        issues.append(f"boundary.unvalidated_classes={b.get('unvalidated_classes')} != {unvalidated}")

    # ⑥ 强数学约束：Ultralytics 的 overall mAP = validated 各类 mAP 的均值。
    #    日志里 4 类 mAP50-95 均值 0.6426925 = fitness 0.64269，mAP50 均值也吻合。
    #    这条比"落在范围内"强得多：伪造任何一类的 AP 都会破坏等式，必然被抓。
    ov = data.get("overall", {})
    for key in ("map50", "map50_95"):
        vals = [row[key] for row in per
                if row.get("validated") and isinstance(row.get(key), (int, float))]
        overall = ov.get(key)
        if vals and isinstance(overall, (int, float)):
            mean = sum(vals) / len(vals)
            # 各类 AP 在 JSON 里保留 3 位小数，均值与 overall 允许 5e-4 的舍入误差
            if abs(mean - overall) > 5e-4:
                issues.append(
                    f"overall {key}={overall} != validated 各类均值 {mean:.5f}"
                    f"（{len(vals)} 类）——AP 可能被篡改")
    return issues


def report(data: dict[str, Any]) -> str:
    """生成人读 Markdown 报告。"""
    tr, ov, b = data["training"], data["overall"], data["boundary"]
    lines: list[str] = [
        "# 视觉模型质量报告",
        "",
        "> 本文件由 `tools/vision_model_report.py` 从 `docs/vision_model_metrics.json` 生成，",
        "> 数据源是 `var/train.log` 中 `best.pt` 的最终验证结果。**不要手改本报告，改 JSON 后重新生成**。",
        "",
        "## 一、训练配置（可复现）",
        "",
        f"- 基座：{tr['base_model']}，{tr['framework']}，{tr['python']}，{tr['torch']}",
        f"- 硬件：{tr['gpu']}；种子 {tr['seed']}；`imgsz={tr['imgsz']}`、`iou={tr['iou']}`、batch {tr['batch']}",
        f"- 数据：train {tr['train_images']} 张 / val {tr['val_images']} 张；{tr['epochs']} epochs，耗时 {tr['duration_hours']} 小时",
        f"- 模型：fused {tr['fused_layers']} 层、{tr['parameters']:,} 参数、{tr['gflops']} GFLOPs；权重 `{data['provenance']['weight_file']}`",
        "",
        "## 二、整体指标（best.pt）",
        "",
        "| P | R | mAP50 | mAP50-95 |",
        "|---|---|---|---|",
        f"| {ov['precision']:.3f} | {ov['recall']:.3f} | {ov['map50']:.3f} | {ov['map50_95']:.3f} |",
        "",
        "## 三、各类别 AP（class id 顺序 = 模型输出顺序）",
        "",
        "| id | 类别 | val 图 | 实例 | P | R | mAP50 | mAP50-95 | 状态 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in data["per_class"]:
        if row["validated"]:
            cells = (f"{row['precision']:.3f}", f"{row['recall']:.3f}",
                     f"{row['map50']:.3f}", f"{row['map50_95']:.3f}")
            status = "✅ 有验证集"
        else:
            cells = ("—", "—", "—", "—")
            status = "⚠️ 无独立验证样本"
        lines.append(
            f"| {row['id']} | {row['name']} | {row['val_images']} | {row['instances']} | "
            + " | ".join(cells) + f" | {status} |"
        )
    lines += [
        "",
        f"`ap_class_index = {data['ap_class_index']}`——只有 {b['validated_classes']} 类进了 AP 计算。",
        "",
        "## 四、已知边界（答辩必须主动说明）",
        "",
        f"- **{', '.join(b['unvalidated_classes'])} 两类无独立验证样本**：{b['reason']}",
        f"- 口径：{b['presentation_rule']}",
        "- 真机/半实物证据仍为 0 条；本报告只证明'训练阶段在验证集上的指标'，不等于厨房现场实测。",
        "",
        "## 五、改进路径（数据缺口，不是代码缺陷）",
        "",
    ]
    lines += [f"{i}. {step}" for i, step in enumerate(b["improvement_path"], 1)]
    lines += [
        "",
        "## 六、速度参考",
        "",
        f"- best.pt 在 {tr['gpu']}：preprocess {data['speed_ms']['preprocess']}ms、"
        f"inference {data['speed_ms']['inference']}ms、postprocess {data['speed_ms']['postprocess']}ms/图。",
        f"- {data['speed_ms']['note']}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="视觉模型质量报告（单一数据源 → 人读报告）")
    ap.add_argument("--check", action="store_true", help="只校验 JSON 自洽，失败退出 1")
    ap.add_argument("--json", type=Path, default=METRICS_PATH, help="指标 JSON 路径")
    ap.add_argument("-o", "--output", type=Path, help="把 Markdown 报告写到文件")
    args = ap.parse_args(argv)

    data = load_metrics(args.json)
    issues = check(data)
    if issues:
        print("指标自洽校验失败：", file=sys.stderr)
        for line in issues:
            print(f"  - {line}", file=sys.stderr)
        return 1
    if args.check:
        print(f"OK：指标自洽（{len(data['per_class'])} 类，"
              f"{data['boundary']['validated_classes']} 类有验证集）")
        return 0

    md = report(data)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(md, encoding="utf-8")
        print(f"报告已写入 {args.output}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
