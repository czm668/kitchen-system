# -*- coding: utf-8 -*-
"""MQTT 真链路（A2）：模拟器/设备端 → broker → 网关 → 规则引擎 → 运行时库。

分层：
- `transport`：发布/订阅抽象。`InprocTransport`（进程内，零依赖，测试与
  离线演示默认）与 `PahoTransport`（真 MQTT broker，paho-mqtt 可选依赖）。
- `gateway`：订阅上行 topic，帧 → `AppState.tick()` → 规则引擎 → 落库。
- `publisher`：设备端模拟器，按注入脚本把遥测帧打包 JSON 上行。

依赖口径与项目一致：paho-mqtt/amqtt 只作为可选依赖（`pip install
kitchen-system[mqtt]`），不装则 Inproc 通道与全部单测照常工作。
"""
from .transport import (
    DEFAULT_ROOT,
    InprocTransport,
    PahoTransport,
    Transport,
    topic_actions,
    topic_events,
    topic_telemetry,
)
from .gateway import GatewayStats, MqttGateway
from .publisher import TelemetryPublisher

__all__ = [
    "DEFAULT_ROOT",
    "GatewayStats",
    "InprocTransport",
    "MqttGateway",
    "PahoTransport",
    "TelemetryPublisher",
    "Transport",
    "topic_actions",
    "topic_events",
    "topic_telemetry",
]
