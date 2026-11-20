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

    def test_类型严判_boot浮点不误判为低分(self):
        # S-1：bool 是 int 子类、float 会被 int() 截断，都不能算低分
        assert not followup.is_low(True)
        assert not followup.is_low(False)
        assert not followup.is_low(1.9)
        assert not followup.is_low(2.9)
        # 整数字符串仍接受（公共规则的既有契约）
        assert followup.is_low("2")

    def test_当日截止是当天235959(self):
        due = followup.due_of("2026-11-20T13:21:12+08:00")
        assert due == "2026-11-20T23:59:59+08:00"

    def test_脏评价时刻返回空due不存脏值(self):
        # S-3：解析失败返回空串，调用方据此不建无截止任务
        assert followup.due_of("garbage") == ""
        assert followup.due_of(None) == ""

    def test_脏due按超时处理_failclosed(self):
        # S-3：存量脏 due 解析不了时判超时，不能让任务永远不超时
        assert followup.is_overdue(
            "garbage", datetime(2026, 11, 21, 9, 0, tzinfo=CST)) is True
        assert followup.is_overdue("", datetime(2026, 11, 21, 9, 0, tzinfo=CST)) is False

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

    def test_due算不出时不建无截止任务而发告警(self, server, monkeypatch):
        # S-3：due_of 失败 → 不建 pending 任务（否则永远不超时），发告警事件
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)
        monkeypatch.setattr(followup, "due_of", lambda _m: "")
        _s, rated = _closed_then_rated(server, manager, 2)
        # 低分但没有 due，也就不建回访任务
        assert rated["followup_state"] == ""
        assert rated["followup_due"] == ""
        assert rated["need_followup"] is False
        # 告警事件与历史留痕都在，不是静默放过
        assert any(h.get("action") == "followup_due_invalid" for h in rated["history"])
        assert any(e.get("op") == "low_score_due_invalid" for e in server.state.events())


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
        # S-2：late 落到字段与视图，不再只躺在审计里
        assert done["followup_late"] is True

    def test_及时率区分当日完成与超时补做(self, server, monkeypatch):
        # S-2：看板能区分"当日做"与"隔天补"，不再被 followup_rate 误导
        manager = login(server, "admin")
        at = datetime(2026, 11, 20, 10, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: at)

        # 第一单：当日完成（on time）
        _s1, r1 = _closed_then_rated(server, manager, 2, key="g-fu-ontime")
        data_of(server, "POST", f"/api/guest/complaints/{r1['id']}/followup",
                session=manager, note="当日已联系",
                expected_version=r1["version"])

        # 第二单：隔天补做（late）
        _s2, r2 = _closed_then_rated(server, manager, 2, key="g-fu-late")
        nxt = datetime(2026, 11, 21, 9, 0, tzinfo=CST)
        monkeypatch.setattr(server.state, "to_datetime", lambda: nxt)
        done2 = data_of(server, "POST",
                        f"/api/guest/complaints/{r2['id']}/followup",
                        session=manager, note="隔天补回访",
                        expected_version=r2["version"])["complaint"]
        assert done2["followup_late"] is True

        stats = data_of(server, "GET", "/api/guest/quality", session=manager)
        assert stats["followup_total"] == 2
        assert stats["followup_done"] == 2
        assert stats["followup_rate"] == 1.0              # 完成率 100%
        assert stats["followup_late_done"] == 1           # 其中 1 单是补做
        assert stats["followup_on_time_rate"] == 0.5      # 及时率只有 50%

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
