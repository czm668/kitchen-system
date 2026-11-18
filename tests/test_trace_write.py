# -*- coding: utf-8 -*-
"""溯源信息录入/更新（管理端写入）：仓储 upsert、journal 持久化、API 与扫码闭环。

覆盖「改了真的生效」这条最容易翻车的链路：不仅断言写入返回，还要断言
扫码视图 `TraceService.view()` 读得到新内容，且 journal 重放后仍在。
"""
from __future__ import annotations

import pytest

from api_support import data_of, login, make_server
from kitchen.process.journal import (
    append_journal,
    journal_batch_payload,
    journal_supplier_payload,
    journal_trace_payload,
    replay_journal,
)
from kitchen.process.repository import MemoryProcessRepository
from kitchen.process.trace import TraceService


@pytest.fixture()
def repo():
    return MemoryProcessRepository.from_seed()


# ---------------------------------------------------------------- 仓储 upsert
def test_put_supplier_新增与更新(repo):
    before = len(repo.suppliers())
    repo.put_supplier(99, {"name": "新供应商", "license_no": "SC-TEST-99",
                           "qualifications": ["营业执照"], "state": "active"})
    assert repo.supplier(99)["name"] == "新供应商"
    repo.put_supplier(99, {"name": "改名后", "license_no": "SC-TEST-99"})
    assert repo.supplier(99)["name"] == "改名后"
    assert len(repo.suppliers()) == before + 1  # 更新不产生重复行


def test_put_batch_时间归一化且可更新(repo):
    repo.put_batch(99, {"batch_no": "B-NEW", "dish_id": 5,
                        "produced_at": "2026-11-16 09:00:00",
                        "expire_at": "2026-11-18 09:00:00", "state": "produced"})
    row = repo.batch(99)
    from datetime import datetime
    assert isinstance(row["produced_at"], datetime) and row["produced_at"].tzinfo
    repo.put_batch(99, dict(row, state="recalled"))
    assert repo.batch(99)["state"] == "recalled"


def test_put_trace_外键校验与更新(repo):
    with pytest.raises(Exception):
        repo.put_trace_code("TC-BAD", {"batch_id": 9999})
    before = len(repo.trace_rows())
    repo.put_trace_code("TC-NEW", {"batch_id": 1, "supplier_id": 1,
                                   "payload": {"检测": "合格"}})
    assert repo.trace_code("TC-NEW")["payload"]["检测"] == "合格"
    repo.put_trace_code("TC-NEW", {"batch_id": 1, "supplier_id": 1,
                                   "payload": {"检测": "复检合格", "报告": "JC-1"}})
    assert repo.trace_code("TC-NEW")["payload"]["报告"] == "JC-1"
    assert len(repo.trace_rows()) == before + 1  # 同 code 覆盖，不新增


# ---------------------------------------------------------------- journal 重放
def test_journal_重放三类溯源且幂等(repo, tmp_path):
    path = tmp_path / "config_journal.jsonl"
    before_codes = len(repo.trace_rows())
    append_journal(path, journal_supplier_payload({
        "id": 98, "name": "重放供应商", "license_no": "SC-REPLAY-98",
        "qualifications": ["营业执照"], "state": "active"}))
    append_journal(path, journal_batch_payload({
        "id": 98, "batch_no": "B-REPLAY", "dish_id": 5,
        "produced_at": "2026-11-16 09:00:00",
        "expire_at": "2026-11-18 09:00:00", "state": "produced"}))
    payload = journal_trace_payload({
        "code": "TC-REPLAY", "id": 98, "batch_id": 98, "supplier_id": 98,
        "payload": {"检测": "合格", "检验员": "李四"},
        "created_at": "2026-11-16 09:05:00"})
    append_journal(path, payload)
    applied, skipped = replay_journal(path, repo)
    assert (applied, skipped) == (3, 0)
    # 再重放一遍：upsert 幂等，不新增行
    replay_journal(path, repo)
    assert len(repo.trace_rows()) == before_codes + 1
    assert repo.supplier(98)["name"] == "重放供应商"

    view = TraceService(repo).view("tc-replay")  # 大小写不敏感
    assert view.batch_no == "B-REPLAY"
    assert view.dish_name == "红烧肉"
    assert view.supplier == "重放供应商"
    assert view.inspection["检验员"] == "李四"


# ---------------------------------------------------------------- API 与扫码闭环
@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def admin(server):
    return login(server, "admin")


def test_api_录入溯源码后顾客可扫码(server, admin):
    # 供应商 → 批次 → 溯源码，按依赖顺序录入（id 取返回值，不写死撞种子）
    sup = data_of(server, "POST", "/api/trace/suppliers", session=admin,
                  name="测试肉联厂", license_no="SC-TEST-1",
                  qualifications=["营业执照"], state="active")
    batch = data_of(server, "POST", "/api/trace/batches", session=admin,
                    batch_no="B-API-001", dish_id=5,
                    produced_at="2026-11-16 10:00:00",
                    expire_at="2026-11-20 10:00:00", state="produced")
    data_of(server, "POST", "/api/trace/codes", session=admin,
            code="TC-API-0001", batch_id=batch["id"], supplier_id=sup["id"],
            payload={"dish": "红烧肉", "检测": "合格", "报告": "JC-API-1"})

    # 顾客端匿名扫码可见
    traced = data_of(server, "GET", "/api/customer/trace?code=TC-API-0001")
    assert traced["batch_no"] == "B-API-001"
    assert traced["supplier"] == "测试肉联厂"
    assert traced["inspection"]["报告"] == "JC-API-1"

    # 管理端批次视图带回完整行，可继续编辑
    batches = data_of(server, "GET", "/api/batches", session=admin)
    assert any(t["code"] == "TC-API-0001" for t in batches["trace_codes"])
    assert any(s["name"] == "测试肉联厂" for s in batches["suppliers"])


def test_api_编辑溯源码后扫码读到新内容(server, admin):
    data_of(server, "POST", "/api/trace/codes", session=admin,
            code="TC-EDIT-0001", batch_id=1, supplier_id=1,
            payload={"检测": "初检"})
    before = data_of(server, "GET", "/api/customer/trace?code=TC-EDIT-0001")
    assert before["inspection"]["检测"] == "初检"

    batches = data_of(server, "GET", "/api/batches", session=admin)
    row = next(t for t in batches["trace_codes"] if t["code"] == "TC-EDIT-0001")
    data_of(server, "POST", "/api/trace/codes", session=admin,
            id=row["id"], code="TC-EDIT-0001", batch_id=1,
            supplier_id=1, payload={"检测": "复检合格", "检验员": "王组长"})

    after = data_of(server, "GET", "/api/customer/trace?code=TC-EDIT-0001")
    assert after["inspection"]["检测"] == "复检合格"
    assert after["inspection"]["检验员"] == "王组长"
    # 同码编辑不新增行
    batches = data_of(server, "GET", "/api/batches", session=admin)
    assert sum(t["code"] == "TC-EDIT-0001" for t in batches["trace_codes"]) == 1


def test_api_无写入权限角色被拒(server):
    guest = login(server, "guest")
    with pytest.raises(Exception):
        data_of(server, "POST", "/api/trace/codes", session=guest,
                code="TC-NO", payload={})
