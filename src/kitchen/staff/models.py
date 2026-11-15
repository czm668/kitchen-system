# -*- coding: utf-8 -*-
"""人员域模型（#8 晨检 / #9 健康证 / #10 资质匹配）。

对应 `migrations/014_kitchen_core.sql` 的五张表：`staff` / `staff_cards` /
`shifts` / `health_certificates` / `morning_checks`。这里不重写表结构，只把它们
翻成不可变对象，让判定逻辑能脱离数据库单测。

## 三条容易做反的口径

1. **健康证是否有效，看 `valid_to`，不看 `state`。**
   `state` 是每日扫描写出来的物化结果，只要扫描任务漏跑一次（服务重启、容器
   重建、时区变更），它就会和真实日期脱节。真出现「state=valid 但 6 月就过期」
   时，拿 `state` 判定等于放一个证已过期的人进后厨——而这类错误不会报错，只会
   在检查时暴露。`state` 只用于列表筛选与统计。

2. **停用的卡要和没登记的卡分开报。**
   `staff_cards` 换卡是「旧行置 enabled=0」而不是删除（留痕要求）。如果实现
   只查 `enabled=1` 然后统一返回「卡未登记」，现场拿到的是「你这张卡不存在」，
   实际是「这张卡上周已换新」——维修方向完全不同。

3. **晨检的唯一键是 `(staff_id, check_date)`。**
   同一天重复刷牌必须 UPSERT。MySQL 上有 `uk_mc_staff_date` 会直接报 1062，
   内存实现如果不做去重就会静默堆行，于是「单测全绿、上线报错」。

## 与规则引擎的关系

人员准入**不走** `env_thresholds`：它不是指标阈值判定，而是「一次刷卡事件的
状态查询」。硬塞进规则引擎会需要给引擎加「枚举型指标 + 事件触发」两套概念，
而收益只是复用闩锁——准入不需要闩锁（拒绝是一次性的，不需要人工复位）。
所以这一域是独立的控制器，产出 `AdmissionResult` 交给上层（Web / TFT / 工单）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

from kitchen.domain import CST as _CST
from kitchen.domain import parse_date as _parse_date

__all__ = [
    "CST", "QUALIFICATION_LABELS", "qualification_label", "parse_qualifications",
    "Staff", "StaffCard", "HealthCertificate", "Shift", "MorningCheck",
    "MorningAnswers", "MORNING_TEMP_LIMIT_C", "MORNING_QUESTIONS",
    "DenyReason", "AdmissionResult", "MorningOutcome", "ReviewOutcome",
]

#: 后厨时区。定义在 `kitchen.domain`（process 域也用同一个），这里只做转出。
#: 服务器跑在 UTC 时用系统本地时区算日期，会在每天早上 8 点前把「昨天」当成
#: 今天，晨检记录直接串日。
CST = _CST

#: 资质字典。左侧 code 与 `staff.qualifications` / `work_stations.kind` 共用，
#: 右侧只用于界面显示；不存在「资质 id 表」——四个值不值得单独建表，
#: 但要有一处唯一定义，避免小程序写「灶台」、TFT 写「热菜」。
QUALIFICATION_LABELS: dict[str, str] = {
    "stove": "灶台/热菜",
    "cold_dish": "冷菜/凉菜",
    "prep": "配菜",
    "sample": "留样",
    "disinfection": "洗消消毒",
    "wash": "粗加工/清洗",
    "lead": "组长（复核权限）",
}


def qualification_label(code: str) -> str:
    return QUALIFICATION_LABELS.get(code, code)


def parse_qualifications(raw: Any) -> frozenset[str]:
    """`staff.qualifications` 是 JSON 数组（SQL 里存字符串）。

    脏数据不要静默变空集：空集意味着「什么工序都不能做」，人会被派不出单却
    查不出原因。这里接受三种形态并明确报错第四种。
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(str(item).strip() for item in raw if str(item).strip())
    text = str(raw).strip()
    if not text:
        return frozenset()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"qualifications 不是合法 JSON：{text!r}") from exc
    if isinstance(parsed, str):          # 形如 "\"stove\"" 的单值
        return frozenset({parsed.strip()}) if parsed.strip() else frozenset()
    if not isinstance(parsed, list):
        raise ValueError(f"qualifications 应为数组，实际是 {type(parsed).__name__}")
    return frozenset(str(item).strip() for item in parsed if str(item).strip())


def parse_date(raw: Any) -> date | None:
    """`DATE` 列 → date。空值返回 None（调用方决定是「缺失」还是「不限」）。

    实现在 `kitchen.domain`，与 process 域共用同一份；这里保留函数名是为了让
    `from kitchen.staff.models import parse_date` 这类既有调用点不受影响。
    """
    return _parse_date(raw)


# ---------------------------------------------------------------- 实体
@dataclass(frozen=True)
class Staff:
    id: int
    code: str
    name: str
    phone: str = ""
    qualifications: frozenset[str] = frozenset()
    enabled: bool = True

    @property
    def qualified_labels(self) -> tuple[str, ...]:
        return tuple(qualification_label(code) for code in sorted(self.qualifications))

    def has(self, qualification: str) -> bool:
        return str(qualification).strip() in self.qualifications

    def covers(self, required: frozenset[str] | set[str] | tuple[str, ...]) -> bool:
        """**全部**要求都要满足（AND）。返回任一即可会让人被派去做不会做的工序。"""
        return frozenset(required) <= self.qualifications


@dataclass(frozen=True)
class StaffCard:
    id: int
    card_uid: str
    staff_id: int
    enabled: bool = True
    bound_at: datetime | None = None


@dataclass(frozen=True)
class HealthCertificate:
    id: int
    staff_id: int
    cert_no: str = ""
    valid_from: date | None = None
    valid_to: date | None = None
    #: 物化状态（`valid` / `expired` / `revoked`）。**判定不看它**，见模块 docstring 第 1 条
    state: str = "valid"

    def is_valid_on(self, day: date) -> bool:
        """当日有效 = 未被吊销 + 有到期日 + 到期日不早于当日。

        到期日当天有效（`valid_to >= day`）：卫生监督的实操口径是「有效期至该日」，
        写成 `>` 会让证件在最后一天突然不可用，现场很难解释。
        """
        if self.state == "revoked":
            return False
        if self.valid_to is None:
            return False
        if self.valid_from is not None and self.valid_from > day:
            return False
        return self.valid_to >= day

    def expires_in(self, day: date) -> int | None:
        """距到期还有几天（负数=已过期）。用于「快到期」提醒。"""
        if self.valid_to is None:
            return None
        return (self.valid_to - day).days


@dataclass(frozen=True)
class Shift:
    id: int
    staff_id: int
    station_id: int | None
    shift_date: date
    shift_no: int = 1
    state: str = "scheduled"
    assignment: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.state == "blocked"


#: 晨检体温门限。37.3℃ 是《餐饮服务从业人员健康检查》现场通用的腋温上限口径，
#: 与发烧判定（37.5/38）不同：门禁宁可严一点，现场有红外额温枪读数偏低的问题。
MORNING_TEMP_LIMIT_C = 37.3

#: 问卷四项，TFT 按此顺序渲染。`key` 与 `MorningAnswers` 字段名一致。
MORNING_QUESTIONS: tuple[tuple[str, str, str], ...] = (
    ("temp_c", "体温（℃）", "number"),
    ("diarrhea", "今日是否腹泻", "bool"),
    ("pharyngitis", "是否有咽部炎症/咳嗽", "bool"),
    ("hand_wound", "手部是否有化脓性伤口", "bool"),
)


@dataclass(frozen=True)
class MorningAnswers:
    """晨检问卷四项。字段名与设计文档 #8 一一对应。"""

    temp_c: float | None = None
    diarrhea: bool = False
    pharyngitis: bool = False
    hand_wound: bool = False

    def failures(self, temp_limit_c: float = MORNING_TEMP_LIMIT_C) -> tuple[str, ...]:
        """不合格项。**缺项算不合格**，不算正常。

        这与环境侧「缺数据宁可不判」正好相反，是刻意的：环境缺数据时报警可能
        是误报，代价是一次白跑；门禁缺数据时放行是漏检，代价是一次食安事故。
        两侧的安全偏好方向不同，不能统一。
        """
        problems: list[str] = []
        if self.temp_c is None:
            problems.append("体温未填写")
        elif float(self.temp_c) >= temp_limit_c:
            problems.append(f"体温 {float(self.temp_c):.1f}℃ ≥ {temp_limit_c:.1f}℃")
        if self.diarrhea:
            problems.append("腹泻")
        if self.pharyngitis:
            problems.append("咽部炎症")
        if self.hand_wound:
            problems.append("手部伤口")
        return tuple(problems)

    def to_dict(self) -> dict[str, Any]:
        return {
            "temp_c": self.temp_c,
            "diarrhea": self.diarrhea,
            "pharyngitis": self.pharyngitis,
            "hand_wound": self.hand_wound,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> MorningAnswers:
        data = raw or {}
        if isinstance(raw, str):
            data = json.loads(raw or "{}")
        temp = data.get("temp_c")
        return cls(
            temp_c=None if temp is None or temp == "" else float(temp),
            diarrhea=bool(data.get("diarrhea")),
            pharyngitis=bool(data.get("pharyngitis")),
            hand_wound=bool(data.get("hand_wound")),
        )

    def describe(self) -> str:
        temp = "体温未填" if self.temp_c is None else f"体温 {self.temp_c:.1f}℃"
        flags = [name for name, on in (
            ("腹泻", self.diarrhea), ("咽炎", self.pharyngitis), ("手部伤口", self.hand_wound),
        ) if on]
        return temp + ("；" + "、".join(flags) if flags else "")


@dataclass(frozen=True)
class MorningCheck:
    """一条晨检记录（对应 `morning_checks` 一行）。

    `result`：`passed` 合格 / `failed` 不合格（当日锁定）/ `excused` 组长复核放行。
    `channel`：留痕通道，取值 `tft` / `web` / `tft+web`（双通道复核）。
    """

    staff_id: int
    check_date: date
    answers: MorningAnswers
    result: str = "passed"
    failures: tuple[str, ...] = ()
    confirmed_by: str = ""
    channel: str = "tft"
    ts: datetime | None = None

    @property
    def locked(self) -> bool:
        """当日是否被锁（不合格且未经复核）。"""
        return self.result == "failed"

    @property
    def admitted(self) -> bool:
        return self.result in ("passed", "excused")

    def describe(self) -> str:
        body = f"{self.check_date.isoformat()} {self.answers.describe()} → {self.result}"
        if self.failures:
            body += f"（{'；'.join(self.failures)}）"
        if self.confirmed_by:
            body += f"，复核 {self.confirmed_by} via {self.channel}"
        return body


# ---------------------------------------------------------------- 结论
class DenyReason(str, Enum):
    """拒绝准入的具体原因。**每种都要能对应一个现场动作**，否则等于没说。"""

    UNKNOWN_CARD = "unknown_card"        # 卡未登记 → 去管理端绑卡
    CARD_DISABLED = "card_disabled"      # 卡已停用（换卡）→ 换新卡或重新启用
    STAFF_DISABLED = "staff_disabled"    # 人员离职/停用 → 找主管
    CERT_MISSING = "cert_missing"        # 无健康证记录 → 补录
    CERT_EXPIRED = "cert_expired"        # 健康证过期 → 办证
    CERT_REVOKED = "cert_revoked"        # 健康证吊销 → 找主管
    MORNING_MISSING = "morning_missing"  # 今日未晨检 → 就地填问卷
    MORNING_FAILED = "morning_failed"    # 晨检不合格 → 当日锁定，组长复核
    QUALIFICATION_MISS = "qualification_miss"  # 资质不符 → 换工位/换人
    SHIFT_BLOCKED = "shift_blocked"      # 当日排班被系统拦截 → 找主管
    NOT_ON_SHIFT = "not_on_shift"        # 今日无排班 → 找主管

    @property
    def label(self) -> str:
        return {
            "unknown_card": "工牌未登记",
            "card_disabled": "工牌已停用（可能已换卡）",
            "staff_disabled": "人员已停用",
            "cert_missing": "无健康证记录",
            "cert_expired": "健康证已过期",
            "cert_revoked": "健康证已吊销",
            "morning_missing": "今日未晨检",
            "morning_failed": "晨检不合格，当日权限已锁定",
            "qualification_miss": "资质不匹配该工位",
            "shift_blocked": "当日排班已被系统拦截",
            "not_on_shift": "今日无排班",
        }[self.value]

    @property
    def next_step(self) -> str:
        """给 TFT 的一句话指引。门禁拒绝不告诉人「然后怎么办」是最常见的失误。"""
        return {
            "unknown_card": "请到管理端绑定工牌后再刷卡",
            "card_disabled": "旧卡已停用，请使用新卡或联系主管启用",
            "staff_disabled": "请联系后厨主管",
            "cert_missing": "请提交健康证编号到管理端登记",
            "cert_expired": "健康证已过期，请先办证；期间不得上岗",
            "cert_revoked": "健康证被吊销，请联系主管与食品安全员",
            "morning_missing": "请先完成今日晨检问卷",
            "morning_failed": "当日不得上岗；如需复核，请组长刷工牌并经管理端确认",
            "qualification_miss": "该工位需要其它资质，请联系组长换岗",
            "shift_blocked": "请到管理端核对排班被拦截的原因",
            "not_on_shift": "今天没有你的排班，请联系组长临时排班",
        }[self.value]


@dataclass(frozen=True)
class AdmissionResult:
    """一次刷卡的准入结论。"""

    ok: bool
    card_uid: str
    staff: Staff | None = None
    station_id: int | None = None
    station_requirement: str = ""
    reasons: tuple[DenyReason, ...] = ()
    checked_at: datetime | None = None
    #: 每个原因的具体说明（含日期、资质名），用于 Web 端展示与申诉
    details: tuple[str, ...] = ()

    @property
    def needs_morning_check(self) -> bool:
        """是否该就地弹问卷（只有这一种拒绝是「当场能解决」的）。"""
        return DenyReason.MORNING_MISSING in self.reasons

    @property
    def masked(self) -> bool:
        """是否需要「硬拦截」——即连问卷都不该给。

        证件/卡问题不能靠填问卷绕过：给了问卷，人会以为填完就能上班。
        """
        hard = {
            DenyReason.UNKNOWN_CARD, DenyReason.CARD_DISABLED,
            DenyReason.STAFF_DISABLED, DenyReason.CERT_MISSING,
            DenyReason.CERT_EXPIRED, DenyReason.CERT_REVOKED,
            DenyReason.SHIFT_BLOCKED,
        }
        return any(reason in hard for reason in self.reasons)

    def describe(self) -> str:
        who = f"{self.staff.name}({self.staff.code})" if self.staff else self.card_uid
        head = f"准入通过 {who}" if self.ok else f"拒绝 {who}"
        if self.station_id is not None:
            head += f" @ 工位{self.station_id}"
        if not self.reasons:
            return head
        body = "；".join(reason.label for reason in self.reasons)
        return f"{head}：{body}"


@dataclass(frozen=True)
class MorningOutcome:
    """提交晨检问卷的结果。"""

    check: MorningCheck
    ok: bool

    @property
    def locked(self) -> bool:
        return self.check.locked

    def describe(self) -> str:
        mark = "晨检通过" if self.ok else "晨检不合格"
        return f"{mark}：{self.check.describe()}"


@dataclass(frozen=True)
class ReviewOutcome:
    """组长复核结果（TFT 刷牌 + Web 二次确认）。"""

    ok: bool
    check: MorningCheck | None = None
    reviewer: str = ""
    reason: str = ""

    def describe(self) -> str:
        if not self.ok:
            return f"复核被拒：{self.reason}"
        return f"复核通过（{self.reviewer}）：{self.check.describe() if self.check else ''}"
