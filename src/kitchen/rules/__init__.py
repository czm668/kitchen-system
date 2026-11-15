# -*- coding: utf-8 -*-
"""规则引擎（`kitchen.rules`）：阈值判定从「单指标比大小」扩到八项能力。

来源与边界：本包由旧工程 `vendor/proj/kernel/rules/`（素材包 `rules-engine`，
ADAPT 分区）改造而来，**不是重写**——`model.py` 的不可变 `Rule` + `describe()`
截断 255 字符、`engine.py` 的去抖 + 冷却、`loader.py` 的「manifest 默认 + DB
覆盖」三层结构都直接沿用。改造的部分见 `engine.py` 的模块注释。

模块划分：

| 模块 | 职责 | 对应能力 |
|---|---|---|
| `model` | `Rule` / `Condition` / 边界与比较符、加载期校验 | 区间判定、多源交叉、`per_rule` 时长 |
| `window` | `SlidingWindow` 与 median/mean/max/min/rise/rate/std | 窗口聚合 |
| `state` | 闩锁 / 冷却状态 + JSON/NVS 两种持久化 | 闩锁 + NVS |
| `engine` | `RuleEngine.evaluate()` 一帧判定，档位与宽限 | 档位阈值、开机宽限、闩锁 + 人工复位 |
| `loader` | `env_thresholds` 行 → `Rule`，脏数据装载期报错 | 配置来源 |

快速上手::

    from kitchen.rules import RuleEngine, Rule, load_rows

    active, disabled = load_rows(rows)          # rows 是 env_thresholds 的结果集
    engine = RuleEngine(device_id="kitchen-node-01", boot_grace_sec=300)
    engine.boot()
    engine.set_rules(active)
    for verdict in engine.evaluate({"gas_level": 0.62}):
        print(verdict.event, verdict.message)
"""
from kitchen.rules.engine import (
    EVENT_FIRED,
    EVENT_LATCHED,
    EVENT_RELEASED,
    AckResult,
    RuleEngine,
    Verdict,
)
from kitchen.rules.loader import (
    SCENARIO_RULES_FILE,
    load_rows,
    load_scenario_rules,
    profile_rules,
    profiles_of,
    rule_from_row,
)
from kitchen.rules.model import (
    AGGREGATES,
    ANY_PROFILE,
    OPS,
    RULE_KINDS,
    Condition,
    Rule,
    RuleError,
    assert_rules_valid,
    bounds_to_op,
    compare,
    describe_bounds,
    validate_rule,
)
from kitchen.rules.state import (
    BREACHED,
    LATCHED,
    LATCH_STATES,
    NORMAL,
    RuleState,
    RuleStateStore,
)
from kitchen.rules.window import SlidingWindow

__all__ = [
    # model
    "Rule", "Condition", "RuleError", "OPS", "AGGREGATES", "RULE_KINDS", "ANY_PROFILE",
    "compare", "bounds_to_op", "describe_bounds", "validate_rule", "assert_rules_valid",
    # window
    "SlidingWindow",
    # state
    "RuleState", "RuleStateStore", "NORMAL", "BREACHED", "LATCHED", "LATCH_STATES",
    # engine
    "RuleEngine", "Verdict", "AckResult", "EVENT_FIRED", "EVENT_RELEASED", "EVENT_LATCHED",
    # loader
    "load_rows", "load_scenario_rules", "rule_from_row", "profile_rules", "profiles_of",
    "SCENARIO_RULES_FILE",
]
