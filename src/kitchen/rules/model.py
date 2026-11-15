# -*- coding: utf-8 -*-
"""规则模型：把 `env_thresholds` 的一行翻译成一个可判定对象。

## 一行 = 一条独立判据（这是本项目最重要的一处口径）

旧工程（`vendor/proj/kernel/rules/model.py`）只有「单指标 · 单比较符 · 单阈值」，
用到本项目会缺三样：区间、持有时长、窗口聚合。本文件在此基础上扩展，但**保留
旧模型「纯函数 + 不可变 dataclass + 便于单测」的形态**，改造而非重写。

`env_thresholds` 的四个 `kind` 不是「四种表」，而是「这条判据的时间维度」：

| kind | 比较值从哪来 | 必填 |
|---|---|---|
| `range` | 瞬时值 | `lower_bound` / `upper_bound` 至少一个 |
| `tier` | 瞬时值，且仅当前档位匹配时参与判定 | 同 range + `profile` 非空 |
| `duration` | 瞬时值，且必须**持续** `duration_sec` | 同 range + `duration_sec` |
| `window` | `window_sec` 窗口内 `aggregate` 聚合值 | 同 range + `window_sec` + `aggregate` |

边界 → 比较符的推导（`bounds_to_op`）：只有 `upper` 得 `>`、只有 `lower` 得 `<`、
两个都有得 `out`（区间外违规）。要「区间内违规」（达标判定）时显式传 `op="in"`，
不靠猜。

这样设计的好处：脏数据在**加载期**就报错（`validate_rule`），不会出现
「运行到凌晨才发现某条阈值没有边界，于是永不触发」这种事。
"""
from __future__ import annotations

import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------- 比较符
# 单边比较符沿用旧工程口径；in/out 为本次扩展（双边区间）
SINGLE_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
}
RANGE_OPS: tuple[str, ...] = ("in", "out")
OPS: tuple[str, ...] = (*SINGLE_OPS, *RANGE_OPS)

# 窗口聚合函数；last 等价于「瞬时值」，用于让 window 规则也能退化成瞬时判定
AGGREGATES: tuple[str, ...] = (
    "last", "median", "mean", "max", "min", "rise", "rate", "std",
)

RULE_KINDS: tuple[str, ...] = ("range", "tier", "duration", "window")

# 档位：运行时切换（#2 营业/备餐/收档由 APScheduler 按时段下发）
# 空档位：规则不限档位
ANY_PROFILE = None

# 取值口径：部署时选定，不随运行时段变。真实值与演示值两套阈值并存，
# 是为了答辩能演（温水 35℃ 触发）而生产不至于误报（0~8℃ 才是真标准）。
# 这两个维度共用了 `env_thresholds.profile` 一个列，必须在代码里分开认。
DATASET_PROFILES: frozenset[str] = frozenset({"real", "demo"})


class RuleError(ValueError):
    """规则定义非法；加载期就该炸，不要等到判定时。"""

    def __init__(self, message: str, rule_id: str = "") -> None:
        super().__init__(f"规则 {rule_id}: {message}" if rule_id else message)
        self.rule_id = rule_id


def compare(
    op: str,
    value: float,
    *,
    threshold: float | None = None,
    lower: float | None = None,
    upper: float | None = None,
) -> bool:
    """统一的边界判定入口：Rule 与 Condition 共用，避免两处逻辑漂移。"""
    if op in SINGLE_OPS:
        if threshold is None:
            raise RuleError(f"比较符 {op!r} 需要 threshold")
        return SINGLE_OPS[op](value, float(threshold))
    if op == "in":
        if lower is None or upper is None:
            raise RuleError("比较符 'in' 需要 lower 与 upper 同时给出")
        return float(lower) <= value <= float(upper)
    if op == "out":
        if lower is None and upper is None:
            raise RuleError("比较符 'out' 需要 lower 或 upper 至少一个")
        if lower is not None and value < float(lower):
            return True
        if upper is not None and value > float(upper):
            return True
        # 只给了一边时，另一边视为不设限
        return False
    raise RuleError(f"不支持的比较符: {op!r}，允许 {list(OPS)}")


def bounds_to_op(lower: float | None, upper: float | None) -> tuple[str, float | None]:
    """由边界推比较符：这是 `env_thresholds` → `Rule` 的桥。

    返回 `(op, threshold)`；双边时 threshold 为 None（用 lower/upper 判定）。
    """
    if lower is not None and upper is not None:
        return "out", None
    if upper is not None:
        return ">", float(upper)
    if lower is not None:
        return "<", float(lower)
    raise RuleError("lower_bound 与 upper_bound 不能同时为空")


def describe_bounds(
    op: str, threshold: float | None, lower: float | None, upper: float | None
) -> str:
    """给报警正文用的人类可读边界，如 `> 0.55` / `0~8℃ 之外`。"""
    if op == "in":
        return f"{lower:g}~{upper:g} 之内"
    if op == "out":
        if lower is not None and upper is not None:
            return f"{lower:g}~{upper:g} 之外"
        if upper is not None:
            return f"> {upper:g}"
        return f"< {lower:g}"
    return f"{op} {threshold:g}"


# ---------------------------------------------------------------- 多源交叉条件
@dataclass(frozen=True)
class Condition:
    """一个附加条件：多源交叉（#3 视觉 AND 火焰）与互锁前提用它表达。

    `op="present"` 是语法糖：只要该 metric 有值即成立（火焰 0/1 这类离散量常用
    `op=">"` + `threshold=0.5`，但 present 更适合「事件/在线」语义）。
    """

    metric: str
    op: str = ">"
    threshold: float | None = None
    lower: float | None = None
    upper: float | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if not self.metric:
            raise RuleError("condition.metric 不能为空")
        if self.op != "present" and self.op not in OPS:
            raise RuleError(f"condition.op={self.op!r} 非法，允许 present + {list(OPS)}")

    def met(self, metrics: Mapping[str, float]) -> bool:
        value = metrics.get(self.metric)
        if value is None:
            return False  # 缺数据一律视为不成立，宁可不触发也不误触发
        if self.op == "present":
            return True
        return compare(
            self.op, float(value),
            threshold=self.threshold, lower=self.lower, upper=self.upper,
        )

    def describe(self) -> str:
        if self.op == "present":
            return f"{self.label or self.metric} 有效"
        return f"{self.label or self.metric} {describe_bounds(self.op, self.threshold, self.lower, self.upper)}"


# ---------------------------------------------------------------- 规则
@dataclass(frozen=True)
class Rule:
    """一条判据。不可变，可安全放进集合/多线程共享。

    字段与 `env_thresholds` 一一对应（见模块 docstring 的映射表），额外三个是
    表里没有、由代码/场景配置补充的：`latch`（闩锁）、`requires`（多源交叉）、
    `actions`（命中后建议下发的动作名，由互锁层再裁决一次）。
    """

    id: str
    metric: str
    op: str = ">"
    threshold: float | None = None
    lower: float | None = None
    upper: float | None = None
    kind: str = "range"
    profile: str | None = ANY_PROFILE
    level: int = 1
    duration_sec: float | None = None
    window_sec: float | None = None
    aggregate: str = "last"
    latch: bool = False
    actions: tuple[str, ...] = ()
    requires: tuple[Condition, ...] = ()
    message: str = ""
    atype: str = ""
    enabled: bool = True
    point_id: int | None = None
    source: str = "db"  # db | manifest | test
    enabled_reason: str = ""  # 停用原因（管理端展示用）
    # 命中后要开的工单类型（"" = 只报警不开单）。#2 收档档触发要生成清理工单，
    # 但工单不是执行器动作（9 个动作里没有 work_order），它是要落库的业务事件，
    # 所以单独一个字段随 verdict 上送，由服务端 A4 的工单模块写 `work_orders`。
    ticket: str = ""
    # 是否允许「刷卡复位」绕过「必须先回落」的检查。
    #
    # 默认 False，因为「带故障复位」是最危险的操作：气还在漏、温度还在升，把
    # 告警按成正常，等于让设备闭嘴。但 #3 明火离人是例外——刷卡的语义**就是**
    # 「我人回来了」，而人回来恰好是这个判据失效的直接原因，等传感器反应过来
    # 再解锁纯属多余，厨师会以为卡刷坏了。
    card_reset: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise RuleError("rule.id 不能为空（建议直接用 env_thresholds.id）")
        if not self.metric:
            raise RuleError("rule.metric 不能为空", self.id)
        if self.op not in OPS:
            raise RuleError(f"比较符 {self.op!r} 非法，允许 {list(OPS)}", self.id)
        if self.kind not in RULE_KINDS:
            raise RuleError(f"kind={self.kind!r} 非法，允许 {list(RULE_KINDS)}", self.id)
        if self.aggregate not in AGGREGATES:
            raise RuleError(f"aggregate={self.aggregate!r} 非法，允许 {list(AGGREGATES)}", self.id)
        if self.duration_sec is not None and float(self.duration_sec) < 0:
            raise RuleError("duration_sec 不能为负", self.id)
        if self.window_sec is not None and float(self.window_sec) <= 0:
            raise RuleError("window_sec 必须 > 0", self.id)
        if self.level not in (0, 1, 2, 3):
            raise RuleError(
                f"level={self.level} 非法，只支持 0/1/2/3（0 = 仅展示不报警，#7 温湿度就是这个级别）",
                self.id,
            )
        # atype / message 缺省补齐沿用旧模型写法
        object.__setattr__(self, "atype", self.atype or self.metric)
        object.__setattr__(
            self, "message", self.message or f"{self.atype} 触发 {self.describe_bound()}",
        )

    # ------------------------------------------------------------ 判定
    def matches(self, value: float) -> bool:
        return compare(
            self.op, float(value),
            threshold=self.threshold, lower=self.lower, upper=self.upper,
        )

    def requires_met(self, metrics: Mapping[str, float]) -> bool:
        """多源交叉：全部条件成立才算规则成立（AND 语义，#3 的核心）。"""
        return all(cond.met(metrics) for cond in self.requires)

    @property
    def effective_duration(self) -> float | None:
        """该规则要求持续多久；None 表示交给引擎默认值。

        窗口规则以前这里返回 0.0，理由是「10 分钟窗口再叠持续时长会变成 20 分钟」。
        这个理由只对「叠一个同样长的持续时长」成立，对「叠默认 3 秒去抖」不成立——
        而 0.0 的代价很实在：`rise` 聚合在**旧点滑出窗口**的那一帧会突变（比如从
        `max-min` 一下掉回小值），没有去抖就是 fired / released / fired 来回闪，
        用户在手机上看到告警自己闪三下。所以这里也交给引擎默认的 3 秒去抖，
        整体效果是「10 分钟窗口 + 3 秒确认」，与 04 文档的口径一致。
        """
        return None if self.duration_sec is None else float(self.duration_sec)

    @property
    def window_key(self) -> tuple[str, float] | None:
        """窗口聚合规则所需的 (metric, window_sec)；非窗口规则返回 None。"""
        if self.kind != "window":
            return None
        if not self.window_sec:
            raise RuleError("kind=window 必须给 window_sec", self.id)
        return (self.metric, float(self.window_sec))

    def accepts_profile(self, profile: str | None, dataset: str | None = None) -> bool:
        """这条规则在当前档位 + 当前取值口径下是否参与判定。

        `profile` 列有两种用途，靠 `profile_kind` 区分：

        - **档位**（`kind="tier"`）：营业/备餐/收档，运行时可切，#2 用；
        - **口径**（值域是 real/demo）：真实阈值 / 演示阈值，部署时选定。

        混在一起判定会出最典型的事故：只传了档位 `business`，于是所有
        `demo`/`real` 规则都被当成「别的档位」过滤掉——阈值一条都不生效，
        而引擎不报任何错。
        """
        if self.profile is None:
            return True
        if self.profile_kind == "dataset":
            return dataset is None or self.profile == dataset
        return self.profile == profile

    @property
    def profile_kind(self) -> str:
        """`profile` 这个值代表什么维度：tier（档位）/ dataset（取值口径）。"""
        if self.kind == "tier":
            return "tier"
        if self.profile in DATASET_PROFILES:
            return "dataset"
        return "tier"

    def describe_bound(self) -> str:
        return describe_bounds(self.op, self.threshold, self.lower, self.upper)

    def describe(self, value: float) -> str:
        """报警正文：规则描述 + 当前值/边界，截断到 DDL 的 255 字符（沿用旧模型）。"""
        text = f"{self.message}（{self.metric}={value:g}，判定 {self.describe_bound()}）"
        if self.requires:
            text += "；前置：" + "、".join(cond.describe() for cond in self.requires)
        return text[:255]


# ---------------------------------------------------------------- 校验（加载期）
def validate_rule(rule: Rule) -> list[str]:
    """返回问题列表；空列表 = 通过。脏阈值在装载时就要被拦住。"""
    problems: list[str] = []
    if rule.op in SINGLE_OPS and rule.threshold is None:
        problems.append(f"op={rule.op} 需要 threshold")
    if rule.op in RANGE_OPS and rule.lower is None and rule.upper is None:
        problems.append(f"op={rule.op} 需要 lower/upper")
    if rule.lower is not None and rule.upper is not None and float(rule.lower) > float(rule.upper):
        problems.append(f"lower({rule.lower:g}) 大于 upper({rule.upper:g})，区间为空")
    if rule.kind == "tier" and not rule.profile:
        problems.append("kind=tier 必须给 profile（营业/备餐/收档…）")
    if rule.kind == "duration" and rule.duration_sec is None:
        problems.append("kind=duration 必须给 duration_sec")
    if rule.kind == "window":
        if not rule.window_sec:
            problems.append("kind=window 必须给 window_sec")
        if rule.aggregate == "last":
            problems.append("kind=window 的 aggregate 不能是 last（那等价于瞬时判定）")
        if rule.aggregate in ("rise", "rate") and rule.op not in ("in", "out") and rule.threshold is None:
            problems.append(f"aggregate={rule.aggregate} 需要 threshold")
    for cond in rule.requires:
        if cond.metric == rule.metric and cond.op == rule.op and cond.threshold == rule.threshold:
            problems.append(f"多源交叉条件与自身重复：{cond.metric}")
    return problems


def assert_rules_valid(rules: Sequence[Rule]) -> None:
    """批量校验并去重 id，把问题一次性抛出来，不要一条条试。"""
    seen: dict[str, int] = {}
    problems: list[str] = []
    for rule in rules:
        if rule.id in seen:
            problems.append(f"规则 id 重复：{rule.id}")
        seen[rule.id] = seen.get(rule.id, 0) + 1
        problems.extend(f"{rule.id}: {item}" for item in validate_rule(rule))
    if problems:
        raise RuleError("规则集校验失败：\n- " + "\n- ".join(problems))
