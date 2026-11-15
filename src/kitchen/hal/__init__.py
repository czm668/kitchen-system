# -*- coding: utf-8 -*-
"""硬件抽象层（HAL）：把「引脚约束」和「ADC 读数」翻译成业务能用的物理量。

这一层存在的理由，是文档 `03_设计文档/02_硬件清单与GPIO分配.md` §4 列的五个坑，
每一个都属于「写代码时看不出来、焊完板子才发现」的类型：

| 坑 | 后果 | 本模块里的对策 |
|---|---|---|
| ADC2 在 WiFi 下不可用 | 读数是随机值，且不报错 | `pins.validate_plan()` 静态拦截 |
| GPIO34/35/36/39 仅输入 | 代码不报错，外设就是不动 | 同上，角色不符即报 error |
| MQ-2 输出 0~5 V | 直连烧引脚 | `VoltageDivider` + `GasChannel` 反算 |
| SCT-013 是交流 | 直接取平均得 0 | `AcCurrentChannel.rms_mv()` + 分辨率诊断 |
| 火焰模块极性不统一 | 有火判成无火，明火离人失效 | `FlameChannel.auto_orient()` |

模块划分：
- `pins`     —— 引脚能力常量、分配表、静态校验、PCF8575 腾脚方案
- `adc`      —— 衰减档量程、两点校准、电阻分压换算与推荐
- `frontend` —— MQ-2 / 火焰 / SCT-013 / HX711 四类模拟通道 → 契约量纲

全部纯标准库、无硬件依赖，可在 PC 上直接单测；接真机时只需一层 `analogRead()` 适配。
"""
from kitchen.hal.adc import (
    ATTENUATIONS,
    ATTEN_0DB,
    ATTEN_2_5DB,
    ATTEN_6DB,
    ATTEN_11DB,
    AdcChannel,
    Atten,
    TwoPointCalibration,
    VoltageDivider,
    recommend_divider,
)
from kitchen.hal.frontend import (
    AcCurrentChannel,
    CurrentDiagnostic,
    FlameChannel,
    FrontendConfig,
    GasChannel,
    ScaleChannel,
    default_frontend,
)
from kitchen.hal.pins import (
    ADC1_PINS,
    ADC2_PINS,
    FLASH_PINS,
    INPUT_ONLY_PINS,
    PIN_PLAN,
    STRAPPING_PINS,
    Issue,
    PinUsage,
    extension_plan_summary,
    i2c_pins_after_extension,
    plan_table,
    validate_extension_plan,
    validate_plan,
)

__all__ = [
    # pins
    "ADC1_PINS", "ADC2_PINS", "INPUT_ONLY_PINS", "FLASH_PINS", "STRAPPING_PINS",
    "PIN_PLAN", "PinUsage", "Issue", "validate_plan", "validate_extension_plan",
    "i2c_pins_after_extension", "extension_plan_summary", "plan_table",
    # adc
    "Atten", "ATTENUATIONS", "ATTEN_0DB", "ATTEN_2_5DB", "ATTEN_6DB", "ATTEN_11DB",
    "AdcChannel", "TwoPointCalibration", "VoltageDivider", "recommend_divider",
    # frontend
    "GasChannel", "FlameChannel", "AcCurrentChannel", "ScaleChannel",
    "CurrentDiagnostic", "FrontendConfig", "default_frontend",
]
