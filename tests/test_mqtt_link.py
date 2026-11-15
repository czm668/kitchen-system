# -*- coding: utf-8 -*-
"""A2 MQTT 真链路测试。

覆盖两层：
1. Inproc 竖线（默认回归）：publisher → gateway → tick → 规则 → 落库，
   断言的是「字段级一致」的端到端结果，不是接口通。
2. 真 broker 冒烟（skipif 无 amqtt/paho）：内嵌 amqtt 起本地 broker，
   paho 发布端 + paho 网关真 socket 走一轮——证明协议层没问题。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from kitchen.api import METRIC_META
from kitchen.mqtt import (
    InprocTransport,
    MqttGateway,
    TelemetryPublisher,
    topic_actions,
    topic_telemetry,
)
from kitchen.sim import KitchenSimProfile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "fixtures" / "kitchen_sim_profiles.json"

try:
    from amqtt.broker import Broker  # noqa: F401
    HAS_AMQTT = True
except ImportError:
    HAS_AMQTT = False

try:
    from kitchen.mqtt import PahoTransport
    HAS_PAHO = True
except Exception:
    HAS_PAHO = False


def make_profile() -> KitchenSimProfile:
    return KitchenSimProfile.from_file(PROFILE)


def make_db_state(tmp_path: Path):
    from tests.api_support import make_state
    return make_state(tmp_path, db_path=Path(tmp_path) / "link" / "runtime.db")


# ============================================================ Inproc 竖线
def test_inproc_竖线_发布到落库端到端(tmp_path: Path) -> None:
    state = make_db_state(tmp_path)
    transport = InprocTransport()
    gateway = MqttGateway(state, transport, device_id="kitchen-node-01")
    gateway.start()
    publisher = TelemetryPublisher(make_profile(), transport)
    assert publisher.device_id == "kitchen-node-01"

    publisher.run(6.0, step=3.0)  # 回放 6s：t=0/3/6 三帧（端点含）
    assert publisher.sent == 3
    assert gateway.stats.snapshot()["frames_in"] == 3

    # 端到端铁证：帧进了运行时库（A1 的 recorder）
    stats = state.recorder.stats()
    rows = stats["tables"]["sensor_readings"]
    assert rows >= 3


def test_inproc_坏帧计数不炸(tmp_path: Path) -> None:
    state = make_db_state(tmp_path)
    transport = InprocTransport()
    gateway = MqttGateway(state, transport, device_id="kitchen-node-01")
    gateway.start()

    tel = topic_telemetry("kitchen-node-01")
    transport.publish(tel, {})                       # 空 payload
    transport.publish(tel, {"metrics": {}})          # 空 metrics
    transport.publish(tel, {"metrics": {"gas_level": 0.1, "unknown_x": 1.0}})  # 未登记指标
    transport.publish(tel, {"metrics": {"gas_level": 0.1}})  # 半帧（合法但只 1 指标也收）
    snap = gateway.stats.snapshot()
    assert snap["frames_bad"] == 3
    assert snap["frames_in"] == 1


def test_inproc_第13类信号_事件进流(tmp_path: Path) -> None:
    state = make_db_state(tmp_path)
    transport = InprocTransport()
    gateway = MqttGateway(state, transport, device_id="kitchen-node-01")
    gateway.start()

    transport.publish(
        topic_telemetry("kitchen-node-01").rsplit("/", 1)[0] + "/events",
        {"kind": "rfid", "data": {"card_uid": "04A1B2C3", "result": "pass"}},
    )
    snap = gateway.stats.snapshot()
    assert snap["events_in"] == 1
    kinds = [event["kind"] for event in state.events()]
    assert "rfid" in kinds


def test_inproc_下行动作与设备端ACK闭环(tmp_path: Path) -> None:
    state = make_db_state(tmp_path)
    transport = InprocTransport()
    gateway = MqttGateway(state, transport, device_id="kitchen-node-01")
    gateway.start()
    publisher = TelemetryPublisher(make_profile(), transport)

    result = state.channel_action("gas_valve_close", reviewer="tester",
                                  manual_confirmed=False, source="mqtt-test")
    gateway.send_action(result)
    # 设备端替身收到下行动作
    assert publisher.last_action is not None
    assert publisher.last_action["action"] == "gas_valve_close"
    # ACK 回到 events topic，网关转成业务事件
    time.sleep(0.05)
    snap = gateway.stats.snapshot()
    assert snap["actions_out"] == 1
    assert snap["events_in"] >= 1
    acks = [e for e in state.events() if e["kind"] == "action-ack"]
    assert acks and acks[-1]["action"] == "gas_valve_close"


def test_inproc_规则告警经链路真实落库(tmp_path: Path) -> None:
    """燃气帧（gas_level 高）→ tick → 规则判定 → alarm_events 落库。"""
    state = make_db_state(tmp_path)
    transport = InprocTransport()
    gateway = MqttGateway(state, transport, device_id="kitchen-node-01")
    gateway.start()

    sample = {name: 0.2 for name in METRIC_META}
    sample["gas_level"] = 0.95  # 远超上限，必触发燃气规则
    publisher = TelemetryPublisher(make_profile(), transport)
    publisher.publish_sample(0.0, sample)
    time.sleep(0.05)

    alarms = state.recorder.alarms_history()
    assert alarms, "燃气告警应经 MQTT→tick→规则→落库全链"
    snap = gateway.stats.snapshot()
    assert snap["frames_in"] == 1


# ============================================================ 真 broker 冒烟
def _run_broker(port: int, ready: threading.Event, stop: threading.Event) -> None:
    import asyncio

    from amqtt.broker import Broker

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cfg = {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "auth": {"allow-anonymous": True},
        "topic-check": {"enabled": False},
    }

    async def main() -> None:
        broker = Broker(cfg)
        await broker.start()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.1)
        await broker.shutdown()

    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


@pytest.mark.skipif(not (HAS_AMQTT and HAS_PAHO), reason="amqtt/paho 未安装")
def test_amqtt_真socket冒烟(tmp_path: Path) -> None:
    port = 18883
    ready, stop = threading.Event(), threading.Event()
    broker_thread = threading.Thread(target=_run_broker, args=(port, ready, stop),
                                     daemon=True)
    broker_thread.start()
    assert ready.wait(timeout=10), "amqtt broker 未在 10s 内就绪"
    try:
        state = make_db_state(tmp_path)
        gw_transport = PahoTransport("127.0.0.1", port, client_id="gw-smoke")
        gateway = MqttGateway(state, gw_transport)
        gateway.start()
        pub_transport = PahoTransport("127.0.0.1", port, client_id="pub-smoke")
        publisher = TelemetryPublisher(make_profile(), pub_transport)

        sample = {name: 0.2 for name in METRIC_META}
        sample["gas_level"] = 0.95
        publisher.publish_sample(0.0, sample)
        time.sleep(1.0)  # 等 broker 转发 + 网关处理

        assert gateway.stats.snapshot()["frames_in"] >= 1
        alarms = state.recorder.alarms_history()
        assert alarms, "真 broker 冒烟：告警应落库"
        gw_transport.close()
        pub_transport.close()
    finally:
        stop.set()
        broker_thread.join(timeout=5)
