# -*- coding: utf-8 -*-
"""kitchen-only 模拟器状态机（纯标准库，不连 MQTT / 数据库）。

与素材库 `tools/sim_device.py` 的关系：那份是**多场景通用模拟器**，要兼容
mingchu / lab_safety 的注入别名、HTTP 注入服务、paho 连接与命令 ACK 线程；
本工程从 0 开始，只保留 kitchen 需要的最小闭环：

- `SimState`：手动 /inject 覆盖 > 注入脚本 profile > None（驱动默认基线）；
- `SimDevice.tick`：按驱动发 12 类遥测，按相对秒回放 RC522 / 三通道 ack 事件；
- `client` 可注入任意带 `publish(topic, payload)` 的对象，方便单测与录屏演示。

真机接入（P1）时把 `client` 换成 MQTT publisher 即可，业务层不用改。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from typing import Any, Protocol

from kitchen.drivers import TELEMETRY_DRIVERS
from kitchen.sim import KitchenSimProfile


class Publisher(Protocol):
    """最小发布接口：paho client / 内存录制器 / 日志文件都可实现。"""

    def publish(self, topic: str, payload: str, qos: int = 0) -> None: ...


class SimState:
    """注入覆盖值（按指标 id 存储，线程安全）。

    `profile` 为空时 `value_for` 与 `get_override` 完全等价（向后兼容口径）；
    挂上 profile 后按时间线补齐 12 个遥测指标，并驱动事件回放。
    """

    def __init__(self, profile: KitchenSimProfile | None = None) -> None:
        self.lock = threading.Lock()
        self.overrides: dict[str, tuple[float, float]] = {}  # metric -> (value, expire_ts)
        self.profile = profile

    def inject(self, metric: str, value: float, hold_s: float) -> None:
        """手动注入；`hold_s < 0` 表示立刻过期（测试/复位用）。"""
        with self.lock:
            self.overrides[str(metric)] = (float(value), time.time() + float(hold_s))

    def get_override(self, metric: str) -> float | None:
        with self.lock:
            item = self.overrides.get(metric)
            if not item:
                return None
            value, expire = item
            if time.time() > expire:
                self.overrides.pop(metric, None)
                return None
            return value

    def active(self) -> list[str]:
        with self.lock:
            now = time.time()
            return [key for key, (_, expire) in self.overrides.items() if expire >= now]

    def value_for(self, metric: str, t: float) -> float | None:
        """本 tick 的注入值：手动覆盖 > profile 脚本 > None（走驱动默认基线）。"""
        manual = self.get_override(metric)
        if manual is not None:
            return manual
        if self.profile is not None:
            return self.profile.sample_metric(metric, t)
        return None


class SimDevice:
    """单个后厨感知/执行节点的离线模拟器。

    构造参数保留 host/port/user/password 是为了与素材库的调用形态对齐，
    但本实现**不建立网络连接**；网络能力由 `client` 注入，便于无硬件联调。
    """

    def __init__(
        self,
        scenario: str,
        device_id: str,
        host: str = "127.0.0.1",
        port: int = 1883,
        user: str = "",
        password: str = "",
        state: SimState | None = None,
        drivers: Iterable[Any] | None = None,
        actions: Iterable[str] = (),
        emit_profile_events: bool = False,
    ) -> None:
        if scenario != "kitchen":
            raise ValueError(f"本模拟器只服务 kitchen 场景，收到 {scenario!r}")
        self.scenario = scenario
        self.device_id = device_id
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.state = state if state is not None else SimState()
        self.drivers = tuple(drivers) if drivers is not None else tuple(cls() for cls in TELEMETRY_DRIVERS)
        self.actions = tuple(actions)
        self.emit_profile_events = bool(emit_profile_events)
        self.client: Publisher | None = None
        self.published: list[tuple[str, dict]] = []  # 无 client 时的内存记录
        self._last_event_t = 0.0

    # ---------------------------------------------------------------- 发布
    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """拼上 `{scenario}/{device_id}/` 前缀后发布；未注入 client 时只记内存。"""
        full = f"{self.scenario}/{self.device_id}/{str(topic).strip('/')}"
        self.published.append((full, dict(payload)))
        if self.client is not None:
            self.client.publish(full, json.dumps(payload, ensure_ascii=False))

    # ---------------------------------------------------------------- 主循环
    def tick(self, t: float) -> None:
        """推进到相对秒 t：发一轮遥测，并回放 (上次 t, 本次 t] 区间事件。"""
        ts = int(time.time())
        for driver in self.drivers:
            if not driver.metrics:
                continue
            overrides = {
                metric: value
                for metric in driver.metrics
                if (value := self.state.value_for(metric, t)) is not None
            }
            payload = driver.sample(t, overrides)
            if not payload:
                continue
            self.publish(driver.topic, {**payload, "ts": ts})

        # 左开右闭，保证同一事件不会被相邻 tick 重复回放
        if self.emit_profile_events and self.state.profile is not None:
            for event in self.state.profile.events_due(self._last_event_t, t):
                topic, event_payload = self.state.profile.render_event(event, ts=ts)
                self.publish(topic, event_payload)
            self._last_event_t = t

    def snapshot(self) -> dict[str, Any]:
        """给演示页 / 排障用的只读快照。"""
        return {
            "scenario": self.scenario,
            "device_id": self.device_id,
            "drivers": [driver.id for driver in self.drivers],
            "profile": self.state.profile.version if self.state.profile else None,
            "active_overrides": self.state.active(),
            "last_event_t": self._last_event_t,
            "published": len(self.published),
        }
