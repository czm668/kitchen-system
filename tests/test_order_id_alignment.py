# -*- coding: utf-8 -*-
"""A24：订单 id 游标对齐（重启不覆盖历史订单）。

背景（云端实测暴露）：订单只存在于内存，运行时库是**写穿镜像**。进程重启后
内存清空、`_next_order` 回到 1，而库里还留着上次运行的行。若不先对齐，
重启后新下的单会拿到 id=1、2、3 去 UPSERT，把历史订单整行覆盖掉——
不抛异常、不写审计，数据静默消失。

这组用例锁三件事：
1. `align_ids` 只往前推，不会把游标改小；
2. `order_id_bounds` 能从库里读出真实最大主键（含空库与首次建表情形）；
3. 端到端：同一套库文件上"重启"两次，第二个进程下的单 id 不与旧行冲突。
"""
from __future__ import annotations

from kitchen.process.orders import OrderService
from kitchen.process.repository import MemoryProcessRepository
from kitchen.staff.repository import MemoryStaffRepository
from kitchen.storage import SqliteRecorder


class _IdleClock:
    def __call__(self):
        from datetime import datetime
        from kitchen.process.models import CST
        return datetime(2026, 11, 18, 12, 0, 0, tzinfo=CST)


def _service() -> OrderService:
    return OrderService(MemoryProcessRepository.from_seed(), clock=_IdleClock())


def _wired(db):
    """建库并把菜品基线物化进去——order_items 有 dish_id 外键，空库写不进。"""
    rec = SqliteRecorder(db, device_id="kitchen-node-01")
    repo = MemoryProcessRepository.from_seed()
    rec.sync_baseline(repo, MemoryStaffRepository.from_seed())
    svc = OrderService(repo, clock=_IdleClock(),
                       sink=lambda order, event: rec.persist_order(order))
    return rec, svc


def test_align_ids_advances_cursor():
    svc = _service()
    first = svc.place_order(items=[(1, 1)])
    assert first.id == 1, "未对齐时应从 1 开始（既有行为不变）"

    svc2 = _service()
    svc2.align_ids(40, 99)
    order = svc2.place_order(items=[(1, 1)])
    assert order.id == 41
    assert order.items[0].id == 100


def test_align_ids_never_moves_backwards():
    svc = _service()
    svc.place_order(items=[(1, 1)])
    svc.place_order(items=[(1, 1)])
    svc.align_ids(0, 0)          # 库里为空（首次启动）时不该把游标归零
    assert svc.place_order(items=[(1, 1)]).id == 3


def test_order_id_bounds_on_empty_runtime_db(tmp_path):
    rec = SqliteRecorder(tmp_path / "empty.db", device_id="kitchen-node-01")
    assert rec.order_id_bounds() == (0, 0)


def test_order_id_bounds_reads_max_from_db(tmp_path):
    rec, svc = _wired(tmp_path / "rows.db")
    a = svc.place_order(items=[(1, 1)])
    b = svc.place_order(items=[(1, 1)])
    assert rec.order_id_bounds() == (max(a.id, b.id), max(i.id for i in b.items))
    # 再下一单后最大值应推进
    c = svc.place_order(items=[(1, 1)])
    assert rec.order_id_bounds()[0] == c.id


def test_restart_does_not_overwrite_history(tmp_path):
    """端到端：模拟「进程重启」——新 OrderService + 对齐 —— 不覆盖旧行。"""
    db = tmp_path / "restart.db"
    rec1, svc1 = _wired(db)
    old = svc1.place_order(items=[(1, 1)])
    old_no, old_id = old.order_no, old.id

    # 新进程：内存为空，先按库里最大值对齐
    rec2, svc2 = _wired(db)
    max_order, max_item = rec2.order_id_bounds()
    assert max_order == old_id
    svc2.align_ids(max_order, max_item)
    new = svc2.place_order(items=[(1, 1)])
    assert new.id > old_id, "重启后新单必须排在历史之后"

    # 旧行仍在，且订单号没被改写
    rows = rec2.conn.query("SELECT id, order_no FROM orders ORDER BY id", [])
    kept = {int(r[0]): r[1] for r in rows}
    assert kept[old_id] == old_no, "历史订单被覆盖了"
    assert new.order_no in kept.values()
