# -*- coding: utf-8 -*-
"""本地 CI 入口（A7）：把全部质量关卡串成一条命令。

## 为什么是这个文件而不是只写 GitHub Actions

工程当前不是 git 仓库，Actions yaml 没法在本机验证；而 CI 的价值在
「每次改动后一键验全部」，这与托管平台无关。所以把关卡实现放这里，
yaml 只是包装本文件——**本文件跑绿 = CI 一定绿**（平台差异只剩
Python 版本与依赖安装）。

## 关卡

1. pytest 全量单测（约 3 分钟）
2. `tools/migrate.py check`（42 表 / 63 索引迁移校验，SQLite 真跑两遍）
3. `tools/web_check.py`（管理端 29 项静态检查 + 构建 + HTTP 冒烟）
4. `tools/miniapp_check.py`（小程序 46 项自检，含 AppID / 配置断言）

用法：
    python tools/ci.py                 # 全部关卡，失败即停
    python tools/ci.py --keep-going    # 失败也继续跑后面的关卡
    python tools/ci.py --cov           # pytest 步骤加覆盖率（需 pytest-cov，
                                       #   缺失时打印提示跳过，不算失败）

退出码：全部通过 0，任一失败 1（可直接挂 git hook / 任务计划）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 关卡清单：(名称, argv, 是否允许跳过)。argv 在 ROOT 下执行。
STAGES: list[tuple[str, list[str], bool]] = [
    ("pytest 全量单测", [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "tests/"], False),
    ("迁移校验", [sys.executable, "tools/migrate.py", "check"], False),
    ("管理端 web_check", [sys.executable, "tools/web_check.py"], False),
    ("小程序 miniapp_check", [sys.executable, "tools/miniapp_check.py"], False),
]


def _run(name: str, argv: list[str]) -> tuple[bool, float, str]:
    """跑一道关卡，返回 (是否通过, 耗时秒, 输出尾部)。"""
    started = time.perf_counter()
    proc = subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    elapsed = time.perf_counter() - started
    tail = "\n".join((proc.stdout or "").splitlines()[-5:])
    if proc.stderr:
        tail += "\n" + "\n".join(proc.stderr.splitlines()[-3:])
    return proc.returncode == 0, elapsed, tail


def _pytest_argv(with_cov: bool) -> list[str]:
    argv = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "tests/"]
    if not with_cov:
        return argv
    try:
        import pytest_cov  # noqa: F401
    except ImportError:
        print("  [提示] 未安装 pytest-cov（pip install pytest-cov），"
              "本次跳过覆盖率；CI 平台会自动带上。")
        return argv
    return argv + [
        "--cov=kitchen", "--cov-report=term-missing:skip-covered",
        "--cov-report=xml",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 CI：串跑全部质量关卡")
    parser.add_argument("--keep-going", action="store_true",
                        help="某关失败也继续跑后面的关卡")
    parser.add_argument("--cov", action="store_true",
                        help="pytest 加覆盖率统计（依赖 pytest-cov，缺失自动跳过）")
    args = parser.parse_args(argv)

    print(f"本地 CI（A7）——工程根：{ROOT}")
    results: list[tuple[str, bool, float]] = []
    failed = False

    for i, (name, argv_i, _skippable) in enumerate(STAGES, 1):
        if name.startswith("pytest"):
            argv_i = _pytest_argv(args.cov)
        print(f"\n=== [{i}/{len(STAGES)}] {name} ===")
        ok, elapsed, tail = _run(name, argv_i)
        results.append((name, ok, elapsed))
        print(f"  {'PASS' if ok else 'FAIL'}  {elapsed:.1f}s")
        if not ok:
            print(tail)
            failed = True
            if not args.keep_going:
                break
        elif args.cov and name.startswith("pytest") and "coverage" in tail.lower():
            print(tail)

    print("\n" + "=" * 56)
    for name, ok, elapsed in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}（{elapsed:.1f}s）")
    total = sum(e for _, _, e in results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"  合计 {passed}/{len(results)} 关通过，{total:.0f}s")
    print("=" * 56)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
