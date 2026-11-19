# -*- coding: utf-8 -*-
"""工程规则静态守卫（审查报告 §5.3 的 R5 时钟守卫 + 入口唯一性）。

R5：两个时间轴必须用名字区分——
  - AppState 只暴露 `monotonic()`（单调秒，计时/超时）与 `wall_epoch()`（epoch 秒，展示/落库）；
  - 不再有 `state.now()` / `state.wall()` 这种看不出时间域的调用；
  - 禁止把 epoch 传给名为 `now` 的参数（`now=self.wall_epoch()`）。

为什么用 lint 而不是靠注释：11-18 的告警刷屏事故根因就是把单调秒与 epoch
混用，改名 + 机器守卫比写十行注释管用。

**范围**：只扫 `src/kitchen/api/`（AppState 与其调用方）。`process/` 下各业务类
自己的 `now() -> datetime` 是另一套、合法的语义，不在本守卫范围。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "src" / "kitchen" / "api"

#: AppState 旧方法名（含无括号的回调引用）。process/ 类自己的 now 不在此范围。
BAD_METHOD_REF = re.compile(r"\b(?:self|state)\.(now|wall)\b")
#: epoch 传给名为 now 的参数
EPOCH_INTO_NOW = re.compile(r"\bnow\s*=\s*(?:self|state)\.wall_epoch\s*\(")


def lint() -> list[str]:
    issues: list[str] = []
    if not API_DIR.is_dir():
        return [f"找不到 {API_DIR}"]

    # 显式断言两个新方法存在（防止整个方法被误删后 lint 假绿）
    state_src = (API_DIR / "state.py").read_text(encoding="utf-8")
    if not re.search(r"def\s+monotonic\s*\(", state_src):
        issues.append("AppState 缺少 monotonic()（R5）")
    if not re.search(r"def\s+wall_epoch\s*\(", state_src):
        issues.append("AppState 缺少 wall_epoch()（R5）")

    for path in sorted(API_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):          # 注释里解释历史事故，允许出现旧名
                continue
            if BAD_METHOD_REF.search(line):
                issues.append(
                    f"{path.relative_to(ROOT)}:{lineno} 仍用 state.now/wall（R5）: "
                    f"{stripped[:80]}")
            if EPOCH_INTO_NOW.search(line):
                issues.append(
                    f"{path.relative_to(ROOT)}:{lineno} 把 epoch 传给 now= 参数（R5）: "
                    f"{stripped[:80]}")
    return issues


def main() -> int:
    issues = lint()
    if issues:
        print("rule_lint: FAIL")
        for item in issues:
            print("  - " + item)
        return 1
    print("rule_lint: OK（R5：api/ 无 state.now/wall 旧名，epoch 未传入 now= 参数）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
