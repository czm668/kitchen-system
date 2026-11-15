# -*- coding: utf-8 -*-
"""经营账本域：流水 / 食材成本 / 毛利 / 净利润 / 日报（A12 扩展模块）。

## 为什么不是 `SUM(order_items.unit_price * qty)`

那会做出一笔**假账**，答辩一问就穿。这个域的口径是：

1. **营收只在子项「已上桌」时确认**（`ItemState.SERVED`）。下单不等于收入：
   未出餐、制作中、已取消的单都不能进流水；退菜子项 `CANCELLED` 天然排除。
2. **项目没有支付链路**（设计文档 09 §一）。所以这里叫「经营口径的已实现营业额」，
   **不是实收、不是银行流水**，界面与导出都带 `settlement_note` 如实标注。
3. **金额只用下单快照** `OrderItem.unit_price`（A11 加的）。菜品后来改价，
   历史账面不变——这是会计的基本要求，也是 `unit_price` 的第二个用途。
4. **A11 补偿凭证要冲账**：免单 / 优惠券冲减营收，重做记额外食材损耗。
   否则客诉模块说「已批准免单」，账本还在算这笔收入，两套账打架。
5. **历史账面冻结**：成本率或标准成本后来改了，**只影响新确认的账目**，
   已落账的行不重算。journal 重放同样不重算。

## 金额精度

全部用**整数分**累加，绝不用 float 求和（0.1 + 0.2 != 0.3）。入口统一
`_cents()` 走 `Decimal` + `ROUND_HALF_UP`，对外展示时才 /100 转回元。

## 持久化

账目是凭证。运行态是内存 + `finance_journal.jsonl` 追加日志（与
`config_journal` / `guest_journal` 同构），启动时按幂等键重放。
**重放不重新扫订单**：订单本身是内存态，服务重启后订单对象就没了，
所以日志里存的是账目快照而不是「订单 id 让启动时去查」。

`sync()` 是懒同步：每次读接口前调一次，把新出现的已上桌子项与已批准
补偿凭证物化成账目。幂等键 `source_type:source_id:kind` 保证重复调用不重复记账。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping

from kitchen.domain import CST, DomainError, NotFoundError
from kitchen.process.journal import append_journal
from kitchen.process.models import ItemState

log = logging.getLogger("kitchen.finance")

__all__ = ["FinanceError", "DEFAULT_COST_RATE", "LedgerEntry", "FinanceService"]

#: 未维护标准成本时按售价的 35% 估算食材成本，**必须标 estimated**。
#: 这是演示口径（餐饮行业食材成本率常见 30~40%），不是核算结果。
DEFAULT_COST_RATE = Decimal("0.35")

#: 补偿方式 → 记账类别。`none` 不产生账目（A11 允许无补偿结案）。
COMPENSATION_KIND = {"waive": "waive", "coupon": "coupon", "redo": "redo"}

#: 期间费用四项，界面与 DDL 共用
OVERHEAD_ITEMS = ("labor", "rent", "utilities", "other")

_CENT = Decimal("0.01")
_PROMPT_VERSION = "finance-daily-v1"
_RULE_MODEL = "rule-engine-v1"


class FinanceError(DomainError):
    """经营账域的可预期拒绝（400）。"""


def _cents(value: Any) -> int:
    """任意数值 → 整数分。用 Decimal 量化，避开二进制浮点误差。"""
    try:
        dec = Decimal(str(value if value is not None else 0))
    except Exception:
        return 0
    if not dec.is_finite():
        return 0
    return int((dec.quantize(_CENT, rounding=ROUND_HALF_UP) * 100).to_integral_value())


def _yuan(cents: int) -> float:
    """整数分 → 展示用元（只在最后一层转换）。"""
    return round(cents / 100, 2)


def _day(value: datetime | str | None, fallback: date) -> str:
    """营业日：一律按 CST 取日期，避免跨午夜把一单算到两天。"""
    if isinstance(value, datetime):
        return value.astimezone(CST).date().isoformat()
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).astimezone(CST).date().isoformat()
        except ValueError:
            head = value.strip()[:10]
            if len(head) == 10 and head[4] == "-":
                return head
    return fallback.isoformat()


@dataclass
class LedgerEntry:
    """一条账目。`amount_cents` **带符号**：收入为正，成本/冲减为负。"""

    id: int
    entry_no: str
    biz_date: str
    occurred_at: str
    kind: str                 # revenue | food_cost | compensation | overhead
    category: str             # sales | waive | coupon | redo | labor | rent | ...
    amount_cents: int
    source_type: str          # order_item | voucher | manual
    source_id: str
    order_no: str = ""
    dish_id: int | None = None
    dish_name: str = ""
    qty: int = 0
    cost_source: str = "n/a"  # estimated | manual | n/a
    note: str = ""

    @property
    def dedup_key(self) -> str:
        return f"{self.source_type}:{self.source_id}:{self.kind}:{self.category}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "entry_no": self.entry_no,
            "biz_date": self.biz_date, "occurred_at": self.occurred_at,
            "kind": self.kind, "category": self.category,
            "amount": _yuan(self.amount_cents),
            "amount_cents": self.amount_cents,
            "source_type": self.source_type, "source_id": self.source_id,
            "order_no": self.order_no, "dish_id": self.dish_id,
            "dish_name": self.dish_name, "qty": self.qty,
            "cost_source": self.cost_source, "note": self.note,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "LedgerEntry":
        return cls(
            id=int(row["id"]), entry_no=str(row["entry_no"]),
            biz_date=str(row["biz_date"]), occurred_at=str(row["occurred_at"]),
            kind=str(row["kind"]), category=str(row["category"]),
            amount_cents=int(row["amount_cents"]),
            source_type=str(row["source_type"]), source_id=str(row["source_id"]),
            order_no=str(row.get("order_no") or ""),
            dish_id=row.get("dish_id"), dish_name=str(row.get("dish_name") or ""),
            qty=int(row.get("qty") or 0),
            cost_source=str(row.get("cost_source") or "n/a"),
            note=str(row.get("note") or ""),
        )


class FinanceService:
    """经营账本。唯一事实源是订单子项 + A11 补偿凭证，账目是物化视图。"""

    def __init__(self, state, *, provider=None, journal_path: Path | None = None):
        self.state = state
        self.provider = provider
        self._journal = journal_path
        self._entries: dict[str, LedgerEntry] = {}
        self._costs: dict[int, float] = {}          # dish_id → 手工标准成本（元/份）
        self._overheads: dict[str, dict[str, int]] = {}  # biz_date → 四项（分）
        self._ai_runs: list[dict[str, Any]] = []
        self._seq = 0
        self._applied = 0
        self._skipped = 0
        self._replay()

    # ------------------------------------------------------------ 持久化
    @property
    def journal_stats(self) -> dict[str, Any]:
        return {
            "path": str(self._journal) if self._journal else None,
            "applied": self._applied, "skipped": self._skipped,
            "entries": len(self._entries),
        }

    def _write(self, record: dict[str, Any]) -> None:
        append_journal(self._journal, record)

    def _replay(self) -> None:
        """逐行重放。**单行失败只跳过那一行**，不能让启动挂掉。"""
        if self._journal is None or not self._journal.exists():
            return
        try:
            lines = self._journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                op = row.get("op")
                if op == "entry":
                    entry = LedgerEntry.from_dict(row["entry"])
                    self._entries[entry.dedup_key] = entry   # 后写覆盖 = 幂等
                    self._seq = max(self._seq, entry.id)
                elif op == "cost":
                    self._costs[int(row["dish_id"])] = float(row["cost"])
                elif op == "overhead":
                    self._replay_overhead(row)
                else:
                    self._skipped += 1
                    continue
                self._applied += 1
            except (ValueError, KeyError, TypeError, OSError):
                self._skipped += 1

    def _replay_overhead(self, row: Mapping[str, Any]) -> None:
        """期间费用重放：先把该日旧账目撤掉，再按配置重新落账。

        这里**不碰 self.state**：重放发生在 AppState.__init__ 装配期间，
        state 的时钟/仓储可能还没就绪，任何依赖都可能让启动直接崩。
        """
        day = str(row["date"])
        values = {key: _cents(row.get(key) or 0) for key in OVERHEAD_ITEMS}
        self._overheads[day] = values
        for key in list(self._entries):
            entry = self._entries[key]
            if entry.kind == "overhead" and entry.biz_date == day:
                del self._entries[key]
        for category, cents in values.items():
            if not cents:
                continue
            self._seq += 1
            entry = LedgerEntry(
                id=self._seq, entry_no=f"L{day[5:7]}{day[8:10]}-{self._seq:04d}",
                biz_date=day, occurred_at=f"{day}T23:59:59",
                kind="overhead", category=category, amount_cents=-abs(cents),
                source_type="manual", source_id=f"{day}:{category}",
                note="期间费用（按日录入）")
            self._entries[entry.dedup_key] = entry

    def _next_no(self, moment: datetime) -> tuple[int, str]:
        self._seq += 1
        return self._seq, f"L{moment.strftime('%m%d')}-{self._seq:04d}"

    def _add(self, entry: LedgerEntry) -> bool:
        if entry.dedup_key in self._entries:
            return False
        self._entries[entry.dedup_key] = entry
        self._write({"op": "entry", "entry": {
            **entry.to_dict(), "amount_cents": entry.amount_cents,
        }})
        return True

    # ------------------------------------------------------------ 懒同步
    def sync(self) -> int:
        """把新出现的经营事实物化成账目。返回新增条数，可重复调用。"""
        added = 0
        now = self.state.to_datetime()
        today = now.astimezone(CST).date()

        for order in self.state.orders.orders():
            # 上桌时刻的近似：Order 只存 updated_at，没有逐子项时间戳。
            # 订单是唯一事实源，不为此另造一套事件表。
            moment = order.updated_at or order.created_at or now
            biz_date = _day(moment, today)
            occurred = moment.astimezone(CST).isoformat(timespec="seconds")
            for item in order.items:
                if item.state is not ItemState.SERVED:
                    continue          # 未出餐 / 制作中 / 已取消一律不确认收入
                if self._has("order_item", str(item.id), "revenue"):
                    continue
                revenue = _cents(item.amount)   # 下单快照价 × 份数，不按现价
                unit, source = self._unit_cost(item)
                cost = _cents(Decimal(str(unit)) * max(0, int(item.qty)))
                eid, eno = self._next_no(now)
                added += int(self._add(LedgerEntry(
                    id=eid, entry_no=eno, biz_date=biz_date,
                    occurred_at=occurred, kind="revenue", category="sales",
                    amount_cents=revenue, source_type="order_item",
                    source_id=str(item.id), order_no=order.order_no,
                    dish_id=item.dish_id, dish_name=item.dish_name,
                    qty=int(item.qty), note="已上桌确认收入（下单快照价）",
                )))
                eid, eno = self._next_no(now)
                added += int(self._add(LedgerEntry(
                    id=eid, entry_no=eno, biz_date=biz_date,
                    occurred_at=occurred, kind="food_cost", category="food",
                    amount_cents=-cost, source_type="order_item",
                    source_id=str(item.id), order_no=order.order_no,
                    dish_id=item.dish_id, dish_name=item.dish_name,
                    qty=int(item.qty), cost_source=source,
                    note="标准成本" if source == "manual" else "按售价 35% 估算",
                )))

        # A11 补偿凭证：批准即冲账。不审批的工单一分钱都不动。
        for complaint in self.state.guest.all_complaints():
            if complaint.state != "approved" or not complaint.voucher_no:
                continue
            category = COMPENSATION_KIND.get(complaint.method)
            if not category:
                continue
            # 幂等判断**只认凭证号前缀**：redo 记在 food_cost、免单记在
            # compensation，按 kind 查会让重做凭证每次 sync 重复记一笔。
            voucher_prefix = f"voucher:{complaint.voucher_no}:"
            if any(key.startswith(voucher_prefix) for key in self._entries):
                continue
            moment_raw = complaint.decided_at or complaint.updated_at.isoformat()
            biz_date = _day(moment_raw, today)
            occurred = moment_raw if "T" in moment_raw else f"{biz_date}T12:00:00"
            if category == "redo":
                # 重做是**额外食材损耗**，不是收入冲减。优先取该单已确认的
                # 食材成本；查不到（订单已清 / 未上桌）才按金额 × 成本率估算。
                # 注意 complaint.order_id 是整数 id，order_no 是 "O1115-004"，
                # 不能直接拿数字去 in 字符串里找。
                target_no = ""
                if complaint.order_id:
                    try:
                        target_no = self.state.orders.order(
                            int(complaint.order_id)).order_no
                    except (NotFoundError, KeyError, ValueError):
                        target_no = ""
                cost = sum(
                    abs(e.amount_cents) for e in self._entries.values()
                    if e.kind == "food_cost" and e.category == "food"
                    and target_no and e.order_no == target_no
                )
                if not cost:
                    cost = _cents(Decimal(str(complaint.amount)) * DEFAULT_COST_RATE)
                amount, kind, note = -cost, "food_cost", "重做损耗（追加食材成本）"
            else:
                amount = -_cents(complaint.amount)
                kind = "compensation"
                note = ("免单冲减营收" if category == "waive"
                        else "优惠券冲减营收")
            eid, eno = self._next_no(now)
            added += int(self._add(LedgerEntry(
                id=eid, entry_no=eno, biz_date=biz_date,
                occurred_at=occurred, kind=kind, category=category,
                amount_cents=amount, source_type="voucher",
                source_id=complaint.voucher_no,
                order_no=str(complaint.order_id or ""),
                note=note + "（演示环境不发生实际资金流）",
            )))
        return added

    def _has(self, source_type: str, source_id: str, kind: str) -> bool:
        prefix = f"{source_type}:{source_id}:{kind}:"
        return any(key.startswith(prefix) for key in self._entries)

    def _unit_cost(self, item) -> tuple[float, str]:
        manual = self._costs.get(item.dish_id)
        if manual is not None:
            return float(manual), "manual"
        return float(Decimal(str(item.unit_price)) * DEFAULT_COST_RATE), "estimated"

    # ------------------------------------------------------------ 配置写接口
    def set_cost(self, dish_id: int, cost: Any, *, actor: str = "unknown") -> dict[str, Any]:
        try:
            dish = self.state.repo.dish(int(dish_id))
        except (NotFoundError, KeyError):
            raise NotFoundError(f"菜品 {dish_id} 不存在") from None
        if dish is None:
            raise NotFoundError(f"菜品 {dish_id} 不存在")
        value = Decimal(str(cost))
        if value < 0 or value > 100000:
            raise FinanceError("标准成本须在 0 ~ 100000 元之间")
        clean = float(value.quantize(_CENT, rounding=ROUND_HALF_UP))
        self._costs[int(dish_id)] = clean
        self._write({"op": "cost", "dish_id": int(dish_id), "cost": clean})
        self.state.audit.record(
            actor=actor, role="manager", action="finance.cost",
            target=str(dish_id), after={"cost": clean})
        return {"dish_id": int(dish_id), "name": dish.name,
                "price": dish.price, "cost": clean, "cost_source": "manual",
                "note": "只影响之后确认的账目，历史账面不重算"}

    def set_overhead(self, day: str, *, labor: Any = 0, rent: Any = 0,
                     utilities: Any = 0, other: Any = 0,
                     actor: str = "unknown") -> dict[str, Any]:
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            raise FinanceError("日期须为 YYYY-MM-DD") from None
        raw = {"labor": labor, "rent": rent, "utilities": utilities, "other": other}
        values = {key: max(0, _cents(value)) for key, value in raw.items()}
        self._overheads[day] = values
        for key in list(self._entries):
            entry = self._entries[key]
            if entry.kind == "overhead" and entry.biz_date == day:
                del self._entries[key]
        for category, cents in values.items():
            if cents:
                self._put_overhead_entry(day, category, cents, persist=True)
        self._write({"op": "overhead", "date": day,
                     **{key: _yuan(cents) for key, cents in values.items()}})
        self.state.audit.record(
            actor=actor, role="manager", action="finance.overhead",
            target=day, after={key: _yuan(v) for key, v in values.items()})
        return {"date": day, **{key: _yuan(v) for key, v in values.items()},
                "total": _yuan(sum(values.values())),
                "note": "未录入期间费用时，净利润等于毛利（不是完整净利润）"}

    def _put_overhead_entry(self, day: str, category: str, cents: int,
                            *, persist: bool) -> None:
        now = self.state.to_datetime()
        eid, eno = self._next_no(now)
        entry = LedgerEntry(
            id=eid, entry_no=eno, biz_date=day,
            occurred_at=f"{day}T23:59:59", kind="overhead", category=category,
            amount_cents=-abs(cents), source_type="manual",
            source_id=f"{day}:{category}", note="期间费用（按日录入）",
        )
        self._entries[entry.dedup_key] = entry
        if persist:
            self._write({"op": "entry", "entry": {
                **entry.to_dict(), "amount_cents": entry.amount_cents}})

    # ------------------------------------------------------------ 查询
    def _range(self, days: int, end: str | None, start: str | None) -> tuple[str, str]:
        today = self.state.to_datetime().astimezone(CST).date()
        try:
            last = date.fromisoformat(end) if end else today
        except (TypeError, ValueError):
            raise FinanceError("end 日期格式应为 YYYY-MM-DD") from None
        if start:
            try:
                first = date.fromisoformat(start)
            except (TypeError, ValueError):
                raise FinanceError("start 日期格式应为 YYYY-MM-DD") from None
        else:
            span = max(1, min(366, int(days or 7)))
            first = last - timedelta(days=span - 1)
        if first > last:
            raise FinanceError("起始日期不能晚于结束日期")
        return first.isoformat(), last.isoformat()

    def _rows(self, first: str, last: str) -> list[LedgerEntry]:
        return sorted(
            (e for e in self._entries.values() if first <= e.biz_date <= last),
            key=lambda e: (e.occurred_at, e.id))

    @staticmethod
    def _sum(rows: Iterable[LedgerEntry], kinds: tuple[str, ...]) -> int:
        return sum(e.amount_cents for e in rows if e.kind in kinds)

    def summary(self, first: str, last: str) -> dict[str, Any]:
        """核心公式（全部整数分，最后一次才转元）：

        revenue   = Σ revenue + Σ compensation(免单/优惠券，负向)
        food_cost = Σ food_cost(负向) + Σ redo(负向)
        gross     = revenue + food_cost
        overhead  = Σ overhead(负向)
        net       = gross + overhead
        """
        rows = self._rows(first, last)
        revenue = self._sum(rows, ("revenue", "compensation"))
        food = self._sum(rows, ("food_cost",))
        overhead = self._sum(rows, ("overhead",))
        gross = revenue + food
        net = gross + overhead
        orders = {e.order_no for e in rows
                  if e.kind == "revenue" and e.order_no}
        return {
            "from": first, "to": last,
            "revenue": _yuan(revenue), "food_cost": _yuan(-food),
            "compensation": _yuan(self._sum(rows, ("compensation",))),
            "redo_loss": _yuan(sum(e.amount_cents for e in rows
                                   if e.category == "redo")),
            "gross": _yuan(gross), "overhead": _yuan(-overhead),
            "net": _yuan(net),
            "gross_margin": round(gross / revenue, 4) if revenue > 0 else 0.0,
            "net_margin": round(net / revenue, 4) if revenue > 0 else 0.0,
            "order_count": len(orders),
            "avg_ticket": _yuan(round(revenue / len(orders))) if orders else 0.0,
            "entry_count": len(rows),
            "settlement_note": (
                "演示环境无支付链路：营业额按「已上桌子项 × 下单快照价」确认，"
                "不是实收金额，也不产生银行流水"),
            "cost_note": (
                "标准成本未维护的菜品按售价 35% 估算（标 estimated）；"
                "期间费用未录入时净利润等于毛利"),
        }

    def overview(self, *, days: int = 7, end: str | None = None,
                 start: str | None = None) -> dict[str, Any]:
        self.sync()
        first, last = self._range(days, end, start)
        rows = self._rows(first, last)
        result = self.summary(first, last)

        # 按日趋势：一天一行，没有账目也要补 0（前端折线不能断）
        by_day: dict[str, dict[str, int]] = {}
        for row in rows:
            slot = by_day.setdefault(row.biz_date, {"revenue": 0, "food": 0,
                                                    "overhead": 0, "net": 0})
            if row.kind in ("revenue", "compensation"):
                slot["revenue"] += row.amount_cents
            elif row.kind == "food_cost":
                slot["food"] += row.amount_cents
            elif row.kind == "overhead":
                slot["overhead"] += row.amount_cents
        cursor = date.fromisoformat(first)
        stop = date.fromisoformat(last)
        trend = []
        while cursor <= stop:
            slot = by_day.get(cursor.isoformat(), {})
            rev = slot.get("revenue", 0)
            food = slot.get("food", 0)
            over = slot.get("overhead", 0)
            trend.append({"date": cursor.isoformat(),
                          "revenue": _yuan(rev), "gross": _yuan(rev + food),
                          "net": _yuan(rev + food + over)})
            cursor += timedelta(days=1)

        # 菜品毛利排行：收入与食材成本按 dish_id 归并
        dishes: dict[int, dict[str, Any]] = {}
        for row in rows:
            if row.dish_id is None or row.kind not in ("revenue", "food_cost"):
                continue
            slot = dishes.setdefault(row.dish_id, {
                "dish_id": row.dish_id, "name": row.dish_name or f"#{row.dish_id}",
                "qty": 0, "revenue": 0, "cost": 0, "estimated": False})
            if row.kind == "revenue":
                slot["revenue"] += row.amount_cents
                slot["qty"] += row.qty
            else:
                slot["cost"] += -row.amount_cents
                slot["estimated"] = slot["estimated"] or row.cost_source == "estimated"
        ranking = []
        for slot in dishes.values():
            gross = slot["revenue"] - slot["cost"]
            ranking.append({
                "dish_id": slot["dish_id"], "name": slot["name"],
                "qty": slot["qty"], "revenue": _yuan(slot["revenue"]),
                "cost": _yuan(slot["cost"]), "gross": _yuan(gross),
                "margin": round(gross / slot["revenue"], 4) if slot["revenue"] else 0.0,
                "cost_source": "estimated" if slot["estimated"] else "manual",
            })
        ranking.sort(key=lambda x: x["gross"], reverse=True)

        # 成本覆盖率按**账目行**统计，不按菜品聚合：同一道菜可能先估算、
        # 后维护了标准成本，聚合成一个「估算」会让用户以为维护没生效。
        food_rows = [e for e in rows if e.kind == "food_cost"
                     and e.category == "food"]
        result.update({
            "trend": trend, "dishes": ranking[:10],
            "cost_coverage": {
                "manual": sum(1 for e in food_rows
                              if e.cost_source == "manual"),
                "estimated": sum(1 for e in food_rows
                                 if e.cost_source == "estimated"),
            },
            "journal": self.journal_stats,
        })
        return result

    def entries_view(self, *, days: int = 7, end: str | None = None,
                     start: str | None = None, kind: str = "",
                     limit: int = 300) -> dict[str, Any]:
        self.sync()
        first, last = self._range(days, end, start)
        rows = self._rows(first, last)
        if kind:
            rows = [e for e in rows if e.kind == kind]
        cap = max(1, min(2000, int(limit)))
        return {"total": len(rows), "from": first, "to": last,
                "entries": [e.to_dict() for e in rows[:cap]]}

    def dish_costs_view(self) -> dict[str, Any]:
        rows = []
        for dish in self.state.repo.dishes():
            manual = self._costs.get(dish.id)
            cost = manual if manual is not None else round(
                float(Decimal(str(dish.price)) * DEFAULT_COST_RATE), 2)
            rows.append({
                "dish_id": dish.id, "code": dish.code, "name": dish.name,
                "category": dish.category, "price": dish.price, "cost": cost,
                "cost_source": "manual" if manual is not None else "estimated",
                "margin": round((dish.price - cost) / dish.price, 4)
                          if dish.price > 0 else 0.0,
            })
        rows.sort(key=lambda x: x["margin"])
        return {"total": len(rows), "dishes": rows,
                "default_rate": float(DEFAULT_COST_RATE),
                "note": "估算值仅用于演示，可在页面上逐菜录入标准成本"}

    def overhead_view(self, day: str | None = None) -> dict[str, Any]:
        target = day or self.state.to_datetime().astimezone(CST).date().isoformat()
        values = self._overheads.get(target, {})
        return {"date": target,
                **{key: _yuan(values.get(key, 0)) for key in OVERHEAD_ITEMS},
                "total": _yuan(sum(values.values())),
                "note": "未录入时净利润等于毛利，界面已标注"}

    # ------------------------------------------------------------ 日报 + AI 点评
    def report(self, day: str | None = None) -> dict[str, Any]:
        """生成日报。复用 `004_llm_reports.sql::reports` 的列形状，不另造表。

        AI 只写**点评文字**：金额全部由 `summary()` 确定性算出，模型输出不参与
        任何计算、不写账目、不改状态。喂给模型的只有聚合数字，没有顾客 key、
        没有客诉原文、没有卡号（隐私护栏）。
        """
        self.sync()
        target = day or self.state.to_datetime().astimezone(CST).date().isoformat()
        try:
            date.fromisoformat(target)
        except (TypeError, ValueError):
            raise FinanceError("日期须为 YYYY-MM-DD") from None
        summary = self.summary(target, target)
        started = time.monotonic()
        mode = "rule"
        model = _RULE_MODEL
        degraded = ""
        comment = ""
        if self.provider is not None and self.provider.mode == "live":
            try:
                raw = self.provider.complete("finance_daily", {
                    "date": target,
                    "revenue": summary["revenue"], "food_cost": summary["food_cost"],
                    "gross": summary["gross"], "net": summary["net"],
                    "gross_margin": summary["gross_margin"],
                    "order_count": summary["order_count"],
                    "compensation": summary["compensation"],
                    "overhead": summary["overhead"],
                })
                text = str((raw or {}).get("summary") or "").strip()
                # 护栏：非空、限长、不许模型夹带「批准/退款/修改金额」这类动作词。
                banned = ("批准", "退款", "已退", "修改金额", "调整账目", "执行关阀")
                if len(text) < 8 or any(word in text for word in banned):
                    raise ValueError("模型输出不符合点评约束")
                comment, mode, model = text[:400], "live", self.provider.model
            except Exception as exc:               # 在线失败一律降级，不拖垮接口
                degraded = f"{type(exc).__name__}: {str(exc)[:60]}"
                comment = self._rule_comment(summary)
        else:
            comment = self._rule_comment(summary)
        latency_ms = int((time.monotonic() - started) * 1000)

        lines = [
            f"# 经营日报 {target}", "",
            f"- 营业额：{summary['revenue']:.2f} 元（已上桌 {summary['order_count']} 单）",
            f"- 食材成本：{summary['food_cost']:.2f} 元",
            f"- 毛利：{summary['gross']:.2f} 元，毛利率 {summary['gross_margin'] * 100:.1f}%",
            f"- 期间费用：{summary['overhead']:.2f} 元",
            f"- 净利润：{summary['net']:.2f} 元，净利率 {summary['net_margin'] * 100:.1f}%",
            f"- 补偿冲减：{summary['compensation']:.2f} 元 / 重做损耗 {summary['redo_loss']:.2f} 元",
            "", "## 经营点评", "", comment, "",
            f"> {summary['settlement_note']}",
            f"> {summary['cost_note']}",
        ]
        risk = ("high" if summary["net"] < 0 else
                "medium" if 0 < summary["gross_margin"] < 0.4 else "low")
        row = {
            "scenario": "finance_daily", "rdate": target,
            "title": f"经营日报 {target}",
            "summary": comment[:512], "content_md": "\n".join(lines),
            "risk_level": risk,
            "source": "llm" if mode == "live" else "rule",
            "model": model, "prompt_version": _PROMPT_VERSION,
            "latency_ms": latency_ms, "degraded_reason": degraded[:32],
            "created_at": self.state.to_datetime().isoformat(timespec="seconds"),
            "metrics": summary,
        }
        self._ai_runs.append({
            "scenario": "finance_daily", "kind": "chat", "mode": mode,
            "source": "llm" if mode == "live" else "rule", "model": model,
            "prompt_version": _PROMPT_VERSION, "status": "ok" if not degraded else "degraded",
            "attempts": 1, "latency_ms": latency_ms,
            "degraded_reason": degraded[:32], "error_msg": "",
        })
        if len(self._ai_runs) > 200:
            del self._ai_runs[:-200]
        return row

    @staticmethod
    def _rule_comment(s: Mapping[str, Any]) -> str:
        """离线确定性点评。可复现、不编造，mode 如实标 rule 不冒充 AI。

        后缀必须走**统一出口**：早退分支（当日无单）也一样要标规则层，
        否则空数据那天看起来像模型生成的结论。
        """
        if not s["order_count"]:
            body = "当日无已完成订单，暂无经营数据，不做趋势推断。"
        else:
            parts = [f"营业额 {s['revenue']:.2f} 元，"
                     f"毛利率 {s['gross_margin'] * 100:.1f}%，"
                     f"净利润 {s['net']:.2f} 元。"]
            if s["net"] < 0:
                parts.append("净利为负，建议核查期间费用与补偿冲减是否集中发生。")
            elif 0 < s["gross_margin"] < 0.4:
                parts.append("毛利率偏低，建议核对高销量菜品的标准成本是否准确。")
            else:
                parts.append("经营指标平稳。")
            if s["compensation"] < 0 or s["redo_loss"] < 0:
                parts.append("存在客诉补偿冲减，建议同步查看客诉工单的核实记录。")
            if not s["overhead"]:
                parts.append("未录入期间费用，当前净利润等于毛利，不是完整净利润。")
            body = "".join(parts)
        return body + "（离线规则点评，非大模型生成）"

    def ai_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._ai_runs[-max(1, min(200, int(limit))):])

    # ------------------------------------------------------------ 导出
    def export_csv(self, *, days: int = 7, end: str | None = None,
                   start: str | None = None) -> dict[str, Any]:
        """CSV 用 UTF-8 BOM 前缀，Excel 直接双击不乱码。标准库 csv，零依赖。"""
        self.sync()
        first, last = self._range(days, end, start)
        rows = self._rows(first, last)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(["营业日", "发生时间", "流水号", "类型", "分类", "单号",
                         "菜品", "数量", "金额(元)", "成本口径", "来源", "备注"])
        kind_label = {"revenue": "营业收入", "food_cost": "食材成本",
                      "compensation": "补偿冲减", "overhead": "期间费用"}
        cat_label = {"sales": "销售", "food": "食材", "waive": "免单",
                     "coupon": "优惠券", "redo": "重做损耗", "labor": "人工",
                     "rent": "房租", "utilities": "水电", "other": "其他"}
        for e in rows:
            writer.writerow([
                e.biz_date, e.occurred_at, e.entry_no,
                kind_label.get(e.kind, e.kind), cat_label.get(e.category, e.category),
                e.order_no, e.dish_name, e.qty, f"{_yuan(e.amount_cents):.2f}",
                e.cost_source, e.source_type, e.note,
            ])
        return {"filename": f"kitchen-ledger-{first}-{last}.csv",
                "csv": "\ufeff" + buf.getvalue(), "rows": len(rows),
                "settlement_note": self.summary(first, last)["settlement_note"]}
