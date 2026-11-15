# -*- coding: utf-8 -*-
"""端侧运行时：配置 → 规则 → 按本地时间切档 → 判定 → 产出「动作 + 工单」。

## 这一层存在的理由

前面的模块各自只解决一件事：`config_store` 管缓存、`schedule` 算档位、
`loader` 转规则、`engine` 判一帧。把它们的装配顺序交给每个调用方自己写，
就会出现三种「各自都写对了、合起来错」的形态：

1. **切档时重新 `set_rules()`**：规则集一变，去抖计时全部归零，10 分钟温升
   窗口也被裁剪。表现是「每次到点切档，判定就晚几分钟才生效」；
2. **档位只在启动时算一次**：跑久了档位不跟着时间走，22:00 收档了设备还以为
   在营业，阈值 280℃ 只播语音——安全漏洞；
3. **配置下发后只更新阈值不更新档位表**：新排班第二天不生效，得重启设备。

所以装配顺序**写死在这一层**，调用方只调 `tick()`：

```
配置变更 → set_rules(全量规则) + set_dataset()
每一帧 → 按本地时间算档位 → 变了才 set_profile() → engine.evaluate()
       → verdicts 汇总成 (actions, tickets)
```

## 「开机第一次 tick 之前不知道档位」怎么办

构造函数里就把档位算好（用注入时钟）。如果这时是 03:00，档位就是 `closed`
（最严档）——设备冷启动不该假设自己在营业。

## 为什么动作名要在装载期校验

`kitchen_rules.json` 里写错一个 `fan_on` → `fanOn`，引擎照常判定、照常上报，
只是 `InterlockController` 永远找不到这个动作，**继电器永远不动**。设备看起来
在正常工作（有告警、有播报），实际最关键的联锁动作从来没执行过。这类问题在
现场排查一次的成本远超在装载期抛个异常。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .channels import ACTION_BINDINGS
from .rules.config_store import KitchenConfig
from .rules.engine import RuleEngine, Verdict
from .rules.loader import load_rows
from .rules.model import Rule, RuleError
from .rules.schedule import ProfileSchedule

log = logging.getLogger("kitchen.runtime")

__all__ = ["KitchenRuntime", "RuntimeConfigError", "TickResult"]


class RuntimeConfigError(ValueError):
    """运行时装配错误（与内建 RuntimeError 区分，避免名字冲突带来的困惑）。"""


@dataclass(frozen=True)
class TickResult:
    """一帧判定的完整结果。调用方不需要自己拼档位和工单。"""

    profile: str
    previous_profile: str | None
    profile_changed: bool
    verdicts: tuple[Verdict, ...] = ()
    actions: tuple[str, ...] = ()     # 去重后的动作，直接交给互锁层
    tickets: tuple[str, ...] = ()     # 要开的工单类型（A4 落库）

    @property
    def empty(self) -> bool:
        return not self.verdicts and not self.profile_changed

    def to_payload(self, device_id: str = "", ts: int | None = None) -> dict:
        body: dict[str, Any] = {
            "profile": self.profile,
            "profile_changed": self.profile_changed,
            "events": [verdict.to_payload(device_id, ts) for verdict in self.verdicts],
        }
        if self.previous_profile and self.profile_changed:
            body["previous_profile"] = self.previous_profile
        if self.actions:
            body["actions"] = list(self.actions)
        if self.tickets:
            body["tickets"] = list(self.tickets)
        return body


class KitchenRuntime:
    """端侧一站式运行时（线程安全由 `RuleEngine` 内部的锁保证）。"""

    #: #2 的档位语义只在厨房场景成立；其它场景不带档位表时按「无档位」跑
    SCHEDULE_METRIC = "hood_temp"

    def __init__(
        self,
        config: KitchenConfig,
        *,
        scenario_rules: Mapping[str, Mapping[str, Any]] | None = None,
        device_id: str = "",
        default_duration_sec: float = 3.0,
        cooldown_sec: float = 60.0,
        boot_grace_sec: float = 0.0,
        clock=None,
        wall_clock=None,
        validate_profiles: bool = True,
    ) -> None:
        self.config = config
        # 不传就用配置自带的动作语义。传了以参数为准（单测要故意写错动作名时用）。
        # 默认值必须是 config 里的，不能是空字典：空字典 = 动作全空 = 告警照报
        # 但继电器永不动，而且不报错。
        self.scenario_rules = dict(
            config.scenario_rules if scenario_rules is None else scenario_rules
        )
        self.device_id = device_id
        self._wall_clock = wall_clock or time.time
        self.engine = RuleEngine(
            device_id=device_id,
            default_duration_sec=default_duration_sec,
            cooldown_sec=cooldown_sec,
            boot_grace_sec=boot_grace_sec,
            clock=clock,
            wall_clock=self._wall_clock,
            dataset=config.dataset,
        )
        self._disabled: list[Rule] = []
        self._previous_profile: str | None = None
        self._validate_profiles = validate_profiles
        self._load_rules()
        # 冷启动先按本地时间定档，之后再跟时间走（不假设自己在营业）
        self.engine.set_profile(config.schedule.profile_at(self._wall_clock()))

    # ---------------------------------------------------------------- 装载
    def _load_rules(self) -> None:
        """装全量规则（含所有档位），档位切换只改 `set_profile`，不重装。

        重装的代价见模块 docstring 第 1 条：去抖计时归零 + 窗口裁剪。
        """
        rows = self.config.rows_for()          # profile=None → 全量
        active, disabled = load_rows(rows, scenario_rules=self.scenario_rules)
        self._assert_actions_known(active)
        if self._validate_profiles:
            self._assert_profiles_known(active)
        self.engine.set_rules(active)
        self.engine.set_dataset(self.config.dataset)
        self._disabled = disabled

    def _assert_actions_known(self, rules: Sequence[Rule]) -> None:
        known = set(ACTION_BINDINGS)
        problems: list[str] = []
        for rule in rules:
            for action in rule.actions:
                if action not in known:
                    problems.append(f"{rule.id}（{rule.metric}）写了未知动作 {action!r}")
        if problems:
            raise RuntimeConfigError(
                "规则动作名不合法（引擎会照常判定，但互锁层永远找不到这个动作）：\n- "
                + "\n- ".join(problems)
                + f"\n允许的动作：{sorted(known)}"
            )

    def _assert_profiles_known(self, rules: Sequence[Rule]) -> None:
        """档位表的每个档位都必须有规则，否则切过去等于「一条都不判」。"""
        known = {rule.profile for rule in rules if rule.profile_kind == "tier"}
        if not known:
            return  # 这套配置不含档位规则，档位表不参与判定，不校验
        try:
            self.config.schedule.validate(known)
        except Exception as exc:  # ScheduleError → 统一成本层的错误
            raise RuntimeConfigError(
                f"档位表与规则集不一致：{exc}"
            ) from exc

    # ---------------------------------------------------------------- 配置热更新
    def apply_config(self, config: KitchenConfig) -> bool:
        """服务端下发新配置。规则集真的变了才重装，否则只切档位。

        返回是否重装了规则（便于调用方决定要不要记一条审计）。
        """
        old_signature = self._signature(self.config)
        new_signature = self._signature(config)
        self.config = config
        self.engine.set_dataset(config.dataset)
        reloaded = old_signature != new_signature
        if reloaded:
            self._load_rules()
        # 无论是否重装，档位都要按新表 + 当前时间重算：排班表可能只改了时段
        self.engine.set_profile(config.schedule.profile_at(self._wall_clock()))
        log.info("配置更新：%s（重装规则=%s）", config.describe(), reloaded)
        return reloaded

    @staticmethod
    def _signature(config: KitchenConfig) -> tuple:
        """判定「规则集是否等价」的指纹。

        档位表**不进**指纹：只改时段不该触发规则重装（重装会重置去抖）。
        """
        rows = sorted(
            (str(row.get("id")), str(row.get("kind")), str(row.get("profile")),
             str(row.get("lower_bound")), str(row.get("upper_bound")),
             str(row.get("duration_sec")), str(row.get("window_sec")),
             str(row.get("level")), str(row.get("enabled")))
            for row in config.threshold_rows
        )
        # 动作语义也要进指纹：管理端把「营业档只播报」改成「也关阀」，这属于
        # 规则变了，必须重装，否则新动作要等重启才生效。
        scenario = tuple(sorted(
            (name, json.dumps(body, ensure_ascii=False, sort_keys=True))
            for name, body in config.scenario_rules.items()
        ))
        return (config.dataset, tuple(rows), scenario)

    # ---------------------------------------------------------------- 判定
    def profile_at(self, now: float | None = None) -> str:
        return self.config.schedule.profile_at(self._wall_clock() if now is None else now)

    def tick(
        self,
        metrics: Mapping[str, float],
        now: float | None = None,
        *,
        raw_metrics: Mapping[str, float] | None = None,
    ) -> TickResult:
        """喂一帧遥测，返回本次结果（含本次是否切了档）。"""
        moment = None if now is None else float(now)
        wall_now = self._wall_clock() if moment is None else moment
        wanted = self.config.schedule.profile_at(wall_now)
        previous = self.engine.profile
        changed = wanted != previous
        if changed:
            self.engine.set_profile(wanted)
        verdicts = self.engine.evaluate(metrics, now=moment, raw_metrics=raw_metrics)
        if changed:
            self._previous_profile = previous
            log.info("档位切换：%s → %s", previous or "-", wanted)
        return TickResult(
            profile=wanted,
            previous_profile=previous if changed else None,
            profile_changed=changed,
            verdicts=tuple(verdicts),
            actions=_dedup(
                action for verdict in verdicts for action in verdict.actions
            ),
            tickets=_dedup(verdict.ticket for verdict in verdicts if verdict.ticket),
        )

    def next_wakeup(self, now: float | None = None) -> float | None:
        """下一次需要重新判定的时刻（档位切换点）。

        端侧用它在切换时刻唤醒一次，而不是每秒轮询档位表——ESP32 常态是
        「采集时醒、其余时间睡」，多一个每秒轮询会直接把电池/发热预算吃光。
        """
        return self.config.schedule.next_change(
            self._wall_clock() if now is None else now
        )

    # ---------------------------------------------------------------- 观测
    def snapshot(self) -> dict:
        body = self.engine.snapshot()
        body.update({
            "device_id": self.device_id,
            "profile": self.engine.profile,
            "profile_slots": [slot.describe() for slot in self.config.schedule.slots],
            "profile_default": self.config.schedule.default,
            "config_source": self.config.source,
            "config_version": self.config.version,
            "config_degraded": self.config.degraded,
            "disabled_rules": [rule.id for rule in self._disabled],
        })
        return body

    def ack(self, rule_id: str, reviewer: str):
        return self.engine.ack(rule_id, reviewer)

    def ack_all(self, reviewer: str):
        return self.engine.ack_all(reviewer)


def _dedup(items) -> tuple[str, ...]:
    """保序去重：多个规则命中同一动作时只下发一次（幂等，但别刷屏）。"""
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(str(item), None)
    return tuple(seen)
