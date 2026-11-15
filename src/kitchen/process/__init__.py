# -*- coding: utf-8 -*-
"""过程执行域（P4 / A4）：订单调度、#4 灶台占用、#12 称重、#13 SOP、#14 留样、#15 消毒。

| 模块 | 负责 | 支撑 |
|---|---|---|
| `models` | 订单/子项状态机、菜品配方 SOP 实体、工位、占用会话 | — |
| `repository` | 静态资料（菜品/配方/SOP/工位/供应链）内存仓储 | A4-01 |
| `orders` | 建单、状态推进、事件流重放、幂等、乐观锁、沽清、退菜 | A4-01 |
| `dispatch` | 上灶占用、点火校验、交期优先派单、排风监测 | #4 |
| `cooking` | 配菜称重判定与锁定、SOP 分步确认、出餐门禁 | #12 / #13 |
| `retention` | 应留样清单、扫码称重（125g）、48h 销毁任务 | #14 / #6 |
| `disinfection` | 消毒会话门控、累计曲线、复消工单与禁出库 | #15 |
| `trace` | 供应商→批次→溯源码一屏溯源、出库门禁、标签 | A4-08 |
| `qr` | 纯标准库二维码编码（byte/M/v1~v10），标签打印用 | A4-08 |

这一域**不接规则引擎**：过程推进是「事件驱动的状态机」，没有阈值、窗口、闩锁。
反过来，#15 消毒的**温度报警**仍走 `env_thresholds`（声光与联锁在端侧），
本域只负责「累计够不够 10 分钟、要不要开复消单」这类会话级判断。
"""
from kitchen.process.cooking import (
    MIN_ABS_TOLERANCE_G,
    CookingBoard,
    CookingSession,
    NotReadyError,
    StepConfirm,
    StepLockedError,
    WeighCheck,
    WeighError,
)
from kitchen.process.dispatch import (
    EXHAUST_CURRENT_MIN,
    FLAME_THRESHOLD,
    DispatchBoard,
    DispatchDecision,
    ExhaustState,
    FlameVerdict,
    QualificationMismatch,
    StaffBusyError,
    StaffMatcher,
    StationBusyError,
)
from kitchen.process.disinfection import (
    DEMO_TIME_SCALE,
    DISINFECTION_ABNORMAL_C,
    DISINFECTION_HOLD_C,
    DISINFECTION_TARGET_C,
    DisinfectionError,
    DisinfectionPolicy,
    DisinfectionReading,
    DisinfectionRecord,
    DisinfectionService,
    DisinfectionSession,
    NotRunningError,
    ReworkTicket,
)
from kitchen.process.qr import QrCode, QrError
from kitchen.process.retention import (
    DESTROY_TYPE,
    RETENTION_KEEP_HOURS,
    RETENTION_MIN_G,
    DestroyTask,
    NotDueError,
    RetentionCheck,
    RetentionError,
    RetentionSample,
    RetentionService,
    RetentionTarget,
)
from kitchen.process.trace import TraceError, TraceService, TraceView
from kitchen.process.models import (
    ITEM_TRANSITIONS,
    READY_TEMP_C,
    STATION_BUSY,
    STATION_IDLE,
    STATION_RESERVED,
    Dish,
    ItemState,
    Order,
    OrderEvent,
    OrderItem,
    OrderState,
    Recipe,
    RecipeItem,
    SopStep,
    StationOccupancy,
    WorkStation,
    can_item_move,
    estimate_minutes,
    rollup_order,
)
from kitchen.process.orders import (
    DEFAULT_CHANNEL,
    OrderError,
    OrderService,
    SoldOutError,
    replay_events,
)
from kitchen.process.repository import (
    MemoryProcessRepository,
    ProcessDomainError,
)

__all__ = [
    # 模型
    "OrderState", "ItemState", "ITEM_TRANSITIONS", "can_item_move", "rollup_order",
    "Dish", "Recipe", "RecipeItem", "SopStep", "WorkStation",
    "Order", "OrderItem", "OrderEvent", "StationOccupancy",
    "STATION_IDLE", "STATION_RESERVED", "STATION_BUSY", "READY_TEMP_C",
    "estimate_minutes",
    # 仓储
    "MemoryProcessRepository", "ProcessDomainError",
    # 订单
    "OrderService", "OrderError", "SoldOutError", "replay_events", "DEFAULT_CHANNEL",
    # 派单与占用
    "DispatchBoard", "DispatchDecision", "FlameVerdict", "ExhaustState",
    "StationBusyError", "StaffBusyError", "FLAME_THRESHOLD", "EXHAUST_CURRENT_MIN",
    # 配菜称重与 SOP
    "CookingSession", "CookingBoard", "WeighCheck", "StepConfirm",
    "WeighError", "StepLockedError", "NotReadyError", "MIN_ABS_TOLERANCE_G",
    # 派单与资质（A4-09）
    "StaffMatcher", "QualificationMismatch",
    # 留样与销毁（#14 / #6）
    "RetentionService", "RetentionTarget", "RetentionCheck", "RetentionSample",
    "DestroyTask", "RetentionError", "NotDueError",
    "RETENTION_MIN_G", "RETENTION_KEEP_HOURS", "DESTROY_TYPE",
    # 消毒与复消（#15）
    "DisinfectionService", "DisinfectionSession", "DisinfectionPolicy",
    "DisinfectionRecord", "DisinfectionReading", "ReworkTicket", "DisinfectionError",
    "NotRunningError", "DISINFECTION_TARGET_C", "DISINFECTION_HOLD_C",
    "DISINFECTION_ABNORMAL_C", "DEMO_TIME_SCALE",
    # 溯源与标签（A4-08）
    "TraceService", "TraceView", "TraceError", "QrCode", "QrError",
]
