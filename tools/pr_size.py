# -*- coding: utf-8 -*-
"""PR 体积提示（标准 §8.2）：超过阈值就提醒，**默认不阻断**。

## 为什么不阻断

体积红线是为了"别让审查者在第 500 行之后只看格式"，不是为了卡人。
一旦做成硬门禁，最常见的应对是**把一个大 PR 拆成两个各自不自洽的坏 PR**——
那比大 PR 更糟。所以这里只提示，`--strict` 才返回非零，由人决定要不要真的拦。

## 量的是什么

默认量的是「基线 → 当前工作树」的净差异，包含三部分：

1. 已提交到 HEAD 的改动；
2. 已暂存 / 未暂存的跟踪文件改动；
3. 未跟踪但**未被忽略**的新文件。

早期版本只算第 1 项。本项目的工作习惯是"攒满工作区再提交"，于是出现过
「已提交 23 行 ✅、实际工作区 4249 行」的**假绿灯**。守卫说谎比没有守卫更糟，
所以默认改为全量；只看已提交部分用 `--committed-only`（旧行为）。

二进制文件计入"文件数"、行数记 0；被 `.gitignore` 忽略的文件不计。
行数按换行符计，与 `git diff --numstat` 对文本文件的口径接近。

## 用法

    python tools/pr_size.py                      # 自动找基线，打印报告
    python tools/pr_size.py --base origin/main   # 指定基线
    python tools/pr_size.py --committed-only     # 只看已提交（旧行为）
    python tools/pr_size.py --strict             # 超阈值时退出 1
    python tools/pr_size.py --max-lines 400 --max-files 10 --max-domains 2

退出码：默认恒为 0（提示级）；`--strict` 且超阈值时为 1；无基线时跳过并返回 0。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent

#: 自动找基线的顺序：优先远端主分支，其次本地主分支
BASE_CANDIDATES = ("origin/main", "origin/master", "main", "master")

#: 顶层目录 → 域；src/ 下按二级目录再细分
TOP_DOMAINS = (
    "tests", "docs", "tools", "web", "miniapp", "deploy",
    "migrations", "firmware", "fixtures", "scenarios",
)

#: git 对重命名文件的 numstat 路径形如 `src/{old.py => new.py}` / `a.py => b.py`
_RENAME_BRACE = re.compile(r"\{[^{}]*? => ([^{}]*)\}")


class Size(NamedTuple):
    lines: int
    files: int
    detail: list[tuple[str, int, int]]
    committed_lines: int
    committed_files: int
    untracked_files: int


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode, (proc.stdout or "").strip()


def _has_ref(ref: str) -> bool:
    rc, _ = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return rc == 0


def resolve_base(explicit: str | None) -> str | None:
    """返回可用的基线 ref；一个都没有就返回 None（例如浅克隆或刚 init）。"""
    if explicit:
        return explicit if _has_ref(explicit) else None
    for ref in BASE_CANDIDATES:
        if _has_ref(ref):
            return ref
    return None


def domain_of(path: str) -> str:
    p = path.replace("\\", "/")
    if p.startswith("src/kitchen/"):
        rest = p[len("src/kitchen/"):]
        return rest.split("/")[0] if "/" in rest else "(顶层)"
    for top in TOP_DOMAINS:
        if p.startswith(top + "/"):
            return top
    return "其他"


def _clean_path(path: str) -> str:
    """把 numstat 的重命名写法归一到新路径，避免同一文件算两次。"""
    p = path.replace("\\", "/")
    m = _RENAME_BRACE.search(p)
    if m:
        return p[:m.start()] + m.group(1) + p[m.end():]
    if " => " in p:
        return p.split(" => ")[-1]
    return p


def _numstat(*args: str) -> list[tuple[str, int, int]]:
    """读 `git diff --numstat`；二进制（加删均为 `-`）行数记 0。"""
    rc, out = _git("diff", "--numstat", *args)
    if rc != 0 or not out:
        return []
    rows: list[tuple[str, int, int]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, dele, path = parts[0], parts[1], parts[2]
        if add == "-" or dele == "-":
            a = d = 0
        else:
            a, d = int(add), int(dele)
        rows.append((_clean_path(path), a, d))
    return rows


def _untracked() -> list[tuple[str, int, int]]:
    """未跟踪且未被忽略的文件；按换行符计行数，二进制前 8KB 探测后跳过。"""
    rc, out = _git("ls-files", "--others", "--exclude-standard", "-z")
    if rc != 0 or not out:
        return []
    rows: list[tuple[str, int, int]] = []
    for raw in out.split("\0"):
        if not raw:
            continue
        path = ROOT / raw
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as fh:
                head = fh.read(8192)
                if b"\0" in head:  # 二进制：大原图不逐字节读，避免 33MB 全量扫描
                    n = 0
                else:
                    n = head.count(b"\n")
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        n += chunk.count(b"\n")
        except OSError:
            n = 0  # 读不到仍算一个文件，不能静默漏掉
        rows.append((raw.replace("\\", "/"), n, 0))
    return rows


def collect(base: str, include_working_tree: bool = True) -> Size:
    """统计相对基线的体积；默认含未提交改动与未跟踪新文件。"""
    _, mb = _git("merge-base", base, "HEAD")
    base_point = mb or base
    committed = _numstat(f"{base_point}...HEAD")

    if not include_working_tree:
        return Size(
            sum(a + d for _, a, d in committed), len(committed), committed,
            sum(a + d for _, a, d in committed), len(committed), 0,
        )

    # `git diff <base_point>` 是「基线树 → 当前工作树」的净差异，
    # 一次性覆盖已提交 + 已暂存 + 未暂存，避免分段相加重复计数。
    tracked = _numstat(base_point)
    extra = _untracked()
    detail = tracked + extra
    return Size(
        sum(a + d for _, a, d in detail), len(detail), detail,
        sum(a + d for _, a, d in committed), len(committed), len(extra),
    )


def report(size: Size, base: str, include_working_tree: bool,
           max_lines: int, max_files: int, max_domains: int) -> bool:
    """打印报告，返回是否超阈值。"""
    counts = Counter(domain_of(p) for p, _, _ in size.detail)
    over: list[str] = []
    if size.lines > max_lines:
        over.append(f"行数 {size.lines} > {max_lines}")
    if size.files > max_files:
        over.append(f"文件 {size.files} > {max_files}")
    if len(counts) > max_domains:
        over.append(f"域 {len(counts)} > {max_domains}")

    if include_working_tree:
        print(f"PR 体积：base={base} → 工作树（已提交 {size.committed_lines} 行 / "
              f"{size.committed_files} 文件）")
    else:
        print(f"PR 体积：base={base} → HEAD")
    if not size.detail:
        print("  无差异（或基线不可比）——无需检查")
        return False

    print(f"  变更 {size.lines} 行 / {size.files} 个文件 / {len(counts)} 个域")
    if size.untracked_files:
        print(f"  其中未跟踪新文件 {size.untracked_files} 个"
              "（尚未 add，但 `git add -A` 会收进去）")
    top = " · ".join(f"{k} {v}" for k, v in counts.most_common(6))
    print(f"  域分布：{top}")

    if over:
        print(f"  ⚠️  超阈值：{'；'.join(over)}")
        print("  建议：按「实现 / 测试 / 文档」拆开；确实不可拆（如迁移+模型联动）")
        print("        就在 PR 描述里写清为什么不能拆（标准 §8.2）。")
        return True

    print(f"  ✅ 在阈值内（≤{max_lines} 行 / ≤{max_files} 文件 / ≤{max_domains} 域）")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR 体积提示（默认不阻断）")
    parser.add_argument("--base", help="对比基线，默认自动取 origin/main 等")
    parser.add_argument("--committed-only", action="store_true",
                        help="只看已提交部分（旧行为；默认含工作区）")
    parser.add_argument("--max-lines", type=int, default=400)
    parser.add_argument("--max-files", type=int, default=10)
    parser.add_argument("--max-domains", type=int, default=2)
    parser.add_argument("--strict", action="store_true", help="超阈值时返回 1（默认只提示）")
    args = parser.parse_args(argv)

    base = resolve_base(args.base)
    if base is None:
        if args.base:
            print(f"pr_size: 基线 {args.base} 不存在，跳过")
        else:
            print("pr_size: 找不到可对比的基线（浅克隆 / 无 main），跳过")
        return 0

    size = collect(base, include_working_tree=not args.committed_only)
    over = report(size, base, not args.committed_only,
                  args.max_lines, args.max_files, args.max_domains)
    if over and args.strict:
        return 1
    if over:
        print("  （提示级，不阻断；如需硬拦加 --strict）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
