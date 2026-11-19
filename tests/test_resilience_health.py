# -*- coding: utf-8 -*-
"""传感器故障判定、工单与超时升级（A6-04）。"""
from __future__ import annotations

import pytest
from resilience_support import FakeClock

from kitchen.resilience.health import (
    DS18B20_SENTINELS,
    TICKET_TYPE,
    FaultKind,
    SensorHealthMonitor,
    SensorPolicy,
)

T0 = 1_700_000_000.0


def make_monitor(**kwargs) -> SensorHealthMonitor:
    return SensorHealthMonitor(SensorPolicy(**kwargs), clock=FakeClock(start=T0))


class TestOutOfRange:
    def test_超量程立刻报(self):
        mon = make_monitor()
        faults = mon.observe({"fridge_temp": 85.0}, now=T0)
        assert len(faults) == 1
        assert faults[0].kind is FaultKind.OUT_OF_RANGE
        assert faults[0].severity == 2

    @pytest.mark.parametrize("value", [85.0, -127.0])
    def test_DS18B20典型异常值文案点明原因(self, value):
        mon = make_monitor()
        fault = mon.observe({"freezer_temp": value}, now=T0)[0]
        assert DS18B20_SENTINELS[value][:12] in fault.message

    def test_正常值不报(self):
        mon = make_monitor()
        assert mon.observe({"fridge_temp": 4.0}, now=T0) == ()

    def test_同一次故障不重复返回(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        assert mon.observe({"fridge_temp": 85.0}, now=T0 + 10.0) == ()

    def test_恢复后清除故障(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        mon.observe({"fridge_temp": 4.0}, now=T0 + 10.0)
        assert mon.faulty() == ()
        assert mon.healthy("fridge_temp") is True


class TestStuck:
    def test_连续相同读数跨越窗口才报(self):
        mon = make_monitor(stuck_repeat=5, stuck_window_sec=30.0)
        for i in range(4):
            mon.observe({"fridge_temp": -18.0}, now=T0 + i)
        assert mon.observe({"fridge_temp": -18.0}, now=T0 + 4) == ()   # 还没到 30s
        faults = mon.observe({"fridge_temp": -18.0}, now=T0 + 40.0)
        # 第 5 个样本 + 跨越 30s → 报
        assert faults and faults[0].kind is FaultKind.STUCK

    def test_读数变化解除卡死(self):
        mon = make_monitor(stuck_repeat=3, stuck_window_sec=5.0)
        for i in range(6):
            mon.observe({"fridge_temp": -18.0}, now=T0 + i)
        assert mon.faulty()
        mon.observe({"fridge_temp": -17.9}, now=T0 + 10.0)
        assert mon.faulty() == ()

    def test_开机短时间不变不算卡死(self):
        mon = make_monitor(stuck_repeat=3, stuck_window_sec=300.0)
        for i in range(10):
            mon.observe({"fridge_temp": -18.0}, now=T0 + i)
        assert mon.faulty() == ()


class TestLost:
    def test_心跳丢失判故障(self):
        mon = make_monitor(heartbeat_timeout_sec=60.0)
        mon.observe({"fridge_temp": 4.0}, now=T0)
        assert mon.faulty() == ()
        faults = mon.observe({"t": 24.0}, now=T0 + 120.0)   # fridge 没数据了
        assert any(fault.kind is FaultKind.LOST for fault in faults)
        assert any(fault.metric == "fridge_temp" for fault in faults)

    def test_丢失比读数卡死更严重(self):
        assert FaultKind.LOST.severity > FaultKind.STUCK.severity

    def test_重新上报解除丢失(self):
        mon = make_monitor(heartbeat_timeout_sec=60.0)
        mon.observe({"fridge_temp": 4.0}, now=T0)
        mon.observe({"t": 24.0}, now=T0 + 120.0)
        assert mon.faulty()
        mon.observe({"fridge_temp": 4.1}, now=T0 + 130.0)
        assert all(fault.metric != "fridge_temp" for fault in mon.faulty())

    def test_从未上报的指标不算丢失(self):
        mon = SensorHealthMonitor(clock=FakeClock(start=T0))
        assert mon.observe({"t": 24.0}, now=T0 + 9999.0) == ()


class TestTicket:
    def test_故障开单带处理时限(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        tickets = mon.tickets()
        assert len(tickets) == 1
        assert tickets[0].due_at == T0 + 1800.0
        assert tickets[0].to_payload()["type"] == TICKET_TYPE

    def test_严重故障起步就是L2(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 4.0}, now=T0)
        mon.observe({"t": 24.0}, now=T0 + 120.0)
        lost = [t for t in mon.tickets() if t.kind is FaultKind.LOST]
        assert lost and lost[0].level == 2

    def test_超时未处理被列出升级(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        assert mon.due_escalations(now=T0 + 100.0) == ()
        due = mon.due_escalations(now=T0 + 1800.0 + 900.0)
        assert [ticket.id for ticket in due] == [1]

    def test_升级只做一次并记原因(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        ticket = mon.escalate(1, now=T0 + 3000.0)
        assert ticket.level == 2
        assert ticket.escalated_at == T0 + 3000.0
        assert "超时未处理" in ticket.notes[-1]
        assert mon.due_escalations(now=T0 + 4000.0) == ()

    def test_接单后不再升级(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        mon.ack(1, reviewer="运维:王")
        assert mon.due_escalations(now=T0 + 3000.0) == ()

    def test_恢复后不自动关单(self):
        """故障消失 ≠ 处理完成：设备自己关单，现场就没有记录证明故障发生过。"""
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        mon.observe({"fridge_temp": 4.0}, now=T0 + 10.0)
        ticket = mon.ticket(1)
        assert ticket.state == "open"
        assert any("待人工确认关闭" in note for note in ticket.notes)

    def test_关单后的新故障另开一单(self):
        mon = make_monitor(heartbeat_timeout_sec=1000.0)
        mon.observe({"fridge_temp": 85.0}, now=T0)
        mon.close(1, reviewer="运维:王", note="换探头", wall_epoch=T0 + 60.0)
        mon.observe({"fridge_temp": 4.0}, now=T0 + 70.0)
        mon.observe({"fridge_temp": -127.0}, now=T0 + 80.0)
        assert len(mon.tickets()) == 2

    def test_冷却期内复发记在同一条单上(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        mon.observe({"fridge_temp": 4.0}, now=T0 + 10.0)
        mon.observe({"fridge_temp": 85.0}, now=T0 + 20.0)
        assert len(mon.tickets()) == 1
        assert any("复发" in note for note in mon.ticket(1).notes)

    def test_关闭已关闭的单是幂等的(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        mon.close(1, wall_epoch=T0 + 10.0)
        assert mon.close(1, wall_epoch=T0 + 20.0).state == "closed"

    def test_接单已关闭的单报错(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        mon.close(1, wall_epoch=T0 + 10.0)
        with pytest.raises(ValueError):
            mon.ack(1, reviewer="运维:王")

    def test_不存在的单报错(self):
        mon = make_monitor()
        with pytest.raises(KeyError):
            mon.escalate(99)


class TestObservability:
    def test_usable反映可信度(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0, "t": 24.0}, now=T0)
        usable = mon.usable()
        assert usable["fridge_temp"] is False
        assert usable["t"] is True

    def test_snapshot结构(self):
        mon = make_monitor()
        mon.observe({"fridge_temp": 85.0}, now=T0)
        body = mon.snapshot(now=T0 + 5.0)
        assert body["open_tickets"] == 1
        assert body["faults"][0]["metric"] == "fridge_temp"
        assert body["series"]["fridge_temp"]["healthy"] is False
        assert body["series"]["fridge_temp"]["idle_sec"] == 5.0

    def test_单指标消费接口(self):
        mon = make_monitor()
        fault = mon.observe_metric("hood_temp", 900.0, now=T0)
        assert fault is not None and fault.kind is FaultKind.OUT_OF_RANGE

    def test_策略可定制量程(self):
        mon = SensorHealthMonitor(
            SensorPolicy(ranges={"weight_g": (0.0, 5000.0)}), clock=FakeClock(start=T0)
        )
        assert mon.observe({"weight_g": 4000.0}, now=T0) == ()
        assert mon.observe({"weight_g": 9000.0}, now=T0 + 1.0)
