# -*- coding: utf-8 -*-
"""人员准入域（P3 / A3）：#8 晨检、#9 健康证、#10 资质、#11 未授权上岗。

四个模块的分工：

| 模块 | 负责 | 支撑 |
|---|---|---|
| `models` | 五张表的不可变对象 + 拒绝原因字典 | — |
| `repository` | 内存仓储，**复刻唯一键与外键** | A3-01 |
| `admission` | 刷卡准入、晨检问卷、组长双通道复核、资质候选、每日证件扫描 | #8 / #9 / #10 |
| `duty` | 在场状态积分 → 未授权上岗告警 | #11 的告警侧 |

这一域**不接规则引擎**：准入是「刷卡事件的状态查询」，不需要去抖/闩锁/窗口聚合，
塞进 `env_thresholds` 会逼引擎新增枚举型指标与事件触发两套概念，而换来的只有
一个用不上的闩锁。需要时间积分的地方（#11）在 `duty.py` 里自己按秒累加。

服务端（FastAPI + MySQL）在 A4 落地时，实现 `repository.StaffRepository` 协议
即可切换数据源，判定代码不改。
"""
from kitchen.staff.admission import (
    LEAD_QUALIFICATION,
    AdmissionController,
    CertificateSweep,
    CertificateSweeper,
    StaffMatcherAdapter,
)
from kitchen.staff.duty import TICKET_UNAUTHORIZED, DutyAlert, DutyGuard
from kitchen.staff.models import (
    MORNING_QUESTIONS,
    MORNING_TEMP_LIMIT_C,
    QUALIFICATION_LABELS,
    AdmissionResult,
    DenyReason,
    HealthCertificate,
    MorningAnswers,
    MorningCheck,
    MorningOutcome,
    ReviewOutcome,
    Shift,
    Staff,
    StaffCard,
    qualification_label,
)
from kitchen.staff.repository import (
    DEFAULT_SEED,
    DuplicateError,
    ForeignKeyError,
    MemoryStaffRepository,
    NotFoundError,
    StaffDomainError,
    StaffRepository,
    normalize_uid,
)

__all__ = [
    # 模型
    "Staff", "StaffCard", "HealthCertificate", "Shift", "MorningCheck",
    "MorningAnswers", "MORNING_QUESTIONS", "MORNING_TEMP_LIMIT_C",
    "QUALIFICATION_LABELS", "qualification_label",
    # 结论
    "AdmissionResult", "DenyReason", "MorningOutcome", "ReviewOutcome",
    # 仓储
    "MemoryStaffRepository", "StaffRepository", "DEFAULT_SEED", "normalize_uid",
    "StaffDomainError", "NotFoundError", "DuplicateError", "ForeignKeyError",
    # 判定
    "AdmissionController", "StaffMatcherAdapter", "LEAD_QUALIFICATION",
    "CertificateSweeper", "CertificateSweep",
    "DutyGuard", "DutyAlert", "TICKET_UNAUTHORIZED",
]
