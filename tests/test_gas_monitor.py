# -*- coding: utf-8 -*-
"""燃气泄漏判定单测（A2-02 验收）。

验收原文：「注入浓度突变 → 预警级只播报不动作；超阈值 → 关阀+开排风+声光+语音+工单；
回落且人工确认后复位；明火期间不误触发。」

四段场景在 `TestLeakScenario` 里按顺序演一遍；上面几个类逐条拆开验证算法边界。
"""
from __future__ import annotations

import pytest

from kitchen.safety import (
    LEVEL_ALARM,
    LEVEL_NORMAL,
    LEVEL_WARN,
    REASON_ALARM,
    REASON_ALARM_RATIO,
    REASON_NO_BASELINE,
    REASON_WARN,
    GasBaseline,
    GasBaselineTracker,
    GasLeakMonitor,
)

WEEK = 7 * 24 * 3600.0


def ready_baseline(value: float = 0.08) -> GasBaseline:
    return GasBaseline(value=value, samples=120, window_sec=300.0, updated_at=1_700_000_000.0, ready=True)


def build_tracker(clock_start: float = 0.0, baseline: float = 0.08,
                  samples: int = 120, window_sec: float = 300.0) -> GasBaselineTracker:
    """造一个「已经建好基线」的 tracker：灌 samples 个点，值为 baseline 附近的小波动。"""
    tracker = GasBaselineTracker(window_sec=window_sec, min_samples=30)
    step = window_sec / (samples + 1)
    for index in range(samples):
        # 交替 ±0.005，让中位数稳定落在 baseline
        tracker.update(clock_start + index * step, baseline + (0.005 if index % 2 else -0.005))
    return tracker


# ================================================================ 基线：中位数而非均值
class TestBaseline:
    def test_中位数不被单点脉冲拉高(self):
        tracker = GasBaselineTracker(window_sec=300.0, min_samples=5)
        for index in range(10):
            tracker.update(index, 0.08)
        tracker.update(10, 0.90)                    # 一个 3 秒的油烟脉冲
        assert tracker.is_ready() is True
        assert tracker.median() == pytest.approx(0.08)   # 均值会是 0.155

    def test_样本不足时不就绪(self):
        tracker = GasBaselineTracker(window_sec=300.0, min_samples=60)
        for index in range(30):
            tracker.update(index, 0.08)
        assert tracker.is_ready() is False
        assert tracker.baseline(wall_now=1000.0).ready is False

    def test_窗口外的旧数据不参与基线(self):
        # 关掉抗污染闸门，单独验证「时间窗淘汰」这一件事（两者是独立机制）
        tracker = GasBaselineTracker(window_sec=60.0, min_samples=5, guard_ratio=100.0)
        for index in range(10):
            tracker.update(index, 0.50)              # 早期高浓度
        for index in range(100, 120):
            tracker.update(index, 0.05)              # 60 秒之后的干净空气
        assert tracker.median() == pytest.approx(0.05)

    def test_抗污染闸门拒绝明显偏离的读数(self):
        """缓慢泄漏时基线会被自己抬高，形成「相对正常」的正反馈。
        闸门把偏离中位数 25% 以上的读数直接挡在窗口外。"""
        tracker = build_tracker(baseline=0.08)
        assert tracker.update(400.0, 0.50) is False
        assert tracker.rejected == 1
        assert tracker.median() == pytest.approx(0.08, abs=0.01)

    def test_抗污染闸门放行正常波动(self):
        tracker = build_tracker(baseline=0.08)
        assert tracker.update(400.0, 0.085) is True   # 偏离 6%，正常
        assert tracker.rejected == 0

    def test_基线不被持续泄漏拖走(self):
        """连续 5 分钟偏高读数：闸门全部挡下，基线纹丝不动。"""
        tracker = build_tracker(baseline=0.08)
        for index in range(60):
            tracker.update(400.0 + index, 0.30)
        assert tracker.median() == pytest.approx(0.08, abs=0.01)
        assert tracker.rejected == 60

    def test_周更固化后清空窗口(self):
        tracker = build_tracker()
        frozen = tracker.freeze(wall_now=1_700_000_000.0)
        assert frozen.ready is True
        assert tracker.samples == 0                  # 窗口清空，重新累积
        assert tracker.is_ready() is True            # 但固化值让基线仍然可用
        assert tracker.baseline(wall_now=1_700_000_100.0).value == pytest.approx(frozen.value)

    def test_周更按时间差而非日历周(self):
        """设备断电几天后重启，比对时间差就知道该不该更新；按星期几算会漏。"""
        tracker = build_tracker()
        tracker.freeze(wall_now=1_700_000_000.0)
        assert tracker.weekly_due(1_700_000_000.0 + WEEK - 1) is False
        assert tracker.weekly_due(1_700_000_000.0 + WEEK + 1) is True

    def test_NVS往返(self, tmp_path):
        tracker = build_tracker()
        tracker.freeze(wall_now=1_700_000_000.0)
        path = tmp_path / "gas_baseline.nvs"
        tracker.save(path, wall_now=1_700_000_000.0)
        reloaded = GasBaselineTracker.load(path, window_sec=300.0, min_samples=60)
        assert reloaded.is_ready() is True
        assert reloaded.baseline(wall_now=1_700_000_100.0).value == pytest.approx(0.08, abs=0.01)

    def test_坏blob解析返回None而不抛错(self):
        assert GasBaseline.from_nvs("坏数据") is None
        assert GasBaseline.from_nvs("0.08|120|300|abc|1") is None
        assert GasBaseline.from_nvs("") is None
        assert GasBaseline.from_nvs("0.08|120|300|1700000000|1").value == pytest.approx(0.08)


# ================================================================ 突变率两级处置
class TestTieredResponse:
    def setup_method(self) -> None:
        self.monitor = GasLeakMonitor(
            absolute_threshold=0.55, warn_ratio=2.0, alarm_ratio=4.0,
            warn_ratio_flame=3.5, alarm_ratio_flame=5.0,
            duration_sec=5.0, duration_sec_flame=30.0,
        )
        self.base = ready_baseline(0.08)

    def test_略高于基线判正常(self):
        result = self.monitor.assess(0.10, self.base)
        assert result.level == LEVEL_NORMAL

    def test_两倍以上判预警且不动作(self):
        result = self.monitor.assess(0.20, self.base)     # 比值 1.5 → 不到 2
        assert result.level == LEVEL_NORMAL
        result = self.monitor.assess(0.25, self.base)     # 比值 2.125
        assert result.level == LEVEL_WARN
        assert result.reason == REASON_WARN
        assert result.should_actuate is False             # 预警级只播报

    def test_四倍以上判联锁且要动作(self):
        result = self.monitor.assess(0.40, self.base)     # 比值 4.0
        assert result.level == LEVEL_ALARM
        assert result.reason == REASON_ALARM_RATIO
        assert result.should_actuate is True

    def test_绝对门限直接联锁不看突变率(self):
        # 基线已经偏高（0.30）时，0.55 只有 1.8 倍，但绝对浓度必须报警
        high_base = ready_baseline(0.30)
        result = self.monitor.assess(0.56, high_base)
        assert result.level == LEVEL_ALARM
        assert result.reason == REASON_ALARM

    def test_基线未就绪时不判定(self):
        result = self.monitor.assess(0.90, GasBaseline(
            value=0.0, samples=3, window_sec=300.0, updated_at=0.0, ready=False,
        ))
        assert result.level == LEVEL_NORMAL
        assert result.reason == REASON_NO_BASELINE
        assert result.should_actuate is False

    def test_基线接近零时比值不爆炸(self):
        """干净空气基线可能是 0.002，直接相除会得到几百倍然后狂报。"""
        base = ready_baseline(0.002)
        result = self.monitor.assess(0.004, base)
        assert result.ratio <= 1.0 + 1e-6 or result.level == LEVEL_NORMAL

    def test_阈值快照随明火切换(self):
        assert self.monitor.thresholds(flame_active=False)["duration_sec"] == 5.0
        flame = self.monitor.thresholds(flame_active=True)
        assert flame["duration_sec"] == 30.0
        assert flame["warn_ratio"] == 3.5
        assert flame["alarm_ratio"] == 5.0

    def test_参数非法时报错(self):
        with pytest.raises(ValueError):
            GasLeakMonitor(warn_ratio=5.0, alarm_ratio=4.0)
        with pytest.raises(ValueError):
            GasLeakMonitor(duration_sec=5.0, duration_sec_flame=3.0)


# ================================================================ 明火期放宽
class TestFlameRelaxation:
    def setup_method(self) -> None:
        self.monitor = GasLeakMonitor()
        self.base = ready_baseline(0.08)

    def test_平时联锁的浓度明火期只预警(self):
        # 0.45 / 0.08 = 4.6 倍增量：平时 ≥ 4 判联锁；明火期要 ≥ 5 才算
        normal = self.monitor.assess(0.45, self.base, flame_active=False)
        flame = self.monitor.assess(0.45, self.base, flame_active=True)
        assert normal.level == LEVEL_ALARM
        assert flame.level == LEVEL_WARN
        assert flame.duration_required == 30.0

    def test_明火期确认窗口延长到30秒(self):
        result = self.monitor.assess(0.20, self.base, flame_active=True)
        assert result.duration_required == 30.0
        assert result.flame_active is True

    def test_绝对门限不因明火放宽(self):
        """真到泄漏门限就不是「油烟呛人」了，跟有没有明火无关。"""
        result = self.monitor.assess(0.60, self.base, flame_active=True)
        assert result.level == LEVEL_ALARM
        assert result.reason == REASON_ALARM

    def test_明火期高突变率仍能联锁(self):
        """放松不等于失效：明火期 5 倍增量（6 倍浓度）仍要联锁，且必须落在
        绝对门限 0.55 之下——否则这条分支永远走不到，等于把明火期判成了瞎子。"""
        base = ready_baseline(0.08)
        result = self.monitor.assess(0.48, base, flame_active=True)   # ratio = 5.0
        assert result.level == LEVEL_ALARM
        assert result.reason == REASON_ALARM_RATIO
        assert result.value < self.monitor.absolute_threshold


# ================================================================ 验收四段场景
class TestLeakScenario:
    """按任务清单的验收原文演一遍：注入突变 → 预警只播报 → 超阈值联锁 → 回落 + 复位。"""

    def test_完整两级处置(self):
        tracker = build_tracker(baseline=0.08)
        monitor = GasLeakMonitor()
        wall = 1_700_000_000.0

        # ① 平稳：基线 0.08 附近，全程正常
        for index in range(20):
            tracker.update(310 + index, 0.082)
        base = tracker.baseline(wall_now=wall)
        assert monitor.assess(0.082, base).level == LEVEL_NORMAL

        # ② 浓度缓升到 0.25：预警级，只播报
        warn = monitor.assess(0.25, base)
        assert warn.level == LEVEL_WARN and warn.should_actuate is False

        # ③ 冲到 0.60：联锁级，要动继电器
        alarm = monitor.assess(0.60, base)
        assert alarm.level == LEVEL_ALARM and alarm.should_actuate is True
        payload = alarm.to_payload()
        assert payload["level"] == LEVEL_ALARM
        assert "gas_valve_close" not in payload         # 动作不在这层定，由规则配置决定

        # ④ 回落后重新判正常（复位动作另需人工确认，见 rules 的 ack）
        assert monitor.assess(0.08, base).level == LEVEL_NORMAL

    def test_明火期间不误触发(self):
        """厨师正常爆炒：明火 + 灶边浓度翻 3 倍，不该报。"""
        tracker = build_tracker(baseline=0.08)
        monitor = GasLeakMonitor()
        for index in range(20):
            tracker.update(310 + index, 0.08)
        base = tracker.baseline(wall_now=1_700_000_000.0)

        result = monitor.assess(0.24, base, flame_active=True)   # 3 倍
        assert result.level == LEVEL_NORMAL
        assert result.duration_required == 30.0
