# -*- coding: utf-8 -*-
"""人员域仓储（A3-01）：把五张表的内存实现做成**与 MySQL 同语义**的替身。

## 为什么不是 `dict` 一把梭

单测里最省事的是 `staffs = {1: Staff(...)}`，但它会让一批缺陷永远测不出来：

| 线上会炸的事 | 裸 dict 的表现 |
|---|---|
| 同一 UID 绑两个人（`uk_cards_uid`） | 静默覆盖，最后绑的人赢了 |
| 同日重复晨检（`uk_mc_staff_date`） | 静默追加两行，报表统计翻倍 |
| 卡绑到不存在的员工（外键） | 静默成功，刷卡时查不到人 |
| 员工编号重复（`uk_staff_code`） | 静默成功，考勤按编号聚合就串人 |

所以这一层**主动复刻唯一键与外键**，冲突时抛异常。代价是写测试时多几行，
收益是「内存里过了 = 库里也会过」。

## 换卡的口径

`bind_card(uid, staff_id, now)` 只新增；要停用旧卡必须显式调 `rebind_card()`。
把「换卡」做成两步，是为了让「误绑」可以撤销——一步到位的实现会在误绑新卡时
直接把旧卡停掉，人第二天刷不了卡，而日志里只有一条「绑定成功」。

## 卡号归一化

RC522 读 NTAG213 拿到的是十六进制串，大小写取决于固件实现；种子与 Web 端录入
是人手敲的，习惯大写。**查询前统一转大写**，否则「A1B2C3D4」和「a1b2c3d4」
在库里是两张卡，现场表现就是「明明绑过的卡刷不上」。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from kitchen.domain import DomainError, DuplicateError, ForeignKeyError, NotFoundError
from kitchen.sqlseed import read_table

from .models import (
    HealthCertificate,
    MorningAnswers,
    MorningCheck,
    Shift,
    Staff,
    StaffCard,
    parse_date,
    parse_qualifications,
)

log = logging.getLogger("kitchen.staff.repository")

__all__ = [
    "StaffDomainError", "NotFoundError", "DuplicateError", "ForeignKeyError",
    "normalize_uid", "StaffRepository", "MemoryStaffRepository", "DEFAULT_SEED",
]

#: 工程根的种子文件（`src/kitchen/staff/repository.py` → parents[3] = 工程根）
DEFAULT_SEED = Path(__file__).resolve().parents[3] / "fixtures" / "seed_kitchen.sql"


# 错误族与 process 域共用（`kitchen.domain`）：两个域对同一类失败给出同一族
# 异常，HTTP 层才能用一处映射把 404/400/409 分开，而不是按模块写三套 if。
StaffDomainError = DomainError          # 旧名保留，历史调用点不用改


def normalize_uid(card_uid: str) -> str:
    """卡号归一化：去空白、转大写、去掉 `0x` 前缀与冒号分隔。"""
    text = str(card_uid or "").strip().replace(":", "").replace(" ", "")
    if text.lower().startswith("0x"):
        text = text[2:]
    return text.upper()


def _as_bool(raw: Any, default: bool = True) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip() not in ("0", "", "False", "false")


def _json_or_empty(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


class StaffRepository(Protocol):
    """仓储接口。服务端接 MySQL 时实现同一套方法即可切换（A4）。"""

    def staff(self, staff_id: int) -> Staff | None: ...
    def staff_by_code(self, code: str) -> Staff | None: ...
    def all_staff(self) -> Sequence[Staff]: ...
    def card(self, card_uid: str) -> StaffCard | None: ...
    def cards_of(self, staff_id: int) -> Sequence[StaffCard]: ...
    def certificates(self, staff_id: int) -> Sequence[HealthCertificate]: ...
    def shifts(self, staff_id: int, day: date) -> Sequence[Shift]: ...
    def morning_check(self, staff_id: int, day: date) -> MorningCheck | None: ...
    def station_requirement(self, station_id: int | None) -> str: ...


class MemoryStaffRepository:
    """内存仓储：种子导入 + 唯一键/外键校验 + 增量写入。"""

    def __init__(
        self, seed_path: str | Path | None = None, *, today: date | None = None
    ) -> None:
        self._staff: dict[int, Staff] = {}
        self._codes: dict[str, int] = {}
        self._cards: dict[str, StaffCard] = {}
        self._certificates: dict[int, list[HealthCertificate]] = {}
        self._shifts: list[Shift] = []
        self._checks: dict[tuple[int, date], MorningCheck] = {}
        self._stations: dict[int, dict[str, Any]] = {}
        #: 工位 → 资质要求的显式覆盖（默认取 `work_stations.kind`）
        self.station_requirements: dict[int, str] = {}
        #: 业务落库写器（A23）。由 `api/state.py` 装配后注入；None = 纯内存
        #: （既有默认，全部单测零影响）。种子装载期不触发，基线由
        #: `sync_baseline()` 一次性物化。
        self.recorder: Any = None
        #: 自增 id 游标，模仿 AUTO_INCREMENT
        self._seq = {"staff": 0, "card": 0, "cert": 0, "shift": 0}
        if seed_path is not None:
            self.load_seed(seed_path, today=today)

    # ------------------------------------------------------------ 导入
    @classmethod
    def from_seed(
        cls, seed_path: str | Path | None = None, *, today: date | None = None
    ) -> MemoryStaffRepository:
        return cls(seed_path or DEFAULT_SEED, today=today)

    def load_seed(
        self, seed_path: str | Path, *, today: date | None = None
    ) -> None:
        """从 `seed_kitchen.sql` 导入人员域六张表（含 `work_stations`）。

        `today` 是种子里 `CURDATE()` 的求值基准；不传则按运行当天。
        单测注入一个未来日期，就能验证「答辩那天重新装配，刷卡仍走晨检」
        而不是靠「今天刚好是写种子那天」碰巧通过。
        """
        path = Path(seed_path)
        anchor = today or date.today()
        for row in read_table(path, "staff", today=anchor):
            self.upsert_staff(
                staff_id=int(row["id"]),
                code=str(row["code"]),
                name=str(row["name"]),
                phone=str(row.get("phone") or ""),
                qualifications=parse_qualifications(row.get("qualifications")),
                enabled=_as_bool(row.get("enabled")),
                _loading=True,
            )
        for row in read_table(path, "staff_cards", today=anchor):
            self._load_card(
                StaffCard(
                    id=int(row["id"]),
                    card_uid=normalize_uid(str(row["card_uid"])),
                    staff_id=int(row["staff_id"]),
                    enabled=_as_bool(row.get("enabled")),
                    bound_at=_dt(row.get("bound_at")),
                )
            )
        for row in read_table(path, "health_certificates", today=anchor):
            self.add_certificate(
                staff_id=int(row["staff_id"]),
                cert_no=str(row.get("cert_no") or ""),
                valid_from=parse_date(row.get("valid_from")),
                valid_to=parse_date(row.get("valid_to")),
                state=str(row.get("state") or "valid"),
                _loading=True,
                _cert_id=int(row["id"]),
            )
        for row in read_table(path, "shifts", today=anchor):
            self.add_shift(
                staff_id=int(row["staff_id"]),
                station_id=None if row.get("station_id") is None else int(row["station_id"]),
                shift_date=parse_date(row["shift_date"]),
                shift_no=int(row.get("shift_no") or 1),
                state=str(row.get("state") or "scheduled"),
                assignment=_json_or_empty(row.get("assignment")),
                _loading=True,
                _shift_id=int(row["id"]),
            )
        try:
            for row in read_table(path, "work_stations", today=anchor):
                self._stations[int(row["id"])] = {
                    "id": int(row["id"]),
                    "code": str(row.get("code") or ""),
                    "name": str(row.get("name") or ""),
                    "kind": str(row.get("kind") or "stove"),
                }
        except Exception:        # 种子里没有这张表时不影响人员域，只是工位要求为空
            self._stations = {}

    # ------------------------------------------------------------ 查
    def staff(self, staff_id: int) -> Staff | None:
        return self._staff.get(int(staff_id))

    def all_staff(self) -> tuple[Staff, ...]:
        """全体员工（按 id）。每日证件扫描与统计要用。"""
        return tuple(self._staff[key] for key in sorted(self._staff))

    def staff_by_code(self, code: str) -> Staff | None:
        sid = self._codes.get(str(code).strip())
        return None if sid is None else self._staff.get(sid)

    def card(self, card_uid: str) -> StaffCard | None:
        return self._cards.get(normalize_uid(card_uid))

    def cards_of(self, staff_id: int) -> tuple[StaffCard, ...]:
        return tuple(
            sorted(
                (card for card in self._cards.values() if card.staff_id == int(staff_id)),
                key=lambda card: card.id,
            )
        )

    def active_card_of(self, staff_id: int) -> StaffCard | None:
        for card in self.cards_of(staff_id):
            if card.enabled:
                return card
        return None

    def certificates(self, staff_id: int) -> tuple[HealthCertificate, ...]:
        return tuple(self._certificates.get(int(staff_id), ()))

    def shifts(self, staff_id: int, day: date) -> tuple[Shift, ...]:
        return tuple(
            shift for shift in self._shifts
            if shift.staff_id == int(staff_id) and shift.shift_date == day
        )

    def all_shifts(self) -> tuple[Shift, ...]:
        """全部班次（按 id）。基线落库（A23）用，按 (人, 日) 过滤取不全。"""
        return tuple(sorted(self._shifts, key=lambda shift: shift.id))

    def shift_on(self, staff_id: int, station_id: int | None, day: date) -> Shift | None:
        for shift in self.shifts(staff_id, day):
            if station_id is None or shift.station_id == int(station_id):
                return shift
        return None

    def morning_check(self, staff_id: int, day: date) -> MorningCheck | None:
        return self._checks.get((int(staff_id), day))

    def station(self, station_id: int) -> dict[str, Any] | None:
        return self._stations.get(int(station_id))

    def station_requirement(self, station_id: int | None) -> str:
        """工位要求的资质 code。

        来源优先级：显式覆盖 → `work_stations.kind` → 空（不限）。
        `kind` 在种子里就是 `stove`，与资质字典天然对齐；不另建要求表是因为
        当前 2 个工位只有 1 种 `kind`，建表等于给未来的自己留一个必须同步的
        第二事实源。真要按工序细分（例如同一灶台按菜品要求冷菜资质）时，
        走 `station_requirements` 覆盖，仍不需要改表。
        """
        if station_id is None:
            return ""
        sid = int(station_id)
        if sid in self.station_requirements:
            return self.station_requirements[sid]
        return str((self._stations.get(sid) or {}).get("kind") or "")

    # ------------------------------------------------------------ 写
    def upsert_staff(
        self,
        *,
        staff_id: int | None = None,
        code: str,
        name: str,
        phone: str = "",
        qualifications: Iterable[str] | str = (),
        enabled: bool = True,
        _loading: bool = False,
    ) -> Staff:
        code = str(code).strip()
        if not code:
            raise StaffDomainError("员工编号不能为空")
        existing_id = self._codes.get(code)
        if existing_id is not None and existing_id != staff_id:
            raise DuplicateError(f"员工编号 {code} 已存在（staff_id={existing_id}）")
        new_id = int(staff_id) if staff_id is not None else self._next("staff")
        entity = Staff(
            id=new_id,
            code=code,
            name=str(name).strip(),
            phone=str(phone or ""),
            qualifications=parse_qualifications(qualifications),
            enabled=bool(enabled),
        )
        self._staff[new_id] = entity
        self._codes[code] = new_id
        if _loading:
            self._seq["staff"] = max(self._seq["staff"], new_id)
        else:
            self._persist("staff", "upsert_staff_row", entity)
        return entity

    def set_staff_enabled(self, staff_id: int, enabled: bool) -> Staff:
        """启用/停用人员。**没有删除**：考勤、工单、晨检记录都要能回溯到人。"""
        staff = self._staff.get(int(staff_id))
        if staff is None:
            raise NotFoundError(f"staff_id={staff_id} 不存在")
        updated = Staff(
            staff.id, staff.code, staff.name, staff.phone,
            staff.qualifications, bool(enabled),
        )
        self._staff[updated.id] = updated
        self._persist("staff", "upsert_staff_row", updated)
        return updated

    def bind_card(
        self, card_uid: str, staff_id: int, *, now: datetime | None = None,
        enabled: bool = True,
    ) -> StaffCard:
        """新增一张卡。同一 UID 已存在 → `DuplicateError`（对应 `uk_cards_uid`）。"""
        uid = normalize_uid(card_uid)
        if not uid:
            raise StaffDomainError("卡号不能为空")
        if staff_id not in self._staff:
            raise ForeignKeyError(f"staff_id={staff_id} 不存在，不能绑卡")
        if uid in self._cards:
            raise DuplicateError(f"卡号 {uid} 已绑定到 staff_id={self._cards[uid].staff_id}")
        card = StaffCard(
            id=self._next("card"), card_uid=uid, staff_id=int(staff_id),
            enabled=bool(enabled), bound_at=now,
        )
        self._cards[uid] = card
        self._persist("staff_cards", "upsert_card_row", card)
        return card

    def set_card_enabled(self, card_uid: str, enabled: bool) -> StaffCard:
        uid = normalize_uid(card_uid)
        card = self._cards.get(uid)
        if card is None:
            raise NotFoundError(f"卡号 {uid} 不存在")
        updated = StaffCard(card.id, card.card_uid, card.staff_id, bool(enabled), card.bound_at)
        self._cards[uid] = updated
        self._persist("staff_cards", "upsert_card_row", updated)
        return updated

    def rebind_card(
        self, old_uid: str, new_uid: str, staff_id: int, *, now: datetime | None = None,
    ) -> tuple[StaffCard, StaffCard]:
        """换卡：新卡生效、旧卡置停用（**不删除**，留痕可查）。"""
        old = self.card(old_uid)
        if old is None:
            raise NotFoundError(f"旧卡 {normalize_uid(old_uid)} 不存在")
        if old.staff_id != int(staff_id):
            raise StaffDomainError(
                f"旧卡属于 staff_id={old.staff_id}，不能直接换给 {staff_id}"
            )
        new_card = self.bind_card(new_uid, staff_id, now=now)
        return self.set_card_enabled(old_uid, False), new_card

    def add_certificate(
        self,
        *,
        staff_id: int,
        cert_no: str = "",
        valid_from: date | None = None,
        valid_to: date | None,
        state: str = "valid",
        _loading: bool = False,
        _cert_id: int | None = None,
    ) -> HealthCertificate:
        if staff_id not in self._staff:
            raise ForeignKeyError(f"staff_id={staff_id} 不存在，不能登记健康证")
        if valid_to is None:
            raise StaffDomainError("健康证必须有到期日（无到期日等于永不失效）")
        cert = HealthCertificate(
            id=int(_cert_id) if _cert_id is not None else self._next("cert"),
            staff_id=int(staff_id), cert_no=str(cert_no or ""),
            valid_from=valid_from, valid_to=valid_to, state=str(state),
        )
        self._certificates.setdefault(int(staff_id), []).append(cert)
        if _loading and _cert_id is not None:
            self._seq["cert"] = max(self._seq["cert"], int(_cert_id))
        else:
            self._persist("health_certificates", "upsert_certificate_row", cert)
        return cert

    def add_shift(
        self,
        *,
        staff_id: int,
        station_id: int | None,
        shift_date: date,
        shift_no: int = 1,
        state: str = "scheduled",
        assignment: Mapping[str, Any] | None = None,
        _loading: bool = False,
        _shift_id: int | None = None,
    ) -> Shift:
        if staff_id not in self._staff:
            raise ForeignKeyError(f"staff_id={staff_id} 不存在，不能排班")
        for shift in self._shifts:
            if (shift.staff_id == int(staff_id) and shift.shift_date == shift_date
                    and shift.shift_no == int(shift_no)):
                raise DuplicateError(
                    f"staff_id={staff_id} 在 {shift_date} 的第 {shift_no} 班已存在"
                    "（对应 uk_shifts_slot）"
                )
        shift = Shift(
            id=int(_shift_id) if _shift_id is not None else self._next("shift"),
            staff_id=int(staff_id), station_id=None if station_id is None else int(station_id),
            shift_date=shift_date, shift_no=int(shift_no), state=str(state),
            assignment=dict(assignment or {}),
        )
        self._shifts.append(shift)
        if _loading and _shift_id is not None:
            self._seq["shift"] = max(self._seq["shift"], int(_shift_id))
        else:
            self._persist("shifts", "upsert_shift_row", shift)
        return shift

    def block_shift(self, shift_id: int, *, reason: str = "") -> Shift:
        """把一条排班置 `blocked`（#9 到期拦截的落点）。**幂等**。"""
        for index, shift in enumerate(self._shifts):
            if shift.id == int(shift_id):
                assignment = dict(shift.assignment)
                if reason:
                    assignment["blocked_reason"] = reason
                updated = Shift(
                    shift.id, shift.staff_id, shift.station_id, shift.shift_date,
                    shift.shift_no, "blocked", assignment,
                )
                self._shifts[index] = updated
                self._persist("shifts", "upsert_shift_row", updated)
                return updated
        raise NotFoundError(f"shift_id={shift_id} 不存在")

    def mark_certificate_state(self, staff_id: int, cert_id: int, state: str) -> HealthCertificate:
        """改写健康证物化状态（#9 每日扫描用）。判定不看它，但它要跟日期同步。"""
        items = self._certificates.get(int(staff_id), [])
        for index, cert in enumerate(items):
            if cert.id == int(cert_id):
                updated = HealthCertificate(
                    cert.id, cert.staff_id, cert.cert_no, cert.valid_from,
                    cert.valid_to, str(state),
                )
                items[index] = updated
                self._persist("health_certificates", "upsert_certificate_row", updated)
                return updated
        raise NotFoundError(f"staff_id={staff_id} 下没有 cert_id={cert_id}")

    def upsert_morning_check(self, check: MorningCheck) -> MorningCheck:
        """写入晨检记录。唯一键 `(staff_id, check_date)` → 同日覆盖。"""
        if check.staff_id not in self._staff:
            raise ForeignKeyError(f"staff_id={check.staff_id} 不存在，不能写晨检")
        self._checks[(int(check.staff_id), check.check_date)] = check
        self._persist("morning_checks", "upsert_morning_check_row", check)
        return check

    def insert_morning_check(self, check: MorningCheck) -> MorningCheck:
        """严格插入（模拟 `INSERT` 不带 `ON DUPLICATE KEY`）。同已存在 → 1062。"""
        key = (int(check.staff_id), check.check_date)
        if key in self._checks:
            raise DuplicateError(
                f"staff_id={check.staff_id} 在 {check.check_date} 已有晨检记录"
                "（对应 uk_mc_staff_date）"
            )
        return self.upsert_morning_check(check)

    # ------------------------------------------------------------ 派生查询
    def eligible(
        self,
        requirement: str | Iterable[str],
        day: date,
        *,
        on_shift_only: bool = True,
    ) -> tuple[Staff, ...]:
        """#10 候选集：能真正干这个工序的人。

        四个条件缺一不可：启用 → 资质覆盖 → 当日健康证有效 → 当日晨检已通过。
        「资质够但证过期」的人如果出现在候选里，派单模块会把他派上去，然后被
        `AdmissionController` 在刷卡时拒绝——工单已经派出去了，只能人工改派。
        候选集必须与准入判定用**同一套**条件，否则两处规则迟早分叉。
        """
        needed = (
            frozenset({requirement}) if isinstance(requirement, str)
            else frozenset(requirement)
        )
        out: list[Staff] = []
        for staff in sorted(self._staff.values(), key=lambda item: item.id):
            if not staff.enabled or not staff.covers(needed):
                continue
            if not self.has_valid_certificate(staff.id, day):
                continue
            check = self.morning_check(staff.id, day)
            if check is None or not check.admitted:
                continue
            if on_shift_only and not self.shifts(staff.id, day):
                continue
            out.append(staff)
        return tuple(out)

    def has_valid_certificate(self, staff_id: int, day: date) -> bool:
        """当日是否有任一有效健康证（**按日期判**，不看 `state`）。"""
        return any(cert.is_valid_on(day) for cert in self.certificates(staff_id))

    def certificate_of(self, staff_id: int, day: date) -> HealthCertificate | None:
        """当日生效的那张证。多张时取到期日最晚的（最新办的）。"""
        valid = [c for c in self.certificates(staff_id) if c.is_valid_on(day)]
        if not valid:
            return None
        return max(valid, key=lambda cert: cert.valid_to or date.min)

    def snapshot(self) -> dict:
        """现场排查用的整体视图（不用于判定）。"""
        return {
            "staff": len(self._staff),
            "cards": {"total": len(self._cards),
                      "active": sum(1 for c in self._cards.values() if c.enabled)},
            "certificates": sum(len(items) for items in self._certificates.values()),
            "shifts": {"total": len(self._shifts),
                       "blocked": sum(1 for s in self._shifts if s.blocked)},
            "morning_checks": len(self._checks),
            "stations": {sid: self.station_requirement(sid) for sid in self._stations},
        }

    # ------------------------------------------------------------ 内部
    def _persist(self, what: str, method: Any, entity: Any) -> None:
        """落库旁路：写器未装配或写失败都不影响内存主路径（口径同 A1：
        落库是锦上添花，失败只计数）。`method` 为方法名字符串或已绑定
        参数的 lambda，避免 `recorder=None` 时提前求值炸出 AttributeError。"""
        if self.recorder is None:
            return
        try:
            fn = getattr(self.recorder, method) if isinstance(method, str) else method
            fn(entity)
        except Exception:  # pragma: no cover - 写器自身已吞错，这里是双保险
            log.warning("人员域落库失败（%s）", what, exc_info=True)

    def _load_card(self, card: StaffCard) -> None:
        if card.staff_id not in self._staff:
            raise ForeignKeyError(f"卡 {card.card_uid} 指向不存在的 staff_id={card.staff_id}")
        if card.card_uid in self._cards:
            raise DuplicateError(f"种子里卡号 {card.card_uid} 重复（uk_cards_uid）")
        self._cards[card.card_uid] = card
        self._seq["card"] = max(self._seq["card"], card.id)

    def _next(self, name: str) -> int:
        self._seq[name] += 1
        return self._seq[name]


def _dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _answers(raw: Any) -> MorningAnswers:
    return MorningAnswers.from_dict(raw)
