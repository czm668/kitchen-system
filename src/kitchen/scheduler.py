# -*- coding: utf-8 -*-
"""轻量定时任务托管（A3）。

零第三方依赖（标准库 threading 就够——任务个位数、秒级精度），替代
APScheduler 的「演示可用生产可换」位：调度语义（daily HH:MM / interval
秒级）与 APScheduler 一致，prod 换 APScheduler 时 JobSpec 直接平移。

四个定时业务点的现状（A3 范围依据侦察结果）：
- 切档：`KitchenRuntime.tick()` 每帧 `profile_at()` 变了才 `set_profile()`——
  **已帧内自动化，无调度缺口**；
- 工单升级：`AppState._collect()` 每帧查 `due_escalations()`——同上；
- 证件扫描（#9 每日）与留样销毁（#14 周期）：域层早有 `run()` / `sweep()`，
  但此前只有 demo 脚本手动调——这两个才是真缺的，由本调度器托管。

设计纪律：
- 崩溃隔离：单个任务抛异常只计失败数，不影响其他任务与调度线程；
- 不并发执行同一任务（上一轮没跑完则跳过本轮并计数 `skipped`）；
- 任务 fn 返回 dict（摘要）→ 通过 `on_result` 回调进业务事件流。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable


@dataclass
class JobSpec:
    """任务定义。`at`（"HH:MM"，每日）与 `every_sec`（间隔秒）二选一。"""

    name: str
    fn: Callable[[], dict[str, Any]]
    at: str | None = None          # daily 时刻，如 "06:00"
    every_sec: float | None = None  # interval 秒
    enabled: bool = True

    def __post_init__(self) -> None:
        if (self.at is None) == (self.every_sec is None):
            raise ValueError(f"任务 {self.name!r}：at 与 every_sec 必须二选一")
        if self.at is not None:
            hour, _, minute = self.at.partition(":")
            if not (hour.isdigit() and minute.isdigit()
                    and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError(f"任务 {self.name!r}：at 格式应为 HH:MM，实际 {self.at!r}")
        if self.every_sec is not None and self.every_sec <= 0:
            raise ValueError(f"任务 {self.name!r}：every_sec 必须 > 0")


@dataclass
class _JobState:
    spec: JobSpec
    next_run: float = 0.0           # wall-clock 时间戳
    run_count: int = 0
    failures: int = 0
    skipped: int = 0
    last_ok: bool | None = None     # None=从未跑过
    last_run_at: str | None = None  # 人可读时刻
    last_duration_ms: int = 0
    last_result: dict[str, Any] | None = None
    running: bool = False


class TaskScheduler:
    """轮询式调度器：守护线程 + Event.wait(poll)，到点同步执行任务。

    同步执行的取舍：任务个位数且都很快（扫描/清理），一次执行串行做完全部
    比线程池好排查——某个任务卡死时其余任务会被推迟并显示 skipped，
    问题在生产里一眼可见，而不是埋在并发里。
    """

    def __init__(self, jobs: list[JobSpec], *, poll_sec: float = 0.5,
                 on_result: Callable[[str, dict[str, Any]], None] | None = None,
                 wall_clock: Callable[[], float] | None = None) -> None:
        if not jobs:
            raise ValueError("jobs 不能为空")
        names = [job.name for job in jobs]
        if len(names) != len(set(names)):
            raise ValueError(f"任务名重复：{names}")
        self._specs = jobs
        self._poll_sec = float(poll_sec)
        self._on_result = on_result
        self._wall = wall_clock or time.time
        self._states: dict[str, _JobState] = {}
        for spec in jobs:
            state = _JobState(spec=spec)
            state.next_run = self._first_run(spec)
            self._states[spec.name] = state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="kitchen-scheduler"
        )
        self._thread.start()

    def stop(self, *, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 调度核心
    def _first_run(self, spec: JobSpec) -> float:
        if spec.every_sec is not None:
            return self._wall() + spec.every_sec
        return self._next_daily(spec.at or "", self._wall())

    @staticmethod
    def _next_daily(at: str, now_wall: float) -> float:
        hour, _, minute = at.partition(":")
        base = datetime.fromtimestamp(now_wall).replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0
        )
        if base.timestamp() <= now_wall:
            base += timedelta(days=1)
        return base.timestamp()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._wall()
            for state in self._states.values():
                if not state.spec.enabled or state.running:
                    continue
                if now >= state.next_run:
                    self._run_job(state, now)
            self._stop.wait(self._poll_sec)

    def _run_job(self, state: _JobState, now: float) -> None:
        # 上一轮还没跑完（同步执行被卡住）→ 跳过本轮，问题可见化
        if state.running:
            state.skipped += 1
            state.next_run = now + (state.spec.every_sec or 60)
            return
        state.running = True
        started = time.perf_counter()
        try:
            result = state.spec.fn() or {}
            state.run_count += 1
            state.last_ok = True
            state.last_result = result
            if self._on_result is not None and result:
                try:
                    self._on_result(state.spec.name, result)
                except Exception:  # noqa: BLE001 —— 事件回调失败不影响任务统计
                    pass
        except Exception as exc:  # noqa: BLE001 —— 崩溃隔离：任务炸不影响调度线程
            state.run_count += 1
            state.failures += 1
            state.last_ok = False
            state.last_result = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            state.last_duration_ms = int((time.perf_counter() - started) * 1000)
            state.last_run_at = datetime.fromtimestamp(
                self._wall()).strftime("%H:%M:%S")
            state.running = False
            state.next_run = (self._wall() + state.spec.every_sec
                              if state.spec.every_sec is not None
                              else self._next_daily(state.spec.at or "",
                                                    self._wall()))

    # ------------------------------------------------------------ 手动触发与体检
    def trigger(self, name: str) -> dict[str, Any]:
        """手动立即执行（管理端「立刻扫描」按钮 / 测试用），返回结果。"""
        state = self._states.get(name)
        if state is None:
            raise KeyError(f"未知任务：{name}")
        self._run_job(state, self._wall())
        if state.last_ok is False and state.last_result:
            raise RuntimeError(state.last_result["error"])
        return dict(state.last_result or {})

    def snapshot(self) -> dict[str, Any]:
        jobs = []
        for state in self._states.values():
            jobs.append({
                "name": state.spec.name,
                "kind": "daily" if state.spec.at else "interval",
                "at": state.spec.at,
                "every_sec": state.spec.every_sec,
                "enabled": state.spec.enabled,
                "running": state.running,
                "next_run_at": datetime.fromtimestamp(state.next_run).strftime(
                    "%Y-%m-%d %H:%M:%S"),
                "run_count": state.run_count,
                "failures": state.failures,
                "skipped": state.skipped,
                "last_ok": state.last_ok,
                "last_run_at": state.last_run_at,
                "last_duration_ms": state.last_duration_ms,
                "last_result": state.last_result,
            })
        return {"running": self.running, "jobs": jobs}
