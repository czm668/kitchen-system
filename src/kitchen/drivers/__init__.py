# -*- coding: utf-8 -*-
"""后厨场景驱动集合（12 遥测指标 + 三通道/独立 GPIO 执行末端）。"""
from __future__ import annotations

from kitchen.drivers.actuators import Relay3ChDriver, SignalDriver
from kitchen.drivers.base import DriverBase
from kitchen.drivers.sensors import (
    AmbientDriver,
    ColdchainDriver,
    CurrentDriver,
    FlameDriver,
    GasDriver,
    ScaleDriver,
    ThermalDriver,
)

TELEMETRY_DRIVERS = (
    AmbientDriver, GasDriver, ThermalDriver, ColdchainDriver,
    FlameDriver, CurrentDriver, ScaleDriver,
)

__all__ = [
    "DriverBase",
    "TELEMETRY_DRIVERS",
    "AmbientDriver", "GasDriver", "ThermalDriver", "ColdchainDriver",
    "FlameDriver", "CurrentDriver", "ScaleDriver",
    "Relay3ChDriver", "SignalDriver",
]
