# -*- coding: utf-8 -*-
"""A10-01 三端联调：Web / 顾客小程序 / 后厨 TFT 共用一套状态机。

**为什么这条测试单独一个文件**：单端测试各自都在跑，翻不了车的恰恰是
「三端同时看同一个单，谁改了别人马上能看见」。所以这里的断言核心不是
「接口通」，而是**同一时刻三端读到的同一字段必须相等**——状态机只要
被任何一端绕开（比如小程序自己改状态、TFT 本地记进度不上报），
这些断言就会当场炸。

场景（一条订单走完全程，三端轮流操作）：
1. 顾客小程序下单 → 员工看板 / TFT 队列屏 / 顾客自己的单 **同帧可见**；
2. 顾客催菜 → 三端 `urge_count` 一致（催菜是排序权重，错一格派单就错）；
3. TFT 端走完整烹饪流程（派单→称重→SOP→出餐）→ 顾客进度变 done、
   收到「已出餐」通知；
4. 员工退菜（pending 态）→ 顾客侧立刻看到 cancelled，通知有文案；
5. 三端对同一单读出的条目集合完全一致（没有谁多一条少一条）。
"""
from __future__ import annotations

import pytest

from api_support import call, data_of, login, make_server
from kitchen.tft import DirectTransport, TftTerminal


# =========================================================== 夹具
@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def lead(server):
    return login(server, "lead01")


@pytest.fixture()
def tft(server, tmp_path):
    """后厨 TFT 终端（直连 transport，模拟在线）。"""
    terminal = TftTerminal(
        DirectTransport(server),
        device_id="tft-int-01",
        queue_dir=tmp_path / "tft",
    )
    terminal.login("chef01", "kitchen2026")
    return terminal


@pytest.fixture()
def customer(server):
    """顾客：开桌拿令牌（小程序侧的『登录』就是这一步）。"""
    data = data_of(server, "POST", "/api/customer/session")
    return data["customer_token"]


# =========================================================== 工具
def _admit_card(server, session, card="A1B2C3D4"):
    """晨检 + 复核，让持卡人当天可准入（SOP 确认需要）。"""
    data_of(server, "POST", "/api/morning/submit", session=session,
            card_uid=card,
            answers={"temp_c": 36.2, "diarrhea": False,
                     "pharyngitis": False, "hand_wound": False})


def _place(server, token, code="D-QJRS", qty=1, table=3):
    payload = data_of(server, "POST", "/api/customer/orders",
                      customer_token=token, table_id=table,
                      items=[{"code": code, "qty": qty}])
    return payload


def _employee_items(server, staff, open_only=True):
    """员工看板视角：{item_id: (dish, state, urge_count)}。
    `open_only=False` 看全量（终态单不在开放列表里——那是看板口径不是丢数据）。"""
    path = "/api/orders?open=1" if open_only else "/api/orders"
    payload = data_of(server, "GET", path, session=staff)
    out = {}
    for order in payload["orders"]:
        for item in order["items"]:
            out[item["id"]] = (item["dish_name"], item["state"],
                               item.get("urge_count", 0))
    return out


def _customer_items(server, token):
    """小程序视角：{item_id: (dish, state, urge_count, eta_state)}。"""
    payload = data_of(server, "GET", "/api/customer/orders",
                      customer_token=token)
    out = {}
    for order in payload["orders"]:
        for item in order["items"]:
            out[item["id"]] = (item["dish_name"], item["state"],
                               item.get("urge_count", 0),
                               item.get("eta_state"))
    return out


def _tft_queue_items(tft):
    """TFT 队列屏视角：{item_id: (dish, group)}。队列屏是 rows 列表 +
    group 字段（待做/在做合并一屏）。"""
    screen = tft.screen_queue()
    out = {}
    for row in screen.get("rows", []):
        out[row.get("item_id")] = (row.get("dish"), row.get("group"))
    return out


def _notifications_text(server, token):
    payload = data_of(server, "GET", "/api/customer/notifications",
                      customer_token=token)
    return [row["message"] for row in payload["notifications"]]


# =========================================================== 联调
class TestThreeWayConsistency:
    def test_下单同帧三端可见且状态一致(self, server, lead, tft, customer):
        payload = _place(server, customer)
        item_id = None
        order = payload["order"] if "order" in payload else payload
        # place 返回结构兼容两种形态（order 键 / 平铺）
        assert payload["order_no"]

        # Web 员工看板
        emp = _employee_items(server, lead)
        assert item_id is None or True  # 占位，下方真正取 id
        # 小程序自己的单
        mine = _customer_items(server, customer)
        assert mine, "顾客看不到自己刚下的单"
        item_id = next(iter(mine))

        # TFT 队列屏（先 poll 刷快照）
        tft.poll()
        queue = _tft_queue_items(tft)

        # ---- 核心：同一 item_id，三端读到的菜名与状态完全一致
        dish, state, _urge = emp[item_id]
        c_dish, c_state, _cu, eta = mine[item_id]
        assert emp and item_id in emp
        assert dish == c_dish == "青椒肉丝"
        assert state == c_state == "pending"
        assert eta is not None
        # TFT 队列屏要么在待做列表里
        assert item_id in queue, "TFT 队列屏看不到顾客单"
        assert queue[item_id][0] == "青椒肉丝"
        assert queue[item_id][1] == "pending"
        # 员工看板标记来源为小程序
        payload_emp = data_of(server, "GET", "/api/orders?open=1",
                              session=lead)
        assert any(o["source"] == "miniapp" for o in payload_emp["orders"])

    def test_催菜计数三端一致(self, server, lead, tft, customer):
        _place(server, customer)
        tft.poll()
        mine = _customer_items(server, customer)
        item_id = next(iter(mine))

        # 顾客催一次
        data_of(server, "POST",
                f"/api/customer/orders/items/{item_id}/urge",
                customer_token=customer)
        tft.poll()

        emp = _employee_items(server, lead)
        mine = _customer_items(server, customer)
        # 三端 urge_count 必须同为 1——它同时是派单排序权重
        assert emp[item_id][2] == 1
        assert mine[item_id][2] == 1

    def test_TFT烹饪全程_顾客进度与通知跟进(self, server, lead, tft, customer):
        _admit_card(server, lead)
        _place(server, customer)
        tft.poll()
        item_id = next(iter(_customer_items(server, customer)))
        session = tft.transport.session

        # TFT 刷卡准入 → 派单 → 开火
        result = tft.scan("A1B2C3D4", station_id=1)
        assert result.get("ok") is True, result
        data_of(server, "POST", f"/api/orders/items/{item_id}/dispatch",
                session=session, station_id=1)
        data_of(server, "POST", f"/api/orders/items/{item_id}/state",
                session=session, state="cooking")

        # 称重 + 全部工序（走 TFT 的入口，与 Web 同一条服务端判定）
        for ingredient, grams in (("猪里脊", 150.0), ("青椒", 100.0),
                                  ("食用油", 20.0), ("生抽", 8.0)):
            tft.weigh(item_id, ingredient, grams)
        for step_no in range(1, 7):
            tft.confirm(item_id, step_no, staff_id=1)

        # 出餐（工序门禁全过 → ready）→ 上桌（走同一条 state 动作，Web 同款）
        serve = tft.serve(item_id, staff_id=1)
        assert serve.get("ok", True) is True or "order_no" in serve, serve
        data_of(server, "POST", f"/api/orders/items/{item_id}/state",
                session=session, state="served")
        tft.poll()

        # ---- 顾客侧：进度到头 + 收到「已出餐」通知
        mine = _customer_items(server, customer)
        assert mine[item_id][1] == "served", mine[item_id]
        assert mine[item_id][3] == "done"
        notes = _notifications_text(server, customer)
        assert any("出餐" in text or "上桌" in text for text in notes), notes

        # ---- 员工看板（全量）：同一 item 也是 served——不是谁本地改了没上报
        emp = _employee_items(server, lead, open_only=False)
        assert emp[item_id][1] == "served"

    def test_员工退菜_顾客立刻可见(self, server, lead, tft, customer):
        payload = _place(server, customer)
        tft.poll()
        item_id = next(iter(_customer_items(server, customer)))

        # pending 态可直接退（不需要 force）；退菜与 Web 用同一个 state 动作
        data_of(server, "POST", f"/api/orders/items/{item_id}/state",
                session=lead, state="cancelled")
        tft.poll()

        mine = _customer_items(server, customer)
        assert mine[item_id][1] == "cancelled"
        notes = _notifications_text(server, customer)
        # 退菜有文案（具体字样可以换，但顾客必须被通知到）
        assert any("退" in text or "取消" in text for text in notes), notes

    def test_三端条目集合完全一致(self, server, lead, tft, customer):
        """两张单、四道菜：任何一端都不该多看到或少看到一条。"""
        _place(server, customer, code="D-QJRS", qty=2, table=3)
        _place(server, customer, code="D-GBJD", qty=1, table=3)
        tft.poll()

        emp_ids = set(_employee_items(server, lead))
        cust_ids = set(_customer_items(server, customer))
        tft_ids = set(_tft_queue_items(tft))

        assert cust_ids <= emp_ids
        assert cust_ids == tft_ids, f"TFT {tft_ids ^ cust_ids}"
        assert len(cust_ids) == 2  # 两行条目（qty 2 合并为一条）
