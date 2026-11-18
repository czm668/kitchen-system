# -*- coding: utf-8 -*-
"""原料溯源专项：溯源的对象是原料到货批次，配方/声明是骨架，成品批次只作出餐留样。

覆盖三件最容易搞错的事：
1. 原料批次按 ingredient_name 关联，同名原料多批次时取未过期的最新一批；
2. 扫码视图展示「用了哪些原料 + 各自供应商/检疫」，且不把配方标准伪装成执行记录；
3. journal 重放原料批次后，扫码仍能追到（重启不丢）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from kitchen.process.journal import (
    append_journal,
    journal_ingredient_batch_payload,
    replay_journal,
)
from kitchen.process.repository import MemoryProcessRepository
from kitchen.process.trace import TraceService


@pytest.fixture()
def repo():
    return MemoryProcessRepository.from_seed()


def test_原料批次按原料名关联且取未过期最新(repo):
    fresh = repo.ingredient_batches_of("猪里脊")
    assert fresh, "种子里应有猪里脊到货批次"
    assert all(row["ingredient_name"] == "猪里脊" for row in fresh)
    assert not fresh[0]["expire_at"] < datetime.now(fresh[0]["expire_at"].tzinfo)

    # 新增一批更早到货的：排序不应把它排到新鲜批次前面
    repo.put_ingredient_batch(900, {
        "batch_no": "IB-TEST-OLD", "ingredient_name": "猪里脊",
        "supplier_id": 1, "received_at": "2026-01-01 08:00:00",
        "expire_at": "2026-01-10 08:00:00",
        "inspection": {"项目": "兽药残留", "结果": "合格"}, "state": "received",
    })
    rows = repo.ingredient_batches_of("猪里脊")
    assert rows[0]["batch_no"] != "IB-TEST-OLD"
    assert any(row["batch_no"] == "IB-TEST-OLD" for row in rows)


def test_扫码展示原料来源而非只有成品批次(repo):
    view = TraceService(repo).view("TC-DEMO-0005")  # 红烧肉（无标准配方，靠 payload 声明）
    data = view.to_dict()

    # 成品批次信息仍在，但只是出餐/留样追溯
    assert data["batch_no"] == "B-DEMO-005"
    # 溯源主体：原料清单
    names = [row["name"] for row in data["ingredients"]]
    assert "五花肉" in names and "冰糖" in names
    pork = next(row for row in data["ingredients"] if row["name"] == "五花肉")
    assert pork["supplier"] == "鲜丰肉联厂"
    assert pork["license_no"]
    assert pork["batch_no"] and pork["batch_no"].startswith("IB-")
    assert pork["inspection"]["项目"] == "兽药残留"
    assert pork["inspection"]["结果"] == "合格"
    # 无标准配方：如实标注未登记，不伪造工序
    assert data["recipe"] is None
    assert data["proof"]["weigh_executed"] is False
    assert data["proof"]["retention_executed"] is False


def test_有配方菜扫码同时展示SOP标准(repo):
    data = TraceService(repo).view("TC-DEMO-0001").to_dict()  # 青椒肉丝（有配方+SOP）
    assert data["recipe"]["version"] == 1
    assert len(data["proof"]["sop_steps"]) >= 1
    assert data["proof"]["ccp_count"] >= 1
    assert data["proof"]["weigh_items"], "有配方应带出称重基准"
    # 标准不等于已执行
    assert data["proof"]["weigh_executed"] is False


def test_journal_重放原料批次后扫码仍可追溯(repo, tmp_path):
    path = tmp_path / "config_journal.jsonl"
    append_journal(path, journal_ingredient_batch_payload({
        "id": 901, "batch_no": "IB-REPLAY", "ingredient_name": "测试特色肉",
        "supplier_id": 1, "received_at": "2026-11-16 08:00:00",
        "expire_at": "2026-11-20 08:00:00",
        "inspection": {"项目": "兽药残留", "结果": "合格", "报告": "JY-REPLAY"},
        "state": "received",
    }))
    applied, skipped = replay_journal(path, repo)
    assert (applied, skipped) == (1, 0)
    rows = repo.ingredient_batches_of("测试特色肉")
    assert len(rows) == 1
    assert rows[0]["inspection"]["报告"] == "JY-REPLAY"
    assert isinstance(rows[0]["received_at"], datetime) and rows[0]["received_at"].tzinfo

    replay_journal(path, repo)  # 幂等
    assert len(repo.ingredient_batches_of("测试特色肉")) == 1
