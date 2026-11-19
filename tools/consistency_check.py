# -*- coding: utf-8 -*-
"""数字守卫：docs/requirements.json 是唯一事实源，README / 矩阵不得手抄。

背景（A27 建立，A28 升级为单一数据源）：2026-11-19 审查发现 README 把 65 条
主路由写成 57、把 19 个迁移写成 014。根因是"数字靠人抄"。本工具把口径收敛到
`docs/requirements.json`，做两层校验：

**第一层：数据源 vs 代码事实**（自动发现漂移，不信任 JSON 里的手填数字）
- 主路由数 = `routes.py` 的 `self.add(` 行数
- 扩展路由数 = 各 `*_api.py` 的 `router.add(` 行数（guest/finance/compliance/ai）
- 迁移最大编号 = `migrations/*.sql` 的最大前缀
- pytest 用例数 = `--collect-only` 的收集数
- AI 场景 = `src/kitchen/ai/specs.py::SCENARIOS` 的键 + 既有四场景

**第二层：数据源 vs 文档宣称**（README / 矩阵都只允许按数据源的口径写）
- "N 条主路由 / N 条 JSON 路由" / "N 个 HTTP 接口" / "扩展 N 条"
- "数据库迁移 001–NNN"
- "共 N 个 pytest 用例" / "矩阵规模表 N"
- "七/7 个 AI 场景" / "N 个 AI 场景"

不检查"取用自旧工程 001–013"这类历史说明（口径不同）。

用法：
    python tools/consistency_check.py        # 只检查，不改文件
    python tools/consistency_check.py --fix  # 把 README / 矩阵数字改对（仍需人工看 diff）
退出码：0 一致 / 1 漂移。挂进 tools/ci.py 第一关。零三方依赖（纯标准库 json）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "requirements.json"


def _load_source() -> dict[str, Any]:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def _grep_count(pattern: str, *paths: str) -> int:
    """用 git grep 计数（与 CI 口径一致），失败回退到逐文件文本扫描。"""
    out = subprocess.run(
        ["git", "grep", "-c", pattern, "--", *paths],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout.strip()
    if out:
        return sum(int(line.rsplit(":", 1)[-1]) for line in out.splitlines()
                   if line.rsplit(":", 1)[-1].isdigit())
    total = 0
    for rel in paths:
        p = ROOT / rel
        if p.is_file():
            total += p.read_text(encoding="utf-8").count(pattern)
    return total


def _migration_max() -> int:
    nums = []
    for p in (ROOT / "migrations").glob("*.sql"):
        head = p.name.split("_", 1)[0]
        if head.isdigit():
            nums.append(int(head))
    return max(nums, default=0)


def _pytest_count() -> int:
    try:
        out = subprocess.run(
            ["python", "-m", "pytest", "-o", "addopts=", "--collect-only", "-q", "tests/"],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
        ).stdout
        m = re.search(r"(\d+)\s+tests?\s+collected", out)
        return int(m.group(1)) if m else 0
    except (OSError, subprocess.TimeoutExpired):
        return 0


def _ai_scenario_count() -> int:
    """从 specs.py 静态提取 SCENARIOS 的键数 + 既有四场景，不 import（避免副作用）。"""
    text = (ROOT / "src" / "kitchen" / "ai" / "specs.py").read_text(encoding="utf-8")
    block = re.search(r"SCENARIOS\s*:\s*dict\[str,\s*AdvSpec\]\s*=\s*\{(.*?)\n\}",
                      text, re.DOTALL)
    if not block:
        return 0
    # 数 SCENARIOS 字典顶层的所有键（`'key': AdvSpec(`），不再硬编码场景名——
    # 否则每加一个场景守卫就漏一个，得手工同步两处（R4 数字单一源的本意）。
    new = len(re.findall(r'^\s+"(\w+)":\s*AdvSpec\(', block.group(1), re.MULTILINE))
    service_text = (ROOT / "src" / "kitchen" / "ai" / "service.py").read_text(encoding="utf-8")
    legacy_block = re.search(r"_LEGACY_SCENARIOS\s*=\s*\((.*?)\)", service_text, re.DOTALL)
    legacy = (len(re.findall(r'"\w+"', legacy_block.group(1)))
              if legacy_block else 0)
    return new + legacy


def code_facts() -> dict[str, int]:
    """从代码/磁盘自动发现的事实。与数据源比对用。"""
    breakdown = {
        name: _grep_count("router.add(", f"src/kitchen/api/{name}.py")
        for name in ("guest_api", "finance_api", "compliance_api", "ai_api")
    }
    main = _grep_count("self.add(", "src/kitchen/api/routes.py")
    ext = sum(breakdown.values())
    return {
        "main_routes": main,
        "extension_routes": ext,
        "total_endpoints": main + ext,
        "migrations": _migration_max(),
        "pytest_cases": _pytest_count(),
        "ai_scenarios": _ai_scenario_count(),
        "_breakdown": breakdown,
    }


def check_source_vs_code(src: dict[str, Any]) -> list[str]:
    """第一层：数据源里的手填数字若与代码事实不符，就是漂移。"""
    facts = code_facts()
    issues: list[str] = []
    scale = src["scale"]
    for key in ("main_routes", "extension_routes", "total_endpoints",
                "migrations", "pytest_cases"):
        if key in scale and scale[key] != facts[key]:
            issues.append(f"数据源 {key}={scale[key]}，代码实际 {facts[key]}")
    # 分项加总应等于扩展总数
    for name, actual in facts["_breakdown"].items():
        declared = src.get("extension_route_breakdown", {}).get(name)
        if declared is not None and declared != actual:
            issues.append(f"数据源扩展路由 {name}={declared}，代码实际 {actual}")
    ai = src.get("ai", {})
    if "total_scenarios" in ai and ai["total_scenarios"] != facts["ai_scenarios"]:
        issues.append(f"数据源 AI 场景={ai['total_scenarios']}，代码实际 {facts['ai_scenarios']}")
    return issues


def check_docs_vs_source(src: dict[str, Any], readme: str, matrix: str) -> list[str]:
    """第二层：README / 功能证据矩阵里的数字宣称与数据源比对。"""
    scale = src["scale"]
    ai_total = src.get("ai", {}).get("total_scenarios")
    issues: list[str] = []
    for token, actual in (("JSON 路由", scale["main_routes"]),
                          ("主路由", scale["main_routes"])):
        for m in re.finditer(rf"(\d+)\s*条\s*{token}", readme):
            if int(m.group(1)) != actual:
                issues.append(f"README {token}：写 {m.group(1)}，应为 {actual}")
    for pat in (r"HTTP API：(\d+)\s*个接口",
                r"(\d+)\s*个\s*HTTP\s*接口",
                r"合计\s*(\d+)\s*个\s*HTTP\s*接口"):
        for m in re.finditer(pat, readme):
            if int(m.group(1)) != scale["total_endpoints"]:
                issues.append(f"README 总接口：写 {m.group(1)}，"
                              f"应为 {scale['total_endpoints']}")
    for pat in (r"主路由\s*(\d+)\s*\+\s*扩展\s*(\d+)",
                r"(\d+)\s*条主路由\s*\+\s*(\d+)\s*条扩展路由",
                r"(\d+)\s*条主路由（扩展\s*(\d+)\s*条在"):
        for m in re.finditer(pat, readme):
            if int(m.group(1)) != scale["main_routes"]:
                issues.append(f"README 主路由：写 {m.group(1)}，"
                              f"应为 {scale['main_routes']}")
            if int(m.group(2)) != scale["extension_routes"]:
                issues.append(f"README 扩展路由：写 {m.group(2)}，"
                              f"应为 {scale['extension_routes']}")
    for m in re.finditer(r"数据库迁移\s*001[–-](\d+)", readme):
        if int(m.group(1)) != scale["migrations"]:
            issues.append(f"README 迁移编号：写 001–{m.group(1)}，"
                          f"应为 001–{scale['migrations']:03d}")
    # pytest 数字有五种写法（审查报告 §2.1：11+ 处漂移就是这么来的）：
    #   badge、规模表、快速开始、目录树、跑单测段。全部纳入守卫。
    pytest_patterns = (
        r"(tests-)(\d+)(-brightgreen)",              # badge
        r"(\| 测试 \| )(\d+)( 项 pytest)",           # 规模表
        r"(单测（)(\d+)( 项，纯标准库)",              # 快速开始
        r"(tests/\s+)(\d+)( 条单测)",                # 目录树
        r"(跑全部单测（)(\d+)( 条)",                  # 跑单测段
        r"(共\s*)(\d+)(\s*个\s*pytest\s*用例)",       # 既有口径
    )
    for pat in pytest_patterns:
        for m in re.finditer(pat, readme):
            if int(m.group(2)) != scale["pytest_cases"]:
                issues.append(f"README pytest 数字：写 {m.group(2)}，"
                              f"应为 {scale['pytest_cases']}（{m.group(0)[:44]}）")

    matrix_rows = (
        (r"\|\s*主路由 / 扩展路由 / 总接口\s*\|\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)",
         ("矩阵主路由", "矩阵扩展路由", "矩阵总接口"),
         (scale["main_routes"], scale["extension_routes"], scale["total_endpoints"])),
        (r"\|\s*迁移 / 表\s*\|\s*(\d+)\s*/\s*(\d+)",
         ("矩阵迁移", "矩阵表"),
         (scale["migrations"], scale["tables"])),
        (r"\|\s*AI 场景[^|]*\|\s*(\d+)", ("矩阵 AI 场景",),
         (ai_total,)),
        (r"\|\s*pytest 用例\s*\|\s*(\d+)", ("矩阵 pytest 用例",),
         (scale["pytest_cases"],)),
    )
    for pat, labels, expected in matrix_rows:
        m = re.search(pat, matrix)
        if not m:
            continue
        for idx, (label, exp) in enumerate(zip(labels, expected), start=1):
            if exp is not None and int(m.group(idx)) != exp:
                issues.append(f"{label}：写 {m.group(idx)}，应为 {exp}")
    return issues


def apply_fix(readme: str, src: dict[str, Any]) -> str:
    scale = src["scale"]
    main = scale["main_routes"]
    ext = scale["extension_routes"]
    total = scale["total_endpoints"]
    text = re.sub(r"(\d+)(条\s*(?:JSON\s*)?主路由)", rf"{main}\2", readme)
    text = re.sub(r"(HTTP API：)\d+(\s*个接口)", rf"\g<1>{total}\2", text)
    text = re.sub(r"\b\d+(\s*个\s*HTTP\s*接口)", rf"{total}\1", text)
    text = re.sub(r"(合计\s*)\d+(\s*个\s*HTTP\s*接口)", rf"\g<1>{total}\2", text)
    text = re.sub(r"(主路由\s*)\d+(\s*\+\s*扩展\s*)\d+",
                  rf"\g<1>{main}\g<2>{ext}", text)
    text = re.sub(r"(\d+)(条主路由\s*\+\s*)\d+(条扩展路由)",
                  rf"{main}\g<2>{ext}\3", text)
    text = re.sub(r"(\d+\s*条主路由（扩展\s*)\d+(\s*条在)",
                  rf"{main} 条主路由（扩展 {ext}\2", text)
    text = re.sub(r"(数据库迁移\s*001[–-])\d+",
                  rf"\g<1>{scale['migrations']:03d}", text)
    text = re.sub(r"(共\s*)\d+(\s*个\s*pytest\s*用例)",
                  rf"\g<1>{scale['pytest_cases']}\2", text)
    for pat in (
        r"(tests-)\d+(-brightgreen)",
        r"(\| 测试 \| )\d+( 项 pytest)",
        r"(单测（)\d+( 项，纯标准库)",
        r"(tests/\s+)\d+( 条单测)",
        r"(跑全部单测（)\d+( 条)",
    ):
        text = re.sub(pat, rf"\g<1>{scale['pytest_cases']}\g<2>", text)
    return text


def apply_fix_matrix(matrix: str, src: dict[str, Any]) -> str:
    """按数据源修正功能证据矩阵的规模表；只改数字，不改措辞。"""
    scale = src["scale"]
    main = scale["main_routes"]
    ext = scale["extension_routes"]
    total = scale["total_endpoints"]
    text = re.sub(
        r"(\|\s*主路由 / 扩展路由 / 总接口\s*\|\s*)\d+\s*/\s*\d+\s*/\s*\d+",
        rf"\g<1>{main} / {ext} / {total}", matrix)
    text = re.sub(r"(\|\s*迁移 / 表\s*\|\s*)\d+\s*/\s*\d+",
                  rf"\g<1>{scale['migrations']} / {scale['tables']}", text)
    ai_total = src.get("ai", {}).get("total_scenarios")
    if ai_total is not None:
        text = re.sub(r"(\|\s*AI 场景[^|]*\|\s*)\d+", rf"\g<1>{ai_total}", text)
    text = re.sub(r"(\|\s*pytest 用例\s*\|\s*)\d+",
                  rf"\g<1>{scale['pytest_cases']}", text)
    return text


def check(fix: bool = False) -> int:
    src = _load_source()
    readme_path = ROOT / "README.md"
    matrix_path = ROOT / "docs" / "功能证据矩阵.md"
    readme = readme_path.read_text(encoding="utf-8")
    matrix = matrix_path.read_text(encoding="utf-8")

    source_issues = check_source_vs_code(src)
    doc_issues = check_docs_vs_source(src, readme, matrix)

    if not source_issues and not doc_issues:
        s = src["scale"]
        print(f"consistency: OK (主路由={s['main_routes']}, 扩展={s['extension_routes']}, "
              f"总接口={s['total_endpoints']}, 迁移={s['migrations']:03d}, "
              f"pytest={s['pytest_cases']}, AI场景={src['ai']['total_scenarios']})")
        return 0

    if source_issues:
        print("数据源与代码漂移（先改 docs/requirements.json，再跑 --fix 改 README / 矩阵）：\n"
              + "\n".join("  - " + i for i in source_issues))
    if fix and not source_issues:
        readme_path.write_text(apply_fix(readme, src), encoding="utf-8")
        matrix_path.write_text(apply_fix_matrix(matrix, src), encoding="utf-8")
        print(f"已按数据源修正 README / 矩阵的 {len(doc_issues)} 处数字，请人工检查 git diff")
        return 0
    if doc_issues:
        print("README / 矩阵与数据源漂移：\n" + "\n".join("  - " + i for i in doc_issues))
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="数字守卫（docs/requirements.json 单一数据源）")
    parser.add_argument("--fix", action="store_true", help="按数据源改 README，需人工复核")
    args = parser.parse_args()
    raise SystemExit(check(args.fix))
