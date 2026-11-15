# -*- coding: utf-8 -*-
"""端侧优先运行时（A6-01 装配层）：一帧 = 判定 → 执行 → 入队。

## 装配顺序固定在这一层

和 `runtime.py` 一样，这里解决的是「模块都对、装起来错」：

```
三条保守保护（不看配置）
      ↓  Verdict
规则引擎 tick（看配置 + 档位）
      ↓  Verdict + actions + tickets
互锁监督器 apply（登记触发源、去重、人工确认）
      ↓  frame.applied
   执行器（继电器 / 语音 / 声光）
      ↓
本地队列（断网时攒着，联网后补传）
```

顺序不能调换的两个理由：

1. **保守保护必须在引擎之前**：引擎的 `Verdict` 会按 `rule_id` 占通道触发源，
   保护抢先一个动作不会互相覆盖，但如果引擎先跑、保护后跑，`#1` 的 30 秒确认
   窗口期间「通道已经 latched」这件事就得靠保护自己再判断一次；
2. **队列必须在执行之后**：先入队再执行，执行失败时队列里会留下「已上报、
   实际没执行」的记录，页面显示阀关了、现场阀是开的。执行成功后才入队，
   入队的语义才是「已经发生的事实」。

## 一条不能越过的线：`tick()` 里不许做网络 IO

补传是 `flush()`，由调用方在**自己的循环里**调，不挂在判定路径上。

理由很具体：`#1` 燃气泄漏的确认窗口是 30 秒、`#3` 是 12 秒，而一次「网络不通」
的 HTTP 超时默认就是 3~10 秒。如果 tick 里同步上传，网络半通不通时每一帧都
卡几秒，采样间隔被拉长到十几秒——**保护还在，但确认窗口全乱了**。
离线优先的整个意义就是「网络差了不影响本地」，别在最后一步把它接回去。

## 断网只影响两件事

- 数据进队列而不是直传；
- 降级报告里出现「离线模式」。

判定、联锁、动作下发、工单开立，全部与网络状态无关。单测里有一条专门断言：
`online=False` 时燃气突变照样 `gas_valve_close`。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..rules.engine import Verdict
from ..safety.interlock import InterlockFrame, InterlockSupervisor
from .degrade import DegradeReport, build_degrade
from .guards import ConservativeGuards, GuardPolicy
from .health import SensorHealthMonitor
from .queue import KIND_ACTION, KIND_ALARM, KIND_EVENT, KIND_TELEMETRY, LocalQueue
from .upload import ReplayReport, ReplayUploader

log = logging.getLogger("kitchen.resilience.offline")

__all__ = ["OfflineFirstRuntime", "StepResult"]


@dataclass(frozen=True)
class _Verdicts:
    """只为了让 `InterlockSupervisor.apply()` 能吃下保护层的判定（它只读 `.verdicts`）。"""

    verdicts: tuple[Verdict, ...] = ()


@dataclass(frozen=True)
class StepResult:
    """一帧的完整结果：判了什么、动了什么、攒了多少、现在是什么状态。"""

    profile: str = ""
    profile_changed: bool = False
    verdicts: tuple[Verdict, ...] = ()
    guard_verdicts: tuple[Verdict, ...] = ()
    actions: tuple[str, ...] = ()          # 真正该下发的动作（已去重）
    blocked: tuple[dict[str, Any], ...] = ()
    tickets: tuple[str, ...] = ()          # 要开的工单类型
    queued: int = 0
    online: bool = True
    degrade: DegradeReport | None = None
    frame: InterlockFrame | None = None
    faults: tuple[Any, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.verdicts and not self.guard_verdicts and not self.actions

    @property
    def guards_fired(self) -> tuple[str, ...]:
        from ..rules.engine import EVENT_FIRED
        return tuple(
            verdict.rule_id for verdict in self.guard_verdicts if verdict.event == EVENT_FIRED
        )

    def describe(self) -> str:
        parts = [f"档位 {self.profile or '-'}"]
        if self.profile_changed:
            parts.append("已切档")
        if self.guards_fired:
            parts.append("保护触发：" + "、".join(self.guards_fired))
        if self.actions:
            parts.append("动作：" + "、".join(self.actions))
        if self.tickets:
            parts.append("工单：" + "、".join(self.tickets))
        if not self.online:
            parts.append(f"离线（待补传 {self.queued} 条）")
        return " | ".join(parts)

    def to_payload(self, device_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "profile": self.profile,
            "profile_changed": self.profile_changed,
            "online": self.online,
            "actions": list(self.actions),
            "events": [verdict.to_payload(device_id) for verdict in self.verdicts],
            "guard_events": [verdict.to_payload(device_id) for verdict in self.guard_verdicts],
            "queued": self.queued,
        }
        if self.tickets:
            body["tickets"] = list(self.tickets)
        if self.blocked:
            body["blocked"] = list(self.blocked)
        if self.degrade is not None:
            body["degrade"] = self.degrade.to_payload()
        if self.faults:
            body["faults"] = [
                fault.to_payload() if hasattr(fault, "to_payload") else dict(fault)
                for fault in self.faults
            ]
        return body


class OfflineFirstRuntime:
    """把规则、保护、互锁、队列装成一条「断网也能干活」的循环。

    用法::

        loop = OfflineFirstRuntime(runtime, InterlockSupervisor(), device_id="A")
        for frame in source:
            step = loop.tick(frame.metrics, now=frame.ts)
            for action in step.actions:
                relay.apply(action)          # 真正写硬件
        loop.set_online(True)
        loop.flush()                          # 由上报循环调用，不在 tick 里
    """

    def __init__(
        self,
        runtime,
        supervisor: InterlockSupervisor | None = None,
        *,
        device_id: str = "",
        queue: LocalQueue | None = None,
        uploader: ReplayUploader | None = None,
        guards: ConservativeGuards | None = None,
        health: SensorHealthMonitor | None = None,
        guard_policy: GuardPolicy | None = None,
        online: bool = True,
        record_telemetry: bool = True,
        clock=None,
        wall_clock=None,
    ) -> None:
        self.runtime = runtime
        self.supervisor = supervisor or InterlockSupervisor()
        self.device_id = str(device_id or getattr(runtime, "device_id", "") or "")
        self.queue = queue
        self.uploader = uploader
        self.guards = guards if guards is not None else ConservativeGuards(
            guard_policy or GuardPolicy()
        )
        self.health = health
        self.online = bool(online)
        self.record_telemetry = bool(record_telemetry)
        self._clock = clock or time.time
        self._wall_clock = wall_clock or time.time
        self._steps = 0
        self._last_step: StepResult | None = None
        self._offline_since: float | None = None

    # ---------------------------------------------------------------- 网络
    def set_online(self, online: bool, *, now: float | None = None) -> ReplayReport | None:
        """切换网络状态。恢复联网时顺手补传一次，返回补传结果（离线返回 None）。

        补传放在这里而不是 tick 里，是为了让调用方**明确**知道自己在做网络操作。
        """
        online = bool(online)
        moment = float(self._wall_clock() if now is None else now)
        if online == self.online:
            return None
        self.online = online
        if not online:
            self._offline_since = moment
            log.warning("网络断开：进入离线模式（本地保护与执行不受影响）")
            return None
        offline_sec = 0.0 if self._offline_since is None else moment - self._offline_since
        self._offline_since = None
        log.info("网络恢复（离线 %.0fs），开始补传", offline_sec)
        return self.flush()

    def flush(self) -> ReplayReport | None:
        """把积压补传。**没有 uploader 或不在线时什么都不做。**"""
        if self.uploader is None or not self.online:
            return None
        report = self.uploader.replay()
        if report.conflicts:
            log.error("补传冲突，需人工介入：%s", report.describe())
        elif report.sent:
            log.info("补传完成：%s", report.describe())
        return report

    # ---------------------------------------------------------------- 主循环
    def tick(
        self,
        metrics,
        *,
        now: float | None = None,
        raw_metrics=None,
        with_degrade: bool = True,
    ) -> StepResult:
        """一帧：保护 → 引擎 → 互锁 → 入队。**不做任何网络 IO。**"""
        self._steps += 1
        guard_verdicts = self.guards.evaluate(metrics, now=now)
        result = self.runtime.tick(metrics, now=now, raw_metrics=raw_metrics)
        verdicts = tuple(result.verdicts)
        self._mark_human_from_card(metrics, now)

        frame = self.supervisor.apply(_Verdicts(verdicts=guard_verdicts + verdicts))
        actions = tuple(dict.fromkeys((*frame.applied,)))

        faults: tuple[Any, ...] = ()
        if self.health is not None:
            faults = tuple(self.health.observe(metrics, now=now))

        queued = self._enqueue(metrics, verdicts, guard_verdicts, actions, faults, now)
        degrade = None
        if with_degrade:
            degrade = self._degrade(metrics=metrics, faults=faults, now=now)

        step = StepResult(
            profile=result.profile,
            profile_changed=result.profile_changed,
            verdicts=verdicts,
            guard_verdicts=guard_verdicts,
            actions=actions,
            blocked=tuple(
                {"action": item.action, "channel": item.channel, "source": item.source,
                 "reason": item.detail}
                for item in frame.blocked
            ),
            tickets=tuple(dict.fromkeys(
                [v.ticket for v in guard_verdicts if v.ticket]
                + [v.ticket for v in verdicts if v.ticket]
            )),
            queued=queued,
            online=self.online,
            degrade=degrade,
            frame=frame,
            faults=faults,
        )
        self._last_step = step
        return step

    def _mark_human_from_card(self, metrics, now: float | None) -> None:
        """刷卡/按键事件（`card_uid` 或 `key_press`）算人工活动，供保护③计时。"""
        try:
            items = metrics.items()
        except AttributeError:
            return
        for key, value in items:
            name = str(key).lower()
            if name in ("card_uid", "key_press", "button", "human") and value:
                self.guards.mark_human(name, now=now)
                return

    # ---------------------------------------------------------------- 入队
    def _enqueue(
        self,
        metrics,
        verdicts: tuple[Verdict, ...],
        guard_verdicts: tuple[Verdict, ...],
        actions: tuple[str, ...],
        faults: tuple[Any, ...],
        now: float | None,
    ) -> int:
        queue = self.queue
        if queue is None:
            return 0
        moment = None if now is None else float(now)
        count = 0
        if self.record_telemetry:
            if queue.append(KIND_TELEMETRY, payload={"metrics": dict(metrics)}, ts=moment):
                count += 1
        for verdict in verdicts:
            kind = KIND_EVENT
            if queue.append(kind, metric=verdict.metric, value=verdict.value,
                           payload=verdict.to_payload(self.device_id), ts=moment):
                count += 1
        for verdict in guard_verdicts:
            # 保护的触发是安全事件，**强制关键**：容量满也不能丢
            if queue.append(KIND_ALARM, metric=verdict.metric, value=verdict.value,
                            payload=verdict.to_payload(self.device_id),
                            critical=True, ts=moment):
                count += 1
        if actions:
            if queue.append(KIND_ACTION, payload={"actions": list(actions)}, ts=moment):
                count += 1
        for fault in faults:
            body = fault.to_payload() if hasattr(fault, "to_payload") else dict(fault)
            if queue.append(KIND_ALARM, metric=str(body.get("metric") or ""),
                            payload=body, critical=True, ts=moment):
                count += 1
        return count

    # ---------------------------------------------------------------- 降级
    def _degrade(self, *, metrics, faults: tuple[Any, ...], now: float | None) -> DegradeReport:
        vision = None
        guard = getattr(self.runtime, "flame_guard", None)
        if guard is not None and hasattr(guard, "vision_health"):
            health = guard.vision_health()
            vision = {"online": bool(health.online), "age_sec": getattr(health, "age_sec", None)}
        return build_degrade(
            network_online=self.online,
            vision=vision,
            sensor_faults=faults,
            config_degraded=bool(getattr(self.runtime.config, "degraded", False)),
            queue_pending=len(self.queue) if self.queue is not None else 0,
            now=now,
            wall_clock=self._wall_clock,
        )

    # ---------------------------------------------------------------- 观测
    def mark_human(self, source: str = "card", *, now: float | None = None) -> None:
        """透传给保护③（刷卡、TFT 按键、Web 确认都算「有人在管」）。"""
        self.guards.mark_human(source, now=now)

    @property
    def last_step(self) -> StepResult | None:
        return self._last_step

    @property
    def offline_sec(self) -> float:
        if self._offline_since is None:
            return 0.0
        return max(0.0, float(self._wall_clock()) - self._offline_since)

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "device_id": self.device_id,
            "online": self.online,
            "offline_sec": round(self.offline_sec, 1),
            "steps": self._steps,
            "guards": self.guards.snapshot(now=now),
            "channels": self.supervisor.channel_view(),
        }
        if self.queue is not None:
            body["queue"] = self.queue.snapshot()
        if self.uploader is not None:
            body["uploader"] = self.uploader.snapshot()
        if self.health is not None:
            body["health"] = self.health.snapshot(now=now)
        if self._last_step is not None and self._last_step.degrade is not None:
            body["degrade"] = self._last_step.degrade.to_payload()
        return body
