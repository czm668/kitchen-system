# -*- coding: utf-8 -*-
"""ADC 换算层：衰减档、两点校准、电阻分压。

数值断言都带明确物理含义，改参数后失败能直接看出错在哪：
- 11 dB 档满量程 3100 mV / 4096 ≈ 0.757 mV/LSB
- 5 V 经 10k+20k 分压 = 3.333 V，**超** 3.3 V 供电轨 → 必须判不可用
- 5 V 经 10k+9.1k 分压 = 2.382 V，落在线性区内
"""
from __future__ import annotations

import pytest

from kitchen.hal import adc


# --------------------------------------------------------------------------
# 衰减档
# --------------------------------------------------------------------------
def test_attenuation_lsb_values():
    assert adc.ATTEN_11DB.lsb_mv() == pytest.approx(3100 / 4096)
    assert adc.ATTEN_0DB.lsb_mv() == pytest.approx(1100 / 4096)
    # 档位越低，量程越小、分辨率越高
    assert adc.ATTEN_0DB.lsb_mv() < adc.ATTEN_2_5DB.lsb_mv() < adc.ATTEN_11DB.lsb_mv()


def test_attenuations_table_contains_all_defined():
    assert set(adc.ATTENUATIONS) == {"0dB", "2.5dB", "6dB", "11dB"}
    for name, atten in adc.ATTENUATIONS.items():
        assert atten.name == name
        assert 0 < atten.linear_limit_mv < atten.full_scale_mv


# --------------------------------------------------------------------------
# 线性换算与诊断
# --------------------------------------------------------------------------
def test_linear_to_mv():
    ch = adc.AdcChannel(adc.ATTEN_11DB)
    assert ch.to_mv(0) == pytest.approx(0.0)
    assert ch.to_mv(4095) == pytest.approx(4095 * 3100 / 4096)
    assert ch.to_mv(2048) == pytest.approx(1550, abs=1.0)


def test_saturation_detection():
    ch = adc.AdcChannel()
    assert ch.is_saturated(0)
    assert ch.is_saturated(1)
    assert ch.is_saturated(4095)
    assert not ch.is_saturated(2000)


def test_nonlinear_zone_detection():
    ch = adc.AdcChannel(adc.ATTEN_11DB)
    # 2450 mV 对应 raw ≈ 3237
    assert not ch.is_nonlinear(3000)          # 2270 mV
    assert ch.is_nonlinear(3600)              # 2724 mV


def test_diagnosis_reports_actionable_text():
    ch = adc.AdcChannel()
    assert "偏置" in ch.diagnosis(0)
    assert "满量程" in ch.diagnosis(4095)
    assert "非线性" in ch.diagnosis(3600)
    assert ch.diagnosis(2000) == "正常"


# --------------------------------------------------------------------------
# 两点校准
# --------------------------------------------------------------------------
def test_two_point_calibration_interpolates():
    cal = adc.TwoPointCalibration(raw_low=100, mv_low=100.0, raw_high=3000, mv_high=2400.0)
    assert cal.to_mv(100) == pytest.approx(100.0)
    assert cal.to_mv(3000) == pytest.approx(2400.0)
    # 中点线性插值
    assert cal.to_mv(1550) == pytest.approx((100 + 2400) / 2, rel=1e-6)


def test_two_point_calibration_absorbs_gain_error():
    """标定后的通道应按真实斜率换算，而不是标称 0.757 mV/LSB。"""
    cal = adc.TwoPointCalibration(raw_low=0, mv_low=0.0, raw_high=4000, mv_high=2000.0)
    ch = adc.AdcChannel(calibration=cal)
    assert ch.lsb_mv == pytest.approx(0.5)
    assert ch.to_mv(4000) == pytest.approx(2000.0)


def test_two_point_calibration_rejects_degenerate_points():
    with pytest.raises(ValueError):
        adc.TwoPointCalibration(raw_low=100, mv_low=0.0, raw_high=100, mv_high=1.0)


def test_calibration_describe_is_readable():
    cal = adc.TwoPointCalibration(raw_low=0, mv_low=0.0, raw_high=4000, mv_high=2000.0)
    text = cal.describe()
    assert "µV/count" in text
    assert "500.000" in text          # 0.5 mV = 500 µV


# --------------------------------------------------------------------------
# 电阻分压
# --------------------------------------------------------------------------
def test_divider_ratio_and_roundtrip():
    div = adc.VoltageDivider(10_000, 20_000)
    assert div.ratio == pytest.approx(2 / 3)
    assert div.output_from_input(5.0) == pytest.approx(3.3333, rel=1e-4)
    assert div.input_from_output(div.output_from_input(4.2)) == pytest.approx(4.2)


def test_divider_rejects_non_positive_resistance():
    with pytest.raises(ValueError):
        adc.VoltageDivider(0, 10_000)
    with pytest.raises(ValueError):
        adc.VoltageDivider(10_000, -1)


def test_divider_10k_20k_is_rejected_for_5v_input():
    """文档旧写法 10k+20k：分压后 3.33 V 超 3.3 V 供电轨，必须判不可用。"""
    ok, hint = adc.VoltageDivider(10_000, 20_000).check(5.0)
    assert ok is False
    assert "满量程" in hint or "3.3" in hint


def test_divider_10k_9k1_is_accepted():
    ok, hint = adc.VoltageDivider(10_000, 9_100).check(5.0)
    assert ok is True
    assert "线性区" in hint


def test_divider_warns_in_nonlinear_zone():
    """1:1 分压 5 V → 2.5 V，超线性上限但未超满量程：可用但必须校准。"""
    ok, hint = adc.VoltageDivider(10_000, 10_000).check(5.0)
    assert ok is True
    assert "非线性" in hint


def test_divider_rejects_signal_too_small():
    """分压太狠导致信号不足 10% 量程，分辨率会被浪费。"""
    ok, hint = adc.VoltageDivider(1_000_000, 10_000).check(5.0)
    assert ok is False
    assert "分辨率" in hint


def test_max_measurable_input():
    div = adc.VoltageDivider(10_000, 10_000)
    assert div.max_measurable_input_mv(adc.ATTEN_11DB) == pytest.approx(6200.0)


# --------------------------------------------------------------------------
# 分压推荐
# --------------------------------------------------------------------------
def test_recommend_divider_keeps_candidates_in_linear_zone():
    for div, served in adc.recommend_divider(5.0):
        assert served <= adc.ATTEN_11DB.linear_limit_mv
        assert div.check(5.0)[0] is True


def test_recommend_divider_limits_result_count():
    assert len(adc.recommend_divider(5.0)) <= 10
    assert len(adc.recommend_divider(5.0, total_ohm_range=(1_000, 5_000))) <= 10


def test_recommend_divider_respects_total_resistance_window():
    candidates = adc.recommend_divider(5.0, total_ohm_range=(20_000, 40_000))
    for div, _ in candidates:
        total = div.r_top_ohm + div.r_bottom_ohm
        assert 20_000 <= total <= 40_000


def test_recommend_divider_top_choice_serves_signal_well():
    """5 V 输入的首选，分压后应在 2.2~2.45 V（既不浪费分辨率也不进非线性区）。"""
    best, served = adc.recommend_divider(5.0)[0]
    assert 2200 <= served <= adc.ATTEN_11DB.linear_limit_mv
    # 源阻抗要满足 ESP32 ADC 建议的 <10 kΩ
    source_impedance = best.r_top_ohm * best.r_bottom_ohm / (
        best.r_top_ohm + best.r_bottom_ohm)
    assert source_impedance < 10_000
