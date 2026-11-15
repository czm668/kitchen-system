# -*- coding: utf-8 -*-
"""MQTT 网关（A2）：上行帧的订阅端。

收到遥测帧 → `AppState.tick(metrics)` → 规则引擎判定 →（AppState 内部）
→ 运行时库落库（A1 的 recorder）。收到事件帧（第 13 类信号）→
`AppState.emit()` 转成业务事件流。

设计纪律（承 A10 联调教训：字段级一致，不是接口通）：
- 帧校验按 `METRIC_META` 的 12 指标口径——缺字段/类型错的帧丢弃并计数，
  不喂给 tick（否则规则引擎拿到半帧会误判）。
- 网关只做「接入 + 校验 + 转发」，不复制任何业务逻辑。
"""
from __future__ import annotations

import threading
from typing import Any

from ..api.state import AppState, METRIC_META
from .transport import Handler, Transport, topic_actions, topic_events, topic_telemetry


class GatewayStats:
    """网关运行统计：收帧 / 坏帧 / 动作 / 坏动作。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.frames_in = 0
        self.frames_bad = 0
        self.events_in = 0
        self.actions_out = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "frames_in": self.frames_in,
                "frames_bad": self.frames_bad,
                "events_in": self.events_in,
                "actions_out": self.actions_out,
            }

    def bump(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)


class MqttGateway:
    """订阅某 device_id 的上行 topic，把 MQTT 流量接入 AppState。

    用法（真 broker）::

        transport = PahoTransport("127.0.0.1", 1883, client_id="kitchen-gw")
        gateway = MqttGateway(state, transport)
        gateway.start()      # 订阅 telemetry / events
        ...
        gateway.close()

    Inproc 时同一个类的用法不变——这正是抽象层的意义：单测和演示
    只换 Transport，不换网关代码。
    """

    def __init__(self, state: AppState, transport: Transport,
                 device_id: str | None = None, root: str = "kitchen") -> None:
        self.state = state
        self.transport = transport
        # 默认取 AppStateConfig.device_id——网关订阅的 topic 必须和
        # 落库/事件流里的设备身份一致（字段级一致口径）。
        self.device_id = device_id or state.config.device_id
        self.root = root
        self.stats = GatewayStats()
        self._action_topic = topic_actions(device_id, root)
        self._started = False

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        handler: Handler = self._on_upstream
        self.transport.subscribe(topic_telemetry(self.device_id, self.root), handler)
        self.transport.subscribe(topic_events(self.device_id, self.root), handler)
        self._started = True

    def close(self) -> None:
        self.transport.close()

    # ------------------------------------------------------------ 上行
    def _on_upstream(self, topic: str, payload: dict[str, Any]) -> None:
        if topic.endswith("/events"):
            self._handle_event(payload)
        else:
            self._handle_frame(payload)

    def _handle_frame(self, payload: dict[str, Any]) -> None:
        """遥测帧：校验 12 指标后喂 tick。坏帧丢弃计数，不抛。"""
        metrics = payload.get("metrics") if isinstance(payload, dict) else None
        if not isinstance(metrics, dict) or not metrics:
            self.stats.bump("frames_bad")
            return
        clean: dict[str, float] = {}
        for name, value in metrics.items():
            if name in METRIC_META and isinstance(value, (int, float)):
                clean[name] = float(value)
        if not clean:
            self.stats.bump("frames_bad")
            return
        if len(clean) < len(metrics):
            self.stats.bump("frames_bad")  # 含未登记指标——丢弃整帧（字段级一致口径）
            return
        self.state.tick(clean)
        self.stats.bump("frames_in")

    def _handle_event(self, payload: dict[str, Any]) -> None:
        """第 13 类信号（RC522 刷卡等）：转成业务事件流。"""
        kind = str(payload.get("kind") or "") if isinstance(payload, dict) else ""
        if not kind:
            self.stats.bump("frames_bad")
            return
        body = payload.get("data")
        self.state.emit(kind, body if isinstance(body, dict) else None)
        self.stats.bump("events_in")

    # ------------------------------------------------------------ 下行
    def send_action(self, result: dict[str, Any]) -> None:
        """把 `AppState.channel_action()` 的裁决结果下发到设备 topic。

        下行 payload 带 action/channel/state——设备端按它驱动继电器，
        并应回 ACK 到 events topic（演示口径：ack 由模拟器回，见 publisher）。
        """
        payload = {
            "kind": "action",
            "action": result.get("action"),
            "channel": result.get("channel"),
            "label": result.get("label"),
            "state": result.get("state"),
        }
        self.transport.publish(self._action_topic, payload)
        self.stats.bump("actions_out")
