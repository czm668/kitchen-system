# -*- coding: utf-8 -*-
"""对话式客服 + 意图分流 + 管理员单人直通 测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from api_support import data_of, login, make_server
from kitchen.guest.intent import classify_intent
from kitchen.guest.service import ComplaintState
from kitchen.api.auth import Session

DISH = "D-QJRS"
KEY = "g-chat-001"
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


def _chat(server, session, text, **kw):
    body = {"customer_token": session["customer_token"], "guest_key": KEY,
            "text": text}
    body.update(kw)
    return data_of(server, "POST", "/api/customer/guest/chat", **body)


class TestIntent:
    def test_强投诉优先于礼貌用语(self):
        r = classify_intent("谢谢，但是菜里有头发，我要投诉")
        assert r["intent"] == "complaint"

    @pytest.mark.parametrize("text,intent", [
        ("我的菜还要多久啊", "urge"),
        ("帮我催一下菜", "urge"),
        ("等了很久还没上菜，帮我看看还要多久", "complaint"),  # 不满时长=投诉，非催菜
        ("查一下我的订单到哪了", "order_query"),
        ("菜单有什么推荐的", "menu_query"),
        ("这道菜多少钱", "menu_query"),
        ("谢谢，辛苦了", "chitchat"),
        ("菜里有异物", "complaint"),
        ("服务员态度太差了", "complaint"),
    ])
    def test_常见意图分流(self, text, intent):
        assert classify_intent(text)["intent"] == intent

    def test_补偿勾选强制投诉(self):
        r = classify_intent("想退款，谢谢", force_complaint=True)
        assert r["intent"] == "complaint"
        assert r["confidence"] == 1.0


class TestChatRouting:
    def test_问菜单不建工单(self, server):
        s = _open(server)
        reply = _chat(server, s, "菜单有什么招牌菜推荐")
        assert reply["intent"] == "menu_query"
        assert reply["routed"] is True
        assert "¥" in reply["reply"]
        complaints = data_of(server, "GET", "/api/customer/guest/complaints",
                             customer_token=s["customer_token"], guest_key=KEY)
        assert complaints["total"] == 0

    def test_催菜真的催厨房不建工单(self, server):
        s = _open(server)
        order = _order(server, s["customer_token"])
        reply = _chat(server, s, "我的菜怎么还不快点上", order_id=order["order_id"])
        assert reply["intent"] == "urge"
        assert reply["action"] == "urge"
        assert reply["data"]["urge_count"] >= 1
        # 订单域的 urge_count 确实加了
        orders = data_of(server, "GET", "/api/customer/orders",
                         customer_token=s["customer_token"])
        item = orders["orders"][0]["items"][0]
        assert item["urge_count"] >= 1
        complaints = data_of(server, "GET", "/api/customer/guest/complaints",
                             customer_token=s["customer_token"], guest_key=KEY)
        assert complaints["total"] == 0

    def test_查单返回进度不建工单(self, server):
        s = _open(server)
        _order(server, s["customer_token"])
        reply = _chat(server, s, "查一下我的订单状态")
        assert reply["intent"] == "order_query"
        assert "订单" in reply["reply"]

    def test_闲聊不建工单(self, server):
        s = _open(server)
        reply = _chat(server, s, "谢谢，辛苦了")
        assert reply["intent"] == "chitchat"
        complaints = data_of(server, "GET", "/api/customer/guest/complaints",
                             customer_token=s["customer_token"], guest_key=KEY)
        assert complaints["total"] == 0

    def test_真投诉才建工单并带工单号(self, server):
        s = _open(server)
        _order(server, s["customer_token"])
        reply = _chat(server, s, "菜里有头发，我要投诉")
        assert reply["intent"] == "complaint"
        assert reply["data"]["complaint_no"].startswith("C")
        complaints = data_of(server, "GET", "/api/customer/guest/complaints",
                             customer_token=s["customer_token"], guest_key=KEY)
        assert complaints["total"] == 1

    def test_令牌不匹配被拒(self, server):
        s = _open(server)
        body = {"customer_token": "bad-token", "guest_key": KEY, "text": "菜单有什么"}
        from api_support import call
        status, _ = call(server, "POST", "/api/customer/guest/chat", **body)
        assert status in (400, 401, 403, 404)


class TestManagerResolve:
    def _paid(self, server, manager):
        s = _open(server)
        order = _order(server, s["customer_token"])
        submitted = data_of(server, "POST", "/api/customer/guest/complaints",
                            customer_token=s["customer_token"], guest_key=KEY,
                            text="菜里有异物，申请补偿", order_id=order["order_id"],
                            wants_refund=True, compensation="waive",
                            evidences=[{"kind": "photo", "content": "现场照片"}])
        # submit 响应不含 id，从管理端列表按工单号取带 id 的完整行
        rows = data_of(server, "GET", "/api/guest/complaints",
                       session=manager)["complaints"]
        return next(c for c in rows
                    if c["complaint_no"] == submitted["complaint_no"])

    def test_管理员单人直通一步结案(self, server):
        manager = login(server, "admin")
        row = self._paid(server, manager)
        resolved = data_of(
            server, "POST", f"/api/guest/complaints/{row['id']}/resolve",
            session=manager, compensation="waive",
            expected_version=row["version"], note="管理员当场处理")["complaint"]
        assert resolved["state"] == ComplaintState.APPROVED
        assert resolved["approver"] == "admin"
        assert resolved["reviewer"] == "admin"   # 单人核实+终审
        assert resolved["voucher_no"].startswith("V")

    def test_非管理员不能单人直通(self, server):
        manager = login(server, "admin")
        lead = login(server, "lead01")
        row = self._paid(server, manager)
        from api_support import call
        status, _ = call(
            server, "POST", f"/api/guest/complaints/{row['id']}/resolve",
            session=lead, compensation="waive", expected_version=row["version"])
        assert status == 403
