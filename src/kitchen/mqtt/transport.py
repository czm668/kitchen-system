# -*- coding: utf-8 -*-
"""发布/订阅传输层抽象（A2）。

两套实现共用一个窄接口（`publish` / `subscribe` / `close`）：

- `InprocTransport`：进程内直接回调，无网络无依赖。单测与「演示整个
  链路但不想要 broker 进程」场景的默认选择。
- `PahoTransport`：真 MQTT 3.1.1 客户端（paho-mqtt ≥2.0），连真实
  broker（本地 amqtt / EMQX / mosquitto 均可），带断线自动重连。

topic 约定（设备单节点口径，两节点是独立 device_id 各走各的）：
    {root}/{device_id}/telemetry   上行：遥测帧（12 指标 JSON）
    {root}/{device_id}/events      上行：第 13 类信号（RC522 刷卡等）
    {root}/{device_id}/actions     下行：三通道动作 + 设备端 ACK
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable, Protocol

#: topic 根前缀与模板来自 fixtures/kitchen_constants.json（审查报告 §3 P2）。
#: 单一来源 + 启动期校验，避免改了 docs/05 的约定却漏改代码里的主题格式。
from ..constants import constants as _constants  # noqa: E402

DEFAULT_ROOT = _constants()["mqtt"]["root"]
_TOPICS = _constants()["mqtt"]["topics"]

#: 订阅回调签名：topic、解码后的 payload
Handler = Callable[[str, dict[str, Any]], None]


def _topic(key: str, device_id: str, root: str) -> str:
    return _TOPICS[key].format(root=root, device_id=device_id)


def topic_telemetry(device_id: str, root: str = DEFAULT_ROOT) -> str:
    return _topic("telemetry", device_id, root)


def topic_events(device_id: str, root: str = DEFAULT_ROOT) -> str:
    return _topic("events", device_id, root)


def topic_actions(device_id: str, root: str = DEFAULT_ROOT) -> str:
    return _topic("actions", device_id, root)


class Transport(Protocol):
    """发布/订阅最小协议。实现必须线程安全（publisher/gateway 异线程）。"""

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """发布一条 JSON 消息。发送失败不应抛出到采样循环——实现内部计数。"""
        ...

    def subscribe(self, topic: str, handler: Handler) -> None:
        """登记订阅；Inproc 支持 # 通配尾段，Paho 走 MQTT 通配符语义。"""
        ...

    def close(self) -> None:
        ...


class InprocTransport:
    """进程内 pub/sub：发布即同步回调所有匹配订阅者（线程安全）。

    通配只支持尾段 `#`（多级），与 MQTT 语义对齐但实现极简——本项目
    topic 层级固定三段，足够用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[tuple[str, Handler]] = []
        self.published = 0
        self.delivered = 0

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        with self._lock:
            subs = [(pat, h) for pat, h in self._subs if self._match(pat, topic)]
            self.published += 1
        for _, handler in subs:
            handler(topic, payload)
            with self._lock:
                self.delivered += 1

    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            self._subs.append((topic, handler))

    @staticmethod
    def _match(pattern: str, topic: str) -> bool:
        """尾段 `#` 通配；其余段精确匹配。"""
        if pattern == topic:
            return True
        if pattern.endswith("/#"):
            prefix = pattern[:-1]  # 去掉 '#'，保留结尾 '/'
            return topic.startswith(prefix)
        return False

    def close(self) -> None:
        with self._lock:
            self._subs.clear()

    def dump(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)


class PahoTransport:
    """paho-mqtt 客户端封装（真 broker）。

    - 自动重连（paho `reconnect_delay_set` + `loop_start` 后台线程）
    - 订阅在 `on_connect` 里重新登记（重连后 broker 会话丢失仍能恢复）
    - `publish()` 用 QoS 1 + 等待确认，失败只计数不抛（同 AuditLog 口径）
    """

    def __init__(self, host: str, port: int = 1883,
                 client_id: str = "kitchen-transport", keepalive: int = 30) -> None:
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415 —— 可选依赖，缺了才导入
        except ImportError as exc:  # pragma: no cover —— 环境里已装，防御性分支
            raise RuntimeError(
                "PahoTransport 需要 paho-mqtt>=2.0：pip install paho-mqtt"
                "（或不传 --broker 用 InprocTransport 跑全链路）"
            ) from exc
        try:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=client_id
            )
        except AttributeError:  # paho 1.x 兼容
            self._client = mqtt.Client(client_id=client_id)
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)
        self._lock = threading.Lock()
        self._subs: list[tuple[str, Handler]] = []
        self._host = host
        self._port = port
        self._keepalive = keepalive
        self._connected = threading.Event()
        self.published = 0
        self.publish_failures = 0

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive)
        self._client.loop_start()
        # 连接确认最多等 10s；连不上也不炸——publisher 照样入队，重连后续传
        self._connected.wait(timeout=10)

    # ------------------------------------------------------------ paho 回调
    def _on_connect(self, _client, _userdata, _flags, reason_code, _props=None) -> None:
        # paho 2.x 的 reason_code 是 ReasonCode 枚举（有 .is_failure），
        # 1.x 是 int——双版本兼容（getattr 的 default 会急切求值，不能用它兜底）。
        failure = reason_code.is_failure if hasattr(reason_code, "is_failure") \
            else int(reason_code) != 0
        if not failure:
            self._connected.set()
            with self._lock:
                for topic, _handler in self._subs:
                    self._client.subscribe(topic, qos=1)

    def _on_message(self, _client, _userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return  # 坏帧丢弃（gateway 侧另计坏帧，这里静默）
        with self._lock:
            subs = list(self._subs)
        for pattern, handler in subs:
            if InprocTransport._match(pattern, msg.topic):
                handler(msg.topic, payload)

    # ------------------------------------------------------------ Transport
    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        info = self._client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
        with self._lock:
            self.published += 1
            if not info.is_published() and info.rc != 0:
                self.publish_failures += 1

    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            self._subs.append((topic, handler))
        if self._connected.is_set():
            self._client.subscribe(topic, qos=1)

    def close(self) -> None:
        with self._lock:
            self._subs.clear()
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 —— 关闭路径尽力而为
            pass
