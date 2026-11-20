# -*- coding: utf-8 -*-
"""点单意图：菜名不再变客诉，明确说「点」才真下单。

真实 bug（2026-11-20）：顾客照着话术「想点哪道直接说」发了「青椒肉丝」，
分类层词表全不命中 → 兜底：有订单判 complaint、无订单判 other，
而 chat 的 other 也走建工单分支——于是点单变成了客诉工单。
这组测试钉住修复后的分流与下单两条路径。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api_support import data_of, make_server
from kitchen.guest.intent import SUBS, classify_intent

DISH = "D-QJRS"          # 青椒肉丝
DISH_NAME = "青椒肉丝"
KEY = "g-order-dish-001"
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


def _complaints(server, session):
    return data_of(server, "GET", "/api/customer/guest/complaints",
                   customer_token=session["customer_token"], guest_key=KEY)


def _my_orders(server, session):
    return data_of(server, "GET", "/api/customer/orders",
                   customer_token=session["customer_token"])


# ---------------------------------------------------------------- 规则层
class TestIntentLayer:
    def test_order_dish_在子意图白名单(self):
        assert "order_dish" in SUBS

    @pytest.mark.parametrize("text,wants", [
        ("青椒肉丝", False),
        ("来个青椒肉丝", True),
        ("点一份青椒肉丝", True),
        ("我要青椒肉丝", True),
    ])
    def test_菜名归点单且区分是否明确购买(self, text, wants):
        r = classify_intent(text, dish_names=(DISH_NAME,))
        assert r["intent"] == "menu_query"
        assert r["sub"] == "order_dish"
        assert ("order_intent" in r["signals"]) is wants

    def test_菜名不抢价格与推荐(self):
        assert classify_intent("青椒肉丝多少钱",
                               dish_names=(DISH_NAME,))["sub"] == "price"
        assert classify_intent("推荐个不辣的",
                               dish_names=(DISH_NAME,))["sub"] == "recommend"

    def test_食安信号优先于菜名(self):
        r = classify_intent("青椒肉丝里有头发", dish_names=(DISH_NAME,))
        assert r["intent"] == "complaint"

    @pytest.mark.parametrize("text", ["我要下单", "点菜", "下单吧", "我要点菜"])
    def test_想下单但没说菜名也归点单(self, text):
        r = classify_intent(text, has_order=True, dish_names=(DISH_NAME,))
        assert r["intent"] == "menu_query"
        assert r["sub"] == "order_dish"
        assert "order_intent_no_dish" in r["signals"]

    def test_不传菜名时行为不变(self):
        """老调用方（handler/llm）不传 dish_names，语义必须与修复前一致。"""
        assert classify_intent("青椒肉丝")["intent"] == "other"
        assert classify_intent("青椒肉丝", has_order=True)["intent"] == "complaint"


# ---------------------------------------------------------------- 裸菜名
class TestMentionOnly:
    def test_有订单时裸菜名不建工单(self, server):
        s = _open(server)
        _order(server, s["customer_token"])
        reply = _chat(server, s, DISH_NAME)
        assert reply["intent"] == "menu_query"
        assert reply["sub"] == "order_dish"
        assert reply["routed"] is True
        assert reply["data"]["confirmed"] is False
        assert DISH_NAME in reply["reply"]
        assert "¥" in reply["reply"]
        assert _complaints(server, s)["total"] == 0

    def test_无订单时裸菜名也不建工单(self, server):
        s = _open(server)
        reply = _chat(server, s, DISH_NAME)
        assert reply["sub"] == "order_dish"
        assert _complaints(server, s)["total"] == 0
        assert _my_orders(server, s)["total"] == 0  # 没擅自下单

    def test_回复给可复制的确认指令(self, server):
        s = _open(server)
        reply = _chat(server, s, DISH_NAME)
        assert f"点{DISH_NAME}" in reply["reply"]


# ---------------------------------------------------------------- 明确点单
class TestExplicitOrder:
    @pytest.mark.parametrize("text", ["点青椒肉丝", "来一份青椒肉丝", "我要青椒肉丝"])
    def test_明确点单真下单且不建工单(self, server, text):
        s = _open(server)
        reply = _chat(server, s, text)
        assert reply["sub"] == "order_dish"
        assert reply["action"] == "order"
        assert reply["data"]["confirmed"] is True
        assert reply["data"]["order_no"]
        assert "已帮您下单" in reply["reply"]
        assert _complaints(server, s)["total"] == 0
        assert _my_orders(server, s)["total"] == 1

    def test_下单归属当前顾客(self, server):
        s = _open(server)
        reply = _chat(server, s, "点青椒肉丝")
        orders = _my_orders(server, s)["orders"]
        assert orders[0]["order_no"] == reply["data"]["order_no"]

    def test_从报价话术照着说能下单(self, server):
        """关键闭环：顾客按上一句给的指令回，不能再掉进兜底。"""
        s = _open(server)
        quoted = _chat(server, s, DISH_NAME)
        assert quoted["data"]["confirmed"] is False
        confirm = _chat(server, s, f"点{DISH_NAME}")
        assert confirm["data"]["confirmed"] is True
        assert _complaints(server, s)["total"] == 0

    @pytest.mark.parametrize("text", ["我要下单", "点菜"])
    def test_没说菜名时引导而不是建工单(self, server, text):
        s = _open(server)
        reply = _chat(server, s, text)
        assert reply["sub"] == "order_dish"
        assert reply["data"].get("confirmed") is not True
        assert "菜名" in reply["reply"]
        assert _complaints(server, s)["total"] == 0
        assert _my_orders(server, s)["total"] == 0

    def test_问价格不会被误下单(self, server):
        s = _open(server)
        reply = _chat(server, s, "青椒肉丝多少钱")
        assert reply["sub"] == "price"
        assert _my_orders(server, s)["total"] == 0


# ---------------------------------------------------------------- 边界
class TestBoundaries:
    def test_真投诉仍建工单(self, server):
        s = _open(server)
        _order(server, s["customer_token"])
        reply = _chat(server, s, "青椒肉丝里有头发，我要投诉")
        assert reply["intent"] == "complaint"
        assert _complaints(server, s)["total"] == 1
        assert _my_orders(server, s)["total"] == 1  # 投诉不会顺手下新单

    def test_不存在的菜不乱下单(self, server):
        s = _open(server)
        reply = _chat(server, s, "点一份佛跳墙")
        # 没有这道菜 → 走通用兜底，不建工单也不下单
        assert "佛跳墙" not in str(reply["data"].get("code") or "")
        assert reply["data"].get("confirmed") is not True

    def test_话术不含禁语(self, server):
        s = _open(server)
        for text in (DISH_NAME, "点青椒肉丝"):
            reply = _chat(server, s, text)
            assert "马上" not in reply["reply"]
            assert "已经给您退" not in reply["reply"]
