# -*- coding: utf-8 -*-
"""GPIO 分配表校验：把「焊完才发现」的坑变成会报错的断言。

测试的重点不是覆盖所有代码行，而是钉住三类真实故障：
ADC2 在 WiFi 下读数随机、仅输入脚做输出、复用 flash 脚。
"""
from __future__ import annotations

import pytest

from kitchen.hal import pins


def _messages(issues: list[pins.Issue]) -> str:
    return "\n".join(str(i) for i in issues)


# --------------------------------------------------------------------------
# 当前分配表本身必须干净
# --------------------------------------------------------------------------
def test_current_plan_passes_validation():
    issues = pins.validate_plan()
    assert issues == [], f"默认分配表不应有问题：\n{_messages(issues)}"


def test_all_analog_inputs_are_on_adc1():
    """WiFi 常开，模拟输入只能在 ADC1——这是最贵的一个错，单独钉一条。"""
    analog = [u for u in pins.PIN_PLAN if u.role == pins.ANALOG_IN]
    assert analog, "分配表里应当有模拟输入"
    for usage in analog:
        assert usage.gpio in pins.ADC1_PINS, (
            f"{usage.peripheral} 的模拟输入在 GPIO{usage.gpio}，不是 ADC1")
        assert usage.gpio not in pins.ADC2_PINS


def test_no_pin_is_assigned_twice():
    gpios = [u.gpio for u in pins.PIN_PLAN]
    assert len(gpios) == len(set(gpios))


def test_expected_peripherals_are_all_present():
    """13 项硬件 + 声光，缺项说明分配表漏了设备。"""
    names = {u.peripheral for u in pins.PIN_PLAN}
    for expected in ("TFT", "RC522", "MAX6675_CH1", "MAX6675_CH2", "HX711",
                     "DHT11", "DS18B20", "FLAME_A", "FLAME_B", "MQ2", "SCT013",
                     "RELAY_CH1", "RELAY_CH2", "RELAY_CH3", "DFPLAYER",
                     "BUZZER", "LED"):
        assert expected in names, f"分配表缺少 {expected}"


# --------------------------------------------------------------------------
# 三类错配必须被拦住
# --------------------------------------------------------------------------
def test_analog_input_on_adc2_is_rejected():
    """把 MQ-2 的模拟输入挪到 13 脚（ADC2），必须报 error。

    这是本项目最贵的坑：WiFi 一开 ADC2 读数就是随机值，而且**不报错**，
    现场只会看到「气体浓度乱跳」，很难联想到引脚选错。
    """
    broken = [u for u in pins.PIN_PLAN if u.gpio != 35]
    broken = [
        pins.PinUsage(13, "MQ2", "AO", pins.ANALOG_IN) if u.gpio == 13 else u
        for u in broken
    ]
    issues = pins.validate_plan(broken)
    errors = [i for i in issues if i.severity == "error" and i.gpio == 13]
    assert errors, f"ADC2 上的模拟输入应报 error：\n{_messages(issues)}"
    assert "ADC2" in errors[0].message
    assert "WiFi" in errors[0].message


def test_output_on_input_only_pin_is_rejected():
    broken = [
        u if u.gpio != 34 else pins.PinUsage(34, "HX711", "DOUT", pins.DIGITAL_OUT)
        for u in pins.PIN_PLAN
    ]
    issues = pins.validate_plan(broken)
    errors = [i for i in issues if i.gpio == 34 and i.severity == "error"]
    assert errors
    assert "仅输入" in errors[0].message


def test_flash_pin_is_rejected():
    broken = list(pins.PIN_PLAN) + [pins.PinUsage(6, "TEST", "IO", pins.DIGITAL_OUT)]
    issues = pins.validate_plan(broken)
    assert any(i.gpio == 6 and "flash" in i.message for i in issues)


def test_duplicate_assignment_is_rejected():
    broken = list(pins.PIN_PLAN) + [pins.PinUsage(21, "EXTRA", "IO", pins.DIGITAL_OUT)]
    issues = pins.validate_plan(broken)
    assert any(i.gpio == 21 and "重复分配" in i.message for i in issues)


def test_strapping_pin_without_boot_level_warns():
    broken = [
        u if u.gpio != 15 else pins.PinUsage(15, "TFT", "CS", pins.DIGITAL_OUT)
        for u in pins.PIN_PLAN
    ]
    issues = pins.validate_plan(broken)
    warnings = [i for i in issues if i.gpio == 15 and i.severity == "warning"]
    assert warnings
    assert "strapping" in warnings[0].message


def test_non_adc_pin_declared_as_analog_is_rejected():
    broken = list(pins.PIN_PLAN) + [pins.PinUsage(21, "FAKE", "AO", pins.ANALOG_IN)]
    # 21 重复了，换成 20（既非 ADC1 也非 ADC2 的少数空闲脚）
    broken = [u for u in pins.PIN_PLAN if u.gpio != 21] + [
        pins.PinUsage(20, "FAKE", "AO", pins.ANALOG_IN)]
    issues = pins.validate_plan(broken)
    assert any(i.gpio == 20 and "不是 ADC" in i.message for i in issues)


# --------------------------------------------------------------------------
# PCF8575 腾脚方案
# --------------------------------------------------------------------------
def test_extension_plan_only_warns_about_boot_pin():
    issues = pins.validate_extension_plan()
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].gpio == 0
    assert "下载模式" in issues[0].message


def test_extension_plan_frees_expected_pins():
    assert pins.i2c_pins_after_extension() == (4, 0)


def test_extension_plan_references_existing_assignments():
    """腾脚方案假定的引脚必须真的被那些外设占着，否则方案已过时。"""
    by_gpio = {u.gpio: u for u in pins.PIN_PLAN}
    for step in pins.EXTENSION_PLAN:
        assert step.gpio in by_gpio, f"GPIO{step.gpio} 不在分配表里"
        usage = by_gpio[step.gpio]
        actual = f"{usage.peripheral} {usage.signal}"
        for token in step.current.split():
            assert token in actual, (
                f"GPIO{step.gpio} 当前是 {actual}，但腾脚方案写的是 {step.current}")


def test_extension_summary_mentions_i2c_and_boot_risk():
    text = pins.extension_plan_summary()
    assert "I2C" in text
    assert "4.7kΩ" in text
    assert "BOOT" in text or "下载模式" in text


# --------------------------------------------------------------------------
# 渲染与工具函数
# --------------------------------------------------------------------------
def test_plan_table_renders_all_rows():
    table = pins.plan_table()
    lines = table.strip().splitlines()
    assert len(lines) == len(pins.PIN_PLAN) + 2     # 表头 + 分隔线 + 每行一条
    assert "ADC2/WiFi下不可用" in table
    assert "仅输入" in table


def test_clone_with_changes_rejects_unknown_gpio():
    with pytest.raises(KeyError):
        pins.clone_with_changes(**{"99": pins.PinUsage(99, "X", "Y", pins.DIGITAL_OUT)})
