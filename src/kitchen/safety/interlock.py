# -*- coding: utf-8 -*-
"""互锁编排层（A2-08）：把「规则引擎的判定」接到「三通道执行对象」上。

## 为什么还要一层

引擎已经算出了「谁该动」，`channels.InterlockController` 已经会「谁不许动」，
但两者之间没人负责把 `Verdict` 翻译成 `request(action, source=...)`。没有这一层
的现实后果：

1. **触发源没有名字**。`controller.request("fan_on")` 不传 `source` 时一律记成
   `anonymous`，三个功能先后开排风，后来只恢复了一个，`anonymous` 照样能凑成
   「全部恢复」→ 风机在燃气还在漏的时候被关掉。所以 `source` 必须是
   `rule_id`，一个功能一个源，缺一不可。
2. **重复下发**。引擎每 `cooldown_sec`（60 秒）重报一次闩锁事件，每次都带
   `fan_on`。不去重就是每 60 秒给继电器重发一次「开」，日志全是噪声，而且
   MQTT 抖动时极易出现「开-关-开」的抖动。
3. **恢复没人管**。契约要求恢复动作**必须人工确认**，引擎在 `released` 时会
   主动把动作清空（见 `engine._verdict`），这时谁来接受那个「确认」按钮？

## 三条硬规则（与契约 §2.3 对齐）

1. **任一触发即开**：任何源的 trip 动作都登记为通道触发源，不管通道当前状态；
2. **全部恢复才关**：只要还有一个源没 `note_recovered`，恢复动作一律被拒
   （`InterlockBlocked`，不是静默忽略——要让调用方知道为什么没动）；
3. **高优先级占用期间低优先级不得释放同一通道**：因为通道是共用的（CH2 同时
   服务燃气、排烟、温湿度），低优先级「我好了」不等于通道可以恢复。这也是
   为什么恢复请求不携带优先级——恢复的判据是「通道还有没有源」，不是「谁的源」。

## CH3 为什么先不接

A2-08 的范围明确是 CH1/CH2：#16 视觉违规的 `power_cut` 要等 A5（视觉算法与
双通道复核 UI）。本层对 CH3 是**通**的（`InterlockController` 已经实现了
「断电即断 + Web/TFT 双重复核才能恢复」），只是默认不给它派源；等 A5 接入时
只需在 `PRIORITY_BY_METRIC` 里加一行，不改本文件。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ..channels import (
    PRIORITY_ENV_COMFORT,
    PRIORITY_EQUIPMENT,
    PRIORITY_FLAME_UNATTENDED,
    PRIORITY_GAS_LEAK,
    PRIORITY_HOOD_HEAT,
    PRIORITY_MANUAL,
    PRIORITY_VISION_VIOLATION,
    ActionBinding,
    Channel,
    InterlockBlocked,
    InterlockController,
    RELAY_CHANNELS,
    resolve_action,
)
from ..rules.engine import EVENT_FIRED, EVENT_LATCHED, EVENT_RELEASED, Verdict

log = logging.getLogger("kitchen.interlock")

__all__ = [
    "ActionDecision",
    "InterlockFrame",
    "InterlockSupervisor",
    "PRIORITY_BY_METRIC",
    "priority_of",
]

#: 指标 → 互锁优先级。**按 metric 而不是按 rule_id**，因为同一指标有多个
#: 档位/等级的规则行（#2 三档、#5 两级），它们抢的是同一个通道，优先级必须
#: 一致——否则「营业档的积热」和「收档档的积热」会给出不同的抢占能力，纯属事故。
PRIORITY_BY_METRIC: dict[str, int] = {
    "gas_level": PRIORITY_GAS_LEAK,
    "flame_a": PRIORITY_FLAME_UNATTENDED,
    "flame_b": PRIORITY_FLAME_UNATTENDED,
    "hood_temp": PRIORITY_HOOD_HEAT,
    "sterilize_temp": PRIORITY_EQUIPMENT,
    "fridge_temp": PRIORITY_EQUIPMENT,
    "freezer_temp": PRIORITY_EQUIPMENT,
    "sample_temp": PRIORITY_EQUIPMENT,
    "t": PRIORITY_ENV_COMFORT,
    # 视觉违规（#16）先登记，等 A5 的 power_cut 进来就能直接排上
    "vision_violation": PRIORITY_VISION_VIOLATION,
}

#: 通道 → 恢复动作（契约里每个通道恰好一个 restore）
RESTORE_ACTION: dict[Channel, str] = {
    Channel.CH1: "gas_valve_open",
    Channel.CH2: "fan_off",
    Channel.CH3: "power_restore",
}

#: **事件型**动作：重复下发本来就是它的语义（每次都该播一遍），不能按「通道
#: 已处于该状态」去重。声光开关是状态型，播报是事件型——这个区别不写出来，
#: 半年后一定有人把 `voice_play` 也加进去，于是「报警只响第一次」。
EVENT_ACTIONS: frozenset[str] = frozenset({"voice_play"})


def priority_of(metric: str) -> int:
    """未登记的指标按最低优先级处理，但**不会**因此绕开互锁。"""
    return PRIORITY_BY_METRIC.get(str(metric), PRIORITY_MANUAL)


@dataclass(frozen=True)
class ActionDecision:
    """单个动作的裁决结果。`ok=False` 时 `detail` 说明为什么没下发。"""

    action: str
    channel: str
    source: str
    priority: int
    ok: bool
    duplicate: bool = False
    detail: str = ""

    def describe(self) -> str:
        if self.duplicate:
            # 重复请求不是错误，但也别让日志看起来像「又下发了一次」
            return (f"{self.action}（{self.channel}）："
                    f"{self.detail or '重复请求，已跳过'}")
        mark = "下发" if self.ok else "拦截"
        body = f"{mark} {self.action}（{self.channel}，源 {self.source}，P{self.priority}）"
        return f"{body}：{self.detail}" if self.detail else body


@dataclass
class InterlockFrame:
    """一帧的互锁结果。`applied` 才是真正该发给执行器的动作序列。"""

    decisions: tuple[ActionDecision, ...] = ()
    applied: tuple[str, ...] = ()
    blocked: tuple[ActionDecision, ...] = ()
    recovered: tuple[tuple[str, str], ...] = ()   # (channel, source)
    channels: dict[str, dict] = field(default_factory=dict)

    @property
    def pending_restore(self) -> dict[str, list[str]]:
        """已经没有任何触发源、可以人工恢复的通道 → 该通道的历史触发源。"""
        return {
            name: body.get("last_sources", [])
            for name, body in self.channels.items()
            if body.get("state") == "releasing" and not body.get("sources")
        }

    def to_payload(self, device_id: str = "") -> dict:
        body = {
            "actions": list(self.applied),
            "blocked": [
                {"action": item.action, "channel": item.channel, "source": item.source,
                 "reason": item.detail}
                for item in self.blocked
            ],
            "channels": self.channels,
        }
        if self.pending_restore:
            body["pending_restore"] = self.pending_restore
        if device_id:
            body["device_id"] = device_id
        return body


class InterlockSupervisor:
    """把 `TickResult` 落到三通道上的编排器（有状态，全在内存）。

    用法::

        sup = InterlockSupervisor()
        result = rt.tick(metrics)
        frame = sup.apply(result)                 # 触发源登记 + 动作下发
        for action in frame.applied:              # 真正写继电器/MQTT
            dispatch(action)
        sup.restore("CH2", reviewer="web:张三")    # 人工确认恢复
    """

    def __init__(
        self,
        controller: InterlockController | None = None,
        *,
        priority_fn: Callable[[str], int] | None = None,
        device_id: str | None = None,
    ) -> None:
        self.controller = controller or InterlockController()
        # 参数名刻意不叫 priority_of：同名会把模块级函数遮住，默认值变成 None
        self._priority_of = priority_fn or priority_of
        if device_id:
            self.controller.device_id = device_id
        #: 每个通道最近一次清空前的触发源，给「可以恢复了」的提示用
        self._last_sources: dict[Channel, list[str]] = {c: [] for c in RELAY_CHANNELS}
        #: 每个通道**最后一次下发到的状态**（`(通道, 状态键) → 状态值`），用于
        #: 抑制重复命令。三路功能先后都要开排风，第二、三路再发一次 `fan_on`
        #: 是纯噪声——继电器已经吸合，重发只会制造「开-开-开」的日志抖动。
        #: 但触发源照旧登记，否则「全部恢复才关」就少了一个源。
        #: 键里带 `state_key` 而不是只用通道：GPIO_SIGNAL 上挂着声光与语音两个
        #: 互不相干的量，只用通道会把它们的状态互相覆盖。
        self._commanded: dict[tuple[Channel, str], str] = {}

    # ------------------------------------------------------------ 主流程
    def apply(self, result) -> InterlockFrame:
        """消费一帧 `TickResult`，返回该下发的动作 + 拦截原因 + 通道状态。"""
        decisions: list[ActionDecision] = []
        applied: list[str] = []
        recovered: list[tuple[str, str]] = []

        for verdict in getattr(result, "verdicts", ()) or ():
            if verdict.event == EVENT_RELEASED:
                recovered.extend(self._release(verdict))
                continue
            if verdict.event not in (EVENT_FIRED, EVENT_LATCHED):
                continue
            for decision in self._trip(verdict):
                decisions.append(decision)
                if decision.ok and not decision.duplicate and decision.action not in applied:
                    applied.append(decision.action)

        return InterlockFrame(
            decisions=tuple(decisions),
            applied=tuple(applied),
            blocked=tuple(item for item in decisions if not item.ok),
            recovered=tuple(recovered),
            channels=self.channel_view(),
        )

    def _trip(self, verdict: Verdict) -> list[ActionDecision]:
        priority = self._priority_of(verdict.metric)
        out: list[ActionDecision] = []
        for action in verdict.actions:
            binding: ActionBinding = resolve_action(action)
            # 声光/语音走独立 GPIO，不受互锁优先级、不占通道（契约 §2.3 ④）
            if binding.is_signal:
                out.append(self._signal(binding, verdict.rule_id, priority))
                continue
            try:
                # 触发源**必须**登记，即使命令会被去重：否则这个功能在通道上的
                # 占用记录就丢了，「全部恢复才关」会提前放行。
                self.controller.request(
                    action, source=verdict.rule_id, priority=priority,
                )
            except InterlockBlocked as exc:      # trip 一般不会走到，留着兜底
                out.append(ActionDecision(action, binding.channel.value, verdict.rule_id,
                                          priority, False, detail=str(exc)))
                continue
            self._last_sources[binding.channel] = self._merge_source(
                self._last_sources[binding.channel], verdict.rule_id,
            )
            out.append(self._emit(binding, verdict.rule_id, priority))
        return out

    def _signal(self, binding: ActionBinding, source: str, priority: int) -> ActionDecision:
        """声光/语音：不进互锁，只做「别重复开灯」这一层去重。"""
        return self._emit(binding, source, priority)

    def _emit(self, binding: ActionBinding, source: str, priority: int) -> ActionDecision:
        """真正下发前的最后一道去重：状态型动作已在目标状态就不重发。"""
        if binding.action in EVENT_ACTIONS:
            self._commanded[(binding.channel, binding.state_key)] = binding.state_value
            return ActionDecision(binding.action, binding.channel.value, source,
                                  priority, True)
        key = (binding.channel, binding.state_key)
        if self._commanded.get(key) == binding.state_value:
            return ActionDecision(
                binding.action, binding.channel.value, source, priority, True,
                duplicate=True,
                detail=f"{binding.state_key} 已处于 {binding.state_value}，命令无需重复下发",
            )
        self._commanded[key] = binding.state_value
        return ActionDecision(binding.action, binding.channel.value, source, priority, True)

    def _release(self, verdict: Verdict) -> list[tuple[str, str]]:
        """规则回落 → 注销它的触发源。**不发恢复动作**（要人工确认）。"""
        out: list[tuple[str, str]] = []
        for channel in self._sources_of(verdict.rule_id):
            self.controller.note_recovered(channel, verdict.rule_id)
            out.append((channel.value, verdict.rule_id))
        return out

    def _sources_of(self, rule_id: str) -> list[Channel]:
        """该规则当前还占着哪些通道（从 controller 反查，不另存一份状态）。"""
        return [channel for channel in RELAY_CHANNELS
                if rule_id in self.controller.active_sources(channel)]

    # ------------------------------------------------------------ 人工恢复
    def restore(
        self,
        channel: Channel | str,
        reviewer: str = "",
        *,
        manual_confirmed: bool = True,
        action: str | None = None,
    ) -> ActionDecision:
        """人工确认后恢复一个通道。被拒时返回 `ok=False` 而不是抛异常。

        调用方（Web 确认按钮 / TFT 刷组长牌）需要拿到具体原因给用户看，
        抛异常会迫使每个调用点都写一层 try。
        """
        target = Channel(channel)
        restore_action = action or RESTORE_ACTION[target]
        reviewer = str(reviewer or "").strip()
        priority = PRIORITY_MANUAL
        if not reviewer:
            return ActionDecision(restore_action, target.value, "-", priority, False,
                                  detail="恢复动作必须记录复核人（留痕）")
        if target is Channel.CH3:
            # CH3 的恢复要 Web + TFT 双通道复核，reviewer 直接写通道名
            self.controller.confirm_manual_reset(target, reviewer)
        wanted = resolve_action(restore_action).state_value
        binding = resolve_action(restore_action)
        if self._commanded.get((target, binding.state_key)) == wanted:
            # 已经是目标状态：不必再发一条命令（现场表现为继电器「咔」两下）
            return ActionDecision(restore_action, target.value, reviewer, priority, True,
                                  duplicate=True,
                                  detail=f"{binding.state_key} 已处于 {wanted}，无需重复下发"
                                         f"（by {reviewer}）")
        try:
            self.controller.request(restore_action, manual_confirmed=manual_confirmed)
        except InterlockBlocked as exc:
            log.info("恢复被拒：%s（%s）", restore_action, exc)
            return ActionDecision(restore_action, target.value, reviewer, priority,
                                  False, detail=str(exc))
        self._commanded[(target, binding.state_key)] = wanted
        # 通道恢复后清掉历史触发源：否则「可恢复」提示会一直挂着上一轮的源名
        self._last_sources[target] = []
        log.info("通道恢复：%s by %s", target.value, reviewer)
        return ActionDecision(restore_action, target.value, reviewer, priority, True,
                              detail=f"by {reviewer}")

    # ------------------------------------------------------------ 观测
    def channel_view(self) -> dict[str, dict]:
        view = self.controller.snapshot()
        for channel in RELAY_CHANNELS:
            body = view[channel.value]
            body["last_sources"] = list(self._last_sources.get(channel, ()))
            body["restore_action"] = RESTORE_ACTION[channel]
        return view

    def snapshot(self) -> dict:
        return {
            "channels": self.channel_view(),
            "degraded": self.controller.degraded,
            "pending_restore": {
                name: body["last_sources"]
                for name, body in self.channel_view().items()
                if body["state"] == "releasing" and not body["sources"]
            },
        }

    def reset(self) -> None:
        """测试 / 现场检修：清空全部通道状态与去重记录。"""
        for channel in RELAY_CHANNELS:
            self.controller.reset_channel(channel)
        self._commanded.clear()
        self._last_sources = {c: [] for c in RELAY_CHANNELS}

    @staticmethod
    def _merge_source(existing: Sequence[str], source: str) -> list[str]:
        out = [item for item in existing if item != source]
        out.append(source)
        return out
