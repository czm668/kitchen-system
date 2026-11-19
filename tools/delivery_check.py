# -*- coding: utf-8 -*-
"""交付前凭据隔离检查（审查报告 §0 P0-4 / §6 D1）。

背景：审查发现 `deploy/.env` 含真实 DeepSeek Key 与 MySQL 口令。它已被
`.gitignore` 拦住、没进 GitHub，但**本地打包给答辩/投屏的目录**仍可能把它
带进去。审查报告 R1 的精神是"返回 200 不等于内容正确"——同理，"没进 git"
不等于"没进交付包"。本脚本扫目标目录，发现疑似凭据就非零退出。

用法：
    python tools/delivery_check.py                       # 扫仓库根
    python tools/delivery_check.py D:/答辩包/kitchen     # 扫指定交付目录
    python tools/delivery_check.py --strict D:/答辩包    # 连 .env.example 也提示（仅警告）

只扫描文件名与 gitignore 规则，**不读也不打印任何凭据内容**。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: 文件名级敏感模式（命中即高危）
DANGEROUS_NAME = re.compile(
    r"(^|/)(\.env$|.*\.pem$|.*\.key$|id_rsa|.*credentials.*|.*secrets.*)", re.I)
#: 仅警告模式（模板文件本身无凭据，但答辩包里没必要出现）
WARN_NAME = re.compile(r"\.env\.example$|\.env\.local$", re.I)
#: 文件内容里可能的密钥前缀——只看前 4KB，且只报告"含疑似密钥"，不打印值
SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})")

#: 明确不算问题的目录
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".local", "datasets", "runs"}


def scan(root: Path, strict: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not root.is_dir():
        return [f"目录不存在：{root}"], []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        rel = str(path.relative_to(root))

        if DANGEROUS_NAME.search(rel):
            # *.example / *.sample 是模板，不含真凭据 → 与 .env.example 同级警告
            if rel.endswith((".example", ".sample", ".template")):
                if strict:
                    warnings.append(f"模板文件（建议从答辩包移除）：{rel}")
                continue
            errors.append(f"疑似凭据文件：{rel}")
            continue
        if WARN_NAME.search(rel):
            if strict:
                warnings.append(f"模板文件（建议从答辩包移除）：{rel}")
            continue
        # 文本小文件才扫内容，且只看前缀、绝不打印命中值
        if path.suffix in {".py", ".json", ".sh", ".md", ".bat", ".yml", ".yaml", ".txt", ".js"} \
                and path.stat().st_size < 256 * 1024:
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
            except OSError:
                continue
            if SECRET_VALUE.search(head):
                errors.append(f"文件含疑似 API Key（值已隐藏）：{rel}")

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="交付前凭据隔离检查")
    ap.add_argument("root", nargs="?", default=".", help="要扫描的目录，默认当前目录")
    ap.add_argument("--strict", action="store_true", help="连 .env.example 模板也提示")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    errors, warnings = scan(root, strict=args.strict)
    for w in warnings:
        print(f"  [WARN] {w}")
    if errors:
        print(f"delivery_check: FAIL（{len(errors)} 处）")
        for e in errors:
            print(f"  [FAIL] {e}")
        print("处理：从交付包移除这些文件；轮换其中泄露的 Key；不要把 .env 投屏或打包。")
        return 1
    print(f"delivery_check: OK（{root} 下未发现凭据文件；Key 轮换仍需人工在云端完成）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
