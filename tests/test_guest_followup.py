# -*- coding: utf-8 -*-
"""低分单当日回访闭环测试。

守住四件事：

1. **低分建任务、高分不建**——只有 1–2 分才生成 pending 任务与当日截止；
2. **回访关任务但不洗白**——回访记 who/when/note，不改评分、不改工单状态；
3. **超时如实标记、不抹掉**——跨天仍可回访，但 `late` 进审计与事件；
4. **看板口径分母正确**——只统计建过任务的低分单，超时单单独可数。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api_support import call, data_of, force_session, login, make_server
from kitchen.guest import followup

DISH = "D-QJRS"
KEY = "g-fu-001"
CST = timezone(timedelta(hours=8))


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


def _open(server, key=KEY):
    return data_of(server, "POST", "/api/customer/session",
                   table_id=5, guest_key=key, tags=[])


def _order(server, session):
    return data_of(server, "POST", "/api/customer/orders",
                   customer_token=session["customer_token"], table_id=5,
                   items=[{"code": DISH, "qty": 1}])


def _submit(server, session, key=KEY):
    order = _order(server, session)
    return data_of(server, "POST", "/api/customer/guest/complaints",
                   customer_token=session["customer_token"], guest_key=key,
                   text="菜里有头发，太失望了", order_id=order["order_id"],
                   wants_refund=True,
                   evidences=[{"kind": "photo", "content": "现场照片"}])


def _row(server, manager, no):
    rows = data_of(server, "GET", "/api/guest/complaints",
                   session=manager)["complaints"]
    return next(c for c in rows if c["complaint_no"] == no)


def _closed_then_rated(server, manager, score, *, key=KEY):
    """提交 → 结案 → 顾客评分，返回 (会话, 评分后的工单行)。测试前请自行 patch 时钟。"""
    s = _open(server, key)
    submitted = _submit(server, s, key)
    row = _row(server, manager, submitted["complaint_no"])
    data_of(server, "POST", f"/api/guest/complaints/{row['id']}/resolve",
            session=manager, compensation="waive",
            expected_version=row["version"])
    rated = data_of(server, "POST",
                    f"/api/customer/guest/complaints/{row['id']}/feedback",
                    customer_token=s["customer_token"], guest_key=key,
                    score=score)["complaint"]
    return s, rated


# ---------------------------------------------------------------- 纯规则
class TestRules:
    def test_低分阈值(self):
        assert followup.is_low(1) and followup.is_low(2)
        assert not followup.is_low(3) and not followup.is_low(5)
        assert not followup.is_low("x") and not followup.is_low(None)

    def test_当日截止是当天235959(self):
        due = followup.due_of("2026-11-20T13:21:12+08:00")
        assert due == "2026-11-20T23:59:59+08:00"

    def test_跨天算超时当天不算(self):
        due = followup.due_of("2026-11-20T13:21:12+08:00")
        assert not followup.is_overdue(due, datetime(2026, 11, 20, 23, 0, tzinfo=CST))
        assert followup.is_overdue(due, datetime(2026, 11, 21, 0, 0, tzinfo=CST))

    def test_话术守红线(self):
        for cat in ("异物", "温度", "菜品质量", "上菜慢", "服务态度", "其他"):
            for variant in ("A", "B"):
                script = followup.script_for(cat, variant=variant, no="C-001")
                assert "C-001" in script["text"]
                for banned in ("已经给您退", "马上", "保证", "一定给您"):
                    assert banned not in script["text"]
                assert "不承诺" in script["forbid"]


# ---------------------------------------------------------------- 建任务
class TestCreateTask:
    def test_低分建pending高分不建(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        s, low = _closed_then_rated(server, manager, 2)
        assert low["followup_state"] == "pending"
        assert low["followup_due"] == "2026-11-20T23:59:59+08:00"
        assert low["need_followup"] is True
        assert low["followup_overdue"] is False
        assert "工单" in low["followup_script"]["text"]

        # 另一单打 5 分：不建任务
        s2 = _open(server, "g-fu-high")
        sub2 = _submit(server, s2, key="g-fu-high")
        row2 = _row(server, manager, sub2["complaint_no"])
        data_of(server, "POST", f"/api/guest/complaints/{row2['id']}/resolve",
                session=manager, compensation="waive",
                expected_version=row2["version"])
        high = data_of(server, "POST",
                       f"/api/customer/guest/complaints/{row2['id']}/feedback",
                       customer_token=s2["customer_token"], guest_key="g-fu-high",
                       score=5)["complaint"]
        assert high["followup_state"] == ""
        assert high["followup_due"] == ""


# ---------------------------------------------------------------- 回访动作
class TestFollowupAction:
    def test_回访关任务但不改评分与状态(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        _s, rated = _closed_then_rated(server, manager, 2)
        rid = rated["id"]
        done = data_of(server, "POST", f"/api/guest/complaints/{rid}/followup",
                       session=manager, note="已电话联系，顾客接受重做方案",
                       expected_version=rated["version"])["complaint"]
        assert done["followup_state"] == "done"
        assert done["followup_by"] == "admin"
        assert done["followup_note"] == "已电话联系，顾客接受重做方案"
        assert done["need_followup"] is False
        # 关键：回访不改评分、不改工单状态
        assert done["feedback_score"] == 2
        assert done["state"] == rated["state"]
        # 历史留痕
        assert any(h["action"] == "followup_done" for h in done["history"])

    def test_重复回访被拒(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        _s, rated = _closed_then_rated(server, manager, 1)
        rid = rated["id"]
        data_of(server, "POST", f"/api/guest/complaints/{rid}/followup",
                session=manager, note="第一次回访",
                expected_version=rated["version"])
        row = _row(server, manager, rated["complaint_no"])
        status, _ = call(server, "POST", f"/api/guest/complaints/{rid}/followup",
                         session=manager, note="第二次",
                         expected_version=row["version"])
        assert status == 409

    def test_无任务或空说明被拒(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        s, high = _closed_then_rated(server, manager, 4)
        # 高分单没有回访任务
        status, _ = call(server, "POST",
                         f"/api/guest/complaints/{high['id']}/followup",
                         session=manager, note="随便回访",
                         expected_version=high["version"])
        assert status == 409

        s2, low = _closed_then_rated(server, manager, 2, key="g-fu-note")
        status, _ = call(server, "POST", f"/api/guest/complaints/{low['id']}/followup",
                         session=manager, note="   ",
                         expected_version=low["version"])
        assert status == 400

    def test_组长无回访权限(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        _s, rated = _closed_then_rated(server, manager, 2)
        lead = force_session("lead-1", "lead")
        status, _ = call(server, "POST",
                         f"/api/guest/complaints/{rated['id']}/followup",
                         session=lead, note="组长回访",
                         expected_version=rated["version"])
        assert status == 403


# ---------------------------------------------------------------- 超时与看板
class TestOverdueAndStats:
    def test_跨天未回访被判超时(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        _s, rated = _closed_then_rated(server, manager, 2)
        assert rated["followup_overdue"] is False

        nxt = datetime(2026, 11, 21, 9, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: nxt)
        row = _row(server, manager, rated["complaint_no"])
        assert row["need_followup"] is True
        assert row["followup_overdue"] is True
        stats = data_of(server, "GET", "/api/guest/quality", session=manager)
        assert stats["followup_total"] == 1
        assert stats["followup_pending"] == 1
        assert stats["followup_overdue"] == 1
        assert stats["followup_done"] == 0
        assert stats["followup_rate"] == 0.0

    def test_超时后回访仍成功但标late(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        _s, rated = _closed_then_rated(server, manager, 2)

        nxt = datetime(2026, 11, 21, 9, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: nxt)
        done = data_of(server, "POST",
                       f"/api/guest/complaints/{rated['id']}/followup",
                       session=manager, note="隔天补回访",
                       expected_version=rated["version"])["complaint"]
        assert done["followup_state"] == "done"
        assert any(h.get("action") == "followup_done" and h.get("late") is True
                   for h in done["history"])

    def test_看板分母只含低分单(self, server, monkeypatch):
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        _closed_then_rated(server, manager, 2)                    # 低分 → 1 个任务
        s2 = _open(server, "g-fu-ok")
        sub2 = _submit(server, s2, key="g-fu-ok")
        row2 = _row(server, manager, sub2["complaint_no"])
        data_of(server, "POST", f"/api/guest/complaints/{row2['id']}/resolve",
                session=manager, compensation="waive",
                expected_version=row2["version"])
        data_of(server, "POST",
                f"/api/customer/guest/complaints/{row2['id']}/feedback",
                customer_token=s2["customer_token"], guest_key="g-fu-ok", score=5)
        stats = data_of(server, "GET", "/api/guest/quality", session=manager)
        assert stats["feedback_count"] == 2
        assert stats["followup_total"] == 1                      # 高分不进分母
