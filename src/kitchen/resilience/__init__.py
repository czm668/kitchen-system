# -*- coding: utf-8 -*-
"""异常兜底域（A6）：断网/掉电/故障/降级下的行为定义。

| 模块 | 职责 | 功能编号 |
|---|---|---|
| `queue` | 有界落盘队列：`seq` 跨重启不重号、关键记录不因容量丢 | A6-01 |
| `guards` | 三条保守保护（燃气突变 / 超温联动风机 / 明火长烧无人） | A6-03 |
| `upload` | 批量补传 + `(device_id, seq)` 去重 + 时钟校准 | A6-02 |
| `health` | 读数卡死 / 超量程 / 心跳丢失 → 故障工单 → 超时升级 | A6-04 |
| `degrade` | 三端降级标记（同一份事实，三种排版） | A6-05 |
| `offline` | 装配层：一帧 = 保护 → 引擎 → 互锁 → 入队，`tick()` 里不做网络 IO | A6-01~05 |

## 用哪一层

- 只想验「保护触发得对不对」→ `ConservativeGuards().evaluate(metrics)`，纯函数式；
- 想验「断网后数据会不会丢」→ `LocalQueue(path)` + `ReplayUploader`，
  不需要任何网络；
- 想跑完整闭环 → `OfflineFirstRuntime(runtime, supervisor)`，逐帧 `tick()`。

## 一条不要越过的线

**这一层不 import `vision`，也不 import `process`。**

视觉的在线状态从 `runtime.flame_guard.vision_health()` 读（那是 `#3` 已有的
公开接口），调度信息一概不碰。理由是兜底模块的职责是「别人坏了之后怎么办」，
一旦它反过来依赖那些子系统，子系统启动失败时兜底层自己也起不来。
"""
from __future__ import annotations

from kitchen.resilience.degrade import (
    CAP_ALARM,
    CAP_CONFIG,
    CAP_GAS,
    CAP_REPLAY,
    CAP_SENSOR,
    CAP_VISION_DISPOSAL,
    CAP_VISION_FLAME,
    STATE_DEGRADED,
    STATE_FULL,
    STATE_OFF,
    Capability,
    DegradeReport,
    build_degrade,
)
from kitchen.resilience.guards import (
    GUARD_FLAME_UNATTENDED,
    GUARD_GAS_SURGE,
    GUARD_THERMAL_FAN,
    ConservativeGuards,
    GuardPolicy,
    GuardState,
)
from kitchen.resilience.health import (
    DEFAULT_RANGES,
    DS18B20_SENTINELS,
    TICKET_TYPE,
    FaultKind,
    FaultTicket,
    SensorFault,
    SensorHealthMonitor,
    SensorPolicy,
)
from kitchen.resilience.offline import OfflineFirstRuntime, StepResult
from kitchen.resilience.queue import (
    KIND_ACTION,
    KIND_ALARM,
    KIND_EVENT,
    KIND_LOG,
    KIND_TELEMETRY,
    LocalQueue,
    QueueEntry,
    QueueFull,
    QueueStats,
    build_replay,
)
from kitchen.resilience.upload import (
    ClockCalibrator,
    ClockState,
    MemoryOfflineStore,
    ReplayReport,
    ReplayUploader,
    UploadBatch,
    build_batch,
    dedup_rows,
)

__all__ = [
    # queue
    "KIND_ACTION", "KIND_ALARM", "KIND_EVENT", "KIND_LOG", "KIND_TELEMETRY",
    "LocalQueue", "QueueEntry", "QueueFull", "QueueStats", "build_replay",
    # upload
    "ClockCalibrator", "ClockState", "MemoryOfflineStore", "ReplayReport",
    "ReplayUploader", "UploadBatch", "build_batch", "dedup_rows",
    # guards
    "GUARD_FLAME_UNATTENDED", "GUARD_GAS_SURGE", "GUARD_THERMAL_FAN",
    "ConservativeGuards", "GuardPolicy", "GuardState",
    # health
    "DEFAULT_RANGES", "DS18B20_SENTINELS", "TICKET_TYPE", "FaultKind",
    "FaultTicket", "SensorFault", "SensorHealthMonitor", "SensorPolicy",
    # degrade
    "CAP_ALARM", "CAP_CONFIG", "CAP_GAS", "CAP_REPLAY", "CAP_SENSOR",
    "CAP_VISION_DISPOSAL", "CAP_VISION_FLAME", "STATE_DEGRADED", "STATE_FULL",
    "STATE_OFF", "Capability", "DegradeReport", "build_degrade",
    # offline
    "OfflineFirstRuntime", "StepResult",
]
