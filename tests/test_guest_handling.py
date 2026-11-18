# -*- coding: utf-8 -*-
"""客诉「处理决策」测试：阶段 / 卡点 / 超时 / 自动推进 / 具体回复。

和 test_guest_service.py 的分工：那边守原来的护栏（AI 不批准、双人复核），
这边验证新增的处理能力真的做了事，而不是又一句「已转交值班人员」。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from api_support import data_of, login, make_server
from kitchen.guest.handler import analyze, compose_reply
from kitchen.guest.service import ComplaintState

DISH = "D-QJRS"
KEY = "g-handle-001"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


def _open(server):
    return data_of(server, "POST", "/api/customer/session",
                   table_id=3, guest_key=KEY, tags=[])


def _order(server, token):
    return data_of(server, "POST", "/api/customer/orders",
                   customer_token=token, table_id=3,
                   items=[{"code": DISH, "qty": 1}])


def _submit(server, session, text, *, order_id=None, item_id=None,
            wants_refund=False, evidences=None):
    return data_of(server, "POST", "/api/customer/guest/complaints",
                   customer_token=session["customer_token"], guest_key=KEY,
                   text=text, order_id=order_id, item_id=item_id,
                   wants_refund=wants_refund, compensation="redo",
                   evidences=evidences or [])


class TestAnalyze:
    def test_三阶段与卡点映射(self):
        now = datetime.now()
        base = {"complaint_no": "C0001", "created_at": now,
                "updated_at": now, "evidences": []}
        submitted = analyze({**base, "state": "submitted", "category": "异物"})
        assert submitted["stage_label"] == "已受理 · 待组长核实证据"
        assert submitted["blocker"] == "组长核实证据"
        assert submitted["blocker_owner"] == "值班组长"
        assert submitted["overdue"] is False

        evidence = analyze({**base, "state": "evidence_ok", "category": "异物"})
        assert evidence["blocker"] == "管理人员终审"
        assert evidence["blocker_owner"] == "值班经理"

        closed = analyze({**base, "state": "auto_closed", "category": "上菜慢"})
        assert closed["blocker"] == ""
        assert closed["terminal"] is True

    def test_超时只在未结案且超过SLA时成立(self):
        now = datetime.now()
        old = now - timedelta(minutes=35)
        base = {"complaint_no": "C0002", "created_at": old,
                "category": "异物", "evidences": []}
        assert analyze({**base, "state": "submitted", "updated_at": old})["overdue"] is True
        assert analyze({**base, "state": "submitted",
                        "updated_at": old})["overdue_min"] >= 5
        assert analyze({**base, "state": "approved", "updated_at": old})["overdue"] is False
        assert analyze({**base, "state": "submitted", "updated_at": now})["overdue"] is False


class TestConcreteReply:
    def test_回复带工单号订单号与菜名(self, server):
        s = _open(server)
        placed = _order(server, s["customer_token"])
        order = server.state.orders.order(placed["order_id"])
        item = order.items[0]
        result = _submit(server, s, "等了很久还没上菜，请看一下",
                         order_id=placed["order_id"], item_id=item.id)
        reply = result["ai_reply"]
        assert result["complaint_no"] in reply
        assert order.order_no in reply
        assert item.dish_name in reply

    def test_高风险回复说清卡点而不是转交值班(self, server):
        s = _open(server)
        placed = _order(server, s["customer_token"])
        result = _submit(server, s, "菜里有头发，要求退款",
                         order_id=placed["order_id"], wants_refund=True)
        reply = result["ai_reply"]
        assert "已转交值班组长" not in reply
        assert "卡点" in reply
        assert result["blocker"] in reply
        assert "当前环节" in reply

    def test_规则层兜底也输出具体信息(self):
        a = {"complaint_no": "C1101", "order_no": "O0007",
             "dish_name": "宫保鸡丁", "stage_label": "已受理 · 待组长核实证据",
             "blocker": "组长核实证据", "next_step": "组长勾选证据",
             "terminal": False, "overdue": False, "sla_remain_min": 26,
             "wants_refund": True, "amount": 28.0}
        out = compose_reply("已收到反馈，转交值班人员处理。", a, [])
        assert "C1101" in out and "宫保鸡丁" in out
        assert "卡点" in out and "¥28.0" in out


class TestAutoActions:
    def test_上菜慢自动催厨房并推进派单(self, server):
        s = _open(server)
        placed = _order(server, s["customer_token"])
        item = server.state.orders.order(placed["order_id"]).items[0]
        before = item.urge_count
        result = _submit(server, s, "等了很久还没上菜，请帮我看一下还要多久",
                         order_id=placed["order_id"], item_id=item.id)
        kinds = [a["type"] for a in result["actions"]]
        assert "urge_kitchen" in kinds
        assert "evidence_linked" in kinds
        assert item.urge_count == before + 1
        assert "向厨房催了" in result["ai_reply"]

    def test_上菜慢未指定菜品也代催一道(self, server):
        s = _open(server)
        placed = _order(server, s["customer_token"])
        item = server.state.orders.order(placed["order_id"]).items[0]
        before = item.urge_count
        result = _submit(server, s, "等了很久还没上菜，请帮我看一下还要多久",
                         order_id=placed["order_id"])
        kinds = [a["type"] for a in result["actions"]]
        assert "urge_kitchen" in kinds
        assert item.urge_count == before + 1
        assert item.dish_name in result["ai_reply"]

    def test_高风险催办责任人但不代批准(self, server):
        s = _open(server)
        placed = _order(server, s["customer_token"])
        result = _submit(server, s, "菜里有头发，要求退款",
                         order_id=placed["order_id"], wants_refund=True,
                         evidences=[{"kind": "text", "content": "已拍照留存"}])
        kinds = [a["type"] for a in result["actions"]]
        assert "nudge_reviewer" in kinds
        assert result["blocker_owner"] == "值班组长"
        # 推进动作绝不动审批状态机和金额
        assert result["state"] == ComplaintState.SUBMITTED
        manager = login(server, "admin")
        row = data_of(server, "GET", "/api/guest/complaints",
                      session=manager)["complaints"][0]
        ai = row["ai_suggestion"]
        for forbidden in ("approve", "approved", "amount", "decision", "state"):
            assert forbidden not in ai
        assert row["voucher_no"] == ""

    def test_每个阶段返回卡点与待跟进(self, server):
        s = _open(server)
        placed = _order(server, s["customer_token"])
        result = _submit(server, s, "菜里有异物，要求退款",
                         order_id=placed["order_id"], wants_refund=True)
        assert result["stage"] == "submitted"
        assert result["blocker"] == "组长核实证据"
        assert any("催办值班组长" in x for x in result["urgent"])
        manager = login(server, "admin")
        row = data_of(server, "GET", "/api/guest/complaints",
                      session=manager)["complaints"][0]
        assert row["blocker"] == "组长核实证据"
        assert row["stage_label"] == "已受理 · 待组长核实证据"
        assert row["actions"]

    def test_工单超时在列表里实时刷新(self, server):
        s = _open(server)
        placed = _order(server, s["customer_token"])
        result = _submit(server, s, "菜里有头发，要求退款",
                         order_id=placed["order_id"], wants_refund=True)
        complaint = server.state.guest.complaint(
            next(c.id for c in server.state.guest.all_complaints()
                 if c.complaint_no == result["complaint_no"]))
        complaint.updated_at -= timedelta(minutes=35)
        manager = login(server, "admin")
        row = data_of(server, "GET", "/api/guest/complaints",
                      session=manager)["complaints"][0]
        assert row["overdue"] is True
        assert row["stage"] == "submitted"
        assert "超时" in "".join(row["urgent"])


class TestQuickPhrases:
    def test_输入框快捷语句已接入(self):
        config = (ROOT / "miniapp" / "config.js").read_text(encoding="utf-8")
        js = (ROOT / "miniapp" / "pages" / "service" / "service.js").read_text(
            encoding="utf-8")
        wxml = (ROOT / "miniapp" / "pages" / "service" / "service.wxml").read_text(
            encoding="utf-8")
        assert "COMPLAINT_QUICK_PHRASES" in config
        assert config.count("',") >= 6
        assert "onQuickPhrase" in js and "quickPhrases" in js
        assert "data-phrase" in wxml and "quickPhrases" in wxml
