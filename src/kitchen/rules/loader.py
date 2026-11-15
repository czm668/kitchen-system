# -*- coding: utf-8 -*-
"""规则装载：`env_thresholds` 行 → `Rule`，以及档位/场景级配置的合并。

## 为什么单独一层

旧工程把「manifest 默认阈值 + DB 覆盖」写在一个 loader 里，好处是简单，坏处是
阈值数据必须来自 SQLAlchemy 模型。本项目服务端用 MySQL、端侧读 NVS 文本、
单测读纯字典，三处数据形态完全一样（都是 `env_thresholds` 的列），
所以这一层只认**普通字典**，不引入 ORM。

## 合并优先级（沿用旧工程口径，只改一处）

1. DB 行有该 `metric + profile + kind` 的配置 → 用 DB；
2. `enabled=0` → 该行整条丢弃，但**保留在 `disabled` 列表**里给管理端展示；
3. `actions` / `message` 属于场景设计，DB 表不存，仍取场景配置；
4. 旧工程里「DB 新增 metric 自动生成通用规则」这一条**删掉**——kitchen 场景
   的 12 个遥测是强校验的，出现未知 metric 说明配置写错了，应当报错而不是
   悄悄生成一条没人维护的规则。

## 判据自洽性检查（本层最值钱的部分）

`kind` 与必填字段的对应关系见 `model.py` 的表。加载时逐条 `validate_rule()`，
把「没有边界的 duration 行」「没有档位名的 tier 行」这类问题在装载期炸出来。
这类脏数据在旧工程里的表现是「规则装进去了，但永远不触发」，排查成本极高。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .model import (
    ANY_PROFILE,
    Rule,
    RuleError,
    assert_rules_valid,
    bounds_to_op,
    validate_rule,
)

# 场景级配置（actions / message / latch / requires）的默认落点
SCENARIO_RULES_FILE = "kitchen_rules.json"


def _num(value: Any) -> float | None:
    """DB 的 NULL 经 JSON/驱动可能变成 None / "" / "NULL"，统一成 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_profile_extra(
    extra: Mapping[str, Any],
    profile: Any,
    rule_id: str = "",
    level: Any = None,
) -> dict[str, Any]:
    """合并 `by_profile` / `by_level` / `by_rule` 覆盖项，得到这一行真正生效的语义配置。

    三种覆盖解决三个不同的问题，都要有，且**按「越具体越优先」的顺序叠加**：

    - `by_profile`：**同一 metric 的动作随档位变**。#2 排烟营业档只预警
      （`actions` 只有 `voice_play`），备餐/收档要动继电器；
    - `by_level`：**同一 metric 的动作随严重度变**，而严重度恰好写在
      `env_thresholds.level` 里。#5 冷链两级（level 1 预警只播语音 / level 2
      报警要声光 + 工单）用它，一条覆盖就同时作用于 real 与 demo 两个口径，
      不必把 6 个 id 一个个列出来；
    - `by_rule`：**按 `env_thresholds.id` 精确覆盖**。给「就这一条要特殊对待」
      留的逃生口，优先级最高。
    """
    merged = {
        key: value for key, value in extra.items()
        if key not in ("by_profile", "by_level", "by_rule")
    }
    by_profile = extra.get("by_profile") or {}
    if profile and isinstance(by_profile, Mapping):
        override = by_profile.get(str(profile).strip())
        if isinstance(override, Mapping):
            merged.update(override)
    by_level = extra.get("by_level") or {}
    if level is not None and isinstance(by_level, Mapping):
        override = by_level.get(str(int(level)))
        if isinstance(override, Mapping):
            merged.update(override)
    by_rule = extra.get("by_rule") or {}
    if rule_id and isinstance(by_rule, Mapping):
        override = by_rule.get(str(rule_id).strip())
        if isinstance(override, Mapping):
            merged.update(override)
    return merged


def rule_from_row(
    row: Mapping[str, Any],
    *,
    scenario_rules: Mapping[str, Mapping[str, Any]] | None = None,
    source: str = "db",
) -> Rule:
    """把一行 `env_thresholds`（可带 join 出来的 metric）转成 `Rule`。

    必须字段：`id`、`metric`（可由 `env_points.metric` join 得到）。
    `scenario_rules` 按 `rule_name` 提供表里不存的语义字段：
    `{"gas_leak": {"actions": [...], "latch": true, "requires": [...], "message": "..."}}`。
    """
    rule_id = str(row.get("id") or "").strip()
    if not rule_id:
        raise RuleError("env_thresholds 行缺少 id")
    metric = str(row.get("metric") or "").strip()
    if not metric:
        raise RuleError(
            f"env_thresholds.id={rule_id} 缺少 metric（需 join env_points.metric）", rule_id,
        )

    kind = str(row.get("kind") or "range").strip()
    lower = _num(row.get("lower_bound"))
    upper = _num(row.get("upper_bound"))
    # 显式 op 优先（允许配置 'in' 做「区间内违规」的达标判定），否则由边界推
    op = str(row.get("op") or "").strip()
    threshold = _num(row.get("threshold"))
    if not op:
        op, threshold = bounds_to_op(lower, upper)

    # 场景级补充配置按 metric 索引：同一指标的多个档位/时长规则共享动作与文案，
    # `rule_name` 只是可选覆盖（想给某一条单独配动作时用）。
    name = str(row.get("rule_name") or row.get("name") or metric).strip()
    extra = _merge_profile_extra(
        (scenario_rules or {}).get(name) or {}, row.get("profile"), rule_id,
        row.get("level"),
    )

    return Rule(
        id=rule_id,
        metric=metric,
        op=op,
        threshold=threshold,
        lower=lower,
        upper=upper,
        kind=kind,
        profile=(str(row["profile"]).strip() or None) if row.get("profile") else ANY_PROFILE,
        level=int(row.get("level") or 1),
        # extra 可覆盖持有时长：安全类联锁必须 duration_sec=0（04 文档 §0.2
        # 「燃气超阈值立即关阀」），但这个「不等」是安全决策，写进配置文件
        # 看得见，不要藏成代码里的隐式默认。
        duration_sec=_num(extra.get("duration_sec", row.get("duration_sec"))),
        window_sec=_num(row.get("window_sec")),
        aggregate=str(row.get("aggregate") or "last").strip(),
        latch=bool(extra.get("latch", row.get("latch", False))),
        actions=tuple(str(a) for a in (extra.get("actions") or row.get("actions") or ())),
        requires=_conditions(extra.get("requires") or row.get("requires") or ()),
        message=str(extra.get("message") or row.get("message") or ""),
        atype=str(extra.get("atype") or name or metric),
        enabled=bool(int(row.get("enabled", 1))),
        point_id=int(row["point_id"]) if row.get("point_id") is not None else None,
        source=source,
        ticket=str(extra.get("ticket") or row.get("ticket") or ""),
        card_reset=bool(extra.get("card_reset", row.get("card_reset", False))),
    )


def _conditions(items: Iterable[Mapping[str, Any]]):
    """把 JSON 里的交叉条件转成 `Condition`；写错的字段在装载期直接报错。"""
    from .model import Condition  # 局部导入避免循环

    out = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise RuleError(f"requires[{index}] 必须是对象，收到 {type(item).__name__}")
        out.append(Condition(
            metric=str(item.get("metric") or ""),
            op=str(item.get("op") or ">"),
            threshold=_num(item.get("threshold")),
            lower=_num(item.get("lower")),
            upper=_num(item.get("upper")),
            label=str(item.get("label") or ""),
        ))
    return tuple(out)


def load_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    scenario_rules: Mapping[str, Mapping[str, Any]] | None = None,
    source: str = "db",
) -> tuple[list[Rule], list[Rule]]:
    """批量转换，返回 `(启用规则, 停用规则)`。

    停用的照样构造并校验（否则停用行里的错配置永远发现不了，等到有人打开它
    才炸），只是不进引擎。
    """
    active: list[Rule] = []
    disabled: list[Rule] = []
    for row in rows:
        rule = rule_from_row(row, scenario_rules=scenario_rules, source=source)
        problems = validate_rule(rule)
        if problems:
            raise RuleError(
                "阈值行不合法：\n- " + "\n- ".join(f"{rule.id}: {p}" for p in problems)
            )
        (active if rule.enabled else disabled).append(rule)
    assert_rules_valid(active + disabled)
    return active, disabled


def load_scenario_rules(path: str | Path) -> dict[str, dict[str, Any]]:
    """读场景级规则补充配置（`fixtures/kitchen_rules.json`）。

    该文件只放 `env_thresholds` 表里没有的语义：动作建议、闩锁、多源交叉、
    文案。**阈值本体不放这里**，避免同一份阈值出现两个来源。
    """
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuleError(f"场景规则配置无法读取：{target}（{exc}）") from exc
    rules = data.get("rules") if isinstance(data, Mapping) else None
    if not isinstance(rules, Mapping):
        raise RuleError(f"{target.name} 顶层必须是 {{'rules': {{...}}}}")
    return {str(name): dict(body) for name, body in rules.items()}


def profile_rules(rules: Sequence[Rule], profile: str | None) -> list[Rule]:
    """挑出某档位可用的规则（含不限档位的通用规则）。"""
    return [rule for rule in rules if rule.accepts_profile(profile)]


def profiles_of(rules: Sequence[Rule]) -> list[str]:
    """规则集里出现过的档位名，给 APScheduler / 管理端下拉框用。"""
    return sorted({rule.profile for rule in rules if rule.profile})
