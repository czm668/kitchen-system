# -*- coding: utf-8 -*-
"""安全判据（`kitchen.safety`）：比「阈值比大小」复杂、但又不该塞进规则引擎的部分。

为什么单独一包：规则引擎（`kitchen.rules`）负责**框架**——去抖、窗口、档位、
闩锁、持久化，它对「燃气」一无所知；而 #1 的判据本身有自己的领域知识
（比的是基线不是绝对值、明火期要放宽）。把领域算法塞进通用引擎里，
引擎会变成 if-else 堆，算法也没法单独测。

| 模块 | 负责 | 支撑 |
|---|---|---|
| `gas` | 滑动中位数基线、突变率两级处置、明火期放宽、周更固化 | #1 |
| `flame` | 视觉信号注入、明火离人工位锁、返岗刷卡复位 | #3 |
| `coldchain` | 开门波动过滤（识别波形）、冷链/留样柜样本前置层 | #5/#6 |
| `interlock` | 判定 → 三通道编排：触发源登记、动作去重、人工恢复 | #1/#2/#5/#6/#7 |

前三个都**只做领域知识**，判定框架（去抖/窗口/闩锁/复位）仍然走 `kitchen.rules`；
`interlock` 是编排层，裁决规则本身留在 `kitchen.channels.InterlockController`。
"""
from kitchen.safety.coldchain import (
    ColdChainFrame,
    ColdChainGuard,
    DEFAULT_COLD_METRICS,
    DoorOpenFilter,
    FilterEvent,
)
from kitchen.safety.flame import (
    FlameGuard,
    CardAckResult,
    StaffCard,
    VisionHealth,
    VISION_NO_PERSON,
)
from kitchen.safety.gas import (
    LEVEL_ALARM,
    LEVEL_NORMAL,
    LEVEL_WARN,
    REASON_ALARM,
    REASON_ALARM_RATIO,
    REASON_NO_BASELINE,
    REASON_NORMAL,
    REASON_WARN,
    GasAssessment,
    GasBaseline,
    GasBaselineTracker,
    GasLeakMonitor,
    median_of,
)
from kitchen.safety.interlock import (
    ActionDecision,
    InterlockFrame,
    InterlockSupervisor,
    PRIORITY_BY_METRIC,
    priority_of,
)

__all__ = [
    # #3 明火离人
    "FlameGuard", "CardAckResult", "StaffCard", "VisionHealth", "VISION_NO_PERSON",
    # #1 燃气泄漏
    "GasLeakMonitor", "GasBaselineTracker", "GasBaseline", "GasAssessment", "median_of",
    "LEVEL_NORMAL", "LEVEL_WARN", "LEVEL_ALARM",
    "REASON_NORMAL", "REASON_WARN", "REASON_ALARM", "REASON_ALARM_RATIO", "REASON_NO_BASELINE",
    # #5 冷链 / #6 留样柜
    "ColdChainGuard", "ColdChainFrame", "DoorOpenFilter", "FilterEvent",
    "DEFAULT_COLD_METRICS",
    # 互锁编排（A2-08）
    "InterlockSupervisor", "InterlockFrame", "ActionDecision", "PRIORITY_BY_METRIC",
    "priority_of",
]
