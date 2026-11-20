# -*- coding: utf-8 -*-
"""审查静态守卫（RL001 / RL002）：把《代码审查标准与流程》里能机器判的条款钉死。

## 为什么是这两条

审查报告 §5.2 把"静默成功"列为第一大根因：health 200 但页面 404、启动成功但
历史订单被覆盖、备份"成功"但文件没落盘。形态各异，共同点是**失败被 `pass` 吃掉**。
人审很难逐行看出"这个 pass 吞了什么"，但机器能——只要规则定得足够窄。

RL002 是 AI 六护栏里唯一能静态断言的半条：AI 只出建议，不许有决定权。
如果哪天 `ai/` 的返回结构里冒出 `approve` / `decision` / `amount` 键，
不等答辩被问，CI 就该红。

## 为什么不用通用 lint（ruff/flake8）

`BLE001` 只要求"有 noqa"，不要求写理由——于是 `# noqa: BLE001` 成了消音器，
半年后没人知道当时为什么吞。本守卫要求**注释里说清为什么可以吞**，
这是通用 linter 表达不了的工程约定（口径见标准 §5.1 / §6.5）。

## 规则

| 编号 | 规则 | 级别 |
|---|---|---|
| RL001 | `except Exception/BaseException/except:` 块体只有 `pass` / `continue` / `return None`，且**没有任何说明理由的注释** | 阻断 |
| RL002 | `src/kitchen/ai/` 内出现 `"approve"` / `"decision"` / `"amount"` 作为字典键（AI 有了决定权） | 阻断 |

**合规写法**（理由写在 except 行、块内、或上两行均可）：

    except Exception:  # noqa: BLE001 - 供应商已删，快照对比跳过
        pass

    except KeyError:
        # 首次物化时这个键本来就不存在，跳过是预期行为
        continue

**不合规**（会被拦）：

    except Exception:
        pass                    # 裸吞：出问题查不出来

    except Exception:  # noqa: BLE001
        pass                    # noqa 不是理由，只是把 linter 静音

## 用法

    python tools/review_lint.py              # 全量扫 src/，有问题退出 1
    python tools/review_lint.py --list       # 只列规则，不扫描

退出码：0 通过，1 有违规。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "kitchen"
AI_DIR = SRC / "ai"

#: 捕获 except 行；group(2) 是异常表达式，用来判断宽窄
EXCEPT = re.compile(r"^(\s*)except\b([^:]*):\s*(#.*)?$")
#: 单行写法：except Exception: pass
EXCEPT_INLINE = re.compile(r"^(\s*)except\b([^:]*):\s*(pass|continue|return None)\s*(#.*)?$")
#: 块体只有这些语句 = 吞掉异常
SILENT_BODY = re.compile(r"^\s*(pass|continue|return None)\s*(#.*)?$")
#: AI 护栏：这些键出现在 ai/ 的字典里，说明 AI 越权给了结论
AI_FORBIDDEN_KEY = re.compile(r"""["'](approve|decision|amount)["']\s*:""")

#: 注释里的"标记"本身不算理由（它们只是给别的工具看的）
_MARKERS = re.compile(
    r"noqa:\s*[A-Z0-9,\s]+|type:\s*ignore(\[[^\]]*\])?|pragma:\s*no\s*cover",
    re.IGNORECASE,
)


def _has_reason(*texts: str) -> bool:
    """注释里是否说了人话（不只是 noqa 标记）。

    判据刻意宽松但不可绕过：去掉标记后仍有中文，或仍有 8 个以上字母/数字。
    中文项目里"有理由"基本等价于"有中文短句"，这条规则逼着作者写清楚，
    而不是复制一行 `# noqa: BLE001`。
    """
    for raw in texts:
        if not raw:
            continue
        body = _MARKERS.sub("", raw).lstrip("# ").strip()
        if any("\u4e00" <= ch <= "\u9fff" for ch in body):
            return True
        if len(re.sub(r"[^A-Za-z0-9]", "", body)) >= 8:
            return True
    return False


def _is_broad(expr: str) -> bool:
    """只在"宽异常"上要求写理由。

    `except (TypeError, ValueError)` 这类**收窄**写法是标准推荐的修法（§6.5），
    它已经把异常域限定住了，不该再要求解释——否则守卫会变成噪音，最后被绕过。
    """
    bare = expr.strip()
    if not bare:                      # bare except:
        return True
    if bare.startswith("("):          # 元组：含 Exception 才算宽
        return bool(re.search(r"\b(?:Base)?Exception\b", bare))
    head = bare.split()[0]            # 去掉 "as exc"
    return head in ("Exception", "BaseException")


def _comments_near(lines: list[str], idx: int, indent: int,
                   body: list[tuple[int, str]]) -> list[str]:
    """取所有可能解释"为什么可以吞"的注释位置。"""
    found: list[str] = []
    line = lines[idx]
    if "#" in line:
        found.append(line.split("#", 1)[1])
    # 块体内部注释（比 except 缩进更深的注释行）
    for nxt in lines[idx + 1:]:
        if not nxt.strip():
            continue
        if len(nxt) - len(nxt.lstrip()) <= indent:
            break
        if nxt.lstrip().startswith("#"):
            found.append(nxt.lstrip()[1:])
    # 块体语句的行尾注释：continue  # 坏值跳过
    for _, stmt in body:
        if "#" in stmt:
            found.append(stmt.split("#", 1)[1])
    # 上方两行（常见写法：在 try 前解释为什么会抛）
    for prev in reversed(lines[max(0, idx - 2):idx]):
        if prev.strip().startswith("#"):
            found.append(prev.strip()[1:])
    return found


def lint_swallow(path: Path) -> list[str]:
    """RL001：静默吞异常且没写理由。"""
    issues: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    rel = path.relative_to(ROOT)

    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue

        m = EXCEPT_INLINE.match(line)
        if m:
            indent, expr = len(m.group(1)), m.group(2)
            stmt, note = m.group(3), m.group(4) or ""
            reasons = [note, *_comments_near(lines, idx, indent, [(idx + 2, stmt)])]
            if _is_broad(expr) and not _has_reason(*reasons):
                issues.append(
                    f"{rel}:{idx + 1} RL001 宽异常静默吞掉（单行 {stmt}）缺理由"
                    f" —— {stripped[:70]}")
            continue

        m = EXCEPT.match(line)
        if not m:
            continue
        indent, expr = len(m.group(1)), m.group(2)
        if not _is_broad(expr):
            continue

        # 收集块体：缩进大于 except 的连续行；空行与注释行不算语句
        body: list[tuple[int, str]] = []
        for off, nxt in enumerate(lines[idx + 1:], start=idx + 2):
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            if nxt.lstrip().startswith("#"):
                continue
            body.append((off, nxt))
            # 只可能吞异常的形态：第一条语句就 pass/continue/return None
            if len(body) > 1:
                break

        if len(body) == 1 and SILENT_BODY.match(body[0][1]):
            if not _has_reason(*_comments_near(lines, idx, indent, body)):
                issues.append(
                    f"{rel}:{idx + 1} RL001 宽异常静默吞掉（块体只有 "
                    f"{body[0][1].strip()}）缺理由 —— {stripped[:70]}")
    return issues


def lint_ai_keys(path: Path) -> list[str]:
    """RL002：AI 输出结构里出现决定类字段（六护栏"只出建议"）。"""
    issues: list[str] = []
    rel = path.relative_to(ROOT)
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = AI_FORBIDDEN_KEY.search(line)
        if m:
            issues.append(
                f"{rel}:{no} RL002 AI 输出出现决定类键 {m.group(1)!r}"
                f"（护栏：只出建议，不改业务状态）—— {line.strip()[:70]}")
    return issues


def lint() -> list[str]:
    if not SRC.is_dir():
        return [f"找不到 {SRC}"]
    issues: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        issues.extend(lint_swallow(path))
        if AI_DIR in path.parents:
            issues.extend(lint_ai_keys(path))
    return issues


RULES = {
    "RL001": "静默吞异常必须写理由（禁止裸 pass / 裸 noqa）",
    "RL002": "AI 输出禁止出现 approve / decision / amount 键（六护栏）",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审查静态守卫（RL001 / RL002）")
    parser.add_argument("--list", action="store_true", help="只打印规则清单")
    args = parser.parse_args(argv)

    if args.list:
        for rid, desc in RULES.items():
            print(f"{rid}: {desc}")
        return 0

    issues = lint()
    if issues:
        print(f"review_lint: {len(issues)} 处违规（口径见 docs/15_代码审查标准与流程.md §5.1）\n")
        for item in issues:
            print(f"  {item}")
        print("\n修法二选一：① 把 except 收窄到具体异常类型；② 加一句中文说明为什么可以吞。")
        return 1

    print(f"review_lint: OK（RL001/RL002 共 {len(RULES)} 条规则，src/ 全量通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
