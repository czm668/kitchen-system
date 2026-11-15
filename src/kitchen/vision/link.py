# -*- coding: utf-8 -*-
"""视觉 → #3 明火离人的接线（A5-06）。

`FlameGuard.observe_vision()` 早就在等真实视觉值（A2 阶段喂的是模拟值）。
本模块只做一件事：把 `VisionFrame` 翻译成那个函数要的三个数。

## 三条口径

1. **按工位算人数，不按整幅画面。**
   2 米外有服务员路过，画面里「有人」，但灶前没人——火还在烧，路过的人
   救不了火。#3 的判据是「**该工位**无人」，所以必须按 `station_id` 取。
   按全画面人数的版本，在有两灶一个传菜口的标准后厨里**永远不触发**，
   而且没人会发现，因为它「看起来在工作」。

2. **视觉离线时不喂。** `observe_vision` 每次调用都会刷新 `_vision_at`，
   离线期间还继续喂「有人」等于给一个瞎了的防线续命：`vision_health()`
   永远显示在线，运维不会去修摄像头。正确做法是**不调**，让守卫自己超时，
   由 `vision_health()` 单独报「视觉离线」，同时 `vision_frame()` 按
   「有人」降级（不误关气）。

3. **「检出 0 人」和「看不清」是两件事，但本层不做区分。**
   镜头被挡住时检测器也返回 0 人，于是 #3 会关气——这是**安全侧的合理
   误判**：宁可关一次气也不能让明火无人。区分靠 `blind` 事件（在
   `pipeline` 里按「连续空帧 + 工位有占用会话」判），它只影响告警文案与
   #11，不影响 #3。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .models import VisionFrame

log = logging.getLogger("kitchen.vision.link")

__all__ = ["FlameVisionLink", "LinkResult"]


@dataclass(frozen=True)
class LinkResult:
    """一次投递的结果。"""

    station_id: int | None
    headcount: int
    no_person: bool
    fed: bool
    signals: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def describe(self) -> str:
        scope = f"ST{self.station_id}" if self.station_id is not None else "全画面"
        if not self.fed:
            return f"视觉未投递（{scope}）：{self.reason}"
        who = "无人" if self.no_person else f"{self.headcount} 人"
        return f"视觉→#3（{scope}）：{who}"


class FlameVisionLink:
    """把一个 `FlameGuard` 接到视觉帧上（一个实例对应一个灶）。"""

    def __init__(
        self,
        guard,
        *,
        station_id: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.guard = guard
        self.station_id = station_id
        self._clock = clock or time.monotonic
        self.fed = 0
        self.skipped = 0

    def push(self, frame: VisionFrame, *, online: bool = True) -> LinkResult:
        """投递一帧。`online=False`（视觉离线）时**不调**守卫（口径 2）。"""
        if not online:
            self.skipped += 1
            return LinkResult(
                station_id=self.station_id,
                headcount=-1,
                no_person=False,
                fed=False,
                reason="视觉离线，交由守卫超时降级为「有人」",
            )

        headcount = self._count(frame)
        no_person = headcount == 0
        signals = self.guard.observe_vision(
            no_person, headcount=headcount, now=self._clock())
        self.fed += 1
        return LinkResult(
            station_id=self.station_id,
            headcount=headcount,
            no_person=no_person,
            fed=True,
            signals=dict(signals or {}),
        )

    def _count(self, frame: VisionFrame) -> int:
        if self.station_id is None:
            return int(frame.headcount)
        return int(frame.station_headcount.get(int(self.station_id), 0))

    def snapshot(self) -> dict:
        health = (self.guard.vision_health()
                  if hasattr(self.guard, "vision_health") else None)
        return {
            "station_id": self.station_id,
            "fed": self.fed,
            "skipped": self.skipped,
            "online": None if health is None else bool(health.online),
            "age_sec": None if health is None else round(float(health.age_sec), 1),
        }
