# -*- coding: utf-8 -*-
"""工单「超时升级」只该发一次（2026-11-18 云端实测的两个坑）。

云端上线半小时后巡检发现：`alarm_events` 25 分钟涨到 4729 行，全是同 6 张
卡死工单反复升级。链路上有两个独立缺陷，单看单元测试都看不出来：

1. **时钟域不一致**：工单的 `opened_at / due_at` 与 `_collect` 的升级判定
   必须用同一根时间轴。这里踩过两个方向：判定用 epoch、开单用单调秒，
   `now >= due_at + 900` 永远成立 → 工单一开就地「超时」，升级说明里算出
   「超期 2988 万分钟」；反过来（开单 epoch、判定单调秒）则永远不升级。
   现行口径：**工单走单调轴**（判定窗口量的是真实采样节奏，演示加速不能把
   传感器「变坏」），   展示层负责把它换算成墙钟——`views.ticket_view` 走
   `state.wall_from_monotonic(opened_at)` 做逆变换。早期直接拿 epoch 减单调秒，
   工单页显示「20777 天」；后来改成「当刻漂移量」外推，`scale=60` 时又把
   1 小时前开的单显示到 113 小时以后（审查 P1-1）。两种错法都在这里钉住。
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

        # ① 时钟域：工单时间戳必须是单调秒（几十到几百秒量级），不是 epoch。
        #    这条挂了说明 observe 与判定又用了两个钟。
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


def test_工单已开时长不会被算成上万年(tmp_path):
    """工单页的「已开」就是 `epoch_now - opened_at`，两端口径必须一致。

    2026-11-19 现场发现：工单页显示「20777 天」。根因是 `state.tick()` 把
    **单调秒**一路喂到了 `health.observe()`，工单 `opened_at` 落成开机后几百秒，
    展示层再拿 epoch 去减，差出 1.79e9 秒（≈20777 天）。同一根因还让档位表
    永远停在 1970 年的闭店档。这里从「跑真的 tick」这一层守住。
    """
    from kitchen.api.views import ticket_view

    state = make_state(tmp_path, time_scale=60.0)
    try:
        # 99℃ 远超量程（-10~60）→ 当帧就开单，不用等卡死窗口
        state.tick({"t": 99.0, "h": 50.0, "gas_level": 0.1, "hood_temp": 40.0,
                    "sterilize_temp": 60.0, "fridge_temp": 4.0, "freezer_temp": -18.0})
        rows = ticket_view(state, {})["tickets"]
        sensor = [row for row in rows if row.get("source") == "sensor"]
        assert sensor, "超量程读数应当开出传感器工单"
        oldest = max(float(row["age_sec"]) for row in sensor)
        # 工单是刚刚开的，age 只能是秒级；1.79e9 秒（20777 天）说明又混钟了
        assert 0 <= oldest < 60, f"已开时长异常：{oldest}s"
        assert all("1970" not in (row["opened_at_text"] or "") for row in sensor)
        # 未到 30 分钟处理时限，不该判超时
        assert not any(row["overdue"] for row in sensor)
    finally:
        state.stop()


def test_加速档下开单时间按开单当刻换算而不是当刻漂移(tmp_path):
    """审查 P1-1 回归：外推「当刻漂移量」会把历史时间戳推到未来。

    旧实现 `opened_at_text = (wall_now - mono_now) + opened_at`，偏差
    `= (mono_now - opened_at) × (scale - 1)`。`scale=1` 时恰好为 0，
    所以本地不加速永远看不出来；`scale=60` 时一张开了 5 分钟的单
    会被显示在 4.9 小时之后。这里白盒推进单调轴，避免 sleep 5 分钟。
    """
    from kitchen.api.views import _ts_text, ticket_view

    state = make_state(tmp_path, time_scale=60.0)
    try:
        mono_before = state.monotonic()
        wall_before = state.wall_epoch()
        # 99℃ 远超量程 → 当帧就开单
        state.tick({"t": 99.0, "h": 50.0, "gas_level": 0.1, "hood_temp": 40.0,
                    "sterilize_temp": 60.0, "fridge_temp": 4.0, "freezer_temp": -18.0})
        tickets = state.health.tickets()
        assert tickets, "超量程读数应当开出传感器工单"
        opened_at = tickets[0].opened_at  # 单调秒

        # 期望值独立算：开单当刻墙钟 = 开单前墙钟 + 单调差 × scale
        expected = _ts_text(wall_before + (opened_at - mono_before) * 60.0)

        # 推进 5 分钟真实时间（改 t0 而不是 sleep）；旧实现会多算 300×59 秒
        state._t0 -= 300.0
        rows = ticket_view(state, {})["tickets"]
        actual = next((r["opened_at_text"] for r in rows
                       if r.get("source") == "sensor"), None)
        assert actual == expected, (
            f"开单时间被外推了：实际 {actual}，应为 {expected}"
        )
        # 判定仍走单调轴，推进 5 分钟后应表现为「已开 5 分钟」而不是几天
        age = max(float(r["age_sec"]) for r in rows if r.get("source") == "sensor")
        assert 299 <= age <= 301, f"已开时长异常：{age}s"
    finally:
        state.stop()
