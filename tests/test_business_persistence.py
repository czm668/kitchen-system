# -*- coding: utf-8 -*-
"""A23 业务数据落库：基线物化 / 订单链 / 人员域 / 过程留痕 / 审计双写 / 失败纪律。

口径：
- `db_path=None`（写器未启用）时全部行为不变——既有用例零影响；
- 写失败只计数不抛：落库是旁路，不能拖死下单、刷卡这些主路径；
- 基线物化幂等（INSERT IGNORE），运行期写路径用 UPSERT（父表不能 REPLACE，
  子表外键 RESTRICT 会让「子表有数据后父表写不进」）。
"""
from __future__ import annotations

from datetime import datetime

from kitchen.api import AppState
from kitchen.api.audit import AuditLog
from kitchen.process.cooking import CookingBoard
from kitchen.process.dispatch import DispatchBoard
from kitchen.process.disinfection import DisinfectionRecord, DisinfectionService
from kitchen.process.orders import OrderService
from kitchen.process.repository import MemoryProcessRepository
from kitchen.process.retention import RetentionSample, RetentionService
from kitchen.staff.models import HealthCertificate, MorningAnswers, MorningCheck
from kitchen.staff.repository import MemoryStaffRepository
from kitchen.storage import SqliteRecorder

from api_support import make_state

TS = datetime(2026, 11, 15, 11, 30, 0)


def _rec(tmp_path, name: str = "run.db") -> SqliteRecorder:
    return SqliteRecorder(tmp_path / name, device_id="kitchen-node-01")


def _repos():
    return MemoryProcessRepository.from_seed(), MemoryStaffRepository.from_seed()


def _counts(rec: SqliteRecorder, *tables: str) -> dict[str, int]:
    return {t: rec.conn.stats().get(t, 0) for t in tables}


# ------------------------------------------------------------ 基线物化
def test_基线物化与内存仓储一致且幂等(tmp_path):
    rec = _rec(tmp_path)
    repo, staff_repo = _repos()
    written = rec.sync_baseline(repo, staff_repo)
    snap = repo.snapshot()
    assert written["dishes"] == snap["dishes"]
    assert written["work_stations"] == snap["stations"]
    assert written["dish_recipes"] == snap["recipes"]
    assert written["sop_steps"] == snap["sop_steps"]
    counts = _counts(rec, "dishes", "staff", "shifts", "health_certificates",
                     "staff_cards", "trace_codes")
    assert counts["dishes"] == snap["dishes"]
    assert counts["staff"] == len(staff_repo.all_staff())
    assert counts["shifts"] == len(staff_repo.all_shifts())
    assert counts["trace_codes"] == len(repo.trace_rows())
    # 幂等：重复物化行数不变
    rec.sync_baseline(repo, staff_repo)
    assert _counts(rec, "dishes", "staff")["dishes"] == counts["dishes"]
    assert _counts(rec, "dishes", "staff")["staff"] == counts["staff"]
    rec.close()


def test_配置页新增与沽清落库(tmp_path):
    rec = _rec(tmp_path)
    repo, staff_repo = _repos()
    rec.sync_baseline(repo, staff_repo)
    repo.recorder = rec
    dish = repo.add_dish(dish_id=9001, code="T9001", name="测试菜", price=9.9)
    repo.set_sold_out(9001, True)
    rows = rec.conn.query(
        "SELECT code, name, price, sold_out FROM dishes WHERE id = 9001")
    assert rows == [("T9001", "测试菜", 9.9, 1)]
    _ = dish
    rec.close()


# ------------------------------------------------------------ 订单链
def test_下单与状态推进落库_催菜计数同步(tmp_path):
    rec = _rec(tmp_path)
    repo, _ = _repos()
    rec.sync_baseline(repo, MemoryStaffRepository.from_seed())

    def sink(order, event):
        rec.persist_order(order)
        rec.append_order_event(event)

    orders = OrderService(repo, clock=lambda: TS, sink=sink)
    dish = repo.dishes()[0]
    order = orders.place_order(items=[(dish.id, 2)], source="miniapp",
                               customer_ref="T03-ABCD")
    rows = rec.conn.query(
        "SELECT order_no, state, version FROM orders WHERE id = ?", (order.id,))
    assert rows[0][0] == order.order_no and rows[0][1] == "created"
    items = rec.conn.query(
        "SELECT dish_id, qty, state FROM order_items WHERE order_id = ?", (order.id,))
    assert items == [(dish.id, 2, "pending")]
    events = rec.conn.query("SELECT COUNT(*) FROM order_events WHERE order_id = ?",
                            (order.id,))
    assert events[0][0] == 1
    # 推进 + 催菜：快照与事件同步更新
    orders.move_item(order.items[0].id, __import__(
        "kitchen.process.models", fromlist=["ItemState"]).ItemState.DISPATCHED)
    orders.urge(order.items[0].id)
    row = rec.conn.query(
        "SELECT state, urge_count FROM order_items WHERE id = ?",
        (order.items[0].id,))[0]
    assert row == ("dispatched", 1)
    n_events = rec.conn.query(
        "SELECT COUNT(*) FROM order_events WHERE order_id = ?", (order.id,))[0][0]
    assert n_events == 3
    rec.close()


def test_订单链跨重启仍在(tmp_path):
    rec = _rec(tmp_path)
    repo, _ = _repos()
    rec.sync_baseline(repo, MemoryStaffRepository.from_seed())
    orders = OrderService(repo, clock=lambda: TS,
                          sink=lambda o, e: (rec.persist_order(o),
                                             rec.append_order_event(e)))
    dish = repo.dishes()[0]
    order = orders.place_order(items=[(dish.id, 1)])
    rec.close()
    # 重新打开同一个库文件：订单/子项/事件都可查
    rec2 = _rec(tmp_path)
    assert rec2.conn.query(
        "SELECT COUNT(*) FROM orders WHERE order_no = ?", (order.order_no,))[0][0] == 1
    assert rec2.conn.query(
        "SELECT COUNT(*) FROM order_items WHERE order_id = ?", (order.id,))[0][0] == 1
    rec2.close()


def test_桌台外键按需补行(tmp_path):
    """orders.table_id 外键指向 dining_tables——内存里没有这张表的仓储，
    persist_order 要按需补最小父行，否则外键 RESTRICT 直接炸落库。"""
    rec = _rec(tmp_path)
    repo, _ = _repos()
    rec.sync_baseline(repo, MemoryStaffRepository.from_seed())
    orders = OrderService(repo, clock=lambda: TS,
                          sink=lambda o, e: (rec.persist_order(o),
                                             rec.append_order_event(e)))
    dish = repo.dishes()[0]
    orders.place_order(items=[(dish.id, 1)], table_id=9)
    rows = rec.conn.query(
        "SELECT id, code, seats FROM dining_tables WHERE id = 9")
    assert rows == [(9, "T9", 4)]
    rec.close()


# ------------------------------------------------------------ 人员域
def test_人员域写路径落库_晨检唯一键覆盖(tmp_path):
    rec = _rec(tmp_path)
    _, staff_repo = _repos()
    rec.sync_baseline(MemoryProcessRepository.from_seed(), staff_repo)
    staff_repo.recorder = rec
    target = staff_repo.all_staff()[0]
    staff_repo.set_staff_enabled(target.id, False)
    staff_repo.bind_card("AABBCCDD", target.id, now=TS)
    card = staff_repo.card("AABBCCDD")
    staff_repo.set_card_enabled("AABBCCDD", False)
    cert = staff_repo.add_certificate(
        staff_id=target.id, cert_no="CERT-001", valid_from=TS.date(),
        valid_to=TS.date().replace(year=TS.date().year + 1))
    check = staff_repo.upsert_morning_check(MorningCheck(
        staff_id=target.id, check_date=TS.date(),
        answers=MorningAnswers(temp_c=36.5), result="passed", ts=TS))
    _ = check
    assert rec.conn.query(
        "SELECT enabled FROM staff WHERE id = ?", (target.id,))[0][0] == 0
    assert rec.conn.query(
        "SELECT enabled FROM staff_cards WHERE card_uid = 'AABBCCDD'")[0][0] == 0
    assert rec.conn.query(
        "SELECT cert_no, state FROM health_certificates WHERE id = ?",
        (cert.id,))[0] == ("CERT-001", "valid")
    # 同日再次晨检 → 唯一键覆盖，不炸 1062
    staff_repo.upsert_morning_check(MorningCheck(
        staff_id=target.id, check_date=TS.date(),
        answers=MorningAnswers(temp_c=38.0), result="failed", ts=TS))
    rows = rec.conn.query(
        "SELECT result FROM morning_checks WHERE staff_id = ? AND check_date = ?",
        (target.id, TS.date().isoformat()))
    assert rows == [("failed",)]
    _ = HealthCertificate
    rec.close()


# ------------------------------------------------------------ 过程执行
def test_占用会话与工序事件落库(tmp_path):
    rec = _rec(tmp_path)
    repo, staff_repo = _repos()
    rec.sync_baseline(repo, staff_repo)
    orders = OrderService(repo, clock=lambda: TS,
                          sink=lambda o, e: (rec.persist_order(o),
                                             rec.append_order_event(e)))
    board = DispatchBoard(repo, orders, clock=lambda: TS, matcher=None)
    board.recorder = rec
    station = repo.stations()[0]
    session = board.occupy(station.id, staff_repo.all_staff()[0].id, now=TS)
    open_row = rec.conn.query(
        "SELECT state, flame_verified FROM station_occupancy WHERE id = ?",
        (session.id,))[0]
    assert open_row == ("open", 0)
    board.observe_flame(station.id, 0.8)
    board.flame_check(station.id)
    verified = rec.conn.query(
        "SELECT flame_verified FROM station_occupancy WHERE id = ?",
        (session.id,))[0][0]
    assert verified == 1
    board.close(station.id, now=TS)
    closed_row = rec.conn.query(
        "SELECT state, ended_at IS NOT NULL FROM station_occupancy WHERE id = ?",
        (session.id,))[0]
    assert closed_row == ("closed", 1)

    # 工序事件：称重 → process_events
    cooking = CookingBoard(repo, orders, clock=lambda: TS)
    cooking.recorder = rec
    dish = repo.dishes()[0]
    order = orders.place_order(items=[(dish.id, 1)])
    orders.dispatch_item(order.items[0].id, station.id)
    recipe_item = repo.recipe_items(order.items[0].recipe_id)[0]
    target = recipe_item.weight_g
    cooking.weigh(order.items[0].id, recipe_item.ingredient, target)
    rows = rec.conn.query(
        "SELECT kind, state FROM process_events WHERE order_item_id = ?",
        (order.items[0].id,))
    assert rows and rows[0][0] == "weigh"
    rec.close()


def test_留样与消毒快照落库(tmp_path):
    rec = _rec(tmp_path)
    repo, _ = _repos()
    rec.sync_baseline(repo, MemoryStaffRepository.from_seed())
    batch_id = repo.batch_ids()[0]
    sample = RetentionSample(
        id=1, dish_batch_id=batch_id, weight_g=150.0, sampled_at=TS,
        destroy_due_at=TS, state="stored")
    assert rec.upsert_retention_sample(sample) is True
    sample.state = "destroyed"
    assert rec.upsert_retention_sample(sample) is True
    assert rec.conn.query(
        "SELECT weight_g, state FROM retention_samples WHERE id = 1")[0] == (150.0, "destroyed")

    svc = DisinfectionService(repo, clock=lambda: TS)
    svc.recorder = rec
    svc.start(batch_id=None, session_uid="DI-TEST", now=TS)
    svc.feed(110.0, session_uid="DI-TEST", now=TS)
    row = rec.conn.query(
        "SELECT session_uid, state, cumulative_sec FROM disinfection_records "
        "WHERE session_uid = 'DI-TEST'")[0]
    assert row[0] == "DI-TEST" and row[1] == "running"
    rec.close()


# ------------------------------------------------------------ 审计
def test_审计SQL双写_JSONL仍落盘(tmp_path):
    rec = _rec(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl", sql_sink=rec.append_audit)
    audit.record(actor="admin", role="manager", action="threshold.update",
                 target="env_thresholds#12", before={"upper": 8}, after={"upper": 9})
    rows = rec.conn.query(
        "SELECT actor, action, target, result FROM audit_logs")
    assert rows == [("admin", "threshold.update", "env_thresholds#12", "ok")]
    assert (tmp_path / "audit.jsonl").exists()  # JSONL 权威留痕仍在
    rec.close()


# ------------------------------------------------------------ 失败纪律
def test_写失败只计数不抛_主路径不受影响(tmp_path):
    rec = _rec(tmp_path)
    repo, _ = _repos()
    rec.sync_baseline(repo, MemoryStaffRepository.from_seed())
    rec.conn.close()  # 人为制造库失败
    orders = OrderService(repo, clock=lambda: TS,
                          sink=lambda o, e: (rec.persist_order(o),
                                             rec.append_order_event(e)))
    dish = repo.dishes()[0]
    order = orders.place_order(items=[(dish.id, 1)])  # 不因落库失败而抛
    assert order.state.value == "created"
    assert rec.write_failures >= 2  # 订单快照 + 事件各失败一次
    assert rec.business_writes > 0  # 基线物化已成功计入
    rec.close()  # 幂等


# ------------------------------------------------------------ AppState 集成
def test_AppState接线_基线订单审计全链路(tmp_path):
    db_path = tmp_path / "runtime.db"
    state = make_state(tmp_path, db_path=db_path)
    assert state.recorder is not None
    assert state.baseline_written, "基线物化应有产出"
    stats = state.recorder.conn.stats()
    assert stats["dishes"] == state.repo.snapshot()["dishes"]
    assert stats["staff"] == len(state.staff_repo.all_staff())
    # 下单 → SQL 可查
    dish = state.repo.dishes()[0]
    order = state.orders.place_order(items=[(dish.id, 1)], source="web")
    assert state.recorder.conn.query(
        "SELECT order_no FROM orders WHERE id = ?", (order.id,))[0][0] == order.order_no
    # 审计双写接线
    state.audit.record(actor="admin", role="manager", action="test.audit")
    assert state.recorder.conn.query(
        "SELECT COUNT(*) FROM audit_logs")[0][0] >= 1
    stats = state.recorder.stats()
    assert stats["business_writes"] > 0
    state.stop()


def test_AppState默认纯内存_零行为变化(tmp_path):
    state = make_state(tmp_path)
    assert state.recorder is None
    dish = state.repo.dishes()[0]
    order = state.orders.place_order(items=[(dish.id, 1)])
    assert order.state.value == "created"  # 主路径照常
    state.stop()
