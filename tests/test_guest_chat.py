# -*- coding: utf-8 -*-
"""对话式客服 + 意图分流 + 管理员单人直通 测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from api_support import data_of, login, make_server
from kitchen.guest import store
from kitchen.guest.intent import SUBS, classify_intent
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


class TestSubIntent:
    """`sub` 是内部路由键，不改 `INTENTS` 六值契约。

    `16_AI客服FAQ话术库.md` §九 的补丁表：同一个 `menu_query` 下，
    问价格、问营业时间、问怎么点菜不能回同一句「最便宜的 4 道」。
    """

    @pytest.mark.parametrize("text,intent,sub", [
        ("菜单有什么推荐的", "menu_query", "recommend"),
        ("水煮鱼多少钱", "menu_query", "price"),
        ("有便宜点的吗", "menu_query", "price"),
        ("营业时间到几点", "menu_query", "store_info"),
        ("你们店在哪", "menu_query", "store_info"),
        ("有宝宝椅吗", "menu_query", "store_info"),
        ("怎么点菜啊", "menu_query", "howto"),
        ("我忌口，有素食吗", "menu_query", "allergy"),
        ("这个菜多少卡路里", "menu_query", "nutrition"),
        ("能打包吗", "menu_query", "takeaway"),
        ("能开发票吗", "menu_query", "payment"),
        ("清炒时蔬还有吗", "menu_query", "soldout"),
        ("帮我催一下菜", "urge", ""),
        ("催了两次还没上", "urge", "escalate"),
        ("查一下我的订单到哪了", "order_query", "status"),
        ("再加个米饭", "order_query", "add"),
        ("现在要排队吗", "order_query", "queue"),
        ("我们换桌了", "order_query", "table_change"),
    ])
    def test_子意图分流(self, text, intent, sub):
        r = classify_intent(text)
        assert r["intent"] == intent
        assert r["sub"] == sub
        # 子意图只在白名单内取值；空串表示该意图不再细分
        assert r["sub"] == "" or r["sub"] in SUBS

    def test_过敏仍按食安投诉处理(self):
        # 「过敏」是强投诉信号（食安风险），不能被菜单的 allergy 子意图抢走
        assert classify_intent("我花生过敏")["intent"] == "complaint"

    def test_门店问题不再误判投诉(self):
        # 有订单时旧逻辑会把它判成 complaint，等于把问地址变成一张投诉单
        r = classify_intent("你们店在哪", has_order=True)
        assert r["intent"] == "menu_query"
        assert r["sub"] == "store_info"


class TestSubIntentAnswers:
    """子意图必须答到点上：价格答价格，门店答门店，没有的数据就说没有。"""

    def test_问价格回该菜单价(self, server):
        s = _open(server)
        reply = _chat(server, s, "水煮鱼多少钱")
        assert reply["sub"] == "price"
        assert "58" in reply["reply"]
        assert "水煮鱼" in reply["reply"]

    def test_未配置门店时如实说没同步(self, server, monkeypatch):
        for env in store.FIELDS.values():
            monkeypatch.delenv(env, raising=False)
        s = _open(server)
        reply = _chat(server, s, "你们几点开门")
        assert reply["sub"] == "store_info"
        assert reply["data"]["configured"] is False
        assert "没同步" in reply["reply"]
        # 关键：不许把菜单列表当答案，也不许编营业时间
        assert "¥" not in reply["reply"]
        assert "11:00" not in reply["reply"]

    def test_配置门店后照实回答(self, server, monkeypatch):
        monkeypatch.setenv("KITCHEN_STORE_HOURS", "11:00-21:30")
        monkeypatch.setenv("KITCHEN_STORE_ADDRESS", "示例路 1 号")
        s = _open(server)
        hours = _chat(server, s, "你们几点开门")
        assert hours["data"]["configured"] is True
        assert "11:00-21:30" in hours["reply"]
        addr = _chat(server, s, "你们店在哪")
        assert "示例路 1 号" in addr["reply"]

    def test_问热量如实说没有(self, server):
        s = _open(server)
        reply = _chat(server, s, "这个菜多少卡路里")
        assert reply["sub"] == "nutrition"
        assert "没有标注热量" in reply["reply"]

    def test_问打包说清不做外卖(self, server):
        s = _open(server)
        reply = _chat(server, s, "能打包吗")
        assert reply["sub"] == "takeaway"
        assert "外卖暂不支持" in reply["reply"]

    def test_问发票先说小程序不收款(self, server):
        s = _open(server)
        reply = _chat(server, s, "能开发票吗")
        assert reply["sub"] == "payment"
        assert "只记账不收款" in reply["reply"]

    def test_问排队不编桌数(self, server):
        s = _open(server)
        reply = _chat(server, s, "现在要排队吗")
        assert reply["sub"] == "queue"
        assert "看不到精确桌数" in reply["reply"]

    def test_问忌口念配方主料不承诺(self, server):
        s = _open(server)
        reply = _chat(server, s, "青椒肉丝我忌口，能吃什么")
        assert reply["sub"] == "allergy"
        # 有配方就念主料克重，没有就请人确认；两种情况都不许出现「绝对不含」
        assert "绝对不含" not in reply["reply"]
        assert "放心吃" not in reply["reply"]

    def test_子意图不改变工单行为(self, server):
        s = _open(server)
        _order(server, s["customer_token"])
        _chat(server, s, "水煮鱼多少钱")
        complaints = data_of(server, "GET", "/api/customer/guest/complaints",
                             customer_token=s["customer_token"], guest_key=KEY)
        assert complaints["total"] == 0


class TestStoreFacts:
    def test_默认未配置且不编造(self, monkeypatch):
        for env in store.FIELDS.values():
            monkeypatch.delenv(env, raising=False)
        assert store.info() == {}
        assert set(store.missing()) == set(store.FIELDS)
        res = store.answer("几点关门")
        assert res["configured"] is False
        assert res["field"] == "hours"
        assert "没同步" in res["reply"]

    def test_配置后返回真实值(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_STORE_PHONE", "010-00000000")
        res = store.answer("你们电话多少")
        assert res["configured"] is True
        assert "010-00000000" in res["reply"]

    def test_空字符串视为未配置(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_STORE_WIFI", "   ")
        assert "wifi" not in store.info()
        assert store.answer("wifi 密码多少")["configured"] is False


class TestReviewP2Followups:
    """审查 P2-4/5：词表单源 + 设施问法不再白路由。"""

    def test_意图词表不再重复两份(self):
        import kitchen.guest.intent as intent
        # store_info / payment 子意图直接引用粗判表，不存在抄第二份的漂移源
        table = dict((s, tuple(w)) for s, w in intent._MENU_SUBS)
        assert table["store_info"] == intent._STORE_INFO_QUERY
        assert table["payment"] == intent._PAYMENT_QUERY
        assert "地址在哪" in intent._STORE_INFO_QUERY  # 旧版只在粗判表里有

    def test_包装咨询不再建投诉(self):
        r = classify_intent("打包用的是塑料盒吗")
        assert r["intent"] == "menu_query"
        assert r["sub"] == "takeaway"

    @pytest.mark.parametrize("text", ["有宝宝椅吗", "洗手间在哪", "有无障碍通道吗"])
    def test_设施问法落到facility而不是unknown(self, text):
        assert store._topic(text) == "facility"
        res = store.answer(text, facts={})
        assert res["field"] == "facility"
        assert res["configured"] is False
        assert "值班同事" in res["reply"]

    def test_设施字段配置后返回真值(self):
        res = store.answer("有宝宝椅吗", facts={"facility": "有宝宝椅，母婴室在二楼"})
        assert res["configured"] is True
        assert "宝宝椅" in res["reply"]
