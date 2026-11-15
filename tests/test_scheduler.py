# -*- coding: utf-8 -*-
"""A3 定时任务托管测试。

两层：调度器本体（触发/崩溃隔离/统计/手动触发）+ AppState 接线
（retention 装配、任务经 on_result 进事件流、体检接口）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from kitchen.scheduler import JobSpec, TaskScheduler


# ============================================================ JobSpec 校验
def test_spec_必须二选一() -> None:
    with pytest.raises(ValueError):
        JobSpec("bad", lambda: {}, at="06:00", every_sec=10)
    with pytest.raises(ValueError):
        JobSpec("bad", lambda: {})


def test_spec_非法时刻与非法间隔() -> None:
    with pytest.raises(ValueError):
        JobSpec("bad", lambda: {}, at="25:00")
    with pytest.raises(ValueError):
        JobSpec("bad", lambda: {}, at="6pm")
    with pytest.raises(ValueError):
        JobSpec("bad", lambda: {}, every_sec=0)


# ============================================================ 调度行为
def test_interval_任务按周期触发() -> None:
    results: list[tuple[str, dict]] = []
    sched = TaskScheduler(
        [JobSpec("beat", lambda: {"ok": True}, every_sec=0.2)],
        poll_sec=0.05,
        on_result=lambda name, r: results.append((name, r)),
    )
    sched.start()
    time.sleep(0.75)
    sched.stop()
    snap = sched.snapshot()
    beat = snap["jobs"][0]
    assert beat["run_count"] >= 2
    assert beat["failures"] == 0
    assert beat["last_ok"] is True
    assert any(name == "beat" for name, _ in results)


def test_崩溃隔离_坏任务不影响好任务() -> None:
    def boom() -> dict:
        raise RuntimeError("炸了")

    sched = TaskScheduler(
        [
            JobSpec("bad", boom, every_sec=0.1),
            JobSpec("good", lambda: {"n": 1}, every_sec=0.15),
        ],
        poll_sec=0.05,
    )
    sched.start()
    time.sleep(0.6)
    sched.stop()
    jobs = {job["name"]: job for job in sched.snapshot()["jobs"]}
    assert jobs["bad"]["failures"] >= 1
    assert jobs["bad"]["last_ok"] is False
    assert "炸了" in jobs["bad"]["last_result"]["error"]
    assert jobs["good"]["failures"] == 0
    assert jobs["good"]["run_count"] >= 1  # 坏任务反复炸，好任务照常跑


def test_trigger_手动执行与异常上抛() -> None:
    def boom() -> dict:
        raise RuntimeError("手动也炸")

    sched = TaskScheduler(
        [
            JobSpec("scan", lambda: {"found": 3}, at="23:59"),
            JobSpec("boom", boom, at="23:59"),
        ],
    )
    assert sched.trigger("scan") == {"found": 3}
    with pytest.raises(RuntimeError):
        sched.trigger("boom")
    with pytest.raises(KeyError):
        sched.trigger("nope")


def test_daily_下次运行在明天() -> None:
    sched = TaskScheduler([JobSpec("daily", lambda: {}, at="00:00")])
    snap = sched.snapshot()["jobs"][0]
    # 今天 00:00 已过 → 下次运行应是明天
    assert "00:00:00" in snap["next_run_at"]
    assert snap["run_count"] == 0
    sched.stop()


# ============================================================ AppState 接线
def test_state_默认不起调度器(tmp_path: Path) -> None:
    from tests.api_support import make_state

    state = make_state(tmp_path)
    assert state.scheduler is None
    assert state.retention is not None  # retention 服务恒装配（视图要用）


def test_state_调度器接线_留样扫描进事件流(tmp_path: Path) -> None:
    from tests.api_support import make_state

    state = make_state(
        tmp_path,
        scheduler_enabled=True,
        retention_sweep_sec=3600.0,  # 别让它自动跑，手动触发验证
    )
    try:
        state.start()
        assert state.scheduler is not None
        names = [job["name"] for job in state.scheduler.snapshot()["jobs"]]
        assert names == ["certificate-sweep", "retention-sweep"]

        # 手动触发证件扫描（种子数据健康证是相对日期，可能有临期）
        result = state.scheduler.trigger("certificate-sweep")
        assert "describe" in result and "expired" in result
        kinds = [event["kind"] for event in state.events()]
        assert "sched-certificate-sweep" in kinds

        # 留样扫描：无到期留样时 opened=0，事件仍进流
        result = state.scheduler.trigger("retention-sweep")
        assert result["opened"] == 0
        kinds = [event["kind"] for event in state.events()]
        assert "sched-retention-sweep" in kinds
    finally:
        state.stop()
    # stop 后调度器回收
    assert state.scheduler is None


def test_state_体检接口_未启用与启用两种口径(tmp_path: Path) -> None:
    from kitchen.api.views import scheduler_view
    from tests.api_support import make_state

    state = make_state(tmp_path)
    assert scheduler_view(state, {}) == {"enabled": False}

    state2 = make_state(tmp_path / "b", scheduler_enabled=True,
                        retention_sweep_sec=3600.0)
    snap = scheduler_view(state2, {})
    assert snap["enabled"] is True
    assert len(snap["jobs"]) == 2
    state2.stop()
