# -*- coding: utf-8 -*-
"""后厨场景 13 项硬件中的遥测侧驱动（P0 占位实现）。

设计口径（详见素材库 docs/kitchen-contract.md §2.3 环境指标命名）：
- 每个类对应一类硬件或一个采集单元；`metrics` 是该类唯一声明的指标集合，
  `scenarios/kitchen.json` 的 `drivers[].metrics` 必须与之完全一致（manifest 强校验）；
- `sample()` 返回**平铺** payload（不带驱动名分组），接入平台底座后由遥测展平逻辑
  落 `sensor_readings`；P0 只给稳定的基准占位值，供模拟器与设备页加载演示，
  **不代表真实量程/校准值**，P1 接真机后逐类替换；
- bool 量用 0/1 数值上报，与 `flatten_payload` 的布尔展平规则一致；
- 刷卡（RC522）、视觉（OV2640）、语音播报、声光、TFT 均不产生数值遥测，
  走独立事件主题 / 独立 GPIO，故本模块不定义对应驱动。

与素材库的关系：13 项硬件里 DHT11 与 MQ-2 在旧场景已有采样逻辑（PORT 分区），
需要时按 `tools/legacy.py fetch sensor-drivers` 取用后拷进本包改造，禁止跨场景直接 import。
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from kitchen.drivers.base import DriverBase


def _value(overrides: Mapping[str, float] | None, metric: str, default: float) -> float:
    """P0 占位取值：模拟器注入优先，否则给一个随时间缓慢变化的稳定基准。"""
    injected = (overrides or {}).get(metric)
    return float(default if injected is None else injected)


class AmbientDriver(DriverBase):
    """DHT11 后厨温湿度（定稿 #7：辅助展示，不参与报警判定）。"""

    id = "ambient"
    topic = "tele/env"
    metrics = ("t", "h")
    aliases = {"env": "t", "temp": "t", "temperature": "t", "humidity": "h"}

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        temp = _value(overrides, "t", 25.0 + 1.5 * math.sin(t / 600.0))
        humid = _value(overrides, "h", 58.0 + 4.0 * math.sin(t / 900.0))
        return {"t": round(temp, 2), "h": round(humid, 2)}


class GasDriver(DriverBase):
    """MQ-2 燃气浓度（定稿 #1）。

    注意：kitchen 场景中该指标语义为「燃气浓度 ADC 归一化值 0~1」，
    与 mingchu 的 `mq2`（烟雾）语义不同，靠场景隔离；基线算法在 P2 实现。
    """

    id = "gas"
    topic = "tele/gas"
    metrics = ("gas_level",)
    aliases = {"gas": "gas_level", "mq2": "gas_level"}

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        value = _value(overrides, "gas_level", 0.12 + 0.02 * math.sin(t / 300.0))
        return {"gas_level": round(value, 3)}


class ThermalDriver(DriverBase):
    """MAX6675 双路 K 型热电偶：CH1 排烟罩（#2）、CH2 消毒柜（#15）。"""

    id = "thermal"
    topic = "tele/thermal"
    metrics = ("hood_temp", "sterilize_temp")
    aliases = {"hood": "hood_temp", "sterilize": "sterilize_temp"}

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        hood = _value(overrides, "hood_temp", 32.0 + 6.0 * math.sin(t / 900.0))
        sterilize = _value(overrides, "sterilize_temp", 25.0)
        return {"hood_temp": round(hood, 1), "sterilize_temp": round(sterilize, 1)}


class ColdchainDriver(DriverBase):
    """DS18B20 单总线三路：冷藏（#5）、冷冻（#5）、留样柜（#6）。"""

    id = "coldchain"
    topic = "tele/cold"
    metrics = ("fridge_temp", "freezer_temp", "sample_temp")
    aliases = {"fridge": "fridge_temp", "freezer": "freezer_temp", "sample": "sample_temp"}

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        return {
            "fridge_temp": round(_value(overrides, "fridge_temp", 4.0), 1),
            "freezer_temp": round(_value(overrides, "freezer_temp", -18.0), 1),
            "sample_temp": round(_value(overrides, "sample_temp", 5.0), 1),
        }


class FlameDriver(DriverBase):
    """火焰传感器 ×2（定稿 #3 明火离人、#4 灶台点火校验），bool 0/1。"""

    id = "flame"
    topic = "tele/flame"
    metrics = ("flame_a", "flame_b")
    aliases = {"flame1": "flame_a", "flame2": "flame_b"}

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        return {
            "flame_a": int(_value(overrides, "flame_a", 0.0)),
            "flame_b": int(_value(overrides, "flame_b", 0.0)),
        }


class CurrentDriver(DriverBase):
    """SCT-013 电流（#4 判断风机运行、#15 判断消毒柜运行）。"""

    id = "current"
    topic = "tele/current"
    metrics = ("sct_current",)
    aliases = {"current": "sct_current", "sct013": "sct_current"}

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        return {"sct_current": round(_value(overrides, "sct_current", 0.0), 3)}


class ScaleDriver(DriverBase):
    """HX711 称重（#12 配菜称重 ±5%、#14 留样称重 125g）。"""

    id = "scale"
    topic = "tele/weight"
    metrics = ("weight_g",)
    aliases = {"weight": "weight_g", "hx711": "weight_g"}

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        return {"weight_g": round(_value(overrides, "weight_g", 0.0), 1)}
