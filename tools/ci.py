# -*- coding: utf-8 -*-
"""本地 CI 入口（A7）：把全部质量关卡串成一条命令。

## 为什么是这个文件而不是只写 GitHub Actions

工程当前不是 git 仓库，Actions yaml 没法在本机验证；而 CI 的价值在
「每次改动后一键验全部」，这与托管平台无关。所以把关卡实现放这里，
yaml 只是包装本文件——**本文件跑绿 = CI 一定绿**（平台差异只剩
Python 版本与依赖安装）。

## 关卡

0. `tools/review_lint.py`（审查守卫 RL001/RL002：宽异常吞掉必须写理由、AI 输出不得有
   approve/decision/amount 键。纯静态、约 1 秒，最先跑；口径见
   `docs/15_代码审查标准与流程.md` §5.1 / §5.4）
1. `ruff check`（语法级 E9/F63/F7，配置在 pyproject.toml。**本机没装 ruff 时打印 SKIP**，
   不算通过也不算失败；GitHub Actions 会 `pip install ruff` 让它真跑）
2. `tools/rule_lint.py`（R5 时钟守卫：api/ 无 state.now/wall 旧名、epoch 不传 now= 参数）
3. `tools/consistency_check.py`（README 规模数字守卫，慢关卡之前先跑）
4. pytest 全量单测（约 3 分钟）
5. `tools/migrate.py check`（51 表迁移校验，SQLite 真跑两遍）
6. `tools/web_check.py`（管理端静态检查 + 构建 + HTTP 冒烟）
7. `tools/miniapp_check.py`（小程序自检，含 AppID / 配置断言）
8. `tools/pr_size.py`（PR 体积提示：>400 行 / >10 文件 / >2 域 时提醒，**恒不阻断**；
   默认含未提交工作区，只看已提交加 `--committed-only`）

用法：
    python tools/ci.py                 # 全部关卡，失败即停
    python tools/ci.py --keep-going    # 失败也继续跑后面的关卡
    python tools/ci.py --cov           # pytest 步骤加覆盖率（需 pytest-cov，
                                       #   缺失时打印提示跳过，不算失败）

退出码：全部通过 0，任一失败 1（可直接挂 git hook / 任务计划）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 关卡清单：(名称, argv, 是否允许跳过)。argv 在 ROOT 下执行。
#: 名称以 `ruff` 开头的那关在装了 ruff 时真跑、没装时明示跳过（不伪造 PASS）。
STAGES: list[tuple[str, list[str], bool]] = [
    ("审查静态守卫", [sys.executable, "tools/review_lint.py"], False),
    ("ruff 语法守卫", [], True),
    ("R5 时钟守卫", [sys.executable, "tools/rule_lint.py"], False),
    ("文档数字守卫", [sys.executable, "tools/consistency_check.py"], False),
    ("pytest 全量单测", [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "tests/"], False),
    ("迁移校验", [sys.executable, "tools/migrate.py", "check"], False),
    ("管理端 web_check", [sys.executable, "tools/web_check.py"], False),
    ("小程序 miniapp_check", [sys.executable, "tools/miniapp_check.py"], False),
    ("PR 体积提示", [sys.executable, "tools/pr_size.py"], True),
]


def _ruff_argv() -> list[str] | None:
    """装了 ruff 才返回 argv；没装返回 None（由调用方打印 SKIP）。

    ruff 是 Rust 二进制，`python -m ruff` 不成立，必须走 PATH 里的可执行文件。
    """
    exe = shutil.which("ruff")
    if not exe:
        return None
    return [exe, "check", "--no-cache", "src", "tests", "tools"]



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
    # (名称, 是否通过, 耗时, 是否跳过)。跳过单独统计，不混进"通过"里凑数。
    results: list[tuple[str, bool, float, bool]] = []
    failed = False

    for i, (name, argv_i, skippable) in enumerate(STAGES, 1):
        if name.startswith("pytest"):
            argv_i = _pytest_argv(args.cov)
        print(f"\n=== [{i}/{len(STAGES)}] {name} ===")

        if name.startswith("ruff"):
            ruff_argv = _ruff_argv()
            if ruff_argv is None:
                print("  SKIP  未安装 ruff（pip install ruff 或 pip install -e '.[dev]'）")
                print("        GitHub Actions 会装上并真跑；本机跳过不算通过")
                results.append((name, True, 0.0, True))
                continue
            argv_i = ruff_argv

        ok, elapsed, tail = _run(name, argv_i)
        results.append((name, ok, elapsed, False))
        print(f"  {'PASS' if ok else 'FAIL'}  {elapsed:.1f}s")
        if not ok:
            print(tail)
            failed = True
            if not args.keep_going:
                break
        elif args.cov and name.startswith("pytest") and "coverage" in tail.lower():
            print(tail)
        elif skippable:
            print(tail)          # 提示级关卡（PR 体积）的报告要看得见

    print("\n" + "=" * 56)
    for name, ok, elapsed, skipped in results:
        if skipped:
            print(f"  ⏭  {name}（未安装 ruff，跳过）")
        else:
            print(f"  {'✅' if ok else '❌'} {name}（{elapsed:.1f}s）")
    total = sum(e for _, _, e, _ in results)
    counted = [r for r in results if not r[3]]
    passed = sum(1 for _, ok, _, skipped in counted if ok)
    note = f"，{len(results) - len(counted)} 关跳过" if len(counted) != len(results) else ""
    print(f"  合计 {passed}/{len(counted)} 关通过{note}，{total:.0f}s")
    print("=" * 56)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
