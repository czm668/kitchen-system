# -*- coding: utf-8 -*-
"""#3 明火离人：视觉无人 AND 火焰有效 → 联锁；返岗刷卡复位。

## 判据拆成两半，各管一件事

- **主判据**在 `env_thresholds`（id=22）：`flame_b > 0.5` 持续 12 秒（灶上有火）；
- **附加条件**在 `kitchen_rules.json` 的 `requires`：`vision_no_person > 0.5`（工位没人）。

两个条件缺一不可。写成 OR 会退化成两个错误：

- 只看火焰 → 每天炒菜都在报警，功能上线三天就被关掉；
- 只看视觉 → 厨师背对摄像头就报警，同样没法用。

## 视觉信号的两条铁律

**1. 不进 12 类遥测契约。** 它由 `ai_service` 通过内部 topic 注入，不是端侧自采的
传感器读数。混进契约会让「遥测完整率」这个运维指标失真（视觉服务挂了会被算成
「传感器掉线」）。

**2. 失联按「有人」处理，但必须单独报。** 视觉过期时如果默认「无人」，一次网络
抖动就会关掉整个厨房的燃气阀、切断灶台电源——误停业的代价远大于它挡住的收益，
于是没人敢开着这个功能。所以过期时喂 `0`（有人）让联锁不成立，同时由
`vision_health()` 单独产出「视觉离线」告警，让平台知道这条防线当前是瞎的。
**降级要说出来，不能装作还在工作。**

## 刷卡复位只能复位 #3，且只复位本灶

`ack_by_card()` 只处理 `metric == flame_b` 的规则。不做这个限定，一张工牌就能把
「气还在漏」的燃气联锁按成正常——这是整个系统里最危险的一条越权路径。

工位维度同理：多灶部署时给每个灶一个 `FlameGuard` 实例（`vision/link.py` 的
既有语义「一个实例对应一个灶」），并用 `rule_ids=` 把它圈在本灶的规则上。
不做这个圈定，A 灶厨师返岗刷卡会把 B 灶的离人锁一起解开——B 灶可能真没人。

工牌**过期仍然允许复位**：过期是合规问题，现场处置是安全问题。合规可以事后追责
（写进 `warnings` 上送），安全不能等。但「工牌不存在」要拒绝——那说明卡是伪造的
或者不属于本店。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from ..rules.engine import AckResult, RuleEngine
from ..rules.model import Condition, Rule

log = logging.getLogger("kitchen.flame")

__all__ = ["CardAckResult", "FlameGuard", "StaffCard", "VisionHealth"]

# 视觉侧内部信号名（不在 12 类遥测契约内）
VISION_NO_PERSON = "vision_no_person"
VISION_HEADCOUNT = "vision_headcount"


@dataclass(frozen=True)
class StaffCard:
    """工牌。`expired` 指健康证过期（#9），不是工牌本身失效。"""

    uid: str
    staff_id: str
    name: str = ""
    expired: bool = False

    def describe(self) -> str:
        return f"{self.name or self.staff_id}（{self.staff_id}）"


@dataclass(frozen=True)
class VisionHealth:
    """视觉侧的数据新鲜度。`stale=True` 表示这条防线的输入已经过期。"""

    online: bool
    age_sec: float
    timeout_sec: float
    message: str = ""

    @property
    def stale(self) -> bool:
        return not self.online


@dataclass(frozen=True)
class CardAckResult:
    """刷卡复位结果。`ok=False` 时 `reason` 说明为什么不让复位。"""

    ok: bool
    card: StaffCard | None = None
    reason: str = ""
    reset: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def describe(self) -> str:
        who = self.card.describe() if self.card else "未知工牌"
        body = f"{who}：{self.reason}"
        if self.reset:
            body += f"（已复位 {'、'.join(self.reset)}）"
        if self.warnings:
            body += f"；提示：{'；'.join(self.warnings)}"
        return body


class FlameGuard:
    """明火离人的端侧守卫：管视觉信号注入、工位锁状态、刷卡复位。

    用法::

        guard = FlameGuard(engine, card_lookup=lookup)
        result = rt.tick({**telemetry, **guard.vision_frame()})
        if guard.locked_rules():
            guard.ack_by_card("04A2B3C1")      # 厨师返岗刷卡

    多灶部署时每个灶一个实例，并用 `rule_ids=` 圈定本灶的规则 id，
    刷卡复位就只解本灶的锁（工位隔离）。
    """

    #: 本功能归属的指标（`env_thresholds.metric`）
    METRIC = "flame_b"

    def __init__(
        self,
        engine: RuleEngine,
        *,
        card_lookup: Callable[[str], StaffCard | None],
        vision_timeout_sec: float = 30.0,
        clock: Callable[[], float] | None = None,
        metric: str = METRIC,
        rule_ids: Iterable[str] | None = None,
    ) -> None:
        self.engine = engine
        self.metric = metric
        # 工位作用域：None = 本 metric 全部规则（单灶部署）；给了就只看这些
        # 规则——多灶部署时每个灶一个实例，刷卡复位只解本灶的锁。
        self._rule_scope: frozenset[str] | None = (
            frozenset(rule_ids) if rule_ids is not None else None
        )
        self.vision_timeout_sec = float(vision_timeout_sec)
        self._card_lookup = card_lookup
        self._clock = clock or time.monotonic
        self._vision_at: float | None = None
        self._no_person = False
        self._headcount: int | None = None

    # ---------------------------------------------------------------- 视觉信号
    def observe_vision(
        self,
        no_person: bool,
        *,
        headcount: int | None = None,
        now: float | None = None,
    ) -> dict[str, float]:
        """登记一次视觉判定，返回要合入遥测帧的增量。

        调用方把返回值 merge 进 metrics 再喂 `engine.evaluate()`——不要塞进
        遥测采集层，那会把「AI 推断」和「传感器读数」混成一条数据流。
        """
        self._vision_at = self._clock() if now is None else float(now)
        self._no_person = bool(no_person)
        self._headcount = None if headcount is None else int(headcount)
        return self.vision_frame(now=self._vision_at)

    def vision_frame(self, now: float | None = None) -> dict[str, float]:
        """当前应喂给引擎的视觉信号。**过期时返回「有人」。**

        见模块 docstring 第 2 条：默认「无人」会让一次网络抖动变成全店停业。
        """
        if self.vision_stale(now):
            return {VISION_NO_PERSON: 0.0}
        return {VISION_NO_PERSON: 1.0 if self._no_person else 0.0}

    def vision_health(self, now: float | None = None) -> VisionHealth:
        """视觉侧是否在线。离线要**单独告警**，不能只是「不触发」。"""
        if self._vision_at is None:
            return VisionHealth(
                online=False, age_sec=float("inf"), timeout_sec=self.vision_timeout_sec,
                message="视觉信号从未到达：明火离人防护当前完全失效",
            )
        moment = self._clock() if now is None else float(now)
        age = max(0.0, moment - self._vision_at)
        if age > self.vision_timeout_sec:
            return VisionHealth(
                online=False, age_sec=age, timeout_sec=self.vision_timeout_sec,
                message=(
                    f"视觉信号已 {age:.0f}s 未更新（阈值 {self.vision_timeout_sec:.0f}s）："
                    "明火离人判定已降级为仅看火焰，不会联锁"
                ),
            )
        return VisionHealth(online=True, age_sec=age, timeout_sec=self.vision_timeout_sec)

    def vision_stale(self, now: float | None = None) -> bool:
        return self.vision_health(now).stale

    # ---------------------------------------------------------------- 工位锁
    def rules(self) -> list[Rule]:
        """本实例管辖的规则（`metric == flame_b`，且在 `rule_ids` 作用域内）。"""
        out = [rule for rule in self.engine.get_rules(include_disabled=True)
               if rule.metric == self.metric]
        if self._rule_scope is not None:
            out = [rule for rule in out if rule.id in self._rule_scope]
        return out

    def locked_rules(self) -> list[str]:
        """当前处于闩锁（= 工位被锁）的规则 id。空列表表示工位可用。"""
        return [rule.id for rule in self.rules()
                if (state := self.engine.state_store.peek(rule.id)) is not None
                and state.latched]

    @property
    def locked(self) -> bool:
        return bool(self.locked_rules())

    def require_conditions(self) -> tuple[Condition, ...]:
        """规则上配的 AND 条件（用于自检：视觉条件是不是真的挂上了）。"""
        for rule in self.rules():
            if rule.requires:
                return tuple(rule.requires)
        return ()

    # ---------------------------------------------------------------- 刷卡复位
    def ack_by_card(self, uid: str) -> CardAckResult:
        """返岗刷卡复位。只解锁本实例作用域内的 #3 工位锁（工位隔离）。"""
        uid = str(uid or "").strip()
        if not uid:
            return CardAckResult(False, None, "未读到卡号")

        card = self._card_lookup(uid)
        if card is None:
            log.warning("刷卡复位被拒：未知卡号 %s", uid)
            return CardAckResult(False, None, f"卡号 {uid} 不在工牌名册（拒绝复位）")

        targets = self.locked_rules()
        if not targets:
            return CardAckResult(
                False, card, "该工位未处于锁定状态，无需复位",
                warnings=self._card_warnings(card),
            )

        reset: list[str] = []
        reasons: list[str] = []
        warnings = list(self._card_warnings(card))
        for rule_id in targets:
            result: AckResult = self.engine.ack(rule_id, card.staff_id, by_card=True)
            if result.ok:
                reset.append(rule_id)
            else:
                reasons.append(f"{rule_id}: {result.reason}")

        if reset:
            # 刷卡本身就是「有人回来了」的证据，同步视觉侧状态，避免下一帧
            # 又把工位锁回去（否则厨师刷完卡，5 秒后又响一次）
            self._no_person = False
            self._headcount = max(1, self._headcount or 1)
            self._vision_at = self._clock()
            log.info("刷卡复位：card=%s staff=%s rules=%s", uid, card.staff_id, reset)
            return CardAckResult(
                True, card, "已复位", tuple(reset), tuple(warnings),
            )
        return CardAckResult(
            False, card, "；".join(reasons) or "复位失败", (), tuple(warnings),
        )

    def _card_warnings(self, card: StaffCard) -> list[str]:
        if not card.expired:
            return []
        return [
            "该员工健康证已过期（#9）——本次复位已放行：现场处置属安全事项，"
            "合规问题由后台追责，不应阻断复位"
        ]

    # ---------------------------------------------------------------- 观测
    def snapshot(self, now: float | None = None) -> dict:
        health = self.vision_health(now)
        return {
            "metric": self.metric,
            "locked": self.locked,
            "locked_rules": self.locked_rules(),
            "vision": {
                "online": health.online,
                "age_sec": round(health.age_sec, 1) if health.age_sec != float("inf") else None,
                "timeout_sec": health.timeout_sec,
                "no_person": self._no_person,
                "headcount": self._headcount,
                "message": health.message,
            },
            "requires": [
                {"metric": cond.metric, "op": cond.op, "threshold": cond.threshold}
                for cond in self.require_conditions()
            ],
        }
