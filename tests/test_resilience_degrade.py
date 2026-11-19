# -*- coding: utf-8 -*-
"""三端降级标记（A6-05）。"""
from __future__ import annotations

from kitchen.resilience.degrade import (
    CAP_GAS,
    CAP_REPLAY,
    CAP_VISION_DISPOSAL,
    CAP_VISION_FLAME,
    STATE_DEGRADED,
    STATE_FULL,
    STATE_OFF,
    build_degrade,
)

TS = 1_700_000_000.0


class TestNormal:
    def test_全绿(self):
        report = build_degrade(wall_epoch=TS)
        assert report.severity == "normal"
        assert report.degraded is False
        assert report.unavailable() == ()
        assert report.describe() == "运行正常：全部能力可用"

    def test_燃气联锁永不受网络影响(self):
        """这是「断网也安全」的核心论据，断网时它的状态必须还是 full。"""
        report = build_degrade(network_online=False, wall_epoch=TS)
        assert report.capability(CAP_GAS).state == STATE_FULL


class TestOffline:
    def test_离线时上报能力关闭(self):
        report = build_degrade(network_online=False, queue_pending=37, wall_epoch=TS)
        assert report.severity == "offline"
        assert report.capability(CAP_REPLAY).state == STATE_OFF
        assert "37 条" in report.capability(CAP_REPLAY).reason

    def test_离线文案必须说明本地仍生效(self):
        """只显示「设备离线」，现场会去拉总闸（见模块 docstring 第 1 条）。"""
        report = build_degrade(network_online=False, queue_pending=5, wall_epoch=TS)
        assert "本地保护仍生效" in report.headline
        assert report.channels["tft"]["note"] == "本地保护仍生效"
        assert any("本地安全保护始终生效" in line
                   for line in build_degrade(wall_epoch=TS).channels["web"]["tooltip"])

    def test_离线红框(self):
        assert build_degrade(network_online=False, wall_epoch=TS).channels["tft"]["box"] == "red"
        assert build_degrade(wall_epoch=TS).channels["tft"]["box"] == "none"


class TestVision:
    def test_视觉离线两项能力都降级(self):
        report = build_degrade(vision={"online": False, "age_sec": 45.0}, wall_epoch=TS)
        assert report.capability(CAP_VISION_FLAME).state == STATE_DEGRADED
        assert report.capability(CAP_VISION_DISPOSAL).state == STATE_OFF
        assert report.severity == "degraded"
        assert "安全能力降级" in report.headline

    def test_降级要逐条说清哪些能力没了(self):
        report = build_degrade(vision={"online": False}, wall_epoch=TS)
        body = report.describe()
        assert "着装与行为监管" in body
        assert "仅看火焰" in body
        assert {item.key for item in report.unavailable()} == {
            CAP_VISION_FLAME, CAP_VISION_DISPOSAL
        }

    def test_视觉在线时全绿(self):
        report = build_degrade(vision={"online": True, "age_sec": 0.2}, wall_epoch=TS)
        assert report.unavailable() == ()


class TestSensors:
    def test_传感器故障列进降级(self):
        faults = [{"metric": "fridge_temp", "label": "超量程"}]
        report = build_degrade(sensor_faults=faults, wall_epoch=TS)
        assert report.severity == "degraded"
        reason = report.capability("sensor_integrity").reason
        assert "fridge_temp" in reason and "超量程" in reason

    def test_接受SensorFault对象(self):
        class FakeFault:
            metric = "hood_temp"

            class kind:
                label = "读数卡死"

        report = build_degrade(sensor_faults=[FakeFault()], wall_epoch=TS)
        assert "hood_temp" in report.capability("sensor_integrity").reason

    def test_配置降级时报警能力也降级(self):
        report = build_degrade(config_degraded=True, wall_epoch=TS)
        assert report.capability("config_sync").state == STATE_DEGRADED
        assert report.capability("threshold_alarm").state == STATE_DEGRADED


class TestChannels:
    def test_三端文案齐备(self):
        report = build_degrade(network_online=False, queue_pending=3, wall_epoch=TS)
        assert set(report.channels) == {"tft", "web", "miniapp"}
        assert report.channels["web"]["level"] == "danger"
        assert report.channels["miniapp"]["status"].startswith("离线")

    def test_TFT最多四行(self):
        report = build_degrade(
            network_online=False, queue_pending=9,
            vision={"online": False},
            sensor_faults=[{"metric": a} for a in ("t", "h", "gas_level")],
            wall_epoch=TS,
        )
        assert len(report.channels["tft"]["lines"]) <= 4

    def test_降级时TFT带图标行(self):
        report = build_degrade(vision={"online": False}, wall_epoch=TS)
        lines = report.channels["tft"]["lines"]
        assert any(line.startswith("○") or line.startswith("◐") for line in lines)

    def test_Web给出受影响清单(self):
        report = build_degrade(vision={"online": False}, wall_epoch=TS)
        affected = report.channels["web"]["affected"]
        assert {item["key"] for item in affected} == {CAP_VISION_FLAME, CAP_VISION_DISPOSAL}

    def test_小程序提示自动补传(self):
        report = build_degrade(network_online=False, wall_epoch=TS)
        assert "自动补传" in report.channels["miniapp"]["hint"]


class TestClock:
    def test_未校准给提示(self):
        report = build_degrade(clock_stable=False, wall_epoch=TS)
        assert any("未校准" in line for line in report.channels["web"]["tooltip"])

    def test_偏差大给出校正说明(self):
        report = build_degrade(clock_stable=True, clock_offset=3.4, wall_epoch=TS)
        assert any("+3.4s" in line for line in report.channels["web"]["tooltip"])

    def test_偏差小不打扰(self):
        report = build_degrade(clock_stable=True, clock_offset=0.2, wall_epoch=TS)
        assert not any("偏差" in line for line in report.channels["web"]["tooltip"])


class TestPayload:
    def test_可序列化且含不可用清单(self):
        import json

        report = build_degrade(vision={"online": False}, queue_pending=2, wall_epoch=TS)
        body = report.to_payload()
        json.dumps(body, ensure_ascii=False)
        assert body["unavailable"] == [CAP_VISION_FLAME, CAP_VISION_DISPOSAL]
        assert body["queue_pending"] == 2
        assert body["ts"] == TS

    def test_capability描述(self):
        report = build_degrade(vision={"online": False}, wall_epoch=TS)
        text = report.capability(CAP_VISION_DISPOSAL).describe()
        assert "不可用" in text
        assert report.capability(CAP_GAS).describe().startswith("●")
