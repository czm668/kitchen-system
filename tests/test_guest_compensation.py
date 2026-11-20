# -*- coding: utf-8 -*-
"""客诉模块 SOP 能力测试：补偿矩阵 / 驳回对客隔离 / 结案回访。

三条要守住的东西：

1. **补偿不能由人凭感觉选**——分类决定标准方案，级别决定上限，
   不可兑现的方案（`points`）在服务端硬拦，不靠培训纪律；
2. **内部口径不能推给顾客**——「金额超过快照上限」这类话只进审计，
   顾客看到的是「按本单金额重新申请」；
3. **结案要能收到评价**——且低分当天就通知，不等日报。
"""
from __future__ import annotations

import pytest

from api_support import call, data_of, login, make_server
from kitchen.guest import compensation
from kitchen.guest.llm import COMPENSATION_LABELS

DISH = "D-QJRS"          # 青椒肉丝 ¥28
KEY = "g-cmp-001"


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


def _open(server, key=KEY):
    return data_of(server, "POST", "/api/customer/session",
                   table_id=3, guest_key=key, tags=[])


def _order(server, session):
    return data_of(server, "POST", "/api/customer/orders",
                   customer_token=session["customer_token"], table_id=3,
                   items=[{"code": DISH, "qty": 1}])


def _submit(server, session, text, *, key=KEY, **kw):
    return data_of(server, "POST", "/api/customer/guest/complaints",
                   customer_token=session["customer_token"], guest_key=key,
                   text=text, **kw)


def _row(server, manager, no):
    rows = data_of(server, "GET", "/api/guest/complaints",
                   session=manager)["complaints"]
    return next(c for c in rows if c["complaint_no"] == no)


def _paid(server, manager, text, *, key=KEY, evidence=True):
    """提交 → 拿到带 id 的工单行。"""
    s = _open(server, key)
    order = _order(server, s)
    submitted = _submit(
        server, s, text, key=key, order_id=order["order_id"],
        wants_refund=True,
        evidences=[{"kind": "photo", "content": "现场照片"}] if evidence else [])
    return s, _row(server, manager, submitted["complaint_no"])


# ---------------------------------------------------------------- 补偿矩阵
class TestMatrix:
    def test_食安必须走L3且免单(self):
        a = compensation.assess(category="异物", level="self_service", amount=28)
        assert a["tier"] == "L3"          # 分类压过模型判的级别
        assert a["suggest"] == "waive"
        assert a["cap"] == 28.0
        assert a["decide_by"] == "店长"

    def test_上菜慢只加急不免单(self):
        a = compensation.assess(category="上菜慢", level="self_service", amount=28)
        assert a["tier"] == "L1" and a["suggest"] == "priority"
        assert a["cap"] == 0.0            # L1 不涉钱

    def test_L2上限按比例与绝对值双约束(self):
        a = compensation.assess(category="服务态度", level="ai_plus_human", amount=200)
        assert a["tier"] == "L2"
        assert a["cap"] == 50.0           # 50% = 100，但绝对上限 50
        b = compensation.assess(category="服务态度", level="ai_plus_human", amount=28)
        assert b["cap"] == 14.0           # 28 的 50%

    def test_无金额只能给不涉钱方案(self):
        a = compensation.assess(category="异物", level="human_now", amount=0)
        assert a["suggest"] == "apology"
        assert a["cap"] == 0.0
        assert any("无金额快照" in r for r in a["reasons"])

    def test_积分被标记不可兑现(self):
        a = compensation.assess(category="菜品质量", level="ai_plus_human", amount=28)
        assert "points" in a["blocked"]
        assert "points" not in a["alternatives"]

    def test_积分硬拦(self):
        a = compensation.assess(category="菜品质量", level="ai_plus_human", amount=28)
        v = compensation.check("points", advice=a, amount=28)
        assert v["ok"] is False and "会员" in v["error"]

    def test_无快照不能选免单(self):
        a = compensation.assess(category="上菜慢", level="self_service", amount=0)
        v = compensation.check("waive", advice=a, amount=0)
        assert v["ok"] is False
        assert "不涉钱" in v["error"]

    def test_偏离标准方案只警告不拦(self):
        a = compensation.assess(category="上菜慢", level="ai_plus_human", amount=28)
        v = compensation.check("coupon", advice=a, amount=28)
        assert v["ok"] is True
        assert "标准方案" in v["warn"]


# ---------------------------------------------------------------- 审批闸门
class TestDecisionGate:
    def test_积分方案在接口层被拒(self, server):
        manager = login(server, "admin")
        _s, row = _paid(server, manager, "菜里有头发")
        status, payload = call(
            server, "POST", f"/api/guest/complaints/{row['id']}/resolve",
            session=manager, compensation="points", expected_version=row["version"])
        assert status == 400
        assert "会员" in str(payload)

    def test_食安免单可直接批(self, server):
        manager = login(server, "admin")
        _s, row = _paid(server, manager, "菜里有头发")
        assert row["compensation"]["tier"] == "L3"
        assert row["compensation"]["suggest"] == "waive"
        done = data_of(server, "POST",
                       f"/api/guest/complaints/{row['id']}/resolve",
                       session=manager, compensation="waive",
                       expected_version=row["version"])["complaint"]
        assert done["state"] == "approved"
        assert done["compensation"]["chosen"] == "waive"
        assert done["compensation"]["chosen_label"] == \
            COMPENSATION_LABELS["waive"]["label"]


# ---------------------------------------------------------------- 驳回
class TestReject:
    def test_内部原因不出现在对客说明(self, server):
        manager = login(server, "admin")
        _s, row = _paid(server, manager, "这菜太少了跟图片差太多", evidence=False)
        done = data_of(server, "POST",
                       f"/api/guest/complaints/{row['id']}/reject",
                       session=manager,
                       reason="补偿金额超过快照上限，且无双人复核",
                       reason_code="amount_exceeded",
                       expected_version=row["version"])["complaint"]
        assert done["state"] == "rejected"
        assert done["reject_reason"] == "补偿金额超过快照上限，且无双人复核"
        assert "上限" not in done["customer_reason"]
        assert "复核" not in done["customer_reason"]
        assert "本单金额" in done["customer_reason"]
        assert done["reject_label"] == "诉求金额超出本单范围"
        assert done["customer_hint"]["next"] == "确认按订单金额调整方案"
        assert done["customer_hint"]["days"] == 7

    def test_未知原因码被拒(self, server):
        manager = login(server, "admin")
        _s, row = _paid(server, manager, "菜有点凉了，没法吃", evidence=False)
        status, _ = call(server, "POST",
                         f"/api/guest/complaints/{row['id']}/reject",
                         session=manager, reason="随便写写",
                         reason_code="whatever",
                         expected_version=row["version"])
        assert status == 400

    def test_不传原因码按其他兜底(self, server):
        manager = login(server, "admin")
        _s, row = _paid(server, manager, "菜有点凉了，没法吃", evidence=False)
        done = data_of(server, "POST",
                       f"/api/guest/complaints/{row['id']}/reject",
                       session=manager, reason="内部说明",
                       expected_version=row["version"])["complaint"]
        assert done["reject_code"] == "other"
        assert done["customer_reason"]
        assert "内部说明" not in done["customer_reason"]


# ---------------------------------------------------------------- 回访
class TestFeedback:
    def _approved(self, server, manager):
        s, row = _paid(server, manager, "菜里有头发")
        done = data_of(server, "POST",
                       f"/api/guest/complaints/{row['id']}/resolve",
                       session=manager, compensation="waive",
                       expected_version=row["version"])["complaint"]
        return s, done

    def test_结案后可评且只评一次(self, server):
        manager = login(server, "admin")
        s, row = self._approved(server, manager)
        assert row["need_feedback"] is True
        rated = data_of(server, "POST",
                        f"/api/customer/guest/complaints/{row['id']}/feedback",
                        customer_token=s["customer_token"], guest_key=KEY,
                        score=5, comment="处理很快")["complaint"]
        assert rated["feedback_score"] == 5
        assert rated["need_feedback"] is False
        status, _ = call(server, "POST",
                         f"/api/customer/guest/complaints/{row['id']}/feedback",
                         customer_token=s["customer_token"], guest_key=KEY,
                         score=1)
        assert status == 400

    def test_未结案不能评(self, server):
        manager = login(server, "admin")
        s, row = _paid(server, manager, "菜里有头发")
        status, _ = call(server, "POST",
                         f"/api/customer/guest/complaints/{row['id']}/feedback",
                         customer_token=s["customer_token"], guest_key=KEY,
                         score=5)
        assert status == 409          # 未结案属状态冲突，不是参数错

    def test_分数越界被拒(self, server):
        manager = login(server, "admin")
        s, row = self._approved(server, manager)
        for bad in (0, 6, "abc"):
            status, _ = call(server, "POST",
                             f"/api/customer/guest/complaints/{row['id']}/feedback",
                             customer_token=s["customer_token"], guest_key=KEY,
                             score=bad)
            assert status == 400

    def test_别人不能替我评价(self, server):
        manager = login(server, "admin")
        _s, row = self._approved(server, manager)
        other = _open(server, "g-other-001")
        status, _ = call(server, "POST",
                         f"/api/customer/guest/complaints/{row['id']}/feedback",
                         customer_token=other["customer_token"],
                         guest_key="g-other-001", score=5)
        assert status in (403, 404)


# ---------------------------------------------------------------- 质检看板
class TestQuality:
    def test_看板口径(self, server):
        manager = login(server, "admin")
        s, row = _paid(server, manager, "菜里有头发")
        data_of(server, "POST", f"/api/guest/complaints/{row['id']}/resolve",
                session=manager, compensation="waive",
                expected_version=row["version"])
        data_of(server, "POST",
                f"/api/customer/guest/complaints/{row['id']}/feedback",
                customer_token=s["customer_token"], guest_key=KEY, score=2)

        other = _open(server, "g-cmp-002")
        submitted = _submit(server, other, "菜里有头发，要个说法", key="g-cmp-002")
        row2 = _row(server, manager, submitted["complaint_no"])
        data_of(server, "POST", f"/api/guest/complaints/{row2['id']}/reject",
                session=manager, reason="证据不足", reason_code="evidence_missing",
                expected_version=row2["version"])

        stats = data_of(server, "GET", "/api/guest/quality", session=manager)
        assert stats["total"] == 2
        assert stats["terminal"] == 1
        assert stats["rejected"] == 1
        assert stats["reject_rate"] == 0.5
        assert stats["csat"] == 2.0
        assert stats["feedback_count"] == 1
        assert stats["human_now"] >= 1
        assert stats["low_scores"] and stats["low_scores"][0]["feedback_score"] == 2
        assert stats["resolve_min"] >= 0
