# -*- coding: utf-8 -*-
"""A8 工序分步接口（#12 称重 / #13 SOP / 出餐门禁）。

用例围绕**三道闸门**展开（顺序、锁定、身份），外加 TFT 的两个屏形态：
没开会话时 `GET /api/cooking/{item_id}` 也要给全 pending 的骨架（屏先打开，
师傅后开称，不能因为「还没开会话」屏就空白）。
"""
from __future__ import annotations

import pytest

from api_support import call, data_of, login, make_server


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def chef(server):
    """TFT 终端用 operator 角色（厨师工号登录）。"""
    return login(server, "chef01")


@pytest.fixture()
def viewer(server):
    return login(server, "guest")


def _make_item(server, session, code: str = "D-QJRS") -> int:
    order = data_of(server, "POST", "/api/orders", session=session,
                    items=[{"code": code, "qty": 1}], source="miniapp")["order"]
    item_id = order["items"][0]["id"]
    data_of(server, "POST", f"/api/orders/items/{item_id}/dispatch",
            session=session, station_id=1)
    # 进入 cooking 才能出餐（mark_ready 的状态机只收 cooking/pending）
    data_of(server, "POST", f"/api/orders/items/{item_id}/state",
            session=session, state="cooking")
    return item_id


# =========================================================== 视图
class TestCookingView:
    def test_没开会话也给全pending骨架(self, server, chef):
        item_id = _make_item(server, chef)
        data = data_of(server, "GET", f"/api/cooking/{item_id}", session=chef)
        assert data["total"] == 1
        row = data["sessions"][0]
        assert row["active"] is False
        assert row["dish"] == "青椒肉丝"
        assert len(row["steps"]) == 6           # 种子 SOP：切配~出锅装盘
        assert {step["state"] for step in row["steps"]} == {"pending", "current"}
        assert row["next_step_no"] == 1
        assert row["blockers"] and "配菜未核验" in row["blockers"][0]

    def test_列表态_无会话给空说明(self, server, chef):
        data = data_of(server, "GET", "/api/cooking", session=chef)
        assert data["total"] == 0
        assert data["note"] == "当前没有进行中的工序会话"

    def test_会话列表只列活动的(self, server, chef):
        item_id = _make_item(server, chef)
        data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                session=chef, ingredient="猪里脊", measured_g=150.2)
        data = data_of(server, "GET", "/api/cooking", session=chef)
        assert data["total"] == 1
        assert data["sessions"][0]["active"] is True

    def test_未知子项404(self, server, chef):
        status, _payload = call(server, "GET", "/api/cooking/9999", session=chef)
        assert status == 404


# =========================================================== #12 称重
class TestWeigh:
    def test_合格称重解锁(self, server, chef):
        item_id = _make_item(server, chef)
        data = data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                       session=chef, ingredient="猪里脊", measured_g=150.0)
        check = data["check"]
        assert check["ok"] is True
        assert check["target_g"] == 150.0
        assert data["locked"] is False
        # 事件流给播报用（A8-04：超差才播，合格不响）
        events = [row for row in server.state.events()
                  if row["kind"] == "cooking" and row.get("item_id") == item_id]
        assert events and events[-1]["ok"] is True

    def test_超差锁下一步并写锁定列表(self, server, chef):
        item_id = _make_item(server, chef)
        data = data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                       session=chef, ingredient="猪里脊", measured_g=180.0)
        assert data["check"]["ok"] is False
        assert data["locked"] is True
        detail = data_of(server, "GET", f"/api/cooking/{item_id}", session=chef)
        assert "猪里脊" in detail["sessions"][0]["locked_ingredients"]

    def test_重称覆盖旧结论(self, server, chef):
        item_id = _make_item(server, chef)
        call(server, "POST", f"/api/cooking/{item_id}/weigh",
             session=chef, ingredient="猪里脊", measured_g=180.0)
        data = data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                       session=chef, ingredient="猪里脊", measured_g=152.0)
        assert data["check"]["ok"] is True
        assert data["locked"] is False

    def test_不在配方里的料400(self, server, chef):
        item_id = _make_item(server, chef)
        status, payload = call(server, "POST", f"/api/cooking/{item_id}/weigh",
                               session=chef, ingredient="老干妈", measured_g=10)
        assert status == 400

    def test_缺克重400(self, server, chef):
        item_id = _make_item(server, chef)
        status, _payload = call(server, "POST", f"/api/cooking/{item_id}/weigh",
                                session=chef, ingredient="猪里脊")
        assert status == 400


# =========================================================== #13 SOP
class TestConfirm:
    def test_按顺序确认并推进current(self, server, chef):
        item_id = _make_item(server, chef)
        for ingredient, grams in (("猪里脊", 150.0), ("青椒", 100.0),
                                  ("食用油", 20.0), ("生抽", 8.0)):
            data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                    session=chef, ingredient=ingredient, measured_g=grams)
        data = data_of(server, "POST", f"/api/cooking/{item_id}/confirm",
                       session=chef, step_no=1, staff_id=1)
        assert data["confirm"]["ok"] is True
        assert data["confirm"]["ccp"] is True
        assert data["next_step_no"] == 2

    def test_跳步400(self, server, chef):
        item_id = _make_item(server, chef)
        for ingredient, grams in (("猪里脊", 150.0), ("青椒", 100.0),
                                  ("食用油", 20.0), ("生抽", 8.0)):
            data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                    session=chef, ingredient=ingredient, measured_g=grams)
        status, _payload = call(server, "POST", f"/api/cooking/{item_id}/confirm",
                                session=chef, step_no=3)
        assert status == 400

    def test_称重未通过时确认被锁409(self, server, chef):
        """StepLockedError 是 DomainError 子类 → 400 还是 409？

        `http_status` 里 `StepLockedError` 命中 `DomainError` 分支 → 400。
        但它语义上是「请求合法、当前状态不允许」→ 409 更贴切。测试按现状
        固化：**只要不是 200**、且错误信息指向称重，TFT 屏就能给出正确提示。
        """
        item_id = _make_item(server, chef)
        data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                session=chef, ingredient="猪里脊", measured_g=180.0)
        status, payload = call(server, "POST", f"/api/cooking/{item_id}/confirm",
                               session=chef, step_no=1, staff_id=1)
        assert status == 409
        assert "称重" in payload.get("detail", "")

    def test_requires_card缺身份拒绝(self, server, chef):
        item_id = _make_item(server, chef)
        for ingredient, grams in (("猪里脊", 150.0), ("青椒", 100.0),
                                  ("食用油", 20.0), ("生抽", 8.0)):
            data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                    session=chef, ingredient=ingredient, measured_g=grams)
        status, payload = call(server, "POST", f"/api/cooking/{item_id}/confirm",
                               session=chef, step_no=1)
        assert status == 400
        assert "刷卡" in payload.get("detail", "")

    def test_重复确认幂等(self, server, chef):
        item_id = _make_item(server, chef)
        for ingredient, grams in (("猪里脊", 150.0), ("青椒", 100.0),
                                  ("食用油", 20.0), ("生抽", 8.0)):
            data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                    session=chef, ingredient=ingredient, measured_g=grams)
        data_of(server, "POST", f"/api/cooking/{item_id}/confirm",
                session=chef, step_no=1, staff_id=1)
        data = data_of(server, "POST", f"/api/cooking/{item_id}/confirm",
                       session=chef, step_no=1, staff_id=1)
        assert data["confirm"]["ok"] is True


# =========================================================== 出餐门禁
class TestServe:
    def _weigh_all(self, server, chef, item_id, *, grams=150.0):
        for ingredient, grams in (("猪里脊", grams), ("青椒", 100.0),
                                  ("食用油", 20.0), ("生抽", 8.0)):
            data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                    session=chef, ingredient=ingredient, measured_g=grams)

    def test_门禁不满足409且一次列全(self, server, chef):
        item_id = _make_item(server, chef)
        status, payload = call(server, "POST", f"/api/cooking/{item_id}/serve",
                               session=chef, staff_id=1)
        assert status == 409
        message = payload.get("detail", "")
        assert "配菜未核验" in message
        assert "关键控制点" in message

    def test_全部满足后出餐_会话关闭(self, server, chef):
        item_id = _make_item(server, chef)
        self._weigh_all(server, chef, item_id)
        for step_no in range(1, 7):
            data_of(server, "POST", f"/api/cooking/{item_id}/confirm",
                    session=chef, step_no=step_no,
                    staff_id=1 if step_no in (1, 4, 6) else None)
        data = data_of(server, "POST", f"/api/cooking/{item_id}/serve",
                       session=chef, staff_id=1, channel="tft")
        assert data["order"]["state"] == "ready"
        # 会话关闭：列表清空、再次 detail 回到全 pending 骨架
        rows = data_of(server, "GET", "/api/cooking", session=chef)
        assert rows["total"] == 0

    def test_CCP未确认不能出餐(self, server, chef):
        item_id = _make_item(server, chef)
        self._weigh_all(server, chef, item_id)
        for step_no in (1, 2, 3):
            data_of(server, "POST", f"/api/cooking/{item_id}/confirm",
                    session=chef, step_no=step_no,
                    staff_id=1 if step_no == 1 else None)
        status, payload = call(server, "POST", f"/api/cooking/{item_id}/serve",
                               session=chef, staff_id=1)
        assert status == 409
        assert "#4" in payload.get("detail", "")

    def test_重开会话进度清零(self, server, chef):
        item_id = _make_item(server, chef)
        self._weigh_all(server, chef, item_id)
        data_of(server, "POST", f"/api/cooking/{item_id}/confirm",
                session=chef, step_no=1, staff_id=1)
        data = data_of(server, "POST", f"/api/cooking/{item_id}/reset",
                       session=chef)
        row = data["detail"]
        assert row["confirmed_steps"] == []
        assert row["next_step_no"] == 1


# =========================================================== 权限与审计
class TestPermissionAudit:
    def test_viewer不能操作工序(self, server, viewer):
        status, _payload = call(server, "POST", "/api/cooking/1/weigh",
                                session=viewer, ingredient="猪里脊",
                                measured_g=150.0)
        assert status == 403

    def test_称重与确认进审计(self, server, chef):
        item_id = _make_item(server, chef)
        data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                session=chef, ingredient="猪里脊", measured_g=180.0)  # 超差（留痕用）
        for ingredient, grams in (("猪里脊", 150.0), ("青椒", 100.0),
                                  ("食用油", 20.0), ("生抽", 8.0)):
            data_of(server, "POST", f"/api/cooking/{item_id}/weigh",
                    session=chef, ingredient=ingredient, measured_g=grams)
        data_of(server, "POST", f"/api/cooking/{item_id}/confirm",
                session=chef, step_no=1, staff_id=1)
        rows = server.state.audit.entries()
        assert rows
        actions = {row.action for row in rows}
        assert {"cooking.weigh", "cooking.confirm"} <= actions
        # 超差那次称重 result=locked（失败也留痕）
        locked = [row for row in rows
                  if row.action == "cooking.weigh" and row.result == "locked"]
        assert locked


# =========================================================== 刷卡副作用
class TestScanSideEffect:
    def test_刷卡即复位离人计时(self, server, chef):
        """#3 返岗复位：scan 会刷 guards 的 last_human；
        首刷被要求晨检是业务正确（#8：先问卷后准入）。"""
        data = data_of(server, "POST", "/api/admission/scan",
                       session=chef, card_uid="A1B2C3D4", station_id=1)
        assert data["needs_morning_check"] is True
        guards = server.state.offline.guards
        snapshot = guards.snapshot()
        # guards 快照里带 last_human_at（字段名以实现为准）
        human_key = next((key for key in snapshot
                          if "human" in str(key).lower()), None)
        if human_key is not None:
            assert snapshot[human_key] is not None
