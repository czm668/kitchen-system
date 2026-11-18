# -*- coding: utf-8 -*-
"""业务数据落库（A1 第二轮 / A23）：配置基线 + 订单链 + 人员域 + 审计。

## 背景

A1 第一轮只落了追加型运行数据（`sensor_readings` / `alarm_events`），50 张表里
其余业务态全在内存——重启即丢，MySQL 成了「两张表的空壳」。本轮把 A1 记录里
挂账的三项收掉，并把最高价值的运行期数据一并落库：

| 写路径 | 落库表 | 时机 |
|---|---|---|
| 配置基线（菜品/工位/配方/SOP/供应商/批次/溯源码） | 014 的 7 张配置表 | 启动时一次性物化 |
| 配置页新增（add_*） | 同上 | 每次成功写入后 |
| 沽清 | `dishes.sold_out` UPDATE | set_sold_out |
| 下单/子项迁移/催菜/退菜 | `orders` / `order_items` | 每条 order_events 落地时快照 |
| 订单事件流 | `order_events` | _record() |
| 人员/卡/健康证/排班/晨检 | 014 人员 5 表 | 仓储写方法成功后（种子装载除外） |
| 上灶占用/释放/点火校验 | `station_occupancy` | occupy/close/verify |
| 称重/SOP/留样/消毒过程事件 | `process_events` | 各服务 _emit() |
| 留样达标/销毁 | `retention_samples` | 称重达标 / destroy |
| 消毒会话 | `disinfection_records` | start + 各 _emit 时点快照 |
| 审计日志 | `audit_logs` | JSONL 之外并行双写 |

## 三条硬口径（与 A1 第一轮 / AuditLog 一致）

1. **写失败只计数不抛**。落库是旁路，不能把下单、刷卡、晨检这些主路径拖死；
   `write_failures` 计数经 stats() 上报，不静默。
2. **父表一律 UPSERT 不用 REPLACE**。REPLACE = DELETE + INSERT，子表外键
   （fk_items_order、fk_cards_staff）RESTRICT 会让「子表有数据后父表写不进」。
3. **种子装载期不落库**。内存仓储 from_seed 的行由 `sync_baseline()` 一次性
   物化（INSERT IGNORE，幂等），逐行双写既慢又把「装载失败」和「落库失败」
   混在一起。

## 范围边界（诚实口径）

`work_orders`（多来源创建：规则引擎/视觉处置/传感器故障/留样销毁）与
`detector_events` 的复核状态仍走内存/既有通道，本轮不收——创建点分散在
四个子系统，收口需要单独一轮。答辩口径：「订单链、人员准入、过程留痕、
审计与遥测告警已落库；工单中心仍为进程内状态」。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

log = logging.getLogger("kitchen.storage.business")

if TYPE_CHECKING:  # 仅为类型标注；storage 不反向依赖业务模块
    from kitchen.api.audit import AuditEntry
    from kitchen.process.dispatch import StationOccupancy
    from kitchen.process.disinfection import DisinfectionRecord
    from kitchen.process.models import Order, OrderEvent, ProcessEvent
    from kitchen.process.retention import RetentionSample
    from kitchen.staff.models import HealthCertificate, MorningCheck, Shift, Staff, StaffCard

__all__ = ["BusinessRecorderMixin"]

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt_ts(value: Any) -> str | None:
    """datetime → 库内 DATETIME 同形字符串。种子行的原始值可能是字符串、
    datetime 或 date——宽容处理，别让一行格式差异炸掉整轮基线物化。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.replace("T", " ")[:19]
    if isinstance(value, datetime):
        value = value.replace(tzinfo=None) if value.tzinfo is not None else value
        return value.strftime(_TS_FMT)
    return value.strftime(_TS_FMT)


def _fmt_date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


class BusinessRecorderMixin:
    """业务写路径的落库方法集。宿主是 `SqliteRecorder`（单连接 + 单锁 + 失败计数）。

    所有方法**尽力而为**：库不可用/约束冲突时记 `write_failures` 并返回 False，
    绝不让业务写路径因此失败。SQL 一律用 `?` 占位符，方言差异由 `RuntimeDB` 收敛。
    """

    # ------------------------------------------------------------ 基线物化
    def sync_baseline(self, repo: Any, staff_repo: Any) -> dict[str, int]:
        """把内存仓储的配置期资料一次性物化进库（INSERT IGNORE，幂等）。

        只在启动时调一次：此时运行期表尚无子表外键引用，IGNORE 语义保证
        「重复启动 = 空操作」。返回各表实际写入行数（供测试与体检）。
        """
        written: dict[str, int] = {}

        def _one(table: str, columns: list[str], row: tuple) -> None:
            self.conn.insert_ignore(table, columns, row)
            written[table] = written.get(table, 0) + 1

        with self._lock, self.conn.transaction():
            for dish in repo.dishes():
                _one("dishes",
                     ["id", "code", "name", "category", "price", "prep_minutes",
                      "sold_out", "enabled"],
                     (dish.id, dish.code, dish.name, dish.category, dish.price,
                      int(dish.prep_minutes), int(dish.sold_out), int(dish.enabled)))
            for station in repo.stations():
                _one("work_stations",
                     ["id", "code", "name", "kind", "device_id", "state", "capacity"],
                     (station.id, station.code, station.name, station.kind,
                      station.device_id or None, "idle", station.capacity))
            for supplier in repo.suppliers():
                _one("suppliers",
                     ["id", "name", "license_no", "contact", "qualifications", "state"],
                     (supplier.get("id"), supplier.get("name"), supplier.get("license_no"),
                      supplier.get("contact"),
                      _json(supplier.get("qualifications")),
                      str(supplier.get("state") or "active")))
            for recipe in repo.all_recipes():
                _one("dish_recipes", ["id", "dish_id", "version", "name", "enabled"],
                     (recipe.id, recipe.dish_id, recipe.version, recipe.name,
                      int(recipe.enabled)))
                for item in repo.recipe_items(recipe.id):
                    _one("dish_recipe_items",
                         ["id", "recipe_id", "ingredient_name", "weight_g",
                          "tolerance_pct", "sort_no"],
                         (item.id, item.recipe_id, item.ingredient, item.weight_g,
                          item.tolerance_pct, item.sort_no))
                for step in repo.sop_steps(recipe.id):
                    _one("sop_steps",
                         ["id", "recipe_id", "step_no", "title", "instruction",
                          "heat_level", "duration_sec", "ccp", "requires_card"],
                         (step.id, step.recipe_id, step.step_no, step.title,
                          step.instruction or None, step.heat_level or None,
                          step.duration_sec, int(step.ccp), int(step.requires_card)))
            for batch_id in repo.batch_ids():
                batch = repo.batch(batch_id)
                _one("dish_batches",
                     ["id", "batch_no", "dish_id", "produced_at", "expire_at", "state"],
                     (batch_id, batch.get("batch_no"), batch.get("dish_id"),
                      _fmt_ts(batch.get("produced_at")), _fmt_ts(batch.get("expire_at")),
                      str(batch.get("state") or "produced")))
            for batch_id in repo.ingredient_batch_ids():
                ing = repo.ingredient_batch(batch_id)
                _one("ingredient_batches",
                     ["id", "batch_no", "ingredient_name", "supplier_id",
                      "received_at", "expire_at", "inspection", "state"],
                     (batch_id, ing.get("batch_no"), ing.get("ingredient_name"),
                      ing.get("supplier_id"), _fmt_ts(ing.get("received_at")),
                      _fmt_ts(ing.get("expire_at")), _json(ing.get("inspection")),
                      str(ing.get("state") or "received")))
            for code, row in repo.trace_rows():
                _one("trace_codes",
                     ["id", "code", "batch_id", "supplier_id", "payload", "created_at"],
                     (row.get("id"), code, row.get("batch_id"), row.get("supplier_id"),
                      _json(row.get("payload")), _fmt_ts(row.get("created_at"))))
            for staff in staff_repo.all_staff():
                _one("staff",
                     ["id", "code", "name", "phone", "qualifications", "enabled"],
                     (staff.id, staff.code, staff.name, staff.phone or None,
                      _json(list(staff.qualifications)), int(staff.enabled)))
                for card in staff_repo.cards_of(staff.id):
                    _one("staff_cards",
                         ["id", "card_uid", "staff_id", "enabled", "bound_at"],
                         (card.id, card.card_uid, card.staff_id, int(card.enabled),
                          _fmt_ts(card.bound_at)))
                for cert in staff_repo.certificates(staff.id):
                    _one("health_certificates",
                         ["id", "staff_id", "cert_no", "valid_from", "valid_to", "state"],
                         (cert.id, cert.staff_id, cert.cert_no or None,
                          _fmt_date(cert.valid_from), _fmt_date(cert.valid_to), cert.state))
            # 班次独立成组：shifts 外键同时指向 staff 与 work_stations，
            # 必须等两张父表全部就位再插，否则「员工 3 的班」会在员工 3
            # 入库前被尝试（第一轮实现实测踩中，报 FOREIGN KEY constraint failed）
            for shift in staff_repo.all_shifts():
                _one("shifts",
                     ["id", "staff_id", "station_id", "shift_date", "shift_no",
                      "state", "assignment"],
                     (shift.id, shift.staff_id, shift.station_id,
                      _fmt_date(shift.shift_date), shift.shift_no, shift.state,
                      _json(shift.assignment)))
        return written

    # ------------------------------------------------------------ 人员域
    def upsert_staff_row(self, staff: "Staff") -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "staff",
                ["id", "code", "name", "phone", "qualifications", "enabled"],
                (staff.id, staff.code, staff.name, staff.phone or None,
                 _json(list(staff.qualifications)), int(staff.enabled)),
                conflict=("id",)),
            "staff")

    def upsert_card_row(self, card: "StaffCard") -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "staff_cards",
                ["id", "card_uid", "staff_id", "enabled", "bound_at"],
                (card.id, card.card_uid, card.staff_id, int(card.enabled),
                 _fmt_ts(card.bound_at)),
                conflict=("id",)),
            "staff_cards")

    def upsert_certificate_row(self, cert: "HealthCertificate") -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "health_certificates",
                ["id", "staff_id", "cert_no", "valid_from", "valid_to", "state"],
                (cert.id, cert.staff_id, cert.cert_no or None,
                 _fmt_date(cert.valid_from), _fmt_date(cert.valid_to), cert.state),
                conflict=("id",)),
            "health_certificates")

    def upsert_shift_row(self, shift: "Shift") -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "shifts",
                ["id", "staff_id", "station_id", "shift_date", "shift_no",
                 "state", "assignment"],
                (shift.id, shift.staff_id, shift.station_id,
                 _fmt_date(shift.shift_date), shift.shift_no, shift.state,
                 _json(shift.assignment)),
                conflict=("id",)),
            "shifts")

    def upsert_morning_check_row(self, check: "MorningCheck") -> bool:
        """唯一键是 `(staff_id, check_date)`（uk_mc_staff_date），按它冲突更新。

        `confirmed_by` 列是 INT（指 users 表），而内存模型记的是复核人姓名
        字符串——姓名进 questionnaire JSON，列保持 NULL，映射无损。
        """
        questionnaire = {
            "answers": check.answers.to_dict() if hasattr(check.answers, "to_dict")
            else str(check.answers),
            "failures": list(check.failures),
            "confirmed_by": check.confirmed_by,
        }
        return self._biz(
            lambda: self.conn.upsert(
                "morning_checks",
                ["staff_id", "check_date", "questionnaire", "result", "confirmed_by",
                 "channel", "ts"],
                (check.staff_id, _fmt_date(check.check_date), _json(questionnaire),
                 check.result, None, _clip(check.channel, 16), _fmt_ts(check.ts)),
                conflict=("staff_id", "check_date")),
            "morning_checks")

    # ------------------------------------------------------------ 配置页写路径
    def insert_dish_row(self, dish: Any) -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO dishes (id, code, name, category, price, prep_minutes, "
                "sold_out, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (dish.id, dish.code, dish.name, dish.category, dish.price,
                 int(getattr(dish, "prep_minutes", 0)), int(dish.sold_out),
                 int(dish.enabled))),
            "dishes")

    def update_dish_sold_out(self, dish_id: int, sold_out: bool) -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "UPDATE dishes SET sold_out = ? WHERE id = ?",
                (int(bool(sold_out)), int(dish_id))),
            "dishes")

    def insert_station_row(self, station: Any) -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO work_stations (id, code, name, kind, device_id, state, "
                "capacity) VALUES (?, ?, ?, ?, ?, 'idle', ?)",
                (station.id, station.code, station.name, station.kind,
                 station.device_id or None, station.capacity)),
            "work_stations")

    def insert_recipe_row(self, recipe: Any) -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO dish_recipes (id, dish_id, version, name, enabled) "
                "VALUES (?, ?, ?, ?, ?)",
                (recipe.id, recipe.dish_id, recipe.version, recipe.name,
                 int(recipe.enabled))),
            "dish_recipes")

    def insert_recipe_item_row(self, item: Any) -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO dish_recipe_items (id, recipe_id, ingredient_name, "
                "weight_g, tolerance_pct, sort_no) VALUES (?, ?, ?, ?, ?, ?)",
                (item.id, item.recipe_id, item.ingredient, item.weight_g,
                 item.tolerance_pct, item.sort_no)),
            "dish_recipe_items")

    def insert_sop_row(self, step: Any) -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO sop_steps (id, recipe_id, step_no, title, instruction, "
                "heat_level, duration_sec, ccp, requires_card) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (step.id, step.recipe_id, step.step_no, step.title,
                 step.instruction or None, step.heat_level or None,
                 step.duration_sec, int(step.ccp), int(step.requires_card))),
            "sop_steps")

    def insert_supplier_row(self, supplier_id: int, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.insert_ignore(
                "suppliers",
                ["id", "name", "license_no", "contact", "qualifications", "state"],
                (supplier_id, row.get("name"), row.get("license_no"),
                 row.get("contact"), _json(row.get("qualifications")),
                 str(row.get("state") or "active"))),
            "suppliers")

    def insert_batch_row(self, batch_id: int, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.insert_ignore(
                "dish_batches",
                ["id", "batch_no", "dish_id", "produced_at", "expire_at", "state"],
                (batch_id, row.get("batch_no"), row.get("dish_id"),
                 _fmt_ts(row.get("produced_at")), _fmt_ts(row.get("expire_at")),
                 str(row.get("state") or "produced"))),
            "dish_batches")

    def insert_trace_row(self, code: str, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.insert_ignore(
                "trace_codes",
                ["id", "code", "batch_id", "supplier_id", "payload", "created_at"],
                (row.get("id"), code, row.get("batch_id"), row.get("supplier_id"),
                 _json(row.get("payload")), _fmt_ts(row.get("created_at")))),
            "trace_codes")

    def insert_ingredient_batch_row(self, batch_id: int, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.insert_ignore(
                "ingredient_batches",
                ["id", "batch_no", "ingredient_name", "supplier_id",
                 "received_at", "expire_at", "inspection", "state"],
                (batch_id, row.get("batch_no"), row.get("ingredient_name"),
                 row.get("supplier_id"), _fmt_ts(row.get("received_at")),
                 _fmt_ts(row.get("expire_at")), _json(row.get("inspection")),
                 str(row.get("state") or "received"))),
            "ingredient_batches")

    # ------------------------------------------------------------ 溯源录入写路径
    # 与 insert_* 的区别：管理端「保存编辑」要对既有行生效。INSERT IGNORE 撞主键
    # 会静默跳过（改了却没变），这里用 UPSERT 原地更新，不碰外键引用的子行。

    def upsert_supplier_row(self, supplier_id: int, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "suppliers",
                ["id", "name", "license_no", "contact", "qualifications", "state"],
                (int(supplier_id), row.get("name"), row.get("license_no"),
                 row.get("contact"), _json(row.get("qualifications")),
                 str(row.get("state") or "active")),
                conflict=["id"]),
            "suppliers")

    def upsert_batch_row(self, batch_id: int, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "dish_batches",
                ["id", "batch_no", "dish_id", "produced_at", "expire_at", "state"],
                (int(batch_id), row.get("batch_no"), int(row.get("dish_id") or 0),
                 _fmt_ts(row.get("produced_at")), _fmt_ts(row.get("expire_at")),
                 str(row.get("state") or "produced")),
                conflict=["id"]),
            "dish_batches")

    def upsert_trace_row(self, code: str, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "trace_codes",
                ["id", "code", "batch_id", "supplier_id", "payload", "created_at"],
                (int(row.get("id") or 0), str(code), row.get("batch_id"),
                 row.get("supplier_id"), _json(row.get("payload")),
                 _fmt_ts(row.get("created_at"))),
                conflict=["code"]),
            "trace_codes")

    def upsert_ingredient_batch_row(self, batch_id: int, row: dict) -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "ingredient_batches",
                ["id", "batch_no", "ingredient_name", "supplier_id",
                 "received_at", "expire_at", "inspection", "state"],
                (int(batch_id), row.get("batch_no"), row.get("ingredient_name"),
                 int(row.get("supplier_id") or 0), _fmt_ts(row.get("received_at")),
                 _fmt_ts(row.get("expire_at")), _json(row.get("inspection")),
                 str(row.get("state") or "received")),
                conflict=["id"]),
            "ingredient_batches")

    # ------------------------------------------------------------ 订单链
    def persist_order(self, order: "Order") -> bool:
        """订单 + 全部子项快照落库（orders / order_items 各一次 UPSERT）。"""
        def _write() -> None:
            if order.table_id is not None:
                # dining_tables 不在内存仓储里（顾客通道只用 table_id 整数），
                # 外键 fk_orders_table 要求父行存在——按需补最小行
                self.conn.insert_ignore(
                    "dining_tables", ["id", "code", "seats", "state"],
                    (order.table_id, f"T{order.table_id}", 4, "idle"))
            self.conn.upsert(
                "orders",
                ["id", "order_no", "table_id", "source", "state", "version",
                 "promised_at", "created_at", "updated_at"],
                (order.id, order.order_no, order.table_id, order.source,
                 order.state.value, order.version, _fmt_ts(order.promised_at),
                 _fmt_ts(order.created_at), _fmt_ts(order.updated_at)),
                conflict=("id",))
            for item in order.items:
                self.conn.upsert(
                    "order_items",
                    ["id", "order_id", "dish_id", "qty", "state", "urge_count",
                     "station_id", "note", "created_at"],
                    (item.id, item.order_id, item.dish_id, item.qty,
                     item.state.value, item.urge_count, item.station_id,
                     _clip(item.note or None, 255), _fmt_ts(order.created_at)),
                    conflict=("id",))
        return self._biz(_write, "orders")

    def append_order_event(self, event: "OrderEvent") -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO order_events (order_id, from_state, to_state, actor, "
                "channel, client_event_id, detail, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event.order_id, event.from_state, event.to_state,
                 _clip(event.actor or None, 64), _clip(event.channel, 16),
                 _clip(event.client_event_id, 96), _json(event.detail),
                 _fmt_ts(event.ts))),
            "order_events")

    # ------------------------------------------------------------ 过程执行
    def upsert_occupancy(self, session: "StationOccupancy") -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "station_occupancy",
                ["id", "station_id", "order_item_id", "staff_id", "device_id",
                 "state", "flame_verified", "started_at", "ended_at"],
                (session.id, session.station_id, session.order_item_id,
                 session.staff_id, _clip(session.device_id or None, 48),
                 session.state, int(session.flame_verified),
                 _fmt_ts(session.started_at), _fmt_ts(session.ended_at)),
                conflict=("id",)),
            "station_occupancy")

    def append_process_event(self, event: "ProcessEvent") -> bool:
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO process_events (order_item_id, station_id, staff_id, "
                "kind, state, session_uid, payload, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event.order_item_id, event.station_id, event.staff_id,
                 event.kind, _clip(event.state or None, 24),
                 _clip(event.session_uid, 96), _json(event.payload),
                 _fmt_ts(event.ts))),
            "process_events")

    def upsert_retention_sample(self, sample: "RetentionSample") -> bool:
        return self._biz(
            lambda: self.conn.upsert(
                "retention_samples",
                ["id", "dish_batch_id", "trace_code_id", "weight_g", "state",
                 "sampled_at", "destroy_due_at", "destroyed_at"],
                (sample.id, sample.dish_batch_id, sample.trace_code_id,
                 sample.weight_g, sample.state, _fmt_ts(sample.sampled_at),
                 _fmt_ts(sample.destroy_due_at), _fmt_ts(sample.destroyed_at)),
                conflict=("id",)),
            "retention_samples")

    def upsert_disinfection_record(self, record: "DisinfectionRecord") -> bool:
        """消毒会话快照。`cumulative_sec/peak_temp` 按调用时点取值——
        feed() 每帧都会动累计值，逐帧双写太重；在 start 与各状态跃迁点快照，
        台账拿到的是「跃迁那一刻」的曲线位置。"""
        return self._biz(
            lambda: self.conn.upsert(
                "disinfection_records",
                ["id", "session_uid", "batch_id", "start_ts", "end_ts",
                 "cumulative_sec", "peak_temp", "state", "forbid_outbound",
                 "rework_order_id"],
                (record.id, record.session_uid, record.batch_id,
                 _fmt_ts(record.start_ts), _fmt_ts(record.end_ts),
                 round(float(record.cumulative_sec)), round(float(record.peak_temp), 1),
                 record.state, int(record.forbid_outbound), record.rework_order_id),
                conflict=("session_uid",)),
            "disinfection_records")

    # ------------------------------------------------------------ 审计
    def append_audit(self, entry: "AuditEntry") -> bool:
        """`audit_logs` 双写：JSONL 仍是权威留痕（含改前改后全量），SQL 供
        跨重启 SQL 查询与 `db/stats` 体检。detail 打包 before/after/detail/source。"""
        detail = {
            "before": entry.before, "after": entry.after,
            "detail": entry.detail, "source": entry.source,
        }
        return self._biz(
            lambda: self.conn.execute(
                "INSERT INTO audit_logs (ts, actor, actor_role, action, target, "
                "result, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_fmt_ts(datetime.fromtimestamp(entry.ts)), _clip(entry.actor, 64),
                 _clip(entry.role, 16), _clip(entry.action, 48),
                 _clip(entry.target or None, 96), _clip(entry.result, 16),
                 _json(detail))),
            "audit_logs")

    # ------------------------------------------------------------ 内部
    def _biz(self, write: Any, what: str) -> bool:
        """统一包装：单锁 + 事务 + 失败计数。写失败返回 False，绝不抛。"""
        try:
            with self._lock, self.conn.transaction():
                write()
        except self.conn.errors:
            self.write_failures += 1
            log.warning("业务数据落库失败（%s，第 %d 次）",
                        what, self.write_failures, exc_info=True)
            return False
        self.business_writes += 1
        return True


