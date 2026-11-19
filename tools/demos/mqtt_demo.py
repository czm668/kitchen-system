# -*- coding: utf-8 -*-
"""A2 MQTT 真链路演示：注入脚本 → broker → 网关 → 规则 → 运行时库。

三种跑法::

    # 1) 全进程内（零依赖，离线也能演示链路代码路径）
    python tools/demos/mqtt_demo.py

    # 2) 内嵌 amqtt broker（真协议真 socket，装了 amqtt+paho 即可）
    python tools/demos/mqtt_demo.py --amqtt

    # 3) 连外部 broker（EMQX / mosquitto / 内网 broker）
    python tools/demos/mqtt_demo.py --broker 127.0.0.1:1883

演示内容：回放 8 秒注入脚本（含 t=3s RC522 刷卡事件帧），燃气脉冲帧触发
规则告警，下发一次三通道动作并等待设备端 ACK；结束打印网关统计 + 运行
时库体检（sensor_readings / alarm_events 行数）——这就是论文实测竖线的
可复跑脚本。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kitchen.api import METRIC_META, AppState, AppStateConfig
from kitchen.mqtt import (
    InprocTransport,
    MqttGateway,
    TelemetryPublisher,
)
from kitchen.sim import KitchenSimProfile

PROFILE_PATH = _ROOT / "fixtures" / "kitchen_sim_profiles.json"


# ------------------------------------------------------------ 内嵌 broker
def _start_amqtt(port: int) -> tuple[threading.Thread, threading.Event, threading.Event]:
    """在独立线程里跑 amqtt broker；返回 (线程, 就绪事件, 停止事件)。"""
    ready = threading.Event()
    stop_evt = threading.Event()

    def run() -> None:
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
            while not stop_evt.is_set():
                await asyncio.sleep(0.2)
            await broker.shutdown()

        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

    thread = threading.Thread(target=run, daemon=True, name="amqtt-broker")
    thread.start()
    return thread, ready, stop_evt


# ------------------------------------------------------------ 主流程
def main() -> None:
    parser = argparse.ArgumentParser(description="MQTT 真链路演示（A2）")
    parser.add_argument("--broker", help="外部 broker，形如 127.0.0.1:1883")
    parser.add_argument("--amqtt", action="store_true",
                        help="内嵌 amqtt broker（端口 18883）")
    parser.add_argument("--seconds", type=float, default=8.0, help="回放时长（脚本秒）")
    parser.add_argument("--step", type=float, default=0.5, help="采样步长（脚本秒）")
    parser.add_argument("--db", default=str(_ROOT / "var" / "mqtt_demo.db"),
                        help="运行时库路径（默认 var/mqtt_demo.db）")
    args = parser.parse_args()

    # ---- 传输层三选一
    if args.amqtt:
        port = 18883
        broker_thread, ready, broker_stop = _start_amqtt(port)
        assert ready.wait(timeout=10), "内嵌 broker 未就绪"
        from kitchen.mqtt import PahoTransport
        gw_t = PahoTransport("127.0.0.1", port, client_id="demo-gateway")
        pub_t = PahoTransport("127.0.0.1", port, client_id="demo-publisher")
        broker_mode = f"内嵌 amqtt（127.0.0.1:{port}）"
    elif args.broker:
        host, _, port = args.broker.partition(":")
        from kitchen.mqtt import PahoTransport
        gw_t = PahoTransport(host or "127.0.0.1", int(port or 1883),
                             client_id="demo-gateway")
        pub_t = PahoTransport(host or "127.0.0.1", int(port or 1883),
                              client_id="demo-publisher")
        broker_mode = f"外部 broker（{args.broker}）"
    else:
        gw_t = pub_t = InprocTransport()
        broker_mode = "进程内 InprocTransport（无 broker 进程）"

    profile = KitchenSimProfile.from_file(PROFILE_PATH)
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    config = AppStateConfig(
        var_dir=db_path.parent / "mqtt_var",
        db_path=db_path,
        tick_interval=3600.0,  # 关掉内置采样线程：这一轮帧全部来自 MQTT
    )
    state = AppState(config=config)
    gateway = MqttGateway(state, gw_t, device_id=profile.device_id)
    gateway.start()
    publisher = TelemetryPublisher(profile, pub_t)

    print(f"=== MQTT 真链路演示 ===")
    print(f"broker：{broker_mode}")
    print(f"设备：{profile.device_id}  注入脚本：{PROFILE_PATH.name}"
          f"（{len(profile.metrics)} 遥测 + {len(profile.event_signals)} 事件信号）")
    print(f"运行时库：{db_path}")
    print()

    # ---- 1) 正常回放 + 一个燃气脉冲帧（触发规则告警）
    for t_s, sample in profile.iter_samples(args.seconds, args.step):
        publisher.publish_sample(t_s, sample)
        time.sleep(args.step / 4.0)  # 4 倍速回放，墙钟别拖太久
    spike = {name: 0.2 for name in METRIC_META}
    spike["gas_level"] = 0.95
    publisher.publish_sample(args.seconds + args.step, spike)
    time.sleep(0.3)
    print(f"[1] 回放 {args.seconds:g}s + 燃气脉冲帧：发送 {publisher.sent} 帧")

    # ---- 2) RC522 刷卡事件（第 13 类信号走 events topic）
    pub_t.publish(
        "kitchen/" + profile.device_id + "/events",
        {"kind": "rfid", "data": {"card_uid": "04A1B2C3", "staff_id": "chef-01",
                                  "result": "pass", "action": "check_in"}},
    )
    time.sleep(0.2)
    print("[2] RC522 刷卡事件上行：events topic → 业务事件流")

    # ---- 3) 下行动作 + 设备端 ACK 闭环
    result = state.channel_action("gas_valve_close", reviewer="demo",
                                  manual_confirmed=False, source="mqtt-demo")
    gateway.send_action(result)
    time.sleep(0.2)
    ack = publisher.last_action
    print(f"[3] 下行动作 {result['action']}（{result['channel']}）"
          f" → 设备端 {'已 ACK' if ack else '未 ACK'}")

    # ---- 收尾与体检
    gw_stats = gateway.stats.snapshot()
    print()
    print(f"网关统计：{json.dumps(gw_stats, ensure_ascii=False)}")
    db_stats = state.recorder.stats() if state.recorder else {"enabled": False}
    if db_stats.get("enabled") is not False:
        tables = db_stats.get("tables", {})
        print(f"运行时库：sensor_readings={tables.get('sensor_readings')} 行，"
              f"alarm_events={tables.get('alarm_events')} 行")
        alarms = state.recorder.alarms_history(limit=5)
        for alarm in alarms[:5]:
            print(f"  告警：{alarm.get('atype')} {alarm.get('message', '')}"
                  f" @ {alarm.get('ts', '')}")
    state.recorder.close() if state.recorder else None
    gw_t.close()
    if pub_t is not gw_t:
        pub_t.close()
    if args.amqtt:
        broker_stop.set()
        broker_thread.join(timeout=5)
    print("\n=== 演示结束（库文件保留，可再跑 /api/admin/db/history 查历史）===")


if __name__ == "__main__":
    main()
