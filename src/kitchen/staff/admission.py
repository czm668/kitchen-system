# -*- coding: utf-8 -*-
"""人员准入判定（#8 晨检 / #9 健康证 / #10 资质）与每日证件扫描。

## 一次刷卡要过的五道门，顺序不能换

```
工牌 → 人员 → 健康证 → 当日排班 → 晨检 → 工位资质
```

顺序不是随便排的，它是**拒绝原因的优先级**，而拒绝原因会直接显示在 TFT 上，
指导现场的人「下一步该做什么」。排错的后果是具体的：

- 先查晨检再查证件：健康证过期的人被引导去填晨检问卷。填完再来一次还是被拒，
  人就会认为「系统坏了」，而不是「证过期了」；
- 先查资质再查晨检：人被拒的理由是「资质不符」，但他今天根本没做晨检——
  换到资质匹配的工位照样进不去，白跑一趟；
- 把「无排班」放到最后：临时顶班的人会被提示「今日未晨检」，做了晨检还是进不去。

所以本模块**一次性收集**所有不通过项（`reasons` 是元组而非单个），TFT 能一次
把问题都说完；但 `next_step` 只取第一条（最该先解决的）。

## 双通道复核的两个硬要求

`review()` 同时要求：组长刷工牌（TFT 通道）+ Web 端二次确认。只做前者，组长
可以在无人监督时把不合格的人放进去；只做后者，谁点的确认查不到现场凭证。
两条都留痕（`channel='tft+web'`），且**复核人不能是自己**——否则当事人借组长
工牌自助放行，留痕看起来还很规范。

## 为什么准入不接规则引擎

见 `models.py` 末尾说明：准入是「刷卡事件的状态查询」，不需要去抖/闩锁/窗口。
唯一需要去抖的是 #11 未授权上岗（持续在岗），那部分在 `duty.py` 里按时间积分。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from .models import (
    CST,
    MORNING_TEMP_LIMIT_C,
    AdmissionResult,
    DenyReason,
    HealthCertificate,
    MorningAnswers,
    MorningCheck,
    MorningOutcome,
    ReviewOutcome,
    Staff,
    StaffCard,
)
from .repository import MemoryStaffRepository, normalize_uid

log = logging.getLogger("kitchen.staff.admission")

__all__ = ["AdmissionController", "CertificateSweep", "CertificateSweeper",
           "StaffMatcherAdapter", "LEAD_QUALIFICATION"]

#: 复核权限资质。走字典而不是硬编码字符串，是因为小程序/Web 也会引用它。
LEAD_QUALIFICATION = "lead"


@dataclass(frozen=True)
class CertificateSweep:
    """一次每日证件扫描的结果（#9）。"""

    day: date
    expired: tuple[tuple[Staff, HealthCertificate], ...] = ()
    blocked_shifts: tuple[int, ...] = ()          # 被置 blocked 的 shift_id
    expiring_soon: tuple[tuple[Staff, HealthCertificate, int], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.expired and not self.expiring_soon

    def describe(self) -> str:
        if self.empty:
            return f"{self.day} 证件扫描：无异常"
        parts = []
        if self.expired:
            parts.append(
                "过期 " + "、".join(f"{s.name}({c.cert_no})" for s, c in self.expired)
            )
        if self.blocked_shifts:
            parts.append(f"拦截排班 {len(self.blocked_shifts)} 条")
        if self.expiring_soon:
            parts.append(
                "临期 " + "、".join(f"{s.name} {days}天" for s, _c, days in self.expiring_soon)
            )
        return f"{self.day} 证件扫描：" + "；".join(parts)


class AdmissionController:
    """刷卡准入 + 晨检问卷 + 组长复核 + 资质候选。"""

    def __init__(
        self,
        repo: MemoryStaffRepository,
        *,
        temp_limit_c: float = MORNING_TEMP_LIMIT_C,
        require_shift: bool = True,
        tz=CST,
    ) -> None:
        self.repo = repo
        self.temp_limit_c = float(temp_limit_c)
        #: 是否要求当日有排班。关掉它只用于演示/联调，生产必须开着——
        #: 否则离职未停用的人只要证没过期就能刷开工位。
        self.require_shift = bool(require_shift)
        self.tz = tz

    # ------------------------------------------------------------ 时间
    def today(self, now: datetime | None = None) -> date:
        """「今天」一律按后厨时区算。传 naive datetime 时按本地时间理解。"""
        if now is None:
            return datetime.now(self.tz).date()
        if now.tzinfo is None:
            return now.date()
        return now.astimezone(self.tz).date()

    # ------------------------------------------------------------ 刷卡
    def scan(
        self,
        card_uid: str,
        *,
        station_id: int | None = None,
        now: datetime | None = None,
    ) -> AdmissionResult:
        """一次刷卡 → 准入结论。**不抛异常**，拒绝也走返回值。

        门禁类操作抛异常会逼调用方写 try/except，而现场最需要的是「把原因显示
        出来」而不是「捕获异常后统一显示失败」。
        """
        day = self.today(now)
        card, staff, reasons, details = self._resolve(card_uid, day)
        requirement = self.repo.station_requirement(station_id)
        if staff is not None and not reasons:
            self._gate(staff, station_id, day, reasons, details)

        result = AdmissionResult(
            ok=not reasons,
            card_uid=normalize_uid(card_uid),
            staff=staff,
            station_id=station_id,
            station_requirement=requirement,
            reasons=tuple(reasons),
            checked_at=now or datetime.now(self.tz),
            details=tuple(details),
        )
        log.info("准入：%s", result.describe())
        return result

    def scan_staff(
        self,
        staff_id: int,
        *,
        station_id: int | None = None,
        now: datetime | None = None,
    ) -> AdmissionResult:
        """按**人**判定准入（来源是视觉/人脸或手动指定，没有卡可刷）。

        A3-05「未授权上岗」用它：人在工位上被识别出来，但当日没晨检或资质不符。
        与 `scan()` 的差别只有一条——不走工牌校验（人已经在里面了，卡在哪不重要），
        其余五道门完全一致。**不能另写一套判定**：两处规则分叉后，会出现
        「刷卡被拒但视觉判定认为在岗合法」这种自相矛盾的状态。
        """
        day = self.today(now)
        reasons: list[DenyReason] = []
        details: list[str] = []
        requirement = self.repo.station_requirement(station_id)
        staff = self.repo.staff(int(staff_id))
        if staff is None:
            return AdmissionResult(
                ok=False, card_uid="", staff=None, station_id=station_id,
                station_requirement=requirement,
                reasons=(DenyReason.UNKNOWN_CARD,),
                checked_at=now or datetime.now(self.tz),
                details=(f"staff_id={staff_id} 不存在",),
            )
        if not staff.enabled:
            reasons.append(DenyReason.STAFF_DISABLED)
            details.append(f"{staff.name}({staff.code}) 已停用")
        cert_reason, cert_detail = self._certificate_state(staff, day)
        if cert_reason is not None:
            reasons.append(cert_reason)
            details.append(cert_detail)
        if not reasons:
            self._gate(staff, station_id, day, reasons, details)
        active = self.repo.active_card_of(staff.id)
        return AdmissionResult(
            ok=not reasons,
            card_uid=active.card_uid if active else "",
            staff=staff, station_id=station_id, station_requirement=requirement,
            reasons=tuple(reasons), checked_at=now or datetime.now(self.tz),
            details=tuple(details),
        )

    def _gate(
        self,
        staff: Staff,
        station_id: int | None,
        day: date,
        reasons: list[DenyReason],
        details: list[str],
    ) -> None:
        """硬门禁之后的四道门：排班 → 晨检 → 资质。

        就地往 `reasons`/`details` 追加，返回 None。调用方只在硬门禁全过时调用它，
        所以内部不必再判空——**但每一段仍要用 `if not reasons` 兜住**：排班被拦截
        的人不该再被追问「为什么没晨检」，TFT 一次只显示第一条指引。
        """
        requirement = self.repo.station_requirement(station_id)

        # 排班：只有硬门禁通过才有意义（证件不全的人没班次，报「无排班」是噪声）
        if not reasons and self.require_shift:
            shift = self.repo.shift_on(staff.id, station_id, day)
            shifts = self.repo.shifts(staff.id, day)
            if not shifts:
                reasons.append(DenyReason.NOT_ON_SHIFT)
                details.append(f"{day} 无排班记录")
            elif shift is not None and shift.blocked:
                reasons.append(DenyReason.SHIFT_BLOCKED)
                details.append(
                    f"排班 #{shift.id} 已被拦截："
                    f"{shift.assignment.get('blocked_reason') or '原因未记录'}"
                )
            elif shift is None and station_id is not None:
                # 有排班但不在这个工位：允许（现场换岗很常见），仅记录
                details.append(
                    f"当日排班在工位 {[s.station_id for s in shifts]}，本次在 {station_id}"
                )

        # 晨检：硬门禁过了才查，且只在证件/排班都过时追问
        if not reasons:
            check = self.repo.morning_check(staff.id, day)
            if check is None:
                reasons.append(DenyReason.MORNING_MISSING)
                details.append(f"{day} 尚未提交晨检问卷")
            elif check.locked:
                reasons.append(DenyReason.MORNING_FAILED)
                details.append("晨检不合格：" + "；".join(check.failures))

        # 资质：最后查（要把人先放进后厨，才谈得上站哪个工位）
        if not reasons and requirement and not staff.covers({requirement}):
            reasons.append(DenyReason.QUALIFICATION_MISS)
            details.append(
                f"工位需要「{requirement}」资质，本人持有 "
                f"{sorted(staff.qualifications) or '（无）'}"
            )

    def _resolve(
        self, card_uid: str, day: date,
    ) -> tuple[StaffCard | None, Staff | None, list[DenyReason], list[str]]:
        """工牌 → 人员 → 健康证。三者的不通过项**都要收集完**。"""
        reasons: list[DenyReason] = []
        details: list[str] = []

        card = self.repo.card(card_uid)
        if card is None:
            return None, None, [DenyReason.UNKNOWN_CARD], [f"卡号 {normalize_uid(card_uid)} 未登记"]
        if not card.enabled:
            # 与「未登记」严格区分：现场「卡坏了」和「换过卡」的处理方式完全不同
            reasons.append(DenyReason.CARD_DISABLED)
            details.append(
                f"卡号 {card.card_uid} 已停用（绑卡时间 "
                f"{card.bound_at.date().isoformat() if card.bound_at else '未知'}）"
            )

        staff = self.repo.staff(card.staff_id)
        if staff is None:
            # 外键在仓储层就挡住了，走到这里说明数据被手工改过
            reasons.append(DenyReason.UNKNOWN_CARD)
            details.append(f"卡号 {card.card_uid} 指向的 staff_id={card.staff_id} 不存在")
            return card, None, reasons, details
        if not staff.enabled:
            reasons.append(DenyReason.STAFF_DISABLED)
            details.append(f"{staff.name}({staff.code}) 已停用")

        cert_reason, cert_detail = self._certificate_state(staff, day)
        if cert_reason is not None:
            reasons.append(cert_reason)
            details.append(cert_detail)
        return card, staff, reasons, details

    def _certificate_state(
        self, staff: Staff, day: date,
    ) -> tuple[DenyReason | None, str]:
        """#9：证件是否可用。**按日期判，不看 `state` 列**（理由见 models docstring）。"""
        certs = self.repo.certificates(staff.id)
        if not certs:
            return DenyReason.CERT_MISSING, "台账里没有健康证记录"
        valid = [cert for cert in certs if cert.is_valid_on(day)]
        if valid:
            return None, ""
        revoked = [cert for cert in certs if cert.state == "revoked"]
        if revoked:
            cert = revoked[0]
            return DenyReason.CERT_REVOKED, f"健康证 {cert.cert_no} 已吊销"
        latest = max(certs, key=lambda item: item.valid_to or date.min)
        gap = abs(((latest.valid_to or day) - day).days)
        return (
            DenyReason.CERT_EXPIRED,
            f"健康证 {latest.cert_no} 到期日 {latest.valid_to}，已过期 {gap} 天",
        )

    # ------------------------------------------------------------ 晨检
    def submit_morning(
        self,
        card_uid: str,
        answers: MorningAnswers | dict[str, Any],
        *,
        now: datetime | None = None,
        channel: str = "tft",
    ) -> MorningOutcome:
        """提交晨检问卷。同日重复提交 = 覆盖（唯一键 `(staff_id, check_date)`）。

        证件/卡类硬门禁不通过时**直接拒绝填问卷**：让证过期的人填完问卷再被拒，
        等于把人当流程的一部分耍了一圈。返回的 `check` 里 `result='failed'` 且
        没有记录写入，调用方看 `ok=False` + `outcome.check.failures` 即可。
        """
        day = self.today(now)
        _card, staff, reasons, details = self._resolve(card_uid, day)
        if staff is None or any(reason in {
            DenyReason.UNKNOWN_CARD, DenyReason.CARD_DISABLED,
            DenyReason.STAFF_DISABLED, DenyReason.CERT_MISSING,
            DenyReason.CERT_EXPIRED, DenyReason.CERT_REVOKED,
        } for reason in reasons):
            placeholder = MorningCheck(
                staff_id=staff.id if staff else 0, check_date=day,
                answers=MorningAnswers(), result="failed",
                failures=tuple(details) or tuple(reason.label for reason in reasons),
                channel=channel, ts=now,
            )
            log.info("晨检被拒：%s %s", normalize_uid(card_uid), "；".join(details))
            return MorningOutcome(check=placeholder, ok=False)

        items = answers if isinstance(answers, MorningAnswers) else MorningAnswers.from_dict(answers)
        failures = items.failures(self.temp_limit_c)
        check = MorningCheck(
            staff_id=staff.id,
            check_date=day,
            answers=items,
            result="failed" if failures else "passed",
            failures=failures,
            channel=channel,
            ts=now or datetime.now(self.tz),
        )
        self.repo.upsert_morning_check(check)
        log.info("晨检：%s %s", staff.name, check.describe())
        return MorningOutcome(check=check, ok=not failures)

    def review(
        self,
        staff_id: int,
        *,
        reviewer_card_uid: str,
        web_confirmed: bool = False,
        reviewer_label: str = "",
        now: datetime | None = None,
    ) -> ReviewOutcome:
        """组长复核（TFT 刷牌 + Web 二次确认，双通道留痕）。"""
        day = self.today(now)
        check = self.repo.morning_check(int(staff_id), day)
        if check is None:
            return ReviewOutcome(False, None, reviewer_label, f"{day} 没有晨检记录可复核")
        if not check.locked:
            # 已通过或已复核：不要重复写一条「复核通过」，否则留痕失去意义
            return ReviewOutcome(False, check, reviewer_label, "该晨检未被锁定，无需复核")

        reviewer, why = self._resolve_reviewer(reviewer_card_uid, day)
        if reviewer is None:
            return ReviewOutcome(False, check, reviewer_label, why)
        if reviewer.id == int(staff_id):
            return ReviewOutcome(False, check, reviewer.name, "不能复核自己的晨检")
        if not web_confirmed:
            # 只刷卡 = 单通道。宁可拒绝也不要把半条留痕写进库
            return ReviewOutcome(False, check, reviewer.name, "缺少管理端二次确认（需双通道）")

        updated = MorningCheck(
            staff_id=check.staff_id, check_date=check.check_date, answers=check.answers,
            result="excused", failures=check.failures,
            confirmed_by=reviewer_label or f"{reviewer.name}({reviewer.code})",
            channel="tft+web", ts=now or datetime.now(self.tz),
        )
        self.repo.upsert_morning_check(updated)
        log.warning(
            "晨检复核放行：staff_id=%s by %s（原不合格：%s）",
            staff_id, updated.confirmed_by, "；".join(check.failures),
        )
        return ReviewOutcome(True, updated, updated.confirmed_by, "")

    def _resolve_reviewer(
        self, reviewer_card_uid: str, day: date,
    ) -> tuple[Staff | None, str]:
        """复核人必须是**持 lead 资质且在岗**的人，且证件有效。"""
        card = self.repo.card(reviewer_card_uid)
        if card is None:
            return None, f"复核卡号 {normalize_uid(reviewer_card_uid)} 未登记"
        if not card.enabled:
            return None, "复核卡已停用"
        staff = self.repo.staff(card.staff_id)
        if staff is None or not staff.enabled:
            return None, "复核人不存在或已停用"
        if not staff.has(LEAD_QUALIFICATION):
            return None, f"{staff.name} 无组长资质（需 {LEAD_QUALIFICATION}）"
        if not self.repo.has_valid_certificate(staff.id, day):
            # 自己的证都过期了还去复核别人，复核本身要被打回
            return None, f"{staff.name} 本人健康证无效，不能执行复核"
        return staff, ""

    # ------------------------------------------------------------ 资质（#10 后台部分）
    def candidates(
        self, requirement: str, *, now: datetime | None = None, on_shift_only: bool = True,
    ) -> tuple[Staff, ...]:
        """能承担该工序的候选人员（资质 + 证件 + 晨检 + 在岗）。派单联调在 A4。"""
        return self.repo.eligible(requirement, self.today(now), on_shift_only=on_shift_only)

    def qualifies(self, staff_id: int, requirement: str) -> bool:
        staff = self.repo.staff(int(staff_id))
        return bool(staff and staff.enabled and staff.covers({requirement}))


class CertificateSweeper:
    """#9 每日扫描：证件到期 → 置 `expired` + 拦截排班。APScheduler 每天跑一次。"""

    def __init__(
        self, repo: MemoryStaffRepository, *, warn_days: int = 30, tz=CST,
    ) -> None:
        self.repo = repo
        #: 提前多少天提醒（办证要时间，到期当天才提醒等于逼人无证上岗）
        self.warn_days = int(warn_days)
        self.tz = tz

    def run(self, day: date | None = None) -> CertificateSweep:
        day = day or datetime.now(self.tz).date()
        expired: list[tuple[Staff, HealthCertificate]] = []
        soon: list[tuple[Staff, HealthCertificate, int]] = []
        blocked: list[int] = []

        for staff in self.repo.all_staff():
            certs = self.repo.certificates(staff.id)
            if not certs:
                continue
            valid = [cert for cert in certs if cert.is_valid_on(day)]
            if not valid:
                latest = max(certs, key=lambda item: item.valid_to or date.min)
                if latest.state != "revoked":
                    self.repo.mark_certificate_state(staff.id, latest.id, "expired")
                expired.append((staff, latest))
                # 只拦截今天及以后的排班；历史排班不回溯改状态（审计要能还原）
                for shift in self.repo.shifts(staff.id, day):
                    if not shift.blocked:
                        self.repo.block_shift(shift.id, reason=f"健康证 {latest.cert_no} 已过期")
                        blocked.append(shift.id)
                continue
            for cert in valid:
                days = cert.expires_in(day)
                if days is not None and 0 <= days <= self.warn_days:
                    soon.append((staff, cert, days))

        sweep = CertificateSweep(
            day=day, expired=tuple(expired), blocked_shifts=tuple(blocked),
            expiring_soon=tuple(soon),
        )
        log.info("%s", sweep.describe())
        return sweep


class StaffMatcherAdapter:
    """把 `AdmissionController` 适配成派单要的两个方法（A4-09）。

    存在的理由：`process.dispatch.StaffMatcher` 只认 `(staff_id, requirement)`，
    而 `AdmissionController.candidates()` 返回 `Staff` 实体。适配写在**人员域**
    这一侧，`process` 就不必 import `staff`——两个域之间只剩一个鸭子类型的
    Protocol，谁都不反向依赖对方。单测里给派单塞个假 matcher 即可。

    候选集口径与 #10 完全一致（资质 + 健康证 + 晨检 + 在岗），不在这里另加条件：
    派单放过去的人，刷卡时必然过得了准入。两处口径一旦分叉，就会出现
    「派给他了、他刷不开」的循环。
    """

    def __init__(self, controller: AdmissionController,
                 *, clock: "Callable[[], datetime] | None" = None) -> None:
        self.controller = controller
        #: 与派单板共用同一个时钟。「今天」决定健康证/排班/晨检三条判定，
        #: 演示与单测若各用各的时钟，会出现「派单说有人、刷卡说没人」
        self._clock = clock

    def _now(self) -> datetime | None:
        return self._clock() if self._clock is not None else None

    def qualifies(self, staff_id: int, requirement: str) -> bool:
        if not requirement:
            return True
        return self.controller.qualifies(int(staff_id), str(requirement))

    def candidates(self, requirement: str) -> tuple[int, ...]:
        if not requirement:
            return ()
        return tuple(staff.id for staff in
                     self.controller.candidates(str(requirement), now=self._now()))
