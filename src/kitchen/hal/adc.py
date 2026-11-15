# -*- coding: utf-8 -*-
"""ESP32 ADC 与分压网络的换算层（纯函数，可在 PC 上单测）。

这一层解决的问题：**ADC 读数本身不是物理量**。`analogRead()` 给你 0~4095，
要变成电压需要知道衰减档、要变成真实电压还得反算分压比、要长期可信还得校准。

三个必须搞清楚的点（写错任何一条，读数都会系统性偏掉）：

1. **衰减档决定量程，不是固定的 3.3V**。ESP32 默认 11dB 档满量程约 3.1V，
   但 `analogSetAttenuation()` 设成 0dB 时只有约 1.1V——同一个读数换档后含义完全不同。
2. **11dB 档在 2.45V 以上进入非线性区**，读数会突然变缓。测量值落在这一段必须
   用分段校准，否则高温/高浓度区间读数失真，而这一段恰好是报警区间。
3. **分压比是反算的**：ADC 量到的是分压后的电压，真实探头电压要乘回 (R_top+R_bottom)/R_bottom。
   而且分压电阻本身有 1% 误差，会直接变成 1% 的系统误差。

工程上最实用的校准方式是**两点校准**：用一个已知电压测两次，得到两个 (raw, mV) 点，
线性插值得出通道的实际增益与偏移。这比查手册曲线更省事，也能吸收板间差异。
"""
from __future__ import annotations

from dataclasses import dataclass

#: ESP32 ADC 位数（Arduino-ESP32 默认 12 位）
DEFAULT_BITS = 12

#: 分压电阻可选值（E24 系列 10Ω~1MΩ 常用段），用于推荐组合
E24_SERIES: tuple[int, ...] = tuple(
    int(base * mult)
    for mult in (1, 10, 100, 1_000, 10_000, 100_000)
    for base in (
        10, 11, 12, 13, 15, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39, 43,
        47, 51, 56, 62, 68, 75, 82, 91,
    )
)


@dataclass(frozen=True)
class Atten:
    """ADC 衰减档。

    `full_scale_mv` 是标称满量程，`linear_limit_mv` 之后进入非线性区。
    数值取自 ESP-IDF `adc_channel` 文档的典型值，实际以自校准为准。
    """

    name: str
    full_scale_mv: float
    linear_limit_mv: float
    note: str = ""

    def lsb_mv(self, bits: int = DEFAULT_BITS) -> float:
        return self.full_scale_mv / (1 << bits)


ATTEN_0DB = Atten("0dB", 1100.0, 950.0, "量程最小、分辨率最高；仅适合 ≤1.1V 信号，常用于带偏置的交流采样")
ATTEN_2_5DB = Atten("2.5dB", 1500.0, 1250.0, "折中档；偏置 0.75V 时可采 ≈±0.7V 峰值")
ATTEN_6DB = Atten("6dB", 2200.0, 1750.0, "少用")
ATTEN_11DB = Atten("11dB", 3100.0, 2450.0, "Arduino 默认档；>2.45V 非线性，报警区间常落在此段，务必校准")

ATTENUATIONS: dict[str, Atten] = {
    a.name: a for a in (ATTEN_0DB, ATTEN_2_5DB, ATTEN_6DB, ATTEN_11DB)
}


@dataclass(frozen=True)
class TwoPointCalibration:
    """两点校准：把 raw 映射回毫伏。

    用万用表量两个输入电压，分别记下 ADC 读数，其余点线性插值。
    这能同时吸收：衰减档误差、参考电压偏差、分压电阻实际误差。
    """

    raw_low: int
    mv_low: float
    raw_high: int
    mv_high: float

    def __post_init__(self) -> None:
        if self.raw_high == self.raw_low:
            raise ValueError("两点校准的两个 raw 值不能相同（量程会退化为无穷斜率）")

    def to_mv(self, raw: float) -> float:
        slope = (self.mv_high - self.mv_low) / (self.raw_high - self.raw_low)
        return self.mv_low + (raw - self.raw_low) * slope

    def describe(self) -> str:
        slope = (self.mv_high - self.mv_low) / (self.raw_high - self.raw_low)
        return (f"标定斜率 {slope * 1000:.3f} µV/count，"
                f"零点 {self.to_mv(0):.1f} mV（未标定时理论值 0）")


class AdcChannel:
    """一路 ADC 输入：负责 raw → 毫伏。

    有校准参数时优先用校准值；没有则按衰减档标称满量程线性换算。
    """

    def __init__(
        self,
        atten: Atten = ATTEN_11DB,
        bits: int = DEFAULT_BITS,
        calibration: TwoPointCalibration | None = None,
        name: str = "adc",
    ) -> None:
        self.atten = atten
        self.bits = bits
        self.calibration = calibration
        self.name = name
        self._max_raw = (1 << bits) - 1

    # --- 换算 -------------------------------------------------------------
    def to_mv(self, raw: float) -> float:
        raw = float(raw)
        if self.calibration is not None:
            return self.calibration.to_mv(raw)
        return raw * self.atten.lsb_mv(self.bits)

    @property
    def lsb_mv(self) -> float:
        if self.calibration is not None:
            return (self.calibration.mv_high - self.calibration.mv_low) / (
                self.calibration.raw_high - self.calibration.raw_low)
        return self.atten.lsb_mv(self.bits)

    # --- 诊断 -------------------------------------------------------------
    def is_saturated(self, raw: int) -> bool:
        """贴着 0 或满量程，说明信号被削顶——换档或改分压，别再调软件。"""
        return raw <= 1 or raw >= self._max_raw - 1

    def is_nonlinear(self, raw: float) -> bool:
        return self.to_mv(raw) > self.atten.linear_limit_mv

    def diagnosis(self, raw: int) -> str:
        if raw <= 1:
            return "读数贴 0：信号为负或偏置丢失，检查偏置电路与接线"
        if raw >= self._max_raw - 1:
            return f"读数满量程（{self.atten.name} 档 ≈{self.atten.full_scale_mv:.0f} mV）：信号超量程，需加大分压比或降档"
        if self.is_nonlinear(raw):
            return (f"读数 {self.to_mv(raw):.0f} mV 已进入 {self.atten.name} 档非线性区"
                    f"（>{self.atten.linear_limit_mv:.0f} mV），必须用两点校准或改分压")
        return "正常"


@dataclass(frozen=True)
class VoltageDivider:
    """电阻分压网络：把探头输出电压降到 ADC 量程内。

    `r_top` 接信号源与 ADC 之间，`r_bottom` 接 ADC 到地：

        信号源 ──[R_top]──┬── ADC
                         [R_bottom]
                          GND
    """

    r_top_ohm: float
    r_bottom_ohm: float

    def __post_init__(self) -> None:
        if self.r_top_ohm <= 0 or self.r_bottom_ohm <= 0:
            raise ValueError("分压电阻必须为正")

    @property
    def ratio(self) -> float:
        """输出/输入。"""
        return self.r_bottom_ohm / (self.r_top_ohm + self.r_bottom_ohm)

    def output_from_input(self, v_in: float) -> float:
        return v_in * self.ratio

    def input_from_output(self, v_out: float) -> float:
        return v_out / self.ratio

    def max_measurable_input_mv(self, atten: Atten = ATTEN_11DB) -> float:
        """该分压下，ADC 能测到的最大原始信号电压（mV）。"""
        return atten.full_scale_mv / self.ratio

    def check(self, v_in_max: float, atten: Atten = ATTEN_11DB,
              headroom: float = 0.10) -> tuple[bool, str]:
        """检查分压后是否落在量程内、且尽量待在线性区。

        `v_in_max` 单位是 **V**（探头侧最高输出电压），内部统一换算成 mV 比较。

        判定分四档，因为「能用」和「好用」不是一回事：
        - 超满量程 → 不可用：会顶到输入保护，读数削顶甚至损伤引脚
        - 进非线性区 → 可用但必须校准：11 dB 档 >2.45 V 后曲线明显变缓
        - 低于 10% 量程 → 不推荐：分辨率浪费，弱信号会被量化噪声吃掉
        - 其余 → 合适
        """
        v_out_mv = self.output_from_input(v_in_max) * 1000.0
        full = atten.full_scale_mv
        linear = atten.linear_limit_mv

        if v_out_mv > full:
            return False, (f"分压后 {v_out_mv:.0f} mV 超 {atten.name} 档满量程 {full:.0f} mV，"
                           f"会削顶并触发输入保护；必须加大 R_top")
        if v_out_mv > linear:
            return True, (f"分压后 {v_out_mv:.0f} mV 落入 {atten.name} 档非线性区"
                          f"（>{linear:.0f} mV），读数会偏小；需两点校准或加大分压比")
        if v_out_mv < full * headroom:
            return False, (f"分压后仅 {v_out_mv:.0f} mV，不足满量程 {headroom:.0%}，"
                           f"分辨率浪费；减小 R_top 或提高 R_bottom")
        return True, (f"分压后最高 {v_out_mv:.0f} mV，占满量程 {v_out_mv / full:.0%}，"
                      f"在线性区内")


def recommend_divider(
    v_in_max: float,
    atten: Atten = ATTEN_11DB,
    target_fill: float = 0.95,
    total_ohm_range: tuple[int, int] = (10_000, 200_000),
) -> list[tuple[VoltageDivider, float]]:
    """给定最大输入电压，从 E24 里挑最合适的分压组合。

    判据用**线性区上限**而不是满量程：11 dB 档满量程 3100 mV，但 >2450 mV 就进入
    非线性区，而报警判据恰好落在高段。所以目标是让最高输入落在
    `linear_limit × target_fill`（默认 95%，即 ≈2327 mV）。

    同时约束：
    - 总阻值落在 `total_ohm_range`：太小费电，太大则源阻抗高、ADC 采样不稳
      （ESP32 ADC 建议信号源阻抗 < 10 kΩ，即 R_top∥R_bottom 要小）
    返回按「接近目标值」排序的候选，最多 10 组。
    """
    goal = atten.linear_limit_mv * target_fill
    results: list[tuple[VoltageDivider, float]] = []
    lo, hi = total_ohm_range
    for r_top in E24_SERIES:
        for r_bottom in E24_SERIES:
            if r_top < 100 or r_bottom < 100:
                continue
            total = r_top + r_bottom
            if not lo <= total <= hi:
                continue
            div = VoltageDivider(float(r_top), float(r_bottom))
            served = div.output_from_input(v_in_max) * 1000.0  # mV
            if served > atten.linear_limit_mv:
                continue
            results.append((div, served))
    results.sort(key=lambda item: abs(goal - item[1]))
    return results[:10]


__all__ = [
    "DEFAULT_BITS", "E24_SERIES", "Atten", "ATTENUATIONS",
    "ATTEN_0DB", "ATTEN_2_5DB", "ATTEN_6DB", "ATTEN_11DB",
    "TwoPointCalibration", "AdcChannel", "VoltageDivider", "recommend_divider",
]
