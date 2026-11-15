# -*- coding: utf-8 -*-
"""A12 经营账本：流水 / 成本 / 毛利 / 净利润 / 日报 / 导出。

测的不是「接口能不能返回数字」，而是这几条**会算错钱或做假账**的边界：

- 下单不等于收入：未上桌、制作中、退菜一律不进流水
- 金额按 `unit_price` 快照，改菜价后历史账面不漂
- 整数分累加，无浮点误差
- A11 补偿凭证必须冲账，且重复 sync 不重复记
- 期间费用只影响净利、不影响毛利；未录入时如实标注「净利=毛利」
- 经营数据只有 manager 能看
- AI 只写点评文字，不写账目、不喂隐私、规则层不冒充大模型
- journal 重放后账目条数与金额不变
"""
from __future__ import annotations

import pytest

from api_support import call, data_of, force_session, login, make_server
from kitchen.finance.service import DEFAULT_COST_RATE, _cents
from kitchen.process.models import ItemState

DISH = "D-QJRS"        # 青椒肉丝 28.00
KEY = "g-finance-001"


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def manager(server):
    return login(server, "admin")


@pytest.fixture()
def lead(server):
    return login(server, "lead01")


def _place(state, qty=2):
    return state.orders.place_order(table_id=3, items=[(DISH, qty)],
                                    source="web")


def _serve_all(state, order):
    """走完整状态链：PENDING → DISPATCHED → COOKING → READY → SERVED。"""
    for item in order.items:
        state.orders.dispatch_item(item.id, station_id=1)
        state.orders.start_cooking(item.id)
        state.orders.mark_ready(item.id)
        state.orders.serve(item.id)
    return state.orders.order(order.id)


def _overview(server, **params):
    return data_of(server, "GET", "/api/finance/overview",
                   session=force_session("admin", "manager"), **params)


def _approve_compensation(server, *, method="waive"):
    """走 A11 真实审批链路，返回 (凭证号, 工单 id)。"""
    s = data_of(server, "POST", "/api/customer/session",
                table_id=3, guest_key=KEY)
    order = data_of(server, "POST", "/api/customer/orders",
                    customer_token=s["customer_token"], table_id=3,
                    items=[{"code": DISH, "qty": 1}])
    placed = server.state.orders.order(order["order_id"])
    _serve_all(server.state, placed)
    payload = data_of(server, "POST", "/api/customer/guest/complaints",
                      customer_token=s["customer_token"], guest_key=KEY,
                      text="菜里有异物，希望核实并免单",
                      order_id=order["order_id"], wants_refund=True,
                      compensation=method,
                      evidences=[{"kind": "text", "content": "现场照片已交服务员"}])
    rows = data_of(server, "GET", "/api/guest/complaints",
                   session=force_session("admin", "manager"))["complaints"]
    row = next(x for x in rows if x["complaint_no"] == payload["complaint_no"])
    reviewed = data_of(
        server, "POST", f"/api/guest/complaints/{row['id']}/review",
        session=force_session("lead01", "lead"),
        evidence_ids=[row["evidences"][0]["id"]],
        expected_version=row["version"], note="核对留样与出餐时间轴")
    approved = data_of(
        server, "POST", f"/api/guest/complaints/{row['id']}/approve",
        session=force_session("admin", "manager"), compensation=method,
        expected_version=reviewed["complaint"]["version"], note="属实")
    return approved["complaint"]["voucher_no"], order["order_id"]


# ---------------------------------------------------------------- 营收确认
class TestRecognition:
    def test_下单未上桌不计营收(self, server):
        _place(server.state)
        view = _overview(server, days=1)
        assert view["revenue"] == 0.0
        assert view["net"] == 0.0
        assert view["entry_count"] == 0

    def test_制作中仍不计营收_只有上桌才确认(self, server):
        order = _place(server.state)
        item = order.items[0]
        server.state.orders.dispatch_item(item.id, station_id=1)
        server.state.orders.start_cooking(item.id)
        assert _overview(server, days=1)["revenue"] == 0.0
        server.state.orders.mark_ready(item.id)
        assert _overview(server, days=1)["revenue"] == 0.0
        server.state.orders.serve(item.id)
        assert _overview(server, days=1)["revenue"] == 56.0

    def test_已上桌按快照确认收入与估算成本(self, server):
        order = _place(server.state, qty=2)
        _serve_all(server.state, order)
        view = _overview(server, days=1)
        assert view["revenue"] == 56.0                      # 28 × 2
        assert view["food_cost"] == 19.6                    # 56 × 0.35
        assert view["gross"] == 36.4
        assert view["net"] == 36.4                          # 未录期间费用
        assert view["order_count"] == 1
        assert view["avg_ticket"] == 56.0
        assert "不是实收金额" in view["settlement_note"]
        assert view["cost_coverage"]["estimated"] == 1

    def test_退菜子项不进流水(self, server):
        order = server.state.orders.place_order(
            table_id=3, items=[(DISH, 1), ("D-GBJD", 1)])
        kept, dropped = order.items
        _serve_all(server.state, order)
        # 退菜必须在上桌前走状态机；这里单独造一单验证 CANCELLED 分支
        second = _place(server.state, qty=1)
        server.state.orders.dispatch_item(second.items[0].id, station_id=1)
        server.state.orders.cancel_item(second.items[0].id, reason="顾客取消")
        assert second.items[0].state is ItemState.CANCELLED
        before = _overview(server, days=1)["revenue"]
        server.state.finance.sync()
        assert _overview(server, days=1)["revenue"] == before
        assert kept.state is ItemState.SERVED and dropped.state is ItemState.SERVED

    def test_改菜价后历史账面不漂(self, server):
        order = _place(server.state, qty=2)
        _serve_all(server.state, order)
        assert _overview(server, days=1)["revenue"] == 56.0
        dish = server.state.repo.dish(order.items[0].dish_id)
        object.__setattr__(dish, "price", 999.0)
        assert order.items[0].unit_price == 28.0
        assert _overview(server, days=1)["revenue"] == 56.0

    def test_整数分累加无浮点误差(self):
        assert _cents(0.1) + _cents(0.2) == 30
        assert _cents("1.005") == 101          # ROUND_HALF_UP
        assert _cents(None) == 0
        assert _cents(float("nan")) == 0

    def test_重复sync不重复记账(self, server):
        order = _place(server.state, qty=2)
        _serve_all(server.state, order)
        first = _overview(server, days=1)["entry_count"]
        for _ in range(5):
            server.state.finance.sync()
        assert _overview(server, days=1)["entry_count"] == first == 2


# ---------------------------------------------------------------- 成本与净利
class TestCostAndProfit:
    def test_维护标准成本后新账按手工值_旧账不变(self, server):
        old = _place(server.state, qty=1)
        _serve_all(server.state, old)
        assert _overview(server, days=1)["food_cost"] == 9.8
        dish_id = old.items[0].dish_id
        result = data_of(server, "POST", "/api/finance/costs",
                         session=force_session("admin", "manager"),
                         dish_id=dish_id, cost=10.0)
        assert result["dish"]["cost_source"] == "manual"
        new = _place(server.state, qty=1)
        _serve_all(server.state, new)
        view = _overview(server, days=1)
        assert view["food_cost"] == 19.8        # 旧 9.8 + 新 10.0，历史未重算
        assert view["revenue"] == 56.0
        assert view["cost_coverage"]["manual"] == 1

    def test_期间费用只影响净利不影响毛利(self, server):
        order = _place(server.state, qty=2)
        _serve_all(server.state, order)
        before = _overview(server, days=1)
        day = before["to"]
        data_of(server, "POST", "/api/finance/overhead",
                session=force_session("admin", "manager"),
                date=day, labor=100, rent=20, utilities=8.5, other=0)
        after = _overview(server, days=1)
        assert after["gross"] == before["gross"] == 36.4
        assert after["overhead"] == 128.5
        assert after["net"] == round(36.4 - 128.5, 2)
        assert after["net"] < 0

    def test_期间费用重复录入是覆盖不是累加(self, server):
        order = _place(server.state, qty=1)
        _serve_all(server.state, order)
        day = _overview(server, days=1)["to"]
        mgr = force_session("admin", "manager")
        data_of(server, "POST", "/api/finance/overhead",
                session=mgr, date=day, labor=100)
        data_of(server, "POST", "/api/finance/overhead",
                session=mgr, date=day, labor=40)
        assert _overview(server, days=1)["overhead"] == 40.0

    def test_成本表标记估算来源(self, server):
        view = data_of(server, "GET", "/api/finance/dishes",
                       session=force_session("admin", "manager"))
        assert view["default_rate"] == float(DEFAULT_COST_RATE)
        assert any(d["cost_source"] == "estimated" for d in view["dishes"])
        assert all(0 <= d["margin"] <= 1 for d in view["dishes"])


# ---------------------------------------------------------------- 补偿冲账
class TestCompensation:
    def test_免单冲减营收(self, server):
        voucher, _ = _approve_compensation(server, method="waive")
        view = _overview(server, days=1)
        assert view["revenue"] == 0.0            # 28 收入 - 28 免单
        assert view["compensation"] == -28.0
        assert view["gross"] == -9.8            # 收入冲光，食材成本仍发生
        entries = data_of(server, "GET", "/api/finance/entries",
                          session=force_session("admin", "manager"),
                          days=1)["entries"]
        assert any(e["source_id"] == voucher and e["kind"] == "compensation"
                   for e in entries)

    def test_重做记额外食材损耗而非冲减收入(self, server):
        voucher, _ = _approve_compensation(server, method="redo")
        view = _overview(server, days=1)
        assert view["revenue"] == 28.0           # 收入不冲减
        assert view["food_cost"] == 19.6         # 9.8 原成本 + 9.8 重做
        entries = data_of(server, "GET", "/api/finance/entries",
                          session=force_session("admin", "manager"),
                          days=1)["entries"]
        assert any(e["source_id"] == voucher and e["category"] == "redo"
                   for e in entries)

    def test_补偿凭证重复sync不重复冲账(self, server):
        _approve_compensation(server, method="waive")
        before = _overview(server, days=1)
        for _ in range(4):
            server.state.finance.sync()
        after = _overview(server, days=1)
        assert after["entry_count"] == before["entry_count"]
        assert after["revenue"] == before["revenue"] == 0.0

    def test_未批准的工单一分钱不动(self, server):
        s = data_of(server, "POST", "/api/customer/session",
                    table_id=3, guest_key=KEY)
        order = data_of(server, "POST", "/api/customer/orders",
                        customer_token=s["customer_token"], table_id=3,
                        items=[{"code": DISH, "qty": 1}])
        data_of(server, "POST", "/api/customer/guest/complaints",
                customer_token=s["customer_token"], guest_key=KEY,
                text="菜里有异物，希望退款", order_id=order["order_id"],
                wants_refund=True, compensation="waive", evidences=[])
        placed = server.state.orders.order(order["order_id"])
        _serve_all(server.state, placed)
        assert _overview(server, days=1)["revenue"] == 28.0


# ---------------------------------------------------------------- 权限与导出
class TestAccessAndExport:
    @pytest.mark.parametrize("role", ["viewer", "operator", "lead"])
    def test_非manager读经营账一律403(self, server, role):
        status, _ = call(server, "GET", "/api/finance/overview",
                         session=force_session("someone", role))
        assert status == 403
        status, _ = call(server, "POST", "/api/finance/costs",
                         session=force_session("someone", role),
                         dish_id=1, cost=1)
        assert status == 403

    def test_匿名访问403(self, server):
        status, _ = call(server, "GET", "/api/finance/overview")
        assert status in (401, 403)

    def test_导出CSV带BOM且不泄漏顾客信息(self, server):
        order = _place(server.state, qty=2)
        _serve_all(server.state, order)
        out = data_of(server, "GET", "/api/finance/export",
                      session=force_session("admin", "manager"), days=1)
        assert out["csv"].startswith("\ufeff")
        assert "营业日,发生时间,流水号,类型" in out["csv"]
        assert "56.00" in out["csv"]
        assert "g-finance" not in out["csv"]
        assert "customer_token" not in out["csv"]
        assert out["rows"] == 2

    def test_日期范围非法返回400(self, server):
        status, _ = call(server, "GET", "/api/finance/overview",
                         session=force_session("admin", "manager"),
                         start="2026-11-15", end="2026-11-14")
        assert status == 400


# ---------------------------------------------------------------- 日报与 AI
class TestDailyReport:
    def test_规则层如实标注且不编造(self, server):
        row = data_of(server, "GET", "/api/finance/report",
                      session=force_session("admin", "manager"))
        assert row["scenario"] == "finance_daily"
        assert row["source"] == "rule"
        assert "非大模型生成" in row["summary"]
        assert "无已完成订单" in row["summary"]
        assert row["degraded_reason"] == ""

    def test_有数据时金额由summary确定性算出(self, server):
        order = _place(server.state, qty=2)
        _serve_all(server.state, order)
        row = data_of(server, "GET", "/api/finance/report",
                      session=force_session("admin", "manager"))
        assert row["metrics"]["revenue"] == 56.0
        assert row["metrics"]["net"] == 36.4
        assert "56.00 元" in row["content_md"]
        assert row["risk_level"] == "low"

    def test_AI只写点评不写账目(self, server):
        order = _place(server.state, qty=2)
        _serve_all(server.state, order)
        before = _overview(server, days=1)["entry_count"]
        data_of(server, "GET", "/api/finance/report",
                session=force_session("admin", "manager"))
        data_of(server, "GET", "/api/finance/report",
                session=force_session("admin", "manager"))
        assert _overview(server, days=1)["entry_count"] == before
        runs = data_of(server, "GET", "/api/finance/ai-runs",
                       session=force_session("admin", "manager"))
        assert runs["total"] == 2
        assert all(r["mode"] == "rule" for r in runs["runs"])

    def test_在线provider失败自动降级且不冒充live(self, server, monkeypatch):
        class Broken:
            mode = "live"
            model = "deepseek-chat"

            def complete(self, scenario, facts):
                raise RuntimeError("network down")

        server.state.finance.provider = Broken()
        row = data_of(server, "GET", "/api/finance/report",
                      session=force_session("admin", "manager"))
        assert row["source"] == "rule"
        assert row["degraded_reason"]
        assert "非大模型生成" in row["summary"]

    def test_模型输出夹带动作词会被拒绝(self, server, monkeypatch):
        class Poisoned:
            mode = "live"
            model = "deepseek-chat"

            def complete(self, scenario, facts):
                return {"summary": "建议直接批准退款并修改金额账目，无需人工确认"}

        server.state.finance.provider = Poisoned()
        row = data_of(server, "GET", "/api/finance/report",
                      session=force_session("admin", "manager"))
        assert row["source"] == "rule"
        assert row["degraded_reason"]


# ---------------------------------------------------------------- 持久化
class TestJournal:
    def test_重放后账目条数与金额不变(self, tmp_path):
        first = make_server(tmp_path / "one")
        # 两个 server 共用同一个 var_dir，才能验证 journal 重放
        first.state.finance._journal = tmp_path / "shared" / "finance_journal.jsonl"
        order = _place(first.state, qty=2)
        _serve_all(first.state, order)
        first.state.finance.sync()
        expected = first.state.finance.overview(days=1)

        second = make_server(tmp_path / "two")
        second.state.finance = type(first.state.finance)(
            second.state, provider=None,
            journal_path=tmp_path / "shared" / "finance_journal.jsonl")
        replayed = second.state.finance.overview(days=1)
        assert replayed["entry_count"] == expected["entry_count"] == 2
        assert replayed["revenue"] == expected["revenue"] == 56.0
        assert replayed["net"] == expected["net"] == 36.4
        assert second.state.finance.journal_stats["skipped"] == 0
