# -*- coding: utf-8 -*-
"""客服培训题库自检 + 判分工具（纯标准库）。

## 两个用途

1. `--check`（默认）：自检题库 `17_training_quiz.json` 与事实源一致——
   * 每题 `source_faq` 在 `16_faq_kb.json` 里存在；
   * 情景题的 `answer_ref` 用**真实代码** `handoff.assess()` / `compensation.assess()`
     按 `verify_input` 复现，逐字段比对，确保「培训教的标准 = 系统跑的标准」；
   * 单选/多选/判断题答案 key 都在选项内、无重复 id、六模块全覆盖。
2. `--grade 答卷.json`：给学员答卷判分，按模块出分、按 quality.GRADES 出等级，
   输出是否达到认证线（默认 80）。答卷格式见 `--sample`。

题库与 FAQ 库一样属运营资料，放在 `03_设计文档/`，**不进代码 docs / CI**；
但本工具 import 真实代码模块，所以放在代码工程 `tools/` 下，用相对路径定位题库。

用法（在代码工程根目录）：
    python tools/training_quiz.py --check
    python tools/training_quiz.py --sample            # 打印空白答卷模板
    python tools/training_quiz.py --grade answers.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_QUIZ = _ROOT.parent.parent / "03_设计文档" / "17_training_quiz.json"
_KB = _ROOT.parent.parent / "03_设计文档" / "16_faq_kb.json"


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def check() -> int:
    from kitchen.guest import handoff, compensation, quality, guard

    quiz, kb = _load(_QUIZ), _load(_KB)
    questions = quiz["questions"]
    faq_ids = {e["id"] for e in kb["entries"]}
    seen: set[str] = set()
    errors: list[str] = []
    stats: dict[str, dict[str, int]] = {}

    for q in questions:
        qid = str(q.get("id") or "")
        if qid in seen:
            errors.append(f"{qid}: 题 id 重复")
        seen.add(qid)
        mod = str(q.get("module") or "?")
        stats.setdefault(mod, {"n": 0, "points": 0})
        stats[mod]["n"] += 1
        stats[mod]["points"] += int(q.get("points") or 0)

        src = str(q.get("source_faq") or "")
        if src and src not in faq_ids:
            errors.append(f"{qid}: source_faq={src} 不在 FAQ 库")

        keys = {o["k"] for o in q.get("options", [])}
        if q["type"] in ("single", "judge", "multi"):
            ans = q.get("answer") or []
            if not ans or not set(ans) <= keys:
                errors.append(f"{qid}: answer={ans} 与选项 {sorted(keys)} 不符")
            if q["type"] in ("single", "judge") and len(ans) != 1:
                errors.append(f"{qid}: 单选/判断题答案应恰好 1 个，实际 {ans}")
            if q["type"] == "multi" and len(ans) < 2:
                errors.append(f"{qid}: 多选题答案至少 2 个，实际 {ans}")

        # 情景题：用真实代码复现 answer_ref
        if q["type"] == "scenario":
            ref = q.get("answer_ref") or {}
            vin = q.get("verify_input") or {}
            if "code" in ref:
                got = handoff.assess(**vin)
                for field in ("level", "code", "owner", "promise_min"):
                    if field in ref and got.get(field) != ref[field]:
                        errors.append(
                            f"{qid}: {field} 培训写 {ref[field]!r}，代码判 {got.get(field)!r}"
                        )
                # 代码归属表自洽：level 必须与 code 所在级别一致
                expect_level = ("human_now" if ref.get("code") in
                                ("food_safety", "allergy_reaction", "legal_threat",
                                 "explicit_escalation", "repeat_complaint")
                                else "self_service" if ref.get("code") == "normal"
                                else "ai_plus_human")
                if ref.get("level") != expect_level:
                    errors.append(f"{qid}: level={ref.get('level')} 与 code 归属 {expect_level} 不符")
            if "tier" in ref:
                got = compensation.assess(**vin)
                for field in ("tier", "decide_by", "suggest", "cap"):
                    if field in ref and got.get(field) != ref[field]:
                        errors.append(
                            f"{qid}: {field} 培训写 {ref[field]!r}，代码判 {got.get(field)!r}"
                        )

    declared = set(quiz.get("modules", {}))
    if declared != set(stats):
        errors.append(f"模块声明 {sorted(declared)} 与题目实际 {sorted(stats)} 不一致")

    # 规则常量自检：培训里引用的阈值不能漂移
    if quiz["pass_line"]["grade_by"] and quality.AUTO_MAX != 70.0:
        errors.append(f"quality.AUTO_MAX 变成 {quality.AUTO_MAX}，培训口径需同步")
    if not any(b[0] == "马上就好" for b in guard.BANNED):
        errors.append("guard.BANNED 已无「马上就好」，题库 M1/M3 需同步")

    print("题库自检：", "PASS" if not errors else "FAIL")
    for mod in sorted(stats):
        print(f"  {mod}: {stats[mod]['n']:>2} 题 / {stats[mod]['points']:>2} 分  "
              f"({quiz['modules'].get(mod, '')})")
    print(f"  合计 {len(questions)} 题 / {sum(s['points'] for s in stats.values())} 分")
    if errors:
        for e in errors:
            print("  ✗", e)
        return 1
    print("  ✓ source_faq 全部命中 FAQ 库；情景题答案与 handoff/compensation 逐字段一致；"
          "答案 key、模块覆盖、规则阈值均通过")
    return 0


def sample() -> int:
    quiz = _load(_QUIZ)
    out = {str(q["id"]): (list(q.get("answer") or []) if q["type"] != "scenario"
                          else dict(q["answer_ref"])) for q in quiz["questions"]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def grade(path: Path) -> int:
    from kitchen.guest import quality

    quiz = _load(_QUIZ)
    answers = _load(path)
    questions = {str(q["id"]): q for q in quiz["questions"]}
    by_mod: dict[str, dict[str, int]] = {}
    total = full = 0
    unknown = [k for k in answers if k not in questions]
    for qid, q in questions.items():
        if qid not in answers:
            continue
        given = answers[qid]
        if q["type"] == "scenario":
            ref = q["answer_ref"]
            ok = isinstance(given, dict) and all(given.get(k) == v for k, v in ref.items())
            real = ref
        else:
            ok = isinstance(given, list) and sorted(given) == sorted(q["answer"])
            real = q["answer"]
        pts = int(q["points"])
        full += pts
        if ok:
            total += pts
        m = str(q["module"])
        by_mod.setdefault(m, {"got": 0, "full": 0})
        by_mod[m]["full"] += pts
        if ok:
            by_mod[m]["got"] += pts
        print(f"  {'✓' if ok else '✗'} {qid} {pts}分  正确答案={real}")

    normalized = round(total / full * 100, 1) if full else 0.0
    passed = normalized >= float(quiz["pass_line"]["certify"])
    print("\n判分结果")
    for m in sorted(by_mod):
        s = by_mod[m]
        print(f"  {m}: {s['got']}/{s['full']} ({round(s['got']/s['full']*100,1)}%)")
    print(f"  总分 {total}/{full} → 折算 {normalized}  等级「{quality.grade_of(normalized)}」"
          f"  认证{'通过' if passed else '未通过'}（线 {quiz['pass_line']['certify']}）")
    if unknown:
        print("  警告：答卷含题库外 id：", ", ".join(unknown))
    return 0 if passed else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="客服培训题库自检与判分")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="自检题库一致性（默认）")
    g.add_argument("--sample", action="store_true", help="打印空白答卷模板")
    g.add_argument("--grade", type=Path, metavar="JSON", help="给答卷判分")
    args = ap.parse_args()
    if args.sample:
        return sample()
    if args.grade:
        return grade(args.grade)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
