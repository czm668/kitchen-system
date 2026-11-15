# -*- coding: utf-8 -*-
"""三条保守保护（A6-03）：不依赖规则引擎的兜底联锁。

## 为什么需要「不经过规则引擎」的保护

规则引擎的输入是**配置**：`env_thresholds` 表、档位表、动作语义。配置是会变的，
而每一种「变」都可能让保护静默失效：

| 配置异常 | 引擎的行为 | 后果 |
|---|---|---|
| 管理端把某条规则 `enabled=false` | 该规则不参与判定 | 燃气泄漏规则被误停 → 泄漏不报警 |
| 档位表少了 `closed` 档 | 切档后一条规则都不匹配 | 收档后全店无保护 |
| 动作语义被写成只播报 | 判定照常、`actions` 里没有 `gas_valve_close` | 有告警、阀不动 |
| 配置下发失败，用了上一版 | 阈值停在旧值 | 新装设备阈值全错 |

这四条都不是「代码 bug」，配置校验也拦不住（它们都是**合法配置**）。
所以最关键的三个保护必须有一条**不读配置**的实现路径：阈值写死在代码里，
输入只有传感器读数，输出直接是联锁动作。

代价是阈值不能现场调。这个代价可以接受——写死的三个值是「物理上不该出现」
（燃气浓度快炸了、罩温 280℃、火连续烧一小时没人管），不需要按店标定。

## 三条各自的入口条件

### ① 燃气突变 → 立即关阀（`GUARD_GAS_SURGE`）

`#1` 的主通道是「突变率 4 倍、确认窗口 30 秒」，明火期还放宽到 30 秒。
30 秒对「厨房里有明火在烧、燃气正在泄漏」这个组合太长了，所以这里补一条：
**越过泄漏门限或基线 2 倍，连续 2 个采样点就关阀**。

为什么不要求 1 个采样点就关：MQ-2 是模拟量，继电器吸合瞬间、油烟机启动、
甚至有人喷了酒精都会造成单点尖峰。一个尖峰关一次阀，现场第一天就会把
这个功能拆掉。2 个连续采样点（≈1 秒）仍然远快于 30 秒，且能滤掉单点噪声。

### ② 排烟/消毒超温 → 强制开风机（`GUARD_THERMAL_FAN`）

罩温 280℃ / 消毒柜 130℃ 是**设备损坏的边界**，不是舒适度指标。所以：

- 阈值写死、不看档位（营业档 280、收档档 180 的分档只属于规则引擎）；
- 只开 CH2 排风，不碰燃气阀、不切工位电——超温不是泄漏，误停业的代价更大；
- 回落要留有 5℃ 余量（滞回），否则在 280 上下反复 trip/release，
  继电器每几秒吸合一次，触点很快粘死。

### ③ 明火长烧 + 无人 → 关气源（`GUARD_FLAME_UNATTENDED`）

`#3` 只管「12 秒内没人」：厨师去接个电话、上个厕所就会触发，
所以它需要视觉信号、需要刷卡复位，语义是「你人回来」。

但有一种场景它管不到：**火焰在烧、人一直不回来、视觉又刚好离线**
（`vision_stale` 时视觉按「有人」处理，见 `safety/flame.py`）。
于是火焰可以烧一整夜。这条补的就是这个洞：

- 火焰连续 `flame_hold_sec`（默认 3600s，演示 `time_scale=60` → 60s）；
- 期间**没有任何刷卡或按键**（`mark_human()` 记录最后一次人工动作）；
- 触发 → 关气源 + 开 `unattended` 工单（不是简单报警：这是要追责的）。

计时起点取「火焰开始」与「最后一次人工动作」中**较晚**的那个。只看「火焰开始
后无人刷卡」会漏掉一种情况：火已经烧了两小时，厨师刚刷卡关掉一次报警又走了，
之后一小时无人——按火焰起点算早就该触发，却因为「中途刷过卡」被清零了。

## 触发后一定要能释放

三条保护都只在**状态跃迁**时产出 `Verdict`（触发一次、回落一次），不在中间
每帧重复产出。回落必须产出 `EVENT_RELEASED`，否则该保护在互锁通道上的
触发源永远不注销——`restore()` 会一直返回「还有触发源没恢复」，现场表现是
**阀关了再也打不开**，只能重启设备。这条比重复报警严重得多，单测里有专门一条。
"""
from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..rules.engine import EVENT_FIRED, EVENT_LATCHED, EVENT_RELEASED, Verdict

log = logging.getLogger("kitchen.resilience.guards")

__all__ = [
    "GUARD_FLAME_UNATTENDED",
    "GUARD_GAS_SURGE",
    "GUARD_THERMAL_FAN",
    "GuardPolicy",
    "GuardState",
    "ConservativeGuards",
]

GUARD_GAS_SURGE = "guard:gas_surge"
GUARD_THERMAL_FAN = "guard:thermal_fan"
GUARD_FLAME_UNATTENDED = "guard:flame_unattended"

#: 火焰「有效」的判据：与 `#3` 的 `flame_b > 0.5` 保持一致
FLAME_ON = 0.5


@dataclass(frozen=True)
class GuardPolicy:
    """三条保护的写死阈值（不读配置，见模块 docstring）。"""

    # ① 燃气突变
    gas_metric: str = "gas_level"
    gas_surge_ratio: float = 2.0
    gas_surge_abs: float = 0.55
    #: 基线低于这个值时比值法没有意义（0.01→0.03 只是噪声）
    gas_baseline_floor: float = 0.02
    #: 确认所需的连续采样点数（滤单点尖峰，约 1 秒）
    gas_confirm_samples: int = 2
    #: 基线窗口：用窗口之前的中位数做基线
    gas_window_sec: float = 60.0
    # ② 超温
    hood_metric: str = "hood_temp"
    hood_overheat: float = 280.0
    sterilize_metric: str = "sterilize_temp"
    sterilize_overheat: float = 130.0
    #: 滞回余量（℃）：回落要低于阈值 5℃ 才解除，防继电器反复吸合
    thermal_hysteresis: float = 5.0
    # ③ 明火长烧无人
    flame_metric: str = "flame_b"
    flame_hold_sec: float = 3600.0
    #: 演示加速：60 → 60 分钟压成 60 秒（与留样/消毒的 time_scale 同一口径）
    time_scale: float = 1.0

    def __post_init__(self) -> None:
        if self.time_scale <= 0:
            raise ValueError(f"time_scale 必须 > 0，实际 {self.time_scale}")
        if self.gas_confirm_samples < 1:
            raise ValueError("gas_confirm_samples 必须 >= 1")
        if self.gas_surge_ratio <= 1.0:
            raise ValueError(f"gas_surge_ratio 必须 > 1，实际 {self.gas_surge_ratio}")

    @property
    def hold_effective_sec(self) -> float:
        """折算后的实际持有时间（演示模式据此压缩）。"""
        return float(self.flame_hold_sec) / float(self.time_scale)


@dataclass(frozen=True)
class GuardState:
    """一条保护的可观测状态（给 TFT / Web 的「保护是否在岗」用）。"""

    guard: str
    tripped: bool
    metric: str
    value: float
    threshold: float
    reason: str = ""
    elapsed: float = 0.0

    def describe(self) -> str:
        mark = "已触发" if self.tripped else "正常"
        return f"{self.guard}：{mark}（{self.metric}={self.value:.2f}，门限 {self.threshold:.2f}）"

    def to_payload(self) -> dict[str, Any]:
        return {
            "guard": self.guard,
            "tripped": self.tripped,
            "metric": self.metric,
            "value": round(self.value, 4),
            "threshold": round(self.threshold, 4),
            "reason": self.reason,
            "elapsed": round(self.elapsed, 1),
        }


@dataclass
class _GasWindow:
    #: **只收「不越绝对门限」的样本**。把正在泄漏的读数算进基线，
    #: 基线会被自己拉高，第二次采样时突变率反而判不出来（真实踩过的坑）。
    baseline_samples: list[tuple[float, float]] = field(default_factory=list)
    streak: int = 0
    tripped: bool = False
    since: float = 0.0


class ConservativeGuards:
    """三条保守保护的执行体。`evaluate()` 每帧调一次，产出 `Verdict`。

    用法::

        guards = ConservativeGuards()
        verdicts = guards.evaluate({**telemetry, "flame_b": 1.0}, now=12.0)
        frame = supervisor.apply(FakeResult(verdicts))   # 与规则引擎同一套互锁
    """

    def __init__(
        self,
        policy: GuardPolicy | None = None,
        *,
        baseline: float | None = None,
        clock=None,
    ) -> None:
        self.policy = policy or GuardPolicy()
        self._clock = clock or time.monotonic
        #: 燃气基线（NVS 周更值的注入点）；None 时用窗口内的历史中位数自建
        self.baseline = baseline
        self._gas = _GasWindow()
        self._thermal_tripped: dict[str, bool] = {}
        self._thermal_since: dict[str, float] = {}
        self._flame_since: float | None = None
        self._flame_tripped = False
        #: 用 None 而不是 0.0 表示「还没登记过」：0.0 是假值，
        #: `if not self._last_human` 会在每个 now=0 开头的用例里被反复重置。
        self._last_human: float | None = None
        self._human_source = ""
        self._last_now = 0.0
        self._states: dict[str, GuardState] = {}

    # ---------------------------------------------------------------- 人工活动
    def mark_human(self, source: str = "card", *, now: float | None = None) -> None:
        """登记一次人工活动（刷卡 / 按键）。#3 长烧保护据此判定「有人看管」。"""
        moment = float(self._clock() if now is None else now)
        self._last_human = moment
        self._human_source = str(source or "card")
        if self._flame_tripped:
            log.info("检测到人工活动（%s），明火长烧保护解除计时", self._human_source)

    @property
    def last_human_at(self) -> float:
        return 0.0 if self._last_human is None else self._last_human

    # ---------------------------------------------------------------- 主入口
    def evaluate(
        self,
        metrics: Mapping[str, float],
        *,
        now: float | None = None,
    ) -> tuple[Verdict, ...]:
        """跑三条保护，返回本帧的**跃迁**事件（触发 / 回落各一条）。"""
        moment = float(self._clock() if now is None else now)
        self._last_now = moment
        if self._last_human is None:
            # 未登记过人工活动时，以「本帧」为起点：设备刚上电不能算「已无人一小时」
            self._last_human = moment
        out: list[Verdict] = []
        out.extend(self._gas_guard(metrics, moment))
        out.extend(self._thermal_guard(metrics, moment))
        out.extend(self._flame_guard(metrics, moment))
        return tuple(out)

    # ---------------------------------------------------------------- ① 燃气
    def _gas_guard(self, metrics: Mapping[str, float], now: float) -> list[Verdict]:
        policy = self.policy
        raw = metrics.get(policy.gas_metric)
        if raw is None:
            return []
        value = float(raw)
        window = self._gas
        abs_limit = float(policy.gas_surge_abs)
        cutoff = now - policy.gas_window_sec
        while window.baseline_samples and window.baseline_samples[0][0] < cutoff:
            window.baseline_samples.pop(0)
        if value < abs_limit:
            window.baseline_samples.append((now, value))

        baseline = self.baseline
        if baseline is None:
            values = [item for _ts, item in window.baseline_samples]
            baseline = statistics.median(values) if values else 0.0
        # 两个门限取「更容易触发」的那个：绝压门限（现场标定）与基线倍数
        ratio_limit = (
            baseline * policy.gas_surge_ratio
            if baseline >= policy.gas_baseline_floor
            else float("inf")
        )
        over_abs = value >= abs_limit
        over_ratio = value >= ratio_limit
        over = over_abs or over_ratio
        threshold = min(abs_limit, ratio_limit)
        detail = ""
        if over:
            if over_ratio:
                ratio = value / baseline if baseline else 0.0
                detail = (
                    f"较基线 {baseline:.3f} 突增 {ratio:.1f} 倍"
                    f"（门限 {policy.gas_surge_ratio} 倍）"
                )
            if over_abs:
                detail = (detail + "；" if detail else "") + f"超过泄漏门限 {abs_limit:.2f}"
        window.streak = window.streak + 1 if over else 0
        out: list[Verdict] = []
        if not window.tripped and window.streak >= policy.gas_confirm_samples:
            window.tripped = True
            window.since = now
            log.warning(
                "保守保护①触发：%s=%.3f，%s → 立即关阀", policy.gas_metric, value, detail,
            )
            out.append(self._verdict(
                GUARD_GAS_SURGE, policy.gas_metric, EVENT_FIRED, value, value, 2,
                f"燃气突变立即关阀：{detail}",
                ("gas_valve_close",), 0.0,
            ))
        elif window.tripped and value <= threshold * 0.8:
            # 回落到门限 80% 以下才解除（滞回，防止在门限附近反复关/开）
            window.tripped = False
            window.streak = 0
            log.info("保守保护①解除：%s=%.3f 已回落", policy.gas_metric, value)
            out.append(self._verdict(
                GUARD_GAS_SURGE, policy.gas_metric, EVENT_RELEASED, value, value, 2,
                "燃气读数已回落", (), now - window.since,
            ))
        # 快照在跃迁之后写（与超温保护同一口径）
        self._states[GUARD_GAS_SURGE] = GuardState(
            guard=GUARD_GAS_SURGE, tripped=window.tripped,
            metric=policy.gas_metric, value=value, threshold=threshold,
            reason=detail or "读数在门限内",
            elapsed=max(0.0, now - window.since) if window.tripped else 0.0,
        )
        return out

    # ---------------------------------------------------------------- ② 超温
    def _thermal_guard(self, metrics: Mapping[str, float], now: float) -> list[Verdict]:
        policy = self.policy
        out: list[Verdict] = []
        pairs = (
            (policy.hood_metric, policy.hood_overheat, "排烟罩"),
            (policy.sterilize_metric, policy.sterilize_overheat, "消毒柜"),
        )
        for metric, limit, label in pairs:
            raw = metrics.get(metric)
            if raw is None:
                continue
            value = float(raw)
            tripped = self._thermal_tripped.get(metric, False)
            if not tripped and value >= limit:
                self._thermal_tripped[metric] = True
                self._thermal_since[metric] = now
                tripped = True
                log.warning(
                    "保守保护②触发：%s=%.1f℃ ≥ %.1f℃ → 强制开风机",
                    metric, value, limit,
                )
                out.append(self._verdict(
                    GUARD_THERMAL_FAN, metric, EVENT_FIRED, value, value, 2,
                    f"{label}超温（{value:.1f}℃ ≥ {limit:.1f}℃），强制开启排风",
                    ("fan_on",), 0.0,
                ))
            elif tripped and value <= limit - policy.thermal_hysteresis:
                self._thermal_tripped[metric] = False
                tripped = False
                since = self._thermal_since.pop(metric, now)
                log.info("保守保护②解除：%s=%.1f℃ 已回落", metric, value)
                out.append(self._verdict(
                    GUARD_THERMAL_FAN, metric, EVENT_RELEASED, value, value, 2,
                    f"{label}温度回落至 {value:.1f}℃", (), now - since,
                ))
            # 快照**在跃迁之后**写：先写再用同一帧的 tripped 更新，会永远慢一帧
            self._states[f"{GUARD_THERMAL_FAN}:{metric}"] = GuardState(
                guard=GUARD_THERMAL_FAN, tripped=tripped,
                metric=metric, value=value, threshold=limit,
                reason=(
                    f"{label}超温，已强制排风" if tripped
                    else f"{label}温度在安全边界内"
                ),
                elapsed=max(0.0, now - self._thermal_since.get(metric, now)) if tripped else 0.0,
            )
        return out

    # ---------------------------------------------------------------- ③ 长烧无人
    def _flame_guard(self, metrics: Mapping[str, float], now: float) -> list[Verdict]:
        policy = self.policy
        raw = metrics.get(policy.flame_metric)
        if raw is None:
            return []
        value = float(raw)
        burning = value >= FLAME_ON
        if not burning:
            if self._flame_tripped:
                since = self._flame_since or now
                self._flame_tripped = False
                self._flame_since = None
                log.info("保守保护③解除：火焰已熄灭")
                self._states[GUARD_FLAME_UNATTENDED] = GuardState(
                    guard=GUARD_FLAME_UNATTENDED, tripped=False,
                    metric=policy.flame_metric, value=value,
                    threshold=policy.hold_effective_sec, reason="火焰已熄灭",
                )
                return [self._verdict(
                    GUARD_FLAME_UNATTENDED, policy.flame_metric, EVENT_RELEASED,
                    value, value, 2, "明火已熄灭，长烧保护解除", (), now - since,
                )]
            self._flame_since = None
            self._states[GUARD_FLAME_UNATTENDED] = GuardState(
                guard=GUARD_FLAME_UNATTENDED, tripped=False,
                metric=policy.flame_metric, value=value,
                threshold=policy.hold_effective_sec, reason="无明火",
            )
            return []
        if self._flame_since is None:
            self._flame_since = now
        # 计时起点取「火焰开始」与「最后一次人工活动」中较晚者
        start = max(self._flame_since, self.last_human_at)
        held = now - start
        limit = policy.hold_effective_sec
        fired = not self._flame_tripped and held >= limit
        if fired:
            self._flame_tripped = True
            log.warning(
                "保守保护③触发：明火已连续 %.0fs（门限 %.0fs）且无人工活动 → 关气源",
                held, limit,
            )
        self._states[GUARD_FLAME_UNATTENDED] = GuardState(
            guard=GUARD_FLAME_UNATTENDED, tripped=self._flame_tripped,
            metric=policy.flame_metric, value=value, threshold=limit,
            reason=(
                "明火长烧且无人工活动，已关闭气源" if self._flame_tripped
                else f"明火已持续 {held:.0f}s / {limit:.0f}s，期间无刷卡或按键"
            ),
            elapsed=held,
        )
        if not fired:
            return []
        return [self._verdict(
            GUARD_FLAME_UNATTENDED, policy.flame_metric, EVENT_FIRED, value, value, 2,
            f"疑似长时间无人看管：明火已连续 {held / 60:.0f} 分钟无刷卡或按键，关闭气源",
            ("gas_valve_close",), held,
            ticket="unattended",
        )]

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _verdict(
        guard: str,
        metric: str,
        event: str,
        value: float,
        raw: float,
        level: int,
        message: str,
        actions: tuple[str, ...],
        elapsed: float,
        *,
        ticket: str = "",
    ) -> Verdict:
        return Verdict(
            rule_id=guard,
            metric=metric,
            event=event,
            value=float(value),
            raw=float(raw),
            level=level,
            message=message,
            actions=tuple(actions),
            latched=event in (EVENT_FIRED, EVENT_LATCHED),
            elapsed=float(elapsed),
            ticket=ticket,
        )

    # ---------------------------------------------------------------- 观测
    def states(self) -> tuple[GuardState, ...]:
        return tuple(self._states.values())

    def tripped(self) -> tuple[str, ...]:
        """当前处于触发状态的保护。读内部标志而不是 `_states`：快照是上一帧写的，
        用它判断「现在有没有触发」会永远慢一帧，调用方拿去决策就会漏一拍。
        """
        out: list[str] = []
        if self._gas.tripped:
            out.append(GUARD_GAS_SURGE)
        if any(self._thermal_tripped.values()):
            out.append(GUARD_THERMAL_FAN)
        if self._flame_tripped:
            out.append(GUARD_FLAME_UNATTENDED)
        return tuple(out)

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        moment = float(self._clock() if now is None else now)
        return {
            "policy": {
                "gas_surge_ratio": self.policy.gas_surge_ratio,
                "gas_surge_abs": self.policy.gas_surge_abs,
                "hood_overheat": self.policy.hood_overheat,
                "sterilize_overheat": self.policy.sterilize_overheat,
                "flame_hold_sec": self.policy.flame_hold_sec,
                "time_scale": self.policy.time_scale,
                "hold_effective_sec": round(self.policy.hold_effective_sec, 1),
            },
            "baseline": None if self.baseline is None else round(self.baseline, 4),
            "tripped": list(self.tripped()),
            "guards": [state.to_payload() for state in self.states()],
            "last_human_at": round(self.last_human_at, 1),
            "human_source": self._human_source,
            "silent_sec": round(max(0.0, moment - self.last_human_at), 1),
        }
