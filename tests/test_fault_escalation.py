# -*- coding: utf-8 -*-
"""工单「超时升级」只该发一次（2026-11-18 云端实测的两个坑）。

云端上线半小时后巡检发现：`alarm_events` 25 分钟涨到 4729 行，全是同 6 张
卡死工单反复升级。链路上有两个独立缺陷，单看单元测试都看不出来：

1. **时钟域不一致**：工单的 `opened_at / due_at` 是 `state.monotonic()`（单调秒），
   而 `_collect` 用 `state.wall_epoch()`（epoch）去判 `due_escalations`。
   `now >= due_at + 900` 对 epoch 永远成立 → 工单一开就地「超时」，
   升级说明里还会算出「超期 2988 万分钟」这种荒谬值。
2. **只发事件不升级**：`due_escalations()` 是只读查询，`escalated_at` 要靠
   调用方 `escalate()` 写回。少调一次，`is_due_for_escalation` 每帧都成立，
   于是同样的工单每秒发 3 行，一天 26 万行 —— 30 GB 系统盘迟早被灌满。

正确语义：开单 30 分钟没人接单 → 升级一次（L1→L2），之后安静。
"""
from __future__ import annotations

from types import SimpleNamespace

from api_support import make_state


def _escalations(state, ticket_id: int | None = None) -> list[dict]:
    """升级事件。按 `escalated_at` 认，而不是按 `kind`：

    `_emit` 是 `{"seq":…, "kind": kind, **body}`，而工单 payload 自带
    `"kind": "stuck"`（故障种类），把事件种类覆盖掉了 —— 落库不受影响
    （`record_event` 用的是函数参数 kind，云上 scenario 列仍是
    fault-escalate），但 SSE 流的 `kind` 看不到 "fault-escalate"。
    这是独立的老问题，不在本次修复范围，测试按状态字段认更稳。
    """
    rows = [row for row in state.events(0, limit=0) if row.get("escalated_at") is not None]
    if ticket_id is not None:
        rows = [row for row in rows if row.get("id") == ticket_id]
    return rows


def _feed_constant(state, frame, *, start=0.0, step=5.0, count=80) -> float:
    """把同一帧喂进去 = 读数卡死；返回喂完后的时刻。"""
    t = start
    for _ in range(count):
        state.health.observe(frame, now=t)
        t += step
    return t


def _collect(state, frame) -> None:
    state._collect(SimpleNamespace(verdicts=(), guard_verdicts=()), frame)


def test_卡死工单超时升级只发一次(tmp_path):
    state = make_state(tmp_path)
    try:
        frame = state.demo.frame(0.0)
        t = _feed_constant(state, frame)

        tickets = list(state.health.tickets(open_only=True))
        assert tickets, "80 帧读数不变，应当开出卡死工单"
        ticket = tickets[0]

        # ① 时钟域：工单时间戳必须是单调秒（几十到几百秒量级），
        #    不是 epoch（1.7e9）。这条挂了说明 observe 与判定又用了两个钟。
        assert ticket.opened_at <= t, "工单 opened_at 混进了 epoch 时钟"
        assert ticket.opened_at < 1e6

        # ② 未到升级点：不发事件，等级不动
        state.monotonic = lambda: ticket.due_at + 899.0
        _collect(state, frame)
        assert _escalations(state, ticket.id) == []
        assert ticket.level == 1

        # ③ 到点：正好发一条，等级 L1→L2，escalated_at 落上
        state.monotonic = lambda: ticket.due_at + 900.0
        _collect(state, frame)
        rows = _escalations(state, ticket.id)
        assert len(rows) == 1
        assert ticket.level == 2
        assert ticket.escalated_at == ticket.due_at + 900.0
        assert rows[0]["level"] == 2 and rows[0]["id"] == ticket.id

        # ④ 回归点：再推 10 帧不许重复发（就是云端每秒 3 行的那条路）
        for extra in range(1, 11):
            state.monotonic = lambda e=extra: ticket.due_at + 900.0 + e
            _collect(state, frame)
        assert len(_escalations(state, ticket.id)) == 1, "升级事件重复发出"
    finally:
        state.stop()
