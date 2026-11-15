# -*- coding: utf-8 -*-
"""顾客归属信息的跨端一致性（A9 补强）。

这份测试回答三个具体问题，别把它当成「又一个下单能不能成功」的用例：

1. 管理端 / 后厨屏能不能知道「这单是哪一桌、哪一次开桌下的」？
   → 能，靠开桌时下发的脱敏编号 `customer_ref`（T03-A1B2）。
2. 能不能知道「这个顾客是谁」？
   → 不能。系统里没有姓名、手机号、openid、会员号；测试逐个钉死这些字段
     **不存在**，防止后来有人顺手加个 nickname 把口径改了却没人发现。
3. 编号会不会串到别的桌？
   → 不会。顾客通知端点按 order_no 白名单过滤，且不下发 customer_ref。

`customer_ref` 只在本次服务进程内有效，不落库。这不是偷懒：匿名开桌本来
就没有可持久化的主体，硬写进 `orders` 表只会制造「看起来能追溯、重启后
全空」的假象。真要按人识别，得另做会员体系（设计文档 09 §一明确不做）。
"""
from __future__ import annotations

import re

import pytest

from api_support import call, data_of, login, make_server  # noqa: F401
from kitchen.tft.render import render_frame
from kitchen.tft.screens import queue_screen_from_orders

DISH = "D-QJRS"


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def chef(server):
    return login(server, "chef01")


def _open(server, table=None):
    kwargs = {} if table is None else {"table_id": table}
    return data_of(server, "POST", "/api/customer/session", **kwargs)


def _place(server, token, table=3):
    return data_of(server, "POST", "/api/customer/orders",
                   customer_token=token, table_id=table,
                   items=[{"code": DISH, "qty": 1}])


def _find_order(data, order_no):
    return next(row for row in data["orders"] if row["order_no"] == order_no)


# ---------------------------------------------------------------- 编号形状
class TestRefShape:
    def test_开桌发脱敏编号且不含完整令牌(self, server):
        session = _open(server, 3)
        assert re.fullmatch(r"T03-[0-9A-F]{4}", session["customer_ref"])
        assert session["table_id"] == 3
        # 只暴露令牌前 4 位，完整令牌不能出现在任何下发字段里
        assert session["customer_ref"].split("-")[1].lower() in session["customer_token"]
        assert session["customer_token"] not in str(
            {k: v for k, v in session.items() if k != "customer_token"})
        assert "匿名" in session["identity_note"]

    def test_不带桌号是散客编号(self, server):
        session = _open(server)
        assert re.fullmatch(r"G-[0-9A-F]{4}", session["customer_ref"])
        assert session["table_id"] is None

    def test_脏桌号不进订单(self, server):
        for bad in ("abc", 0, 9999, "", None):
            session = data_of(server, "POST", "/api/customer/session", table_id=bad)
            assert session["customer_ref"].startswith("G-"), bad

    def test_同一桌两次开桌编号不同(self, server):
        first, second = _open(server, 3), _open(server, 3)
        assert first["customer_ref"] != second["customer_ref"]


# ---------------------------------------------------------------- 其他端可见
class TestCrossEnds:
    def test_管理端订单带顾客编号与桌台(self, server, chef):
        session = _open(server, 3)
        placed = _place(server, session["customer_token"], 3)

        data = data_of(server, "GET", "/api/orders", session=chef)
        row = _find_order(data, placed["order_no"])
        assert row["customer_ref"] == session["customer_ref"] == placed["customer_ref"]
        assert row["table_label"] == "3 号桌"
        assert row["source"] == "miniapp"
        # 边界：能认出「哪桌哪次开桌」，但认不出「这个人是谁」
        for key in ("customer_name", "name", "phone", "openid",
                    "member_id", "nickname", "customer_token"):
            assert key not in row

    def test_员工手工单没有顾客编号(self, server, chef):
        placed = data_of(server, "POST", "/api/orders", session=chef,
                         items=[{"code": DISH, "qty": 1}])["order"]
        data = data_of(server, "GET", "/api/orders", session=chef)
        row = _find_order(data, placed["order_no"])
        assert row["customer_ref"] is None
        assert row["source"] == "web"

    def test_审计留痕带编号但不带令牌(self, server):
        session = _open(server, 3)
        _place(server, session["customer_token"], 3)
        # 审计是 param.write 权限，operator（chef01）读不了，用 manager
        data = data_of(server, "GET", "/api/audit?limit=50",
                       session=login(server, "admin"))
        rows = [row for row in data["entries"] if row["action"] == "customer.order"]
        assert rows
        assert rows[-1]["after"]["customer_ref"] == session["customer_ref"]
        assert rows[-1]["after"]["table_id"] == 3
        assert session["customer_token"] not in str(rows[-1])

    def test_后厨队列屏带桌号并真的渲染出来(self, server, chef):
        session = _open(server, 5)
        _place(server, session["customer_token"], 5)
        data = data_of(server, "GET", "/api/orders", session=chef)

        screen = queue_screen_from_orders(data, status={})
        assert screen["rows"], "顾客单应出现在后厨队列"
        row = screen["rows"][0]
        assert row["customer_ref"] == session["customer_ref"]
        assert row["table_short"] == "5桌"
        assert "5桌" in render_frame(screen)


# ---------------------------------------------------------------- 归属隔离
class TestIsolation:
    def test_顾客能查到自己的编号_通知里拿不到别桌编号(self, server):
        a, b = _open(server, 3), _open(server, 4)
        _place(server, a["customer_token"], 3)
        _place(server, b["customer_token"], 4)

        mine = data_of(server, "GET", "/api/customer/orders",
                       customer_token=a["customer_token"])
        assert mine["customer_ref"] == a["customer_ref"]
        assert mine["table_id"] == 3
        assert mine["total"] == 1
        assert mine["orders"][0]["customer_ref"] == a["customer_ref"]

        notes = data_of(server, "GET", "/api/customer/notifications",
                        customer_token=a["customer_token"], since=0)
        for note in notes["notifications"]:
            assert "customer_ref" not in note
            assert b["customer_ref"] not in str(note)
            assert a["customer_token"] not in str(note)

    def test_令牌失效后编号随之失效(self, tmp_path):
        first = make_server(tmp_path)
        session = _open(first, 3)
        second = make_server(tmp_path / "again")
        status, _ = call(second, "GET", "/api/customer/orders",
                         customer_token=session["customer_token"])
        assert status == 401
        with pytest.raises(Exception):
            second.state.customer.profile_of(session["customer_token"])
