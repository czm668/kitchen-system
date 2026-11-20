# -*- coding: utf-8 -*-
"""质检评分卡测试。

核心不是「分数算对」，而是三条**不能松的口径**：

1. **不伪造软性项**：礼貌/同理心/耐心 30 分必须标 `pending`，
   系统一分都不许替人工打——这是这套评分卡能拿去答辩的前提。
2. **不适用项不白送分**：没驳回的单不评「驳回隔离」，没补偿的单不评
   「方案合规」。否则所有单都从 70 分起步，团队均分失真。
3. **护栏命中评的是 AI 输出质量，不是顾客受损**：禁语被改写、模型判轻
   被规则兜住，都要在 `why` 里写明「已拦截 / 已兜住」，防止质检员把
   这类扣分误记成客诉事故。
"""
from __future__ import annotations

import pytest

from api_support import data_of, login, make_server
from kitchen.guest import quality
from kitchen.guest.service import ComplaintState

KEY = "g-qa-001"


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


def _open(server, key=KEY):
    return data_of(server, "POST", "/api/customer/session",
                   table_id=3, guest_key=key, tags=[])


def _submit(server, session, text, *, key=KEY, **kw):
    return data_of(server, "POST", "/api/customer/guest/complaints",
                   customer_token=session["customer_token"], guest_key=key,
                   text=text, **kw)


def _row(server, complaint_no, manager=None):
    manager = manager or login(server, "admin")
    rows = data_of(server, "GET", "/api/guest/complaints",
                   session=manager)["complaints"]
    return next(r for r in rows if r["complaint_no"] == complaint_no)


# ---------------------------------------------------------------- 纯函数：结构与口径
class TestCardShape:
    def test_软性项一律待人工评(self):
        """这是整套评分卡的诚实底线：系统不替人打服务态度分。"""
        card = quality.score_complaint({})
        assert card["manual_max"] == 30
        assert len(card["manual_items"]) == 4
        assert all(x["status"] == "pending" and x["score"] is None
                   for x in card["manual_items"])
        assert card["is_full_score"] is False

    def test_覆盖度明确不足100(self):
        card = quality.score_complaint({})
        assert card["coverage"] == 0.7
        assert card["auto_max"] + card["manual_max"] == 100

    def test_等级阈值与参考库一致(self):
        assert [quality.grade_of(x) for x in (95, 85, 75, 65, 55)] == [
            "优秀", "良好", "合格", "待改进", "不合格"]

    def test_每项都有来源与理由(self):
        card = quality.score_complaint({
            "state": "submitted", "category": "异物", "text": "有头发",
            "handoff": {"level": "human_now", "code": "food_safety"},
            "handling": {"guard": [{"term": "马上就好"}]},
        })
        for item in card["items"]:
            assert item["source"] and item["why"]
            assert 0 <= item["score"] <= item["max"]
        banned = next(x for x in card["items"] if x["id"] == "banned_words")
        assert "已" in banned["why"] and "拦截" in banned["why"]

    def test_模型判轻写明已兜住(self):
        card = quality.score_complaint({
            "state": "submitted", "category": "上菜慢", "text": "催了半小时",
            "handoff": {"level": "human_now", "code": "slow_repeat",
                        "overridden": True},
        })
        risk = next(x for x in card["items"] if x["id"] == "risk_called")
        assert risk["score"] < risk["max"]
        assert "兜住" in risk["why"]

    def test_食安漏判直接零分(self):
        card = quality.score_complaint({
            "state": "submitted", "category": "异物", "text": "有玻璃",
            "handoff": {"level": "self_service", "code": "normal"},
        })
        risk = next(x for x in card["items"] if x["id"] == "risk_called")
        assert risk["score"] == 0
        assert "漏判" in risk["why"]

    def test_未结案不给闭环分(self):
        card = quality.score_complaint({
            "state": "submitted", "category": "温度", "text": "菜凉了",
        })
        closure = next(x for x in card["items"] if x["id"] == "closure")
        assert closure["score"] == 0

    def test_驳回时内部口径外泄零分(self):
        card = quality.score_complaint({
            "state": "rejected", "category": "其他", "text": "我要退款",
            "reject_code": "amount_exceeded",
            "reject_reason": "金额超过快照上限，无双人复核",
            "customer_reason": "金额超过快照上限，无双人复核",
        })
        iso = next(x for x in card["items"] if x["id"] == "reject_isolation")
        assert iso["score"] == 0
        assert "泄露" in iso["why"]

    def test_正常驳回按原因码满分(self):
        card = quality.score_complaint({
            "state": "rejected", "category": "其他", "text": "我要退款",
            "reject_code": "evidence_missing",
            "reject_reason": "缺少现场照片",
            "customer_reason": "麻烦您补一张菜品照片，7 天内提交",
        })
        iso = next(x for x in card["items"] if x["id"] == "reject_isolation")
        assert iso["score"] == iso["max"]

    def test_不适用项不进分母(self):
        """没驳回的单，驳回隔离项既不加分也不扣分。"""
        base = {"state": "submitted", "category": "温度", "text": "菜凉了"}
        card = quality.score_complaint(base)
        ids = {x["id"] for x in card["items"]}
        assert "reject_isolation" not in ids
        assert "compensation_fit" not in ids

    def test_涉钱无快照要扣分(self):
        card = quality.score_complaint({
            "state": "submitted", "category": "温度", "text": "退款",
            "wants_refund": True, "order_snapshot_amount": 0,
        })
        rec = next(x for x in card["items"] if x["id"] == "record_integrity")
        assert rec["score"] < rec["max"]

    def test_补偿超额与偏离都留痕(self):
        card = quality.score_complaint({
            "state": "approved", "category": "异物", "text": "有头发",
            "method": "waive",
            "compensation": {"suggest": "coupon", "chosen": "waive",
                             "cap": 20.0, "amount": 58.0},
        })
        fit = next(x for x in card["items"] if x["id"] == "compensation_fit")
        assert fit["score"] <= 3
        assert "需店长复核" in fit["why"]

    def test_满分工单能拿到优秀(self):
        """每一项都做到时不能因为「不适用项被剔除」而拿不到满分。"""
        card = quality.score_complaint({
            "state": "approved", "category": "异物", "text": "有头发",
            "created_at": "2026-11-20T10:00:00",
            "decided_at": "2026-11-20T10:05:00",
            "handoff": {"level": "human_now", "code": "food_safety"},
            "handling": {"guard": []},
            "order_snapshot_amount": 28.0,
            "method": "waive",
            "compensation": {"suggest": "waive", "chosen": "waive",
                             "cap": 28.0, "amount": 28.0},
        })
        assert card["normalized"] == 100.0
        assert card["grade"] == "优秀"


# ---------------------------------------------------------------- 汇总
class TestSummarize:
    def test_空数据不崩(self):
        r = quality.summarize([])
        assert r["count"] == 0 and r["avg"] == 0

    def test_等级分布与TOP扣分(self):
        rows = [
            {"state": "approved", "category": "异物", "text": "有头发",
             "created_at": "2026-11-20T10:00:00", "decided_at": "2026-11-20T10:05:00",
             "handoff": {"level": "human_now", "code": "food_safety"},
             "handling": {"guard": []}, "method": "waive",
             "compensation": {"suggest": "waive", "chosen": "waive", "cap": 28.0, "amount": 28.0}},
            {"state": "submitted", "category": "其他", "text": "在吗",
             "handoff": {"level": "self_service", "code": "normal"},
             "handling": {"guard": [{"term": "马上就好"}]}},
        ]
        r = quality.summarize(rows)
        assert r["count"] == 2
        assert sum(r["grades"].values()) == 2
        assert r["excellent_rate"] == 0.5
        # 两张单分别有「未闭环」和「禁语+分类其他」扣分，TOP 里应能找到它们
        top_ids = {x["id"] for x in r["top_deductions"]}
        assert {"closure", "banned_words"} <= top_ids

    def test_按最后处理人聚合(self):
        rows = [{
            "state": "approved", "category": "温度", "text": "菜凉了",
            "created_at": "2026-11-20T10:00:00", "decided_at": "2026-11-20T10:05:00",
            "handoff": {"level": "self_service", "code": "normal"},
            "history": [{"action": "review_evidence", "actor": "lead-1"},
                        {"action": "approve", "actor": "manager-1"}],
        }]
        r = quality.summarize(rows)
        assert "manager-1" in r["by_actor"]
        assert "lead-1" not in r["by_actor"]


# ---------------------------------------------------------------- 真实 HTTP 链路
class TestQualityApi:
    def test_工单视图带评分卡(self, server):
        s = _open(server)
        sub = _submit(server, s, "菜里有头发")
        row = _row(server, sub["complaint_no"])
        assert "qa" in row
        assert row["qa"]["is_full_score"] is False
        assert row["qa"]["manual_items"][0]["status"] == "pending"

    def test_看板带团队质检汇总(self, server):
        s = _open(server)
        _submit(server, s, "菜里有头发")
        q = data_of(server, "GET", "/api/guest/quality",
                    session=login(server, "admin"))
        assert q["qa"]["count"] >= 1
        assert 0 <= q["qa"]["avg"] <= 100
        assert q["qa"]["coverage"] == 0.7

    def test_评分卡不回写工单状态(self, server):
        """质检是只读的：评分前后工单 state/version 不许变。"""
        s = _open(server)
        sub = _submit(server, s, "服务员态度太差")
        before = _row(server, sub["complaint_no"])
        data_of(server, "GET", "/api/guest/quality",
                session=login(server, "admin"))
        after = _row(server, sub["complaint_no"])
        assert (before["state"], before["version"]) == (after["state"], after["version"])
