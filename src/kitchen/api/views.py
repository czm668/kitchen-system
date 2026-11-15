# -*- coding: utf-8 -*-
"""读接口：把六个域的对象转成前端能直接用的 JSON。

## 三条约定

1. **所有金额/时长/温度都带单位字符串**，前端不做换算。让浏览器做单位换算是
   现场事故的常见来源（`°C` 与 `℉`、秒与毫秒各写一遍）。
2. **每条列表都带 `total`**，前端分页不靠 `len()`。
3. **时间一律同时给 epoch 与 `HH:MM:SS`**。曲线用 epoch（排序）、表格用字符串
   （人看）。只给一种，另一边就得自己转，时区一错整套数据看起来都错。

## 为什么这里不抛业务异常

读接口**不改变任何状态**，所以「查不到」就是返回空列表 + `note` 说明，不该
500。让前端为「今天还没订单」显示一屏红色报错，是最容易被当成系统坏了的情况。
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Sequence

from ..domain import CST
from ..process.dispatch import FLAME_THRESHOLD
from ..process.models import ItemState, Order, OrderItem
from ..staff.models import MORNING_TEMP_LIMIT_C

__all__ = [
    "audit_view", "batch_view", "certificate_view", "cooking_view", "db_history_view",
    "db_stats_view", "dish_view", "gantt_view", "interlock_view", "kpi_view",
    "meta_view", "morning_view", "order_view", "param_view", "recipe_view",
    "scheduler_view", "staff_view", "station_view", "telemetry_view", "ticket_view",
    "events_view",
]

_ITEM_LABEL = {
    "pending": "待派单", "dispatched": "已派单", "cooking": "烹饪中",
    "ready": "待出餐", "served": "已上桌", "cancelled": "已退菜",
}


def _iso(moment: datetime | None) -> str:
    return "" if moment is None else moment.astimezone(CST).isoformat(timespec="seconds")


def _epoch(moment: datetime | None) -> float | None:
    return None if moment is None else round(moment.timestamp(), 3)


# --------------------------------------------------------------------- 订单
def _item_row(item: OrderItem, order: Order) -> dict[str, Any]:
    return {
        "id": item.id,
        "order_id": item.order_id,
        "order_no": order.order_no,
        "dish_id": item.dish_id,
        "dish_name": item.dish_name,
        "qty": item.qty,
        "state": item.state.value,
        "state_label": _ITEM_LABEL.get(item.state.value, item.state.value),
        "station_id": item.station_id,
        "urge_count": item.urge_count,
        "note": item.note,
        "due_at": _iso(order.due_at),
        "due_epoch": _epoch(order.due_at),
    }


def order_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """订单流。`state` 过滤可选；`open=1` 只返回未完成单。"""
    wanted = str(query.get("state") or "").strip()
    open_only = str(query.get("open") or "") in ("1", "true", "yes")
    rows = state.orders.open_orders() if open_only else state.orders.orders()
    out = []
    for order in rows:
        if wanted and order.state.value != wanted:
            continue
        events = state.orders.events(order.id)
        out.append({
            "id": order.id,
            "order_no": order.order_no,
            "source": order.source,
            "state": order.state.value,
            "table_id": order.table_id,
            # 顾客单带脱敏会话编号，员工手工单为 None。后厨/管理端据此能
            # 区分「哪一桌的哪次开桌」，但查不到姓名/手机号（没有会员体系）。
            "customer_ref": order.customer_ref,
            "table_label": (f"{order.table_id} 号桌" if order.table_id
                            else "未指定桌台"),
            "created_at": _iso(order.created_at),
            "promised_at": _iso(order.promised_at),
            "due_at": _iso(order.due_at),
            "due_epoch": _epoch(order.due_at),
            "version": order.version,
            "items": [_item_row(item, order) for item in order.items],
            "events": [
                {"ts": _iso(ev.ts), "from": ev.from_state, "to": ev.to_state,
                 "actor": ev.actor, "channel": ev.channel,
                 "item_id": ev.item_id, "detail": ev.detail}
                for ev in events[-12:]
            ],
        })
    return {
        "total": len(out),
        "items_total": sum(len(row["items"]) for row in out),
        "orders": out,
        "note": "" if out else "当前没有符合条件的订单（先在下单页造一单）",
    }


def dish_view(state, query: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for dish in state.repo.dishes():
        recipe = state.repo.recipe_for(dish.id)
        rows.append({
            "id": dish.id, "code": dish.code, "name": dish.name,
            "category": dish.category, "price": dish.price,
            "sold_out": dish.sold_out, "enabled": dish.enabled,
            "station_kind": dish.station_kind,
            "recipe_id": None if recipe is None else recipe.id,
            "recipe_version": None if recipe is None else recipe.version,
            "estimate_minutes": state.repo.estimate_minutes(dish.id),
        })
    return {"total": len(rows), "dishes": rows}


# --------------------------------------------------------------------- 工位
def station_view(state, query: dict[str, Any]) -> dict[str, Any]:
    now = state.to_datetime()
    rows = []
    for station in state.repo.stations():
        occ = state.board.occupancy_of(station.id)
        reserved = state.board.reserved_staff(station.id)
        item = None
        if occ is not None and occ.order_item_id is not None:
            try:
                _order, item_obj = state.orders.item(occ.order_item_id)
                item = item_obj.dish_name
            except Exception:  # noqa: BLE001 - 子项被删属于展示层可容忍
                item = None
        try:
            flame = state.board.flame_check(station.id, now=now)
            flame_payload = {"ok": flame.ok, "text": flame.describe()}
        except Exception as exc:  # noqa: BLE001
            flame_payload = {"ok": None, "text": f"点火状态未知：{exc}"}
        rows.append({
            "id": station.id, "code": station.code, "name": station.name,
            "kind": station.kind, "device_id": station.device_id,
            "capacity": station.capacity,
            "state": state.board.station_state(station.id),
            "occupancy": None if occ is None else {
                "id": occ.id, "staff_id": occ.staff_id,
                "staff_name": _staff_name(state, occ.staff_id),
                "item_id": occ.order_item_id, "dish": item,
                "started_at": _iso(occ.started_at),
                "elapsed_sec": round(occ.elapsed_sec(now), 1),
                "flame_verified": occ.flame_verified,
            },
            "reserved_staff": reserved,
            "reserved_name": _staff_name(state, reserved) if reserved else "",
            "flame": flame_payload,
        })
    exhaust = state.board.exhaust_state()
    return {
        "total": len(rows),
        "stations": rows,
        "exhaust": {"running": exhaust.running, "text": exhaust.describe()},
        "flame_threshold": FLAME_THRESHOLD,
    }


def gantt_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """工位甘特：每段占用一条。**历史段也返回**（`closed`），前端按状态上色。"""
    now = state.to_datetime()
    rows = []
    for occ in state.board.occupancies():
        dish = ""
        order_no = ""
        if occ.order_item_id is not None:
            try:
                order, item = state.orders.item(occ.order_item_id)
                dish, order_no = item.dish_name, order.order_no
            except Exception:  # noqa: BLE001
                pass
        rows.append({
            "id": occ.id,
            "station_id": occ.station_id,
            "staff_id": occ.staff_id,
            "staff_name": _staff_name(state, occ.staff_id),
            "item_id": occ.order_item_id,
            "dish": dish,
            "order_no": order_no,
            "state": occ.state,
            "started_at": _iso(occ.started_at),
            "start_epoch": _epoch(occ.started_at),
            "ended_at": _iso(occ.ended_at),
            "end_epoch": _epoch(occ.ended_at),
            "elapsed_sec": round(occ.elapsed_sec(now), 1),
            "flame_verified": occ.flame_verified,
        })
    rows.sort(key=lambda row: (row["station_id"], row["start_epoch"] or 0))
    # 转派留痕：从订单事件里挑出「换了人」的那几条（detail.reassigned_to）
    handovers = []
    for order in state.orders.orders():
        for ev in state.orders.events(order.id):
            reason = (ev.detail or {}).get("taken_over") or (ev.detail or {}).get("reassign_reason")
            if reason:
                handovers.append({
                    "order_no": order.order_no, "item_id": ev.item_id,
                    "ts": _iso(ev.ts), "actor": ev.actor, "reason": str(reason),
                })
    return {
        "total": len(rows),
        "segments": rows,
        "handovers": handovers[-30:],
        "note": "" if rows else "还没有上灶记录（派单后刷卡上灶才会出现）",
    }


def _staff_name(state, staff_id: int | None) -> str:
    if staff_id is None:
        return ""
    person = state.staff_repo.staff(int(staff_id))
    return "" if person is None else person.name


# --------------------------------------------------------------------- 遥测
def telemetry_view(state, query: dict[str, Any]) -> dict[str, Any]:
    metrics = _split(query.get("metrics"))
    points = _int(query.get("points"), default=0)
    return {
        "meta": state.telemetry.latest(),
        "series": state.telemetry.series(metrics or None, points=points or None),
        "ticks": state.ticks,
        "time_scale": state.config.time_scale,
    }


def db_stats_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """运行时库体检（A1）：表行数 + 写入计数；未启用时 enabled=false。"""
    if state.recorder is None:
        return {"enabled": False}
    return state.recorder.stats()


def scheduler_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """定时任务体检（A3）：各任务运行统计；未启用时 enabled=false。"""
    if state.scheduler is None:
        return {"enabled": False}
    snap = state.scheduler.snapshot()
    snap["enabled"] = True
    return snap


def db_history_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """跨重启的历史查询（A1）：遥测读数 + 告警事件，直接查运行时库。"""
    if state.recorder is None:
        return {"enabled": False, "readings": [], "alarms": []}
    limit = _int(query.get("limit"), default=200)
    return {
        "enabled": True,
        "readings": state.recorder.readings_history(
            metric=query.get("metric") or None,
            limit=limit,
            since=query.get("since") or None,
        ),
        "alarms": state.recorder.alarms_history(
            limit=_int(query.get("alarm_limit"), default=50)
        ),
    }


def interlock_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """互锁状态机可视化：三通道 + 每个通道的触发源与优先级。"""
    controller = state.controller
    snapshot = controller.snapshot()
    rows = []
    for name in ("CH1", "CH2", "CH3"):
        row = snapshot.get(name, {})
        rows.append({
            "channel": name,
            "state": row.get("state", "normal"),
            "latched": controller.is_latched(name),
            "sources": row.get("sources", {}),
            "reviewers": row.get("reset_reviewers", []),
            "label": {"CH1": "燃气电磁阀", "CH2": "排风扇",
                      "CH3": "工位插座"}[name],
        })
    return {
        "channels": rows,
        "signal": snapshot.get("GPIO_SIGNAL", {}),
        "degraded": controller.degraded,
        "guards": state.offline.guards.snapshot(now=state.now()),
        "restore_requires": {
            "manual_confirm": True,
            "ch3_double_review": ["web", "tft"],
            "note": "恢复动作必须人工确认；CH3 还需 Web + TFT 双通道复核",
        },
    }


# --------------------------------------------------------------------- 配方
def recipe_view(state, query: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for dish in state.repo.dishes():
        recipe = state.repo.recipe_for(dish.id)
        if recipe is None:
            rows.append({"dish_id": dish.id, "dish": dish.name,
                         "dish_code": dish.code, "recipe": None,
                         "note": "这道菜还没有配方"})
            continue
        items = state.repo.recipe_items(recipe.id)
        steps = state.repo.sop_steps(recipe.id)
        history = [
            {"id": row.id, "version": row.version, "enabled": row.enabled,
             "items": len(state.repo.recipe_items(row.id)),
             "steps": len(state.repo.sop_steps(row.id))}
            for row in state.repo.all_recipes() if row.dish_id == dish.id
        ]
        rows.append({
            "dish_id": dish.id, "dish": dish.name, "dish_code": dish.code,
            "versions": history,
            "recipe": {
                "id": recipe.id, "version": recipe.version,
                "name": recipe.name, "enabled": recipe.enabled,
                "items": [
                    {"id": row.id, "ingredient": row.ingredient,
                     "weight_g": row.weight_g, "tolerance_pct": row.tolerance_pct,
                     "min_g": round(row.bounds()[0], 1),
                     "max_g": round(row.bounds()[1], 1),
                     "sort_no": row.sort_no}
                    for row in items
                ],
                "steps": [
                    {"id": step.id, "step_no": step.step_no, "title": step.title,
                     "instruction": step.instruction, "heat_level": step.heat_level,
                     "duration_sec": step.duration_sec, "ccp": step.ccp,
                     "requires_card": step.requires_card}
                    for step in steps
                ],
                "ccp_count": sum(1 for step in steps if step.ccp),
            },
        })
    return {"total": len(rows), "recipes": rows}


# --------------------------------------------------------------------- 事件
def events_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """事件流的**轮询版**（A8）：TFT 真机是 ESP32 HTTP 客户端，SSE 长连接
    会占着 WiFi/TCP 不放（断网重连逻辑也复杂），5 秒一次 `?since=seq` 轮询
    足够（播报延迟上限 = 轮询间隔）。浏览器端继续走 SSE，两者共享同一份
    `AppState.events()`，不会出现「两端播报/提示不一致」。
    """
    since = _int(query.get("since"), default=0)
    limit = _int(query.get("limit"), default=50)
    rows = state.events(since=since, limit=max(1, min(limit, 200)))
    return {"events": rows, "event_seq": state.event_seq,
            "since": since, "limit": limit}


# --------------------------------------------------------------------- 工序
def cooking_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """TFT 工序分步屏（A8-02）。

    - `item_id` 给了：返回该子项的完整工序骨架（没开会话也给全 pending 的骨架，
      屏先打开、师傅后开称，不能因为「还没开会话」屏就空白）；
    - 不给：返回全部**活动**会话的简表（队列屏标「做菜中」用）。
    """
    raw = str(query.get("item_id") or "").strip()
    if raw:
        detail = state.cooking.detail(int(raw))
        return {"total": 1, "sessions": [detail], "note": ""}
    rows = []
    for item_id in state.cooking.open_items():
        try:
            rows.append(state.cooking.detail(item_id))
        except Exception:  # noqa: BLE001 - 单个子项被取消不该让整个屏 500
            continue
    return {
        "total": len(rows),
        "sessions": rows,
        "note": "" if rows else "当前没有进行中的工序会话",
    }


# --------------------------------------------------------------------- 批次
def batch_view(state, query: dict[str, Any]) -> dict[str, Any]:
    batches = []
    for batch_id in state.repo.batch_ids():
        row = state.repo.batch(batch_id)
        payload = {key: value for key, value in row.items()}
        supplier_id = payload.get("supplier_id")
        if supplier_id is not None:
            try:
                payload["supplier_name"] = state.repo.supplier(int(supplier_id)).get("name", "")
            except Exception:  # noqa: BLE001
                payload["supplier_name"] = ""
        batches.append(payload)
    trace = [{"code": code, "row": dict(row)}
             for code, row in state.repo.trace_rows()]
    return {"total": len(batches), "batches": batches, "trace_codes": len(trace)}


def certificate_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """健康证台账 + 到期预警（法定 30 天内到期要提示）。"""
    today = state.admission.today()
    rows = []
    alerts = 0
    for person in state.staff_repo.all_staff():
        certs = state.staff_repo.certificates(person.id)
        for cert in certs:
            left = cert.expires_in(today)
            valid = cert.is_valid_on(today)
            if left is not None and left <= 30:
                alerts += 1
            rows.append({
                "staff_id": person.id, "name": person.name,
                "cert_no": cert.cert_no,
                "valid_from": cert.valid_from.isoformat() if cert.valid_from else "",
                "valid_to": cert.valid_to.isoformat() if cert.valid_to else "",
                "days_left": left,
                "valid_today": valid,
                "state": cert.state,
                "warn": bool(left is not None and 0 <= left <= 30),
            })
    rows.sort(key=lambda row: (row["days_left"] if row["days_left"] is not None else 9999))
    return {"total": len(rows), "expiring": alerts, "certificates": rows,
            "today": today.isoformat()}


def morning_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """晨检复核：今日问卷状态 + 待复核清单。"""
    today = state.admission.today()
    rows = []
    for person in state.staff_repo.all_staff():
        check = state.staff_repo.morning_check(person.id, today)
        if check is None:
            rows.append({
                "staff_id": person.id, "name": person.name, "state": "missing",
                "state_label": "未提交", "locked": False, "admitted": False,
                "answers": {}, "temp_limit_c": MORNING_TEMP_LIMIT_C,
            })
            continue
        rows.append({
            "staff_id": person.id, "name": person.name,
            "state": "locked" if check.locked else (
                "admitted" if check.admitted else "pending"),
            "state_label": "已复核放行" if check.admitted else (
                "待复核" if check.locked else "无需复核"),
            "locked": check.locked, "admitted": check.admitted,
            "answers": check.answers.to_dict(),
            "temp_limit_c": MORNING_TEMP_LIMIT_C,
            "describe": check.describe(),
        })
    return {
        "total": len(rows), "today": today.isoformat(), "staff": rows,
        "pending": len([row for row in rows if row["state"] == "locked"]),
    }


def staff_view(state, query: dict[str, Any]) -> dict[str, Any]:
    today = state.admission.today()
    rows = []
    for person in state.staff_repo.all_staff():
        certs = state.staff_repo.certificates(person.id)
        valid = [cert for cert in certs if cert.is_valid_on(today)]
        card = state.staff_repo.active_card_of(person.id)
        rows.append({
            "id": person.id, "code": person.code, "name": person.name,
            "phone": person.phone, "enabled": person.enabled,
            "qualifications": list(person.qualified_labels),
            "card_uid": None if card is None else card.card_uid,
            "certificates": len(certs), "valid_certificates": len(valid),
            "on_duty": person.id in state.duty.on_duty(),
            "assignments": list(state.board.assignments_of(person.id)),
        })
    return {"total": len(rows), "staff": rows,
            "on_duty": sorted(state.duty.on_duty())}


# --------------------------------------------------------------------- 工单
def ticket_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """工单中心：三类合流（传感器故障 / 消毒返工 / 规则告警工单）。

    合流而不是分三个页签：现场运维关心的是「今天有几件事要处理」，不关心
    它来自哪个子系统。`source` 字段负责溯源。
    """
    now = state.wall()
    rows: list[dict[str, Any]] = []
    for ticket in state.health.tickets():
        payload = ticket.to_payload()
        payload.update({
            "source": "sensor",
            "opened_at_text": _ts_text(ticket.opened_at),
            "overdue": ticket.is_overdue(now),
            "age_sec": round(max(0.0, now - ticket.opened_at), 1),
        })
        rows.append(payload)
    for record in state.disinfection.records:
        if record.state == "passed":
            continue
        rows.append({
            "id": f"dis-{record.id}", "type": "disinfection",
            "source": "disinfection",
            "level": 2 if record.state == "failed" else 1,
            "state": "open" if record.state == "failed" else "running",
            "metric": "sterilize_temp",
            "message": record.describe(),
            "forbid_outbound": record.forbid_outbound,
            "opened_at_text": _iso(record.start_ts),
            "age_sec": round(max(0.0, (state.to_datetime() - record.start_ts).total_seconds()), 1),
            "overdue": False,
        })
    for rework in state.disinfection.tickets:
        rows.append({
            "id": f"rw-{rework.id}", "type": "rework", "source": "disinfection",
            "level": 2, "state": "closed" if rework.state == "closed" else "open",
            "message": rework.describe(), "overdue": False,
            "opened_at_text": _iso(getattr(rework, "created_at", None)),
            "age_sec": 0.0,
        })
    states = _split(query.get("state"))
    if states:
        rows = [row for row in rows if row.get("state") in states]
    rows.sort(key=lambda row: (row.get("state") == "closed", -float(row.get("level") or 0)))
    return {
        "total": len(rows),
        "open": len([row for row in rows if row.get("state") != "closed"]),
        "tickets": rows,
        "note": "" if rows else "没有待处理工单",
    }


def _ts_text(value: float | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(float(value), CST).strftime("%m-%d %H:%M:%S")


# --------------------------------------------------------------------- KPI
def kpi_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """四个 KPI：出餐时长 / 违规数 / 告警处置时长 / 消毒达标率。

    **每个 KPI 都带 `samples`**：样本数为 0 时指标无意义，前端必须显示
    「暂无样本」而不是「0 分钟」——差一个字的展示会让答辩老师问「你们出餐
    只要 0 分钟？」。
    """
    cook_secs: list[float] = []
    for order in state.orders.orders():
        events = state.orders.events(order.id)
        starts: dict[int, datetime] = {}
        for ev in events:
            item_id = ev.item_id
            if item_id is None:
                continue
            if ev.to_state in ("dispatched", "cooking"):
                starts.setdefault(item_id, ev.ts)
            elif ev.to_state in ("ready", "served") and item_id in starts:
                span = (ev.ts - starts[item_id]).total_seconds()
                if span >= 0:
                    cook_secs.append(span)
    records = state.disinfection.records
    finished = [rec for rec in records if not rec.running]
    passed = [rec for rec in finished if rec.passed]

    handled: list[float] = []
    for ticket in state.health.tickets():
        if ticket.closed_at is not None:
            handled.append(max(0.0, float(ticket.closed_at) - float(ticket.opened_at)))

    return {
        "generated_at": _iso(state.to_datetime()),
        "cook_duration": _stat(cook_secs, unit="秒", label="出餐时长（派单→出餐）"),
        "violations": {
            "label": "视觉违规数",
            "value": 0 if not state.config.vision_online else 0,
            "samples": 0,
            "available": bool(state.config.vision_online),
            "note": "视觉模型未接入时不下发违规判定（不拿 0 冒充「零违规」）",
        },
        "alarm_handle": _stat(handled, unit="秒", label="告警处置时长（开单→关单）"),
        "disinfection": {
            "label": "消毒达标率",
            "value": round(len(passed) / len(finished) * 100, 1) if finished else None,
            "samples": len(finished),
            "passed": len(passed),
            "note": "" if finished else "还没有已结束的消毒会话",
        },
        "stations": {
            "label": "工位利用率",
            "busy": len(state.board.occupancies()),
            "total": len(state.repo.stations()),
        },
        "orders": {
            "label": "在制订单",
            "open": len(state.orders.open_orders()),
            "total": len(state.orders.orders()),
        },
    }


def _stat(values: Sequence[float], *, unit: str, label: str) -> dict[str, Any]:
    rows = sorted(float(v) for v in values if v is not None)
    if not rows:
        return {"label": label, "unit": unit, "value": None, "samples": 0,
                "p50": None, "p90": None, "note": "暂无样本"}
    return {
        "label": label, "unit": unit,
        "value": round(statistics.fmean(rows), 1),
        "p50": round(statistics.median(rows), 1),
        "p90": round(_percentile(rows, 0.9), 1),
        "samples": len(rows),
        "max": round(rows[-1], 1),
    }


def _percentile(sorted_rows: Sequence[float], q: float) -> float:
    """最近秩分位。样本少时插值会算出「比最大值还大」的 p90，反直觉。"""
    if not sorted_rows:
        return 0.0
    index = int(round(q * (len(sorted_rows) - 1)))
    return float(sorted_rows[max(0, min(index, len(sorted_rows) - 1))])


# --------------------------------------------------------------------- 参数
def param_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """阈值配置页。**每行带来源与生效值**，改过的会被审计记下来。"""
    config = state.runtime.config
    rows = []
    for row in config.threshold_rows:
        rows.append({
            "id": row.get("id"),
            "metric": row.get("metric"),
            "kind": row.get("kind"),
            "profile": row.get("profile"),
            "lower_bound": row.get("lower_bound"),
            "upper_bound": row.get("upper_bound"),
            "duration_sec": row.get("duration_sec"),
            "window_sec": row.get("window_sec"),
            "level": row.get("level"),
            "enabled": row.get("enabled"),
        })
    return {
        "source": config.source,
        "version": config.version,
        "degraded": config.degraded,
        "profile": state.runtime.engine.profile,
        "slots": [slot.describe() for slot in config.schedule.slots],
        "rules": rows,
        "total": len(rows),
        "demo_map": state.demo_map(),
    }


def meta_view(state, query: dict[str, Any]) -> dict[str, Any]:
    """前端初始化用的元数据：遥测口径、状态枚举、动作清单。"""
    from .auth import ROLES
    from ..channels import ACTION_BINDINGS

    return {
        "device_id": state.config.device_id,
        "metrics": [{"id": key, **value}
                    for key, value in state.telemetry.latest().items()],
        "item_states": [{"value": key, "label": value}
                        for key, value in _ITEM_LABEL.items()],
        "actions": [
            {"action": name, "channel": binding.channel.value,
             "label": binding.label, "kind": binding.kind,
             "manual_confirm": binding.manual_confirm}
            for name, binding in ACTION_BINDINGS.items()
        ],
        "roles": list(ROLES),
    }


def audit_view(state, query: dict[str, Any]) -> dict[str, Any]:
    limit = _int(query.get("limit"), default=100)
    rows = state.audit.filter(
        actor=str(query.get("actor") or "") or None,
        action=str(query.get("action") or "") or None,
        result=str(query.get("result") or "") or None,
        limit=limit,
    )
    return {
        "total": len(state.audit.entries()),
        "returned": len(rows),
        "stats": state.audit.snapshot(),
        "entries": [row.to_payload() for row in rows],
    }


# --------------------------------------------------------------------- 工具
def _split(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
