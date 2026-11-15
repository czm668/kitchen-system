# -*- coding: utf-8 -*-
"""A8-02：核心 4 屏的**帧模型**。

## 帧模型是什么、为什么不是 HTML/LVGL 代码

真机是 LVGL（C 固件），演示/测试是文本渲染。两边如果各自从接口数据
「随手拼」屏内容，第三版就会拼出两个长得不一样的屏。所以这里先固化
**屏的结构**（哪些行、什么状态、哪些字段），渲染层（LVGL 或文本）只做
「帧 → 像素」的机械翻译。契约用测试锁死（test_tft_screens）。

## 四屏（320x240，2.8"）

1. **queue 任务队列**：待配（pending）与在做（cooking）分两栏，行=子项；
2. **steps 工序分步**：一个子项的配料称重 + SOP 步骤（当前步高亮、CCP 标记）；
3. **card 刷卡结果**：绿放行 / 红拦截 + 原因 + 晨检提示（最多停 4 秒）；
4. **status 状态栏**：所有屏顶部——时间、在线、**离线红框**、积压数。

`status` 不是独立屏而是帧头：离线红框必须**每一屏**都在，否则师傅在做
工序屏时断网了看不见，离线模式形同虚设（这正是 A8-05 的验收口径）。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "build_status", "build_queue_screen", "build_steps_screen",
    "build_card_screen", "queue_screen_from_orders",
]

#: 屏高度预算：240 行里状态栏吃 3 行、底部提示 1 行，正文最多 ~13 行。
#: **队列超行就截断**（真实屏放不下 20 行，模拟器放得下也不许放——
#: 否则演示效果与真机不一致，答辩现场「模拟器能看 20 行、真机只能 12 行」
#: 会被当成 bug）。
QUEUE_MAX_ROWS = 10


def build_status(
    *,
    clock_text: str,
    device_id: str,
    online: bool,
    offline_sec: float | None = None,
    queue_pending: int = 0,
    pending_actions: int = 0,
    degrade_level: str = "",
) -> dict[str, Any]:
    """帧头（所有屏共用）。离线时 `banner=offline`，渲染层据此画红框。"""
    return {
        "type": "status",
        "clock": clock_text,
        "device": device_id,
        "online": bool(online),
        "banner": "" if online else "offline",
        "offline_sec": (round(float(offline_sec), 1)
                        if offline_sec is not None and not online else None),
        "queue_pending": int(queue_pending),
        "pending_actions": int(pending_actions),
        "degrade": str(degrade_level or ""),
        # 离线期间排队未同步的动作数——师傅要能看见「我刷的卡还没同步」
        "hint": ("" if online
                 else f"离线模式 · 待同步动作 {int(pending_actions)} 条"),
    }


def build_queue_screen(
    *, pending: list[Mapping[str, Any]], cooking: list[Mapping[str, Any]],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """任务队列屏（待配 / 在做两栏合并成一个列表，`group` 字段分组）。

    行结构刻意做薄：只放 TFT 320 宽放得下的字段（菜名、单号、份数、
    催菜、工位）。放大时间戳进屏里是浪费行数——那不是师傅要的信息。
    """
    rows = []
    for row in pending[:QUEUE_MAX_ROWS]:
        rows.append({
            "group": "pending",
            # 订单快照字段叫 id，本地拼屏可能用 item_id——两个都收
            # （A10-01 联调测试抓出：只读 item_id 时队列行 id 恒为 None）
            "item_id": row.get("item_id") if row.get("item_id") is not None else row.get("id"),
            "dish": str(row.get("dish") or ""),
            "order_no": str(row.get("order_no") or ""),
            "qty": row.get("qty", 1),
            "urge": int(row.get("urge") or row.get("urge_count") or 0),
            "station": row.get("station_id"),
            # 扫码单带桌号短标签：师傅要知道这盘菜送去哪桌。
            # 顾客编号随行数据下发（管理端/联调可查），但 320 宽屏不渲染它。
            "table_short": str(row.get("table_short") or ""),
            "customer_ref": row.get("customer_ref"),
            "mark": "催" if int(row.get("urge") or row.get("urge_count") or 0) > 0 else "",
        })
    for row in cooking[:QUEUE_MAX_ROWS]:
        rows.append({
            "group": "cooking",
            "item_id": row.get("item_id") if row.get("item_id") is not None else row.get("id"),
            "dish": str(row.get("dish") or ""),
            "order_no": str(row.get("order_no") or ""),
            "qty": row.get("qty", 1),
            "urge": int(row.get("urge") or row.get("urge_count") or 0),
            "station": row.get("station_id"),
            "table_short": str(row.get("table_short") or ""),
            "customer_ref": row.get("customer_ref"),
            "mark": "做",
        })
    truncated = max(0, len(pending) - QUEUE_MAX_ROWS) + max(
        0, len(cooking) - QUEUE_MAX_ROWS)
    return {
        "type": "queue",
        "status": dict(status),
        "rows": rows,
        "truncated": truncated,
        "empty_text": "" if rows else "暂无任务（等单或检查网络）",
    }


def queue_screen_from_orders(
    orders: Mapping[str, Any], *, status: Mapping[str, Any],
) -> dict[str, Any]:
    """从 `/api/orders`（或离线缓存快照）拼队列屏。"""
    pending, cooking = [], []
    for order in orders.get("orders", ()):  # order 级
        table = order.get("table_id")
        extra = {"order_no": order.get("order_no", ""),
                 "table_short": f"{table}桌" if table else "",
                 "customer_ref": order.get("customer_ref")}
        for item in order.get("items", ()):
            dish = item.get("dish_name") or item.get("dish")
            if item.get("state") in ("pending", "created", "dispatched"):
                pending.append({**item, **extra, "dish": dish})
            elif item.get("state") in ("cooking",):
                cooking.append({**item, **extra, "dish": dish})
    return build_queue_screen(pending=pending, cooking=cooking, status=status)


def build_steps_screen(
    detail: Mapping[str, Any], *, status: Mapping[str, Any],
) -> dict[str, Any]:
    """工序分步屏：一个子项的称重 + SOP（当前步高亮、CCP/刷卡标记）。"""
    steps = []
    for step in detail.get("steps", ()):
        state = step.get("state", "pending")
        flags = []
        if step.get("ccp"):
            flags.append("CCP")
        if step.get("requires_card"):
            flags.append("卡")
        steps.append({
            "step_no": step.get("step_no"),
            "title": step.get("title", ""),
            "state": state,                      # confirmed / current / pending
            "flags": " ".join(flags),
        })
    weighs = []
    for row in detail.get("weighs", ()):
        weighs.append({
            "ingredient": row.get("ingredient", ""),
            "target_g": row.get("target_g"),
            "measured_g": row.get("measured_g"),
            "state": ("untouched" if row.get("measured_g") is None
                      else "ok" if row.get("ok") else "bad"),
        })
    return {
        "type": "steps",
        "status": dict(status),
        "dish": detail.get("dish", ""),
        "order_no": detail.get("order_no", ""),
        "station_id": detail.get("station_id"),
        "session_active": bool(detail.get("active")),
        "locked": bool(detail.get("locked")),
        "locked_ingredients": list(detail.get("locked_ingredients", ())),
        "next_step_no": detail.get("next_step_no"),
        "steps": steps,
        "weighs": weighs,
        "blockers": list(detail.get("blockers", ())),
        "ready": bool(detail.get("ready")),
    }


def build_card_screen(
    result: Mapping[str, Any], *, status: Mapping[str, Any],
    offline_local: bool = False,
) -> dict[str, Any]:
    """刷卡确认屏：绿放行 / 红拦截；离线本地放行走单独提示。"""
    ok = bool(result.get("ok"))
    needs_morning = bool(result.get("needs_morning_check"))
    tone = "green" if ok else "red"
    lines = []
    if offline_local and ok:
        tone = "yellow"
        lines.append("离线放行 · 恢复后服务端复核")
    lines.append(str(result.get("detail") or result.get("message") or
                     ("身份确认" if ok else "未通过")))
    if needs_morning:
        lines.append("需先完成晨检问卷")
    return {
        "type": "card",
        "status": dict(status),
        "ok": ok,
        "tone": tone,
        "name": str(result.get("name") or ""),
        "staff_id": result.get("staff_id"),
        "needs_morning_check": needs_morning,
        "reason": str(result.get("reason") or ""),
        "lines": lines,
    }
