# -*- coding: utf-8 -*-
"""#12 配菜称重防错 + #13 SOP 分步确认（A4-03 / A4-04）。

两个验收项：
- 「多放 10% 触发锁定；补足或更正后解锁」→ `TestWeigh`
- 「跳过 CCP 步骤无法出餐」→ `TestServeGate`
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kitchen.domain import CST
from kitchen.process import (
    ItemState,
    MemoryProcessRepository,
    OrderService,
)
from kitchen.process.cooking import (
    MIN_ABS_TOLERANCE_G,
    CookingSession,
    NotReadyError,
    StepLockedError,
    WeighError,
)

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture()
def ctx():
    """一单已上灶的青椒肉丝 → (repo, svc, session)。"""
    clock = FakeClock()
    repo = MemoryProcessRepository.from_seed()
    svc = OrderService(repo, clock=clock)
    order = svc.place_order(table_id=1, items=[("D-QJRS", 1)])
    item = order.items[0]
    svc.dispatch_item(item.id, 1)
    svc.start_cooking(item.id, actor="1")
    session = CookingSession(repo, svc, item.id, session_uid="S1", clock=clock)
    return repo, svc, session


def weigh_all(session: CookingSession, *, overrides: dict | None = None) -> None:
    overrides = overrides or {}
    for row in session.recipe_items:
        session.weigh(row.ingredient, overrides.get(row.ingredient, row.weight_g))


def confirm_all(session: CookingSession, staff_id: int = 1) -> None:
    for step in session.steps:
        session.confirm(step.step_no, staff_id=staff_id)


# ================================================================ #12 称重
class TestWeigh:
    def test_落点在容差内合格(self, ctx):
        _repo, _svc, session = ctx
        check = session.weigh("猪里脊", 152.0)
        assert check.ok
        assert check.delta_g == 2.0
        assert check.state_text == "ok"

    def test_多放一成被锁(self, ctx):
        _repo, _svc, session = ctx
        check = session.weigh("猪里脊", 165.0)          # 目标 150g，+10%
        assert not check.ok
        assert check.state_text == "over"
        assert session.locked

    def test_少放也被锁(self, ctx):
        _repo, _svc, session = ctx
        check = session.weigh("青椒", 70.0)            # 目标 100g，-30%
        assert not check.ok
        assert check.state_text == "under"
        assert "少了" in check.detail

    def test_补足后解锁(self, ctx):
        _repo, _svc, session = ctx
        session.weigh("猪里脊", 165.0)
        assert session.locked
        assert session.weigh("猪里脊", 150.0).ok
        assert not session.locked

    def test_重新称覆盖旧结论(self, ctx):
        """称错了再称是常态；做成累加/取平均会让师傅永远修不回区间。"""
        _repo, _svc, session = ctx
        session.weigh("猪里脊", 165.0)
        session.weigh("猪里脊", 150.0)
        assert session.weighed("猪里脊").ok
        assert session.weighed("猪里脊").measured_g == 150.0

    def test_边界值刚好在容差内算合格(self, ctx):
        """「偏差超过 ±5% 才锁」→ 正好 5%（157.5g）不锁。"""
        _repo, _svc, session = ctx
        assert session.weigh("猪里脊", 157.5).ok
        assert not session.weigh("猪里脊", 157.6).ok

    def test_小料按绝对容差(self, ctx):
        """生抽 8g 的 5% 只有 0.4g，HX711 抖一下就超——必须抬到 ±1g。"""
        _repo, _svc, session = ctx
        check = session.weigh("生抽", 8.7)
        assert check.ok, f"8.7g 与 8g 差 0.7g，落在 ±{MIN_ABS_TOLERANCE_G}g 内应合格"
        assert not session.weigh("生抽", 9.6).ok

    def test_不在配方里的食材被拒并列出应称项(self, ctx):
        _repo, _svc, session = ctx
        with pytest.raises(WeighError) as excinfo:
            session.weigh("牛肉", 100.0)
        assert "青椒" in str(excinfo.value)

    def test_负读数被拒(self, ctx):
        """HX711 没去皮时读数是负的，直接判「少了 150g」会误导师傅。"""
        _repo, _svc, session = ctx
        with pytest.raises(WeighError) as excinfo:
            session.weigh("猪里脊", -12.0)
        assert "去皮" in str(excinfo.value)

    def test_称重落process_event(self, ctx):
        _repo, _svc, session = ctx
        session.weigh("猪里脊", 165.0, staff_id=1)
        event = session.events[-1]
        assert event.kind == "weigh"
        assert event.state == "over"
        assert event.payload["measured_g"] == 165.0
        assert event.staff_id == 1


# ================================================================ 锁定
class TestLock:
    def test_锁定期间不能确认下一步(self, ctx):
        _repo, _svc, session = ctx
        session.weigh("猪里脊", 165.0)
        with pytest.raises(StepLockedError) as excinfo:
            session.confirm(1, staff_id=1)
        assert "猪里脊" in str(excinfo.value)

    def test_锁定只影响未确认的步骤(self, ctx):
        """已确认的步骤不该被回滚——原料超差是配菜环节的事，不是切配白做了。"""
        _repo, _svc, session = ctx
        session.weigh("猪里脊", 150.0)
        session.confirm(1, staff_id=1)
        session.weigh("青椒", 200.0)
        with pytest.raises(StepLockedError):
            session.confirm(2, staff_id=1)
        assert session.confirmed_steps == (1,)


# ================================================================ #13 SOP
class TestSop:
    def test_按序确认(self, ctx):
        _repo, _svc, session = ctx
        result = session.confirm(1, staff_id=1)
        assert result.ok and result.ccp
        assert session.next_step().step_no == 2

    def test_不能跳步(self, ctx):
        _repo, _svc, session = ctx
        with pytest.raises(WeighError) as excinfo:
            session.confirm(3, staff_id=1)
        assert "下一步应确认步骤 1" in str(excinfo.value)

    def test_需要刷卡的步骤必须带身份(self, ctx):
        """写进 process_events 的身份是事后追责与台账的唯一凭据。"""
        _repo, _svc, session = ctx
        with pytest.raises(WeighError) as excinfo:
            session.confirm(1, staff_id=None)
        assert "刷卡" in str(excinfo.value)

    def test_不需刷卡的步骤可以无身份(self, ctx):
        _repo, _svc, session = ctx
        session.confirm(1, staff_id=1)
        assert session.confirm(2).ok

    def test_重复确认幂等(self, ctx):
        """TFT 双击很常见；报错会让人以为没生效，然后去点第三次。"""
        _repo, _svc, session = ctx
        session.confirm(1, staff_id=1)
        again = session.confirm(1, staff_id=1)
        assert again.ok and "此前已确认" in again.detail
        assert session.confirmed_steps == (1,)

    def test_不存在的步骤号(self, ctx):
        _repo, _svc, session = ctx
        with pytest.raises(WeighError) as excinfo:
            session.confirm(99, staff_id=1)
        assert "有效步骤" in str(excinfo.value)

    def test_确认落process_event带身份(self, ctx):
        _repo, _svc, session = ctx
        session.confirm(1, staff_id=3)
        event = session.events[-1]
        assert event.kind == "sop_step"
        assert event.staff_id == 3
        assert event.payload["ccp"] is True


# ================================================================ 出餐门禁
class TestServeGate:
    def test_跳过CCP无法出餐(self, ctx):
        _repo, _svc, session = ctx
        weigh_all(session)
        session.confirm(1, staff_id=1)
        with pytest.raises(NotReadyError) as excinfo:
            session.serve(staff_id=1)
        assert any("关键控制点" in text for text in excinfo.value.blockers)
        assert session.item.state is ItemState.COOKING

    def test_配菜没称完不能出餐(self, ctx):
        _repo, _svc, session = ctx
        confirm_all(session)
        with pytest.raises(NotReadyError) as excinfo:
            session.serve(staff_id=1)
        assert any("配菜未核验" in text for text in excinfo.value.blockers)

    def test_一次列全所有阻塞原因(self, ctx):
        """逐条抛会让师傅「补一样、被拒一次」循环，现场会直接打电话关系统。"""
        _repo, _svc, session = ctx
        session.weigh("猪里脊", 165.0)
        blockers = session.blockers()
        assert len(blockers) == 3      # 未核验 3 项 + 超差 1 项 + CCP 未确认 3 项

    def test_全部满足可以出餐(self, ctx):
        _repo, _svc, session = ctx
        weigh_all(session)
        confirm_all(session)
        assert session.ready
        order = session.serve(staff_id=1)
        assert order.state.value == "ready"
        assert session.item.state is ItemState.READY

    def test_出餐只推到ready(self, ctx):
        """上桌是传菜的动作；合成一步就把传菜时间混进出餐时长统计。"""
        _repo, _svc, session = ctx
        weigh_all(session)
        confirm_all(session)
        session.serve(staff_id=1)
        assert session.item.state is ItemState.READY
        assert session.item.station_id == 1, "灶要等释放动作才腾出来"

    def test_已取消的项不能开课(self, ctx):
        repo, svc, session = ctx
        svc.cancel_item(session.item.id, reason="退菜", force=True)
        with pytest.raises(WeighError) as excinfo:
            CookingSession(repo, svc, session.item.id, clock=FakeClock())
        assert "无需再做" in str(excinfo.value)

    def test_没有配方的菜直接可出餐(self):
        """没配方的菜（如清炒时蔬）不该被卡死——否则只能给每道菜都配 SOP。"""
        clock = FakeClock()
        repo = MemoryProcessRepository.from_seed()
        repo.set_sold_out(3, False)  # seed 里清炒时蔬是沽清演示位，先恢复在售
        svc = OrderService(repo, clock=clock)
        order = svc.place_order(table_id=1, items=[("D-QGD", 1)])
        item = order.items[0]
        svc.dispatch_item(item.id, 1)
        svc.start_cooking(item.id)
        session = CookingSession(repo, svc, item.id, clock=clock)
        assert session.ready
        assert session.serve(staff_id=1).state.value == "ready"


# ================================================================ 视图
class TestSummary:
    def test_摘要给TFT够用(self, ctx):
        _repo, _svc, session = ctx
        session.weigh("猪里脊", 150.0)
        view = session.summary()
        assert view["dish"] == "青椒肉丝"
        assert view["locked"] is False
        assert "切配" in view["next_step"]
        assert len(view["blockers"]) >= 1
