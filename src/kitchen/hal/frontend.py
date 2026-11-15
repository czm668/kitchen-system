# -*- coding: utf-8 -*-
"""模拟前端：把 ADC 读数换算成**契约里的物理量**。

`scenarios/kitchen.json` 声明的 12 个遥测里，有 4 个来自模拟通道，本模块负责它们的换算：

| 契约指标 | 量纲 | 硬件路径 |
|---|---|---|
| `gas_level` | **0~1 无量纲** | MQ-2 → 分压 → ADC1(35) |
| `flame_a` / `flame_b` | **0 / 1** | 火焰模块 AO → ADC1(36/39) |
| `sct_current` | **A** | SCT-013 → burden → 偏置 → ADC1(32) |
| `weight_g` | **g** | HX711（数字协议，24 位，不经 ADC） |

这四个换算都有各自的坑，逐个在类文档里写清：

- MQ-2 输出的是**电阻变化**不是线性电压，必须反算 Rs 再对基线归一化；
- 火焰模块的**极性不统一**（不同厂模块有火时 AO 升高或降低都有），必须现场自判；
- SCT-013 是**交流**信号，直接读 ADC 得到的是带偏置的瞬时值，要算 RMS；
- HX711 是 24 位差分 ADC，**必须标定**才知道 1 克对应多少 counts。

所有类都不碰硬件：入参是 `raw` 读数，出参是物理量。这样整套换算能在 PC 上单测，
真机阶段只需要一层薄薄的 `analogRead()` 适配。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from kitchen.hal.adc import ATTEN_11DB, ATTEN_2_5DB, AdcChannel, Atten, VoltageDivider


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else (high if value > high else value)


# ==========================================================================
# 1. MQ-2 可燃气体 → gas_level (0~1)
# ==========================================================================
class GasChannel:
    """MQ-2 采样通道：ADC → 传感器电阻 Rs → 对基线归一化的 0~1 浓度。

    **为什么不能直接用电压当浓度**：MQ-2 是半导体气敏电阻，输出随浓度的变化
    在对数坐标上才是直线；而且受温湿度、老化、油烟污染影响很大。
    所以本项目的口径（见 `04_功能规格与联动阈值.md` 功能 #1）是：

    - 只用它做**趋势判定**，不做绝对浓度（ppm）报警；
    - 基线用滑动窗口**中位数**（抗脉冲干扰），开机前 5 分钟只建立基线不判定；
    - 按「浓度相对基线的变化」分级，而不是按电压绝对值。

    换算链路：
        ADC raw → mV → 反算分压前的模块电压 Vout
        Rs = (Vc − Vout) / Vout × RL        ← 气敏电阻，浓度升高时 Rs 下降
        k  = Rs_baseline / Rs               ← 相对变化倍数，基线时为 1
        level = clamp((k − 1) / (alarm_k − 1))
    """

    def __init__(
        self,
        adc: AdcChannel,
        divider: VoltageDivider,
        v_module: float = 5.0,
        r_load_ohm: float = 10_000.0,
        alarm_k: float = 4.0,
        baseline_rs_ohm: float | None = None,
    ) -> None:
        self.adc = adc
        self.divider = divider
        self.v_module = v_module
        self.r_load_ohm = r_load_ohm
        #: Rs 降到基线的 1/alarm_k 时判定为 level=1（对应「明显泄漏」侧）
        self.alarm_k = alarm_k
        self.baseline_rs_ohm = baseline_rs_ohm

    # --- 换算 -------------------------------------------------------------
    def module_mv(self, raw: float) -> float:
        """反算分压电阻之前的模块 AO 电压（mV）。"""
        return self.divider.input_from_output(self.adc.to_mv(raw))

    def rs_ohm(self, raw: float) -> float:
        """由模块输出电压反算气敏电阻。"""
        v_out = self.module_mv(raw) / 1000.0
        if v_out <= 0.001:
            return float("inf")          # 输出贴地＝Rs 极大（洁净空气或断线）
        if v_out >= self.v_module:
            return 0.0                   # 输出顶到 Vcc＝Rs 极小（短路或极高浓度）
        return (self.v_module - v_out) / v_out * self.r_load_ohm

    def capture_baseline(self, raws: list[int] | tuple[int, ...]) -> float:
        """用一批「洁净空气」读数建立基线。

        用**中位数**而不是均值：油烟、点火、水蒸气都会造成单点尖峰，
        均值会被拉偏，中位数不受影响。文档口径是开机前 5 分钟采集、
        每周更新一次并存 NVS（断电不丢）。
        """
        if not raws:
            raise ValueError("基线采样不能为空")
        values = [self.rs_ohm(r) for r in raws]
        values = [v for v in values if math.isfinite(v)]
        if not values:
            raise ValueError("基线采样全部无效（分压/接线可能有问题）")
        self.baseline_rs_ohm = statistics.median(values)
        return self.baseline_rs_ohm

    @property
    def baseline_ready(self) -> bool:
        return self.baseline_rs_ohm is not None and self.baseline_rs_ohm > 0

    def level(self, raw: float) -> float:
        """归一化浓度 0~1，与 `kitchen.json` 的 `gas_level` 量纲一致。"""
        if not self.baseline_ready:
            raise RuntimeError("未建立基线：先调用 capture_baseline()")
        rs = self.rs_ohm(raw)
        if not math.isfinite(rs):
            return 0.0
        if rs <= 0:
            return 1.0
        k = self.baseline_rs_ohm / rs            # type: ignore[operator]
        return _clamp((k - 1.0) / (self.alarm_k - 1.0))

    def raw_for_level(self, level: float) -> float:
        """反推：给定目标 level，需要的 Rs（用于现场标定报警点）。"""
        k = 1.0 + level * (self.alarm_k - 1.0)
        return self.baseline_rs_ohm / k          # type: ignore[operator]

    # --- 突变率 -----------------------------------------------------------
    def crossover_rate(self, samples: list[tuple[float, int]],
                       window_s: float = 60.0) -> float:
        """浓度突变率（level 单位 / 分钟）。

        `samples` 是 `(时间戳秒, raw)` 列表。文档功能 #1 用它做「浓度突变率」判定，
        而不是等绝对值超限——燃气泄漏早期正是靠斜率抓到的。

        只取窗口内的首尾两点，避免长时间段把斜率拉平。
        """
        if len(samples) < 2:
            raise ValueError("至少需要 2 个采样点")
        ordered = sorted(samples, key=lambda item: item[0])
        end_t = ordered[-1][0]
        head = ordered[0]
        tail = next((s for s in reversed(ordered) if s[0] >= end_t - window_s), ordered[-1])
        if tail[0] - head[0] <= 0:
            return 0.0
        delta_level = self.level(tail[1]) - self.level(head[1])
        return delta_level / (tail[0] - head[0]) * 60.0


# ==========================================================================
# 2. 火焰传感器 → flame_a / flame_b (0/1)
# ==========================================================================
class FlameChannel:
    """火焰传感器通道：ADC → 0/1，带迟滞与极性自判。

    **必须现场判极性**：市面上火焰模块（KY-026 / YL-38 一类）AO 输出极性并不统一，
    有的模块有火时电压升高，有的降低。硬编码一个方向，换模块就全体反了，
    而反了的后果是「有火判成无火」——明火离人功能直接失效。因此：

    1. `auto_orient(idle_raw, flame_raw)`：上电自检时各测一次，自动定方向；
    2. `update()` 带**迟滞**：单点抖动不会让 0/1 反复翻转，
       避免误触发「明火离人」联锁去关燃气阀。
    """

    def __init__(
        self,
        adc: AdcChannel,
        threshold_raw: int = 1500,
        hysteresis_raw: int = 120,
        active_high: bool = True,
        name: str = "flame",
    ) -> None:
        self.adc = adc
        self.threshold_raw = threshold_raw
        self.hysteresis_raw = hysteresis_raw
        self.active_high = active_high
        self.name = name
        self._state: int = 0

    @property
    def state(self) -> int:
        return self._state

    def update(self, raw: int) -> int:
        """喂一个读数，返回到目前为止的稳定判定 0/1。"""
        high = raw >= self.threshold_raw + self.hysteresis_raw
        low = raw <= self.threshold_raw - self.hysteresis_raw
        if self.active_high:
            if high:
                self._state = 1
            elif low:
                self._state = 0
        else:
            if low:
                self._state = 1
            elif high:
                self._state = 0
        return self._state

    def auto_orient(self, idle_raw: int, flame_raw: int) -> bool:
        """根据「无火读数 / 有火读数」自动判定极性并写入。"""
        if idle_raw == flame_raw:
            raise ValueError("两次读数相同，无法判定极性（可能传感器未对准火焰）")
        self.active_high = flame_raw > idle_raw
        # 阈值取中值，迟滞取间距的 15%（夹在 30~400 之间，太小抗不住噪声、太大反应迟钝）
        span = abs(flame_raw - idle_raw)
        self.threshold_raw = int((idle_raw + flame_raw) / 2)
        self.hysteresis_raw = int(_clamp(span * 0.15, 30, 400))
        self._state = 0
        return self.active_high

    def describe(self) -> str:
        direction = "有火时读数升高" if self.active_high else "有火时读数降低"
        return (f"{self.name}: {direction}，阈值 {self.threshold_raw} "
                f"±{self.hysteresis_raw}（raw）")


# ==========================================================================
# 3. SCT-013 电流互感器 → sct_current (A)
# ==========================================================================
@dataclass(frozen=True)
class CurrentDiagnostic:
    """SCT-013 在某负载电流下的可行性评估。"""

    load_amps: float
    signal_rms_mv: float
    counts_rms: float
    peak_mv: float
    headroom_amps: float
    ok: bool
    hint: str


class AcCurrentChannel:
    """SCT-013 交流电流采样：ADC 样本 → 一次侧电流（A）。

    SCT-013 输出的是**交流电流**，必须：

    1. **burden 电阻**转成电压（本互感器 100A:50mA，即 2000:1）；
    2. **1.65V 偏置**把交流抬到 ADC 量程中位，否则负半周直接被削掉；
    3. 软件算 **RMS**（去直流后平方均值开方），不能取瞬时值或平均值。

    分辨率是这里最容易被忽略的问题：11dB 档 LSB ≈ 0.757 mV，
    burden 33Ω 时 1A 负载只产生 16.5 mV RMS ≈ 22 counts，
    排风机低档 0.3A 时只剩 6~7 counts——RMS 抖动会大到不可用。
    所以默认配置给 burden **100Ω**（1A → 66 counts），
    `diagnose()` 会直接告诉你某个负载电流下够不够。
    """

    def __init__(
        self,
        adc: AdcChannel,
        burden_ohm: float = 100.0,
        turns_ratio: float = 2000.0,
        bias_mv: float = 1650.0,
        mains_hz: float = 50.0,
        samples_per_cycle: int = 40,
    ) -> None:
        self.adc = adc
        self.burden_ohm = burden_ohm
        #: 一次/二次电流比。SCT-013-000 为 100A/50mA = 2000
        self.turns_ratio = turns_ratio
        self.bias_mv = bias_mv
        self.mains_hz = mains_hz
        self.samples_per_cycle = samples_per_cycle

    @property
    def samples_per_reading(self) -> int:
        """一次 RMS 计算建议采多少个点。

        工频 50 Hz 下按每周期 40 点、累计 10 个周期，即 400 点（约 200 ms）。
        少于 10 个周期时，非整周期截断会引入明显误差。
        """
        return max(200, self.samples_per_cycle * 10)

    # --- 换算 -------------------------------------------------------------
    def rms_mv(self, raws: list[int] | tuple[int, ...]) -> float:
        """去直流后的交流 RMS（mV）。

        偏置用**样本均值**估计而不是死记 1650 mV：偏置网络会随温度漂移，
        用均值估计能自动跟随，且对纯交流信号无偏。
        """
        if len(raws) < 2:
            raise ValueError("RMS 至少需要 2 个采样点")
        mv = [self.adc.to_mv(r) for r in raws]
        mean = sum(mv) / len(mv)
        squares = [(v - mean) ** 2 for v in mv]
        return math.sqrt(sum(squares) / len(squares))

    def amps(self, raws: list[int] | tuple[int, ...]) -> float:
        """一次侧电流有效值（A）。"""
        secondary_a = (self.rms_mv(raws) / 1000.0) / self.burden_ohm
        return secondary_a * self.turns_ratio

    def amps_from_rms_mv(self, rms_mv: float) -> float:
        return (rms_mv / 1000.0) / self.burden_ohm * self.turns_ratio

    # --- 可行性评估 -------------------------------------------------------
    def signal_rms_mv(self, load_amps: float) -> float:
        secondary_a = load_amps / self.turns_ratio
        return secondary_a * self.burden_ohm * 1000.0

    def diagnose(self, load_amps: float) -> CurrentDiagnostic:
        """在给定负载下评估这套参数能不能用，并给出可执行建议。

        判据两条，缺一不可：
        - **下限**：RMS 至少 10 个 ADC count，否则量化噪声会盖过信号；
        - **上限**：偏置 + 峰值不得超满量程，否则正半周削顶，RMS 会系统性偏小。
        """
        rms = self.signal_rms_mv(load_amps)
        lsb = self.adc.lsb_mv
        counts = rms / lsb
        peak = self.bias_mv + rms * math.sqrt(2)
        full = self.adc.atten.full_scale_mv
        # 可测上限：峰值顶到满量程时对应的一次电流
        usable_peak = max(full - self.bias_mv, 0.0)
        headroom = self.amps_from_rms_mv(usable_peak / math.sqrt(2))

        if counts < 10:
            ok = False
            hint = (f"{load_amps:g}A 仅 {rms:.1f} mV ≈ {counts:.0f} counts（<10），"
                    f"RMS 会被量化噪声淹没不可用。两个改法："
                    f"① burden 提到 {self.required_burden(load_amps):.0f}Ω"
                    f"（代价是缩小可测上限）；"
                    f"② 换 {ATTEN_2_5DB.name} 档并把偏置改到 "
                    f"{ATTEN_2_5DB.full_scale_mv / 2:.0f} mV（LSB 减半，counts 翻倍）")
        elif peak > full:
            ok = False
            hint = (f"偏置 {self.bias_mv:.0f} mV + 峰值 {rms * math.sqrt(2):.0f} mV "
                    f"= {peak:.0f} mV，超 {full:.0f} mV 满量程，正半周削顶；"
                    f"该配置可测上限约 {headroom:.1f}A，超了就减小 burden 或降量程档")
        elif counts < 30:
            ok = True
            hint = (f"{load_amps:g}A 时 {counts:.0f} counts，可测但不宽裕；"
                    f"读数若漂，burden 提到 {self.required_burden(load_amps, 60):.0f}Ω "
                    f"可到 60 counts")
        else:
            ok = True
            hint = (f"{load_amps:g}A 时 {counts:.0f} counts，可测上限约 "
                    f"{headroom:.1f}A，余量充足")

        return CurrentDiagnostic(
            load_amps=load_amps,
            signal_rms_mv=rms,
            counts_rms=counts,
            peak_mv=peak,
            headroom_amps=headroom,
            ok=ok,
            hint=hint,
        )

    def required_burden(self, load_amps: float, target_counts: float = 80.0) -> float:
        """要让 `load_amps` 达到 `target_counts`，burden 需要多大。"""
        target_mv = target_counts * self.adc.lsb_mv
        target_secondary_a = load_amps / self.turns_ratio
        if target_secondary_a <= 0:
            return self.burden_ohm
        return target_mv / 1000.0 / target_secondary_a

    @staticmethod
    def bias_volts_for(atten: Atten) -> float:
        """给定衰减档，推荐的中位偏置电压：满量程的一半。"""
        return atten.full_scale_mv / 2.0


# ==========================================================================
# 4. HX711 称重 → weight_g (g)
# ==========================================================================
class ScaleChannel:
    """HX711 称重通道：24 位差分计数 → 克。

    HX711 不经过 ESP32 ADC，走的是自有时钟协议，所以难点不在换算而在**标定**：

    - `tare()`：空载取中位数当零点（文档 §4 要求「上电先做去皮」）；
    - `calibrate(known_g, raw)`：放一个已知砝码（如 100 g），算出 counts/g；
    - 传感器温漂与蠕变无法用软件消除，但**中位数滤波**能压掉单点跳变，
      这对「偏差超 ±5% 锁定下一步」的判据很关键——一次抖动就锁单，体验会很差。
    """

    def __init__(
        self,
        counts_per_gram: float = 1.0,
        offset_counts: float = 0.0,
        median_window: int = 5,
    ) -> None:
        if counts_per_gram == 0:
            raise ValueError("counts_per_gram 不能为 0")
        self.counts_per_gram = counts_per_gram
        self.offset_counts = offset_counts
        self.median_window = median_window
        self._history: list[int] = []

    def _filtered(self, raw: int) -> float:
        self._history.append(int(raw))
        if len(self._history) > self.median_window:
            self._history.pop(0)
        return statistics.median(self._history)

    def grams(self, raw: int) -> float:
        return (self._filtered(raw) - self.offset_counts) / self.counts_per_gram

    def tare(self, raws: list[int] | tuple[int, ...]) -> float:
        """空载去皮：把当前中位数记为零点。"""
        if not raws:
            raise ValueError("去皮采样不能为空")
        self.offset_counts = statistics.median(raws)
        self._history.clear()
        return self.offset_counts

    def calibrate(self, known_grams: float, raw: int) -> float:
        """用已知重量标定；返回新的 counts/g。"""
        delta = raw - self.offset_counts
        if known_grams <= 0 or delta == 0:
            raise ValueError("标定砝码需 >0 g，且读数要与零点不同")
        self.counts_per_gram = delta / known_grams
        return self.counts_per_gram

    def deviation(self, raw: int, target_grams: float) -> float:
        """相对目标重量的偏差（正负百分比），对应功能 #12 的 ±5% 判据。"""
        if target_grams == 0:
            raise ValueError("目标重量不能为 0")
        return (self.grams(raw) - target_grams) / target_grams


# ==========================================================================
# 出厂推荐配置（写进文档，也用来自检）
# ==========================================================================
@dataclass(frozen=True)
class FrontendConfig:
    """一套可用的前端参数组合，供文档与自检共用。"""

    gas_divider: VoltageDivider
    gas_r_load_ohm: float
    current_burden_ohm: float
    current_bias_mv: float
    flame_threshold_raw: int
    notes: tuple[str, ...] = field(default_factory=tuple)


def default_frontend() -> FrontendConfig:
    """本项目默认用的一套参数（对应 `06_模拟前端实施方案.md` §2–§4）。

    - MQ-2：**10 kΩ + 9.1 kΩ** 分压，5 V → 2.38 V。选它而不是等值 10k+10k，
      是因为 5 V 极端值经 1:1 分压得 2.5 V，已超 11 dB 档线性上限（2450 mV）；
      10k∥9.1k = 4.8 kΩ 的低源阻抗也满足 ESP32 ADC 建议的 <10 kΩ。
    - SCT-013：burden **100 Ω**、偏置 **1.65 V**（两个 10 kΩ 分压 + 10 µF 滤波）。
    - 火焰：阈值 1500 raw 起步，上电 `auto_orient()` 后自动改写。
    """
    return FrontendConfig(
        gas_divider=VoltageDivider(10_000.0, 9_100.0),
        gas_r_load_ohm=10_000.0,
        current_burden_ohm=100.0,
        current_bias_mv=1650.0,
        flame_threshold_raw=1500,
        notes=(
            "MQ-2 绝不能用 10k+20k：分压后 3.33 V 超过 3.3 V 供电轨，"
            "会把 ADC 输入保护二极管打开，读数失真且长期损伤引脚",
            "SCT-013 用 100Ω 而非 33Ω：33Ω 时 1A 仅约 22 counts、排风机低档只剩 6~7 counts，"
            "RMS 抖动大到不可用",
            "火焰阈值必须由 auto_orient() 现场改写，出厂值只是占位",
        ),
    )


__all__ = [
    "GasChannel", "FlameChannel", "AcCurrentChannel", "ScaleChannel",
    "CurrentDiagnostic", "FrontendConfig", "default_frontend",
    "ATTEN_11DB", "ATTEN_2_5DB",
]
