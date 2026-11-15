# -*- coding: utf-8 -*-
"""模拟前端四类通道：MQ-2 / 火焰 / SCT-013 / HX711。

这些断言对应「现场会怎么坏」：
- MQ-2 用电压当浓度 → 基线漂了就误报，所以必须对基线归一化；
- 火焰模块极性反了 → 有火判无火，明火离人失效，所以要自判；
- SCT-013 取平均 → 永远是 0，所以要算 RMS；
- HX711 没有标定 → 报出来的克数是随机数，所以要 tare + calibrate。
"""
from __future__ import annotations

import math

import pytest

from kitchen.hal import adc, frontend
from kitchen.manifest import TELEMETRY_METRICS


# --------------------------------------------------------------------------
# 工具：造一段 50 Hz 正弦采样，用来验证 RMS → 安培的还原精度
# --------------------------------------------------------------------------
def _sine_raws(ch: frontend.AcCurrentChannel, amps: float,
               cycles: int = 10, samples: int = 400) -> list[int]:
    lsb = ch.adc.lsb_mv
    peak_mv = ch.signal_rms_mv(amps) * math.sqrt(2)
    raws = []
    for i in range(samples):
        phase = 2 * math.pi * cycles * i / samples
        mv = ch.bias_mv + peak_mv * math.sin(phase)
        raws.append(max(0, min(4095, round(mv / lsb))))
    return raws


def _gas_channel(divider: adc.VoltageDivider | None = None,
                 baseline_rs: float | None = None) -> frontend.GasChannel:
    return frontend.GasChannel(
        adc=adc.AdcChannel(adc.ATTEN_11DB),
        divider=divider or adc.VoltageDivider(10_000, 9_100),
        v_module=5.0,
        r_load_ohm=10_000.0,
        alarm_k=4.0,
        baseline_rs_ohm=baseline_rs,
    )


def _raw_for_module_volts(ch: frontend.GasChannel, v_out: float) -> int:
    """给定 MQ-2 模块 AO 电压，算出对应的 ADC raw。"""
    adc_mv = ch.divider.output_from_input(v_out) * 1000.0
    return round(adc_mv / ch.adc.lsb_mv)


# --------------------------------------------------------------------------
# MQ-2 → gas_level
# --------------------------------------------------------------------------
def test_gas_rs_formula():
    ch = _gas_channel()
    raw = _raw_for_module_volts(ch, 2.0)
    # Rs = (Vc − Vout)/Vout × RL = (5−2)/2 × 10k = 15k
    assert ch.rs_ohm(raw) == pytest.approx(15_000, rel=0.02)


def test_gas_rs_handles_extremes():
    ch = _gas_channel()
    assert ch.rs_ohm(0) == float("inf")       # 输出贴地
    # 模块输出等于 Vcc 时 Rs≈0；用分压能表达的最大 ADC 值构造
    assert ch.rs_ohm(4095) < 100


def test_gas_baseline_uses_median_not_mean():
    """油烟脉冲会造成个别高读数，中位数不受影响，均值会被拉偏。"""
    ch = _gas_channel()
    clean = _raw_for_module_volts(ch, 2.0)      # Rs = 15k
    spike = _raw_for_module_volts(ch, 4.0)      # Rs = 2.5k，模拟油烟尖峰
    median = ch.capture_baseline([clean] * 9 + [spike])
    assert median == pytest.approx(15_000, rel=0.02)


def test_gas_level_is_zero_at_baseline():
    ch = _gas_channel(baseline_rs=15_000)
    raw = _raw_for_module_volts(ch, 2.0)        # Rs 回到基线
    assert ch.level(raw) == pytest.approx(0.0, abs=0.02)


def test_gas_level_reaches_one_at_alarm_k():
    ch = _gas_channel(baseline_rs=15_000)
    # Rs 降到基线 1/4 = 3750Ω → Vout: Rs=(5-V)/V*10k=3750 → V=3.636
    raw = _raw_for_module_volts(ch, 3.636)
    assert ch.level(raw) == pytest.approx(1.0, abs=0.02)


def test_gas_level_is_monotonic_with_concentration():
    ch = _gas_channel(baseline_rs=15_000)
    levels = [ch.level(_raw_for_module_volts(ch, v)) for v in (2.0, 2.5, 3.0, 3.5)]
    assert levels == sorted(levels), "浓度上升时 level 必须单调不减"
    assert levels[0] < levels[-1]


def test_gas_level_requires_baseline():
    ch = _gas_channel()
    with pytest.raises(RuntimeError, match="基线"):
        ch.level(1000)


def test_gas_baseline_rejects_empty_and_invalid():
    ch = _gas_channel()
    with pytest.raises(ValueError):
        ch.capture_baseline([])
    with pytest.raises(ValueError):
        ch.capture_baseline([0, 0, 0])       # 全部贴地，Rs 无穷


def test_gas_crossover_rate_detects_rising_leak():
    ch = _gas_channel(baseline_rs=15_000)
    samples = [
        (0.0, _raw_for_module_volts(ch, 2.0)),
        (30.0, _raw_for_module_volts(ch, 2.5)),
        (60.0, _raw_for_module_volts(ch, 3.0)),
    ]
    rate = ch.crossover_rate(samples, window_s=60.0)
    assert rate > 0, "浓度上升时突变率应为正"


def test_gas_crossover_rate_is_zero_for_flat_signal():
    ch = _gas_channel(baseline_rs=15_000)
    raw = _raw_for_module_volts(ch, 2.0)
    assert ch.crossover_rate([(0.0, raw), (10.0, raw), (20.0, raw)]) == pytest.approx(0.0)


def test_gas_crossover_rate_validates_input():
    ch = _gas_channel(baseline_rs=15_000)
    with pytest.raises(ValueError):
        ch.crossover_rate([(0.0, 1000)])


# --------------------------------------------------------------------------
# 火焰 → 0/1
# --------------------------------------------------------------------------
def test_flame_hysteresis_holds_state_in_dead_band():
    ch = frontend.FlameChannel(adc.AdcChannel(), threshold_raw=1500,
                               hysteresis_raw=120, active_high=True)
    assert ch.update(1800) == 1            # 超上阈 → 有火
    assert ch.update(1450) == 1            # 落在死区内 → 保持
    assert ch.update(1300) == 0            # 低于下阈 → 无火
    assert ch.update(1550) == 0            # 死区内 → 保持无火
    assert ch.update(1700) == 1


def test_flame_active_low_polarity():
    """部分模块有火时 AO 反而降低，极性必须能配。"""
    ch = frontend.FlameChannel(adc.AdcChannel(), threshold_raw=1500,
                               hysteresis_raw=120, active_high=False)
    assert ch.update(1000) == 1
    assert ch.update(1450) == 1
    assert ch.update(1700) == 0


def test_flame_auto_orient_detects_rising_polarity():
    ch = frontend.FlameChannel(adc.AdcChannel())
    assert ch.auto_orient(idle_raw=800, flame_raw=2500) is True
    assert 800 < ch.threshold_raw < 2500
    assert 30 <= ch.hysteresis_raw <= 400


def test_flame_auto_orient_detects_falling_polarity():
    ch = frontend.FlameChannel(adc.AdcChannel())
    assert ch.auto_orient(idle_raw=2600, flame_raw=900) is False


def test_flame_auto_orient_rejects_identical_readings():
    ch = frontend.FlameChannel(adc.AdcChannel())
    with pytest.raises(ValueError, match="极性"):
        ch.auto_orient(idle_raw=1200, flame_raw=1200)


def test_flame_describe_is_readable():
    ch = frontend.FlameChannel(adc.AdcChannel(), name="flame_a")
    ch.auto_orient(800, 2500)
    text = ch.describe()
    assert "flame_a" in text
    assert "升高" in text


# --------------------------------------------------------------------------
# SCT-013 → 安培
# --------------------------------------------------------------------------
def test_sct_rms_ignores_dc_bias():
    """只有偏置、没有交流信号时 RMS 必须是 0——这正是「直接取平均会错」的反面。"""
    ch = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=100.0,
                                   bias_mv=1650.0)
    flat = [round(1650 / ch.adc.lsb_mv)] * 100
    assert ch.rms_mv(flat) == pytest.approx(0.0, abs=1e-6)
    assert ch.amps(flat) == pytest.approx(0.0, abs=1e-6)


def test_sct_roundtrip_restores_current():
    ch = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=100.0)
    for amps in (1.0, 1.8, 5.0):
        restored = ch.amps(_sine_raws(ch, amps))
        assert restored == pytest.approx(amps, rel=0.03), (
            f"注入 {amps}A，还原 {restored:.3f}A")


def test_sct_diagnose_flags_too_small_signal():
    ch = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=100.0)
    diag = ch.diagnose(0.2)
    assert diag.ok is True                     # 可用但不宽裕
    assert diag.counts_rms < 30
    assert "counts" in diag.hint


def test_sct_diagnose_marks_insufficient_counts_as_unusable():
    """33Ω 是常见抄来的值，但 0.3A 时只有 6 counts，必须判不可用。"""
    ch = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=33.0)
    diag = ch.diagnose(0.3)
    assert diag.ok is False
    assert "counts" in diag.hint


def test_sct_diagnose_flags_peak_over_range():
    ch = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=100.0)
    diag = ch.diagnose(22.0)
    assert diag.ok is False
    assert "削顶" in diag.hint


def test_sct_headroom_matches_full_scale():
    """可测上限应满足：偏置 + 峰值 = 满量程。"""
    ch = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=100.0)
    diag = ch.diagnose(1.0)
    peak_at_limit = ch.signal_rms_mv(diag.headroom_amps) * math.sqrt(2)
    assert ch.bias_mv + peak_at_limit == pytest.approx(
        ch.adc.atten.full_scale_mv, rel=1e-6)


def test_sct_required_burden_hits_target_counts():
    ch = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=100.0)
    burden = ch.required_burden(1.0, target_counts=80)
    rebuilt = frontend.AcCurrentChannel(adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=burden)
    assert rebuilt.diagnose(1.0).counts_rms == pytest.approx(80, rel=0.02)


def test_sct_samples_per_reading_covers_ten_cycles():
    ch = frontend.AcCurrentChannel(adc.AdcChannel(), samples_per_cycle=40)
    assert ch.samples_per_reading == 400


def test_sct_bias_recommendation_is_half_scale():
    assert frontend.AcCurrentChannel.bias_volts_for(adc.ATTEN_11DB) == pytest.approx(1550)


# --------------------------------------------------------------------------
# HX711 → 克
# --------------------------------------------------------------------------
def test_scale_tare_sets_zero():
    ch = frontend.ScaleChannel(counts_per_gram=42.0)
    offset = ch.tare([100, 101, 99, 100, 500])   # 中位数抗单个离群点
    assert offset == pytest.approx(100)
    assert ch.grams(100) == pytest.approx(0.0, abs=1e-9)


def test_scale_calibrate_from_known_weight():
    ch = frontend.ScaleChannel()
    ch.tare([1000] * 5)
    cpg = ch.calibrate(known_grams=100, raw=1000 + 4200)
    assert cpg == pytest.approx(42.0)
    assert ch.grams(1000 + 4200) == pytest.approx(100.0)


def test_scale_calibration_validates_input():
    ch = frontend.ScaleChannel()
    ch.tare([1000] * 5)
    with pytest.raises(ValueError):
        ch.calibrate(known_grams=0, raw=2000)
    with pytest.raises(ValueError):
        ch.calibrate(known_grams=100, raw=1000)   # 与零点相同


def test_scale_median_filter_suppresses_single_spike():
    """一次跳变不能让「±5% 锁定下一步」误触发。"""
    ch = frontend.ScaleChannel(counts_per_gram=42.0, offset_counts=0, median_window=5)
    for _ in range(4):
        ch.grams(42 * 125)                       # 稳定的 125 g
    spiked = ch.grams(42 * 125 + 5000)           # 单点大幅跳变
    assert spiked == pytest.approx(125.0), "中位数滤波应吃掉单点尖峰"


def test_scale_deviation_matches_judgement_threshold():
    ch = frontend.ScaleChannel(counts_per_gram=10.0, offset_counts=0, median_window=1)
    assert ch.deviation(10 * 125, 125) == pytest.approx(0.0)
    assert ch.deviation(10 * 130, 125) == pytest.approx(0.04)    # +4%，未超 ±5%
    assert abs(ch.deviation(10 * 135, 125)) > 0.05               # +8%，应锁定
    with pytest.raises(ValueError):
        ch.deviation(100, 0)


# --------------------------------------------------------------------------
# 与场景契约对齐（防止 HAL 输出名和 kitchen.json 脱节）
# --------------------------------------------------------------------------
def test_hal_outputs_match_scenario_metrics():
    """HAL 负责的 4 个指标必须都在 manifest 的遥测清单里。"""
    for metric in ("gas_level", "flame_a", "flame_b", "sct_current", "weight_g"):
        assert metric in TELEMETRY_METRICS


def test_default_frontend_is_self_consistent():
    """出厂推荐参数自身必须能通过校验，否则文档和代码互相打脸。"""
    cfg = frontend.default_frontend()
    ok, hint = cfg.gas_divider.check(5.0)
    assert ok is True, hint
    ch = frontend.AcCurrentChannel(
        adc.AdcChannel(adc.ATTEN_11DB), burden_ohm=cfg.current_burden_ohm,
        bias_mv=cfg.current_bias_mv)
    assert ch.diagnose(1.8).ok is True      # 演示用的排风机电流
