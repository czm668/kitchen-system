# -*- coding: utf-8 -*-
"""端侧优先运行时（A6-01 装配 / 三条保护的离线验收）。"""
from __future__ import annotations

import json

from resilience_support import (
    CountingSink,
    StubRuntime,
    collect_actions,
    collect_guard_events,
    make_queue,
    metrics,
)

from kitchen.resilience.guards import (
    GUARD_FLAME_UNATTENDED,
    GUARD_GAS_SURGE,
    GUARD_THERMAL_FAN,
    ConservativeGuards,
    GuardPolicy,
)
from kitchen.resilience.offline import OfflineFirstRuntime
from kitchen.resilience.queue import KIND_ALARM, KIND_TELEMETRY
from kitchen.resilience.upload import MemoryOfflineStore, ReplayUploader
from kitchen.safety.interlock import InterlockSupervisor


def make_loop(tmp_path, *, online=False, guard_policy=None, with_uploader=True, **kwargs):
    queue = make_queue(tmp_path, **kwargs.pop("queue_kwargs", {}))
    store = MemoryOfflineStore()
    uploader = (
        ReplayUploader(queue, store, device_id="K-TEST") if with_uploader else None
    )
    loop = OfflineFirstRuntime(
        StubRuntime(), InterlockSupervisor(), device_id="K-TEST",
        queue=queue, uploader=uploader,
        guards=ConservativeGuards(guard_policy or GuardPolicy(time_scale=60.0)),
        online=online,
    )
    return loop, queue, store, uploader


class TestOfflineProtection:
    """验收：拔网 30 秒，燃气/超温/火焰三条保护仍然生效。"""

    def test_断网时燃气突变照样关阀(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(gas_level=0.9), now=0.0)
        step = loop.tick(metrics(gas_level=0.95), now=1.0)
        assert step.online is False
        assert "gas_valve_close" in step.actions
        assert GUARD_GAS_SURGE in step.guards_fired

    def test_断网时超温照样开风机(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        step = loop.tick(metrics(hood_temp=290.0), now=0.0)
        assert "fan_on" in step.actions
        assert GUARD_THERMAL_FAN in step.guards_fired

    def test_断网时明火长烧照样关气源(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(flame_b=1.0), now=0.0)
        step = loop.tick(metrics(flame_b=1.0), now=61.0)
        assert "gas_valve_close" in step.actions
        assert GUARD_FLAME_UNATTENDED in step.guards_fired
        assert "unattended" in step.tickets

    def test_三条保护在同一个离线窗口内全部触发(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        steps = [
            loop.tick(metrics(gas_level=0.9, hood_temp=290.0, flame_b=1.0), now=t)
            for t in (0.0, 1.0, 30.0, 61.0)
        ]
        fired = {name for name, event in collect_guard_events(steps) if event == "fired"}
        assert fired == {GUARD_GAS_SURGE, GUARD_THERMAL_FAN, GUARD_FLAME_UNATTENDED}
        assert set(collect_actions(steps)) == {"gas_valve_close", "fan_on"}
        assert all(step.online is False for step in steps)

    def test_真运行时离线窗口内三条都生效(self, tmp_path):
        """接真的 `KitchenRuntime`：引擎与保护同时工作，动作不能互相顶掉。"""
        from kitchen.rules.config_store import load_builtin
        from kitchen.runtime import KitchenRuntime

        runtime = KitchenRuntime(load_builtin(), device_id="K-REAL")
        loop = OfflineFirstRuntime(
            runtime, InterlockSupervisor(), device_id="K-REAL",
            queue=make_queue(tmp_path),
            guards=ConservativeGuards(GuardPolicy(flame_hold_sec=3600.0, time_scale=600.0)),
            online=False,
        )
        steps = [
            loop.tick(metrics(gas_level=0.9, hood_temp=295.0, flame_b=1.0), now=t)
            for t in (0.0, 1.0, 12.0)
        ]
        fired = {name for name, event in collect_guard_events(steps) if event == "fired"}
        assert fired == {GUARD_GAS_SURGE, GUARD_THERMAL_FAN, GUARD_FLAME_UNATTENDED}
        actions = set(collect_actions(steps))
        assert {"gas_valve_close", "fan_on"} <= actions
        assert all(step.online is False for step in steps)


class TestNoNetworkInTick:
    def test_一帧都不触发上传(self, tmp_path):
        """补传必须由调用方显式 `flush()`；判定路径卡在网络超时里，保护窗口就乱了。"""
        loop, queue, store, _up = make_loop(tmp_path)
        loop.uploader.sink = CountingSink(store)
        for t in range(5):
            loop.tick(metrics(), now=float(t))
        assert loop.uploader.sink.calls == 0
        assert len(queue) > 0

    def test_离线时flush不做任何事(self, tmp_path):
        loop, queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(), now=0.0)
        assert loop.flush() is None
        assert len(queue) == 1


class TestQueueing:
    def test_每帧都落一条遥测(self, tmp_path):
        loop, queue, _store, _up = make_loop(tmp_path)
        for t in range(3):
            loop.tick(metrics(), now=float(t))
        assert len(queue) == 3
        assert queue.snapshot()["kinds"][KIND_TELEMETRY] == 3

    def test_保护的告警按关键记录入队(self, tmp_path):
        loop, queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(hood_temp=290.0), now=0.0)
        critical = [entry for entry in queue.pending() if entry.critical]
        assert len(critical) == 1
        assert critical[0].kind == KIND_ALARM
        assert critical[0].payload["rule_id"] == GUARD_THERMAL_FAN

    def test_关掉遥测记录后只有事件(self, tmp_path):
        loop, queue, _store, _up = make_loop(tmp_path)
        loop.record_telemetry = False
        loop.tick(metrics(), now=0.0)
        assert len(queue) == 0

    def test_动作落进队列(self, tmp_path):
        loop, queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(hood_temp=290.0), now=0.0)
        kinds = [entry.kind for entry in queue.pending()]
        assert "action" in kinds


class TestReplayFlow:
    def test_恢复网络自动补传并清空队列(self, tmp_path):
        loop, queue, store, _up = make_loop(tmp_path)
        for t in range(3):
            loop.tick(metrics(), now=float(t))
        assert len(queue) == 3
        report = loop.set_online(True, wall_epoch=3.0)
        assert report.sent == 3 and report.accepted == 3
        assert len(queue) == 0 and len(store) == 3
        assert loop.online is True

    def test_重复置为在线不补传(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path, online=True)
        assert loop.set_online(True) is None

    def test_补传后再上线不会重复写(self, tmp_path):
        loop, queue, store, _up = make_loop(tmp_path)
        loop.tick(metrics(), now=0.0)
        loop.set_online(True, wall_epoch=1.0)
        loop.set_online(False, wall_epoch=2.0)
        loop.tick(metrics(), now=3.0)
        loop.set_online(True, wall_epoch=4.0)
        assert len(store) == 2
        assert len(store.values("t")) == 2

    def test_掉电重启后接着补传且不重号(self, tmp_path):
        loop, queue, _store, _up = make_loop(tmp_path)
        for t in range(3):
            loop.tick(metrics(), now=float(t))
        last_seq = queue.last_seq

        # 「重启」：同一个队列文件重新装载，换一个服务端接收端
        reloaded = make_queue(tmp_path)
        store = MemoryOfflineStore()
        assert reloaded.last_seq == last_seq
        ReplayUploader(reloaded, store, device_id="K-TEST").replay()
        assert len(store) == 3

        # 重启后继续攒，seq 接着往下走，补传不重
        loop2 = OfflineFirstRuntime(
            StubRuntime(), InterlockSupervisor(), device_id="K-TEST",
            queue=reloaded, uploader=ReplayUploader(reloaded, store, device_id="K-TEST"),
            online=False,
        )
        loop2.tick(metrics(), now=100.0)
        assert reloaded.last_seq == last_seq + 1
        loop2.set_online(True, wall_epoch=101.0)
        assert len(store) == 4
        assert len(store.values("t")) == 4


class TestHuman:
    def test_刷卡延后明火长烧保护(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(flame_b=1.0), now=0.0)
        loop.mark_human("card", now=30.0)
        assert loop.tick(metrics(flame_b=1.0), now=60.0).guards_fired == ()
        assert GUARD_FLAME_UNATTENDED in loop.tick(metrics(flame_b=1.0), now=95.0).guards_fired

    def test_刷卡事件从遥测帧里识别(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(flame_b=1.0), now=0.0)
        loop.tick({**metrics(flame_b=1.0), "card_uid": "04A2B3C1"}, now=30.0)
        assert loop.guards.last_human_at == 30.0
        assert loop.tick(metrics(flame_b=1.0), now=60.0).guards_fired == ()


class TestRecovery:
    def test_释放后人工恢复通道(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(hood_temp=290.0), now=0.0)
        assert loop.supervisor.controller.channel_state("CH2") == "latched"
        loop.tick(metrics(hood_temp=200.0), now=1.0)
        decision = loop.supervisor.restore("CH2", reviewer="tft:组长")
        assert decision.ok is True


class TestObservability:
    def test_降级报告随帧产出(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        step = loop.tick(metrics(), now=0.0)
        assert step.degrade is not None
        assert step.degrade.severity == "offline"
        assert step.degrade.capability("gas_protection").state == "full"

    def test_快照含队列与保护(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(hood_temp=290.0), now=0.0)
        body = loop.snapshot(now=1.0)
        assert body["online"] is False
        assert body["steps"] == 1
        assert body["queue"]["size"] >= 1
        assert GUARD_THERMAL_FAN in body["guards"]["tripped"]
        json.dumps(body, ensure_ascii=False, default=str)

    def test_事件载荷可上送(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        step = loop.tick(metrics(hood_temp=290.0), now=0.0)
        payload = step.to_payload("K-TEST")
        json.dumps(payload, ensure_ascii=False)
        assert payload["actions"] == ["fan_on"]
        assert payload["guard_events"][0]["rule_id"] == GUARD_THERMAL_FAN
        assert "unattended" not in payload

    def test_离线时长可查(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        loop.tick(metrics(), now=0.0)
        assert loop.offline_sec >= 0.0

    def test_描述串含保护与待补传(self, tmp_path):
        loop, _queue, _store, _up = make_loop(tmp_path)
        step = loop.tick(metrics(hood_temp=290.0), now=0.0)
        assert "保护触发" in step.describe()
        assert "待补传" in step.describe()
