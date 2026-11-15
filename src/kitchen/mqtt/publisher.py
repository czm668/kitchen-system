# -*- coding: utf-8 -*-
"""遥测发布端（A2）：设备端模拟器。

把 `KitchenSimProfile.iter_samples()` 产出的样本按采样步长打包成 MQTT
帧上行——它就是「ESP32 控制节点」在软件层的替身：真机上这段由固件把
ADC 读数打包 publish，软件层用注入脚本回放，链路代码完全同一份。
"""
from __future__ import annotations

import time
from typing import Any, Iterator

from ..sim import KitchenSimProfile
from .transport import Transport, topic_actions, topic_telemetry


class TelemetryPublisher:
    """按步长回放注入脚本，把 12 指标帧 publish 到 telemetry topic。"""

    def __init__(self, profile: KitchenSimProfile, transport: Transport,
                 device_id: str | None = None, root: str = "kitchen") -> None:
        self.profile = profile
        self.transport = transport
        # device_id 以注入脚本声明为准（两节点口径：脚本里写的是哪个节点
        # 就发哪个 topic），显式参数可覆盖（演示换名用）。
        self.device_id = device_id or profile.device_id
        self.root = root
        self._telemetry_topic = topic_telemetry(self.device_id, root)
        self._action_topic = topic_actions(self.device_id, root)
        self.sent = 0
        # 订阅下行动作（设备端 ACK 演示用）：真机上这段是「收到 action →
        # 驱动继电器 → 回 ack」，软件层收到就记下来 + 回 ack 帧。
        self.last_action: dict[str, Any] | None = None
        self.transport.subscribe(self._action_topic, self._on_action)

    # ------------------------------------------------------------ 上行
    def publish_sample(self, t_s: float, sample: dict[str, float]) -> None:
        payload = {
            "device_id": self.device_id,
            "ts": round(t_s, 3),
            "metrics": {k: round(float(v), 4) for k, v in sample.items()},
        }
        self.transport.publish(self._telemetry_topic, payload)
        self.sent += 1

    def run(self, seconds: float, step: float = 1.0,
            *, speed: float = 1.0) -> int:
        """回放 seconds 秒的注入脚本（speed>1 为加速回放，只影响墙钟节奏
        不影响时间戳语义）。返回发送帧数。"""
        count = 0
        for t_s, sample in self.profile.iter_samples(seconds, step):
            self.publish_sample(t_s, sample)
            count += 1
            if speed != 1.0:
                time.sleep(step / speed)
            else:
                time.sleep(step)
        return count

    # ------------------------------------------------------------ 下行（设备端替身）
    def _on_action(self, _topic: str, payload: dict[str, Any]) -> None:
        self.last_action = payload
        # 回 ACK 到 events topic——闭环的关键一环：管理端能看到「指令已
        # 到设备」，而不是只在服务端状态机里打转。
        self.transport.publish(
            self._telemetry_topic.rsplit("/", 1)[0] + "/events",
            {"kind": "action-ack",
             "data": {"action": payload.get("action"),
                      "channel": payload.get("channel"),
                      "ack": True}},
        )
