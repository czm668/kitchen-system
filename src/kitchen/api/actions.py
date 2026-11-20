# -*- coding: utf-8 -*-
"""写接口：所有会改变状态的请求。

## 统一约定

- 每个写操作**先做权限校验**（`auth.require`），再落审计，再改状态。
  审计放在改状态**之前**写「意图」，改完再写结果？不 —— 两次写会让一次操作
  变成两条记录，翻日志时数不清「到底改了几次」。这里的选择是：**捕获异常后
  把失败也记一条**，成功则在改完之后记一条带 before/after 的记录。
- 返回值一律是「改完之后的相关状态」，前端不需要再读一遍。
- 业务异常（`DomainError` / `ActuationError` / `OrderError`）由路由层映射成
  4xx，这里**不吞**，直接往上抛。

## 为什么动作接口要把 `manual_confirmed` 原样传下去

「二次确认」不是前端的一个弹窗，它是**服务端的必填参数**。前端弹窗只防误点，
防不了「有人直接调接口」。所以 `manual_confirmed` 是请求体里的硬字段，
`InterlockController` 拿它做判据：恢复类动作没有它就是 `InterlockBlocked`。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from ..domain import CST
from ..process.journal import (
    journal_batch_payload,
    journal_ingredient_batch_payload,
    journal_recipe_payload,
    journal_supplier_payload,
    journal_trace_payload,
)
from ..process.models import ItemState
from .auth import Session, require

__all__ = ["dispatch", "ACTIONS"]

_ITEM_STATE = {state.value: state for state in ItemState}


def _audit(state, session: Session, action: str, *, target: str = "",
           before: Mapping[str, Any] | None = None,
           after: Mapping[str, Any] | None = None,
           result: str = "ok", detail: str = "", source: str = "") -> None:
    state.audit.record(
        actor=session.subject, role=session.role, action=action, target=target,
        before=dict(before or {}), after=dict(after or {}),
        result=result, detail=detail, source=source,
    )


# --------------------------------------------------------------------- 下单
def place_order(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "order.write")
    raw = body.get("items") or []
    items: list[tuple[Any, int]] = []
    for row in raw:
        if isinstance(row, Mapping):
            ref = row.get("dish_id") or row.get("code")
            qty = row.get("qty") or 1
        else:
            ref, qty = row, 1
        if ref is None:
            continue
        items.append((ref, int(qty)))
    order = state.orders.place_order(
        items=items,
        source=str(body.get("source") or "web"),
        channel=str(body.get("channel") or "web"),
        table_id=body.get("table_id"),
        client_event_id=body.get("client_event_id") or None,
    )
    _audit(state, session, "order.place", target=order.order_no,
           after={"state": order.state.value, "items": len(order.items)})
    state.emit("order", {"order_no": order.order_no, "state": order.state.value,
                         "by": session.subject})
    return {"order": _order_brief(state, order.id)}


def dispatch_item(state, session: Session, params, body) -> dict[str, Any]:
    """派单到工位。**候选集来自 `DispatchBoard` 的资质校验**，不在这里判。"""
    require(session.role, "order.write")
    item_id = int(params["item_id"])
    station_id = int(body.get("station_id") or 0)
    if station_id <= 0:
        raise ValueError("dispatch 需要 station_id")
    order = state.board.orders.dispatch_item(item_id, station_id)
    item = state.orders.item(item_id)[1]
    _audit(state, session, "order.dispatch", target=f"item#{item_id}",
           before={"state": "pending"},
           after={"state": item.state.value, "station_id": station_id})
    state.emit("order", {"item_id": item_id, "station_id": station_id,
                         "state": item.state.value, "by": session.subject})
    return {"order": _order_brief(state, order.id), "item": _item_brief(item)}


def urge_item(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "order.write")
    item_id = int(params["item_id"])
    order = state.orders.urge(item_id, actor=session.subject,
                             now=state.to_datetime())
    item = state.orders.item(item_id)[1]
    _audit(state, session, "order.urge", target=f"item#{item_id}",
           after={"urge_count": item.urge_count})
    state.emit("order", {"item_id": item_id, "urge": item.urge_count,
                         "order_no": order.order_no})
    return {"order": _order_brief(state, order.id), "urge_count": item.urge_count}


def move_item(state, session: Session, params, body) -> dict[str, Any]:
    """通用状态推进（出餐 / 上桌 / 退菜）。状态机不允许的迁移由域层拒绝。"""
    require(session.role, "order.write")
    item_id = int(params["item_id"])
    target = str(body.get("state") or "").strip()
    if target not in _ITEM_STATE:
        raise ValueError(f"未知目标状态 {target!r}，允许：{list(_ITEM_STATE)}")
    before = state.orders.item(item_id)[1].state.value
    order = state.orders.move_item(
        item_id, _ITEM_STATE[target], actor=session.subject,
        channel=str(body.get("channel") or "web"), now=state.to_datetime(),
    )
    _audit(state, session, "order.move", target=f"item#{item_id}",
           before={"state": before}, after={"state": target})
    state.emit("order", {"item_id": item_id, "state": target,
                         "order_no": order.order_no, "by": session.subject})
    return {"order": _order_brief(state, order.id)}


def sold_out(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "recipe.write")
    dish_id = int(params["dish_id"])
    flag = bool(body.get("sold_out", True))
    before = state.repo.dish(dish_id).sold_out
    dish = state.repo.set_sold_out(dish_id, flag)
    _audit(state, session, "dish.soldout", target=dish.code,
           before={"sold_out": before}, after={"sold_out": dish.sold_out})
    state.emit("dish", {"dish": dish.name, "sold_out": dish.sold_out,
                        "by": session.subject})
    return {"dish": {"id": dish.id, "name": dish.name, "sold_out": dish.sold_out}}


# --------------------------------------------------------------------- 配方
def create_dish(state, session: Session, params, body) -> dict[str, Any]:
    """新建菜品（编码/名称必填）。新菜品无配方，随后在页面上直接录入并保存 v1。"""
    require(session.role, "recipe.write")
    code = str(body.get("code") or "").strip()
    name = str(body.get("name") or "").strip()
    if not code or not name:
        raise ValueError("菜品编码与名称都要填")
    new_id = max((row.id for row in state.repo.dishes()), default=0) + 1
    dish = state.repo.add_dish(
        dish_id=new_id, code=code, name=name,
        category=str(body.get("category") or ""),
        price=float(body.get("price") or 0.0),
        prep_minutes=max(0, int(body.get("prep_minutes") or 0)),
    )
    _audit(state, session, "recipe.dish", target=dish.code,
           after={"dish_id": dish.id, "name": dish.name}, detail="新建菜品")
    state.emit("dish", {"dish": dish.name, "sold_out": False, "by": session.subject})
    archived = state.journal_config({
        "op": "dish", "code": dish.code, "name": dish.name,
        "category": dish.category, "price": dish.price,
        "prep_minutes": dish.prep_minutes,
    })
    return {"dish_id": dish.id, "code": dish.code, "name": dish.name,
            "note": "新菜品还没有配方，请在右侧录入配料与工序后保存 v1"
                    if archived else
                    "新菜品已生效；但配置归档失败，服务重启后会丢失"}


# --------------------------------------------------------------------- 溯源录入
def save_supplier(state, session: Session, params, body) -> dict[str, Any]:
    """新增/编辑供应商。id 省略则现算（max+1），带 id 则更新既有行。"""
    require(session.role, "recipe.write")
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("供应商名称必填")
    sid = body.get("id")
    sid = int(sid) if sid not in (None, "") else max(
        (int(row.get("id") or 0) for row in state.repo.suppliers())) + 1
    before = None
    try:
        before = dict(state.repo.supplier(sid))
    except Exception:  # noqa: BLE001 - 新增时不存在是正常的
        pass
    row = {
        "id": sid, "name": name,
        "license_no": str(body.get("license_no") or "").strip() or None,
        "contact": str(body.get("contact") or "").strip(),
        "qualifications": list(body.get("qualifications") or []),
        "state": str(body.get("state") or "active"),
    }
    state.repo.put_supplier(sid, row)
    _audit(state, session, "trace.supplier", target=name,
           before=before, after=row, detail="保存供应商")
    archived = state.journal_config(journal_supplier_payload(row))
    return {"id": sid, "name": name,
            "note": "已保存" if archived else "已生效；但配置归档失败，重启会丢失"}


def save_batch(state, session: Session, params, body) -> dict[str, Any]:
    """新增/编辑食材批次。批次号必填且唯一，菜品外键必存在。"""
    require(session.role, "recipe.write")
    batch_no = str(body.get("batch_no") or "").strip()
    if not batch_no:
        raise ValueError("批次号必填")
    dish_id = int(body.get("dish_id") or 0)
    state.repo.dish(dish_id)  # 外键校验，先于任何写入
    bid = body.get("id")
    bid = int(bid) if bid not in (None, "") else max(state.repo.batch_ids(), default=0) + 1
    before = None
    try:
        old = state.repo.batch(bid)
        before = {k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v)
                  for k, v in old.items()}
    except Exception:  # noqa: BLE001 - 新增时记录尚不存在，取不到"修改前"快照属正常
        pass
    row = {
        "id": bid, "batch_no": batch_no, "dish_id": dish_id,
        "produced_at": body.get("produced_at"),
        "expire_at": body.get("expire_at"),
        "state": str(body.get("state") or "produced"),
    }
    state.repo.put_batch(bid, row)
    _audit(state, session, "trace.batch", target=batch_no,
           before=before, after=row, detail="保存批次")
    archived = state.journal_config(journal_batch_payload(row))
    return {"id": bid, "batch_no": batch_no,
            "note": "已保存" if archived else "已生效；但配置归档失败，重启会丢失"}


def save_ingredient_batch(state, session: Session, params, body) -> dict[str, Any]:
    """新增/编辑原料批次——溯源主体（这批肉/菜从哪来、谁供的、检疫结果）。

    与成品批次分开：成品批次供留样/出库门禁，原料批次供顾客扫码追溯来源。
    供应商外键必存在；时间由仓储归一化成带时区 datetime，避免 str/datetime 混用。
    """
    require(session.role, "recipe.write")
    batch_no = str(body.get("batch_no") or "").strip()
    if not batch_no:
        raise ValueError("原料批次号必填")
    ingredient_name = str(body.get("ingredient_name") or "").strip()
    if not ingredient_name:
        raise ValueError("原料名称必填")
    supplier_id = int(body.get("supplier_id") or 0)
    state.repo.supplier(supplier_id)  # 外键校验先于写入
    bid = body.get("id")
    bid = int(bid) if bid not in (None, "") else max(
        state.repo.ingredient_batch_ids(), default=0) + 1
    before = None
    try:
        old = state.repo.ingredient_batch(bid)
        before = {k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v)
                  for k, v in old.items()}
    except Exception:  # noqa: BLE001 - 新增时记录尚不存在，取不到"修改前"快照属正常
        pass
    inspection = body.get("inspection")
    if isinstance(inspection, str):
        try:
            inspection = json.loads(inspection)
        except (ValueError, TypeError):
            inspection = {}
    row = {
        "id": bid,
        "batch_no": batch_no,
        "ingredient_name": ingredient_name,
        "supplier_id": supplier_id,
        "received_at": body.get("received_at"),
        "expire_at": body.get("expire_at"),
        "inspection": dict(inspection) if isinstance(inspection, dict) else {},
        "state": str(body.get("state") or "received"),
    }
    state.repo.put_ingredient_batch(bid, row)
    _audit(state, session, "trace.ingredient_batch", target=batch_no,
           before=before, after={k: (v.strftime("%Y-%m-%d %H:%M:%S")
                                      if isinstance(v, datetime) else v)
                                 for k, v in row.items()},
           detail="保存原料批次")
    archived = state.journal_config(journal_ingredient_batch_payload(row))
    return {"id": bid, "batch_no": batch_no,
            "note": "已保存" if archived else "已生效；但配置归档失败，重启会丢失"}


def save_trace(state, session: Session, params, body) -> dict[str, Any]:
    """新增/编辑溯源码。code 是业务唯一键，批次/供应商外键按需校验。"""
    require(session.role, "recipe.write")
    code = str(body.get("code") or "").strip().upper()
    if not code:
        raise ValueError("溯源码必填")
    tid = body.get("id")
    tid = int(tid) if tid not in (None, "") else max(
        (int(dict(row).get("id") or 0) for _c, row in state.repo.trace_rows())) + 1
    before = None
    try:
        old = state.repo.trace_code(code)
        # 审计记录要能 JSON 序列化：仓储里的 created_at 是 datetime，先转字符串
        before = {k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v)
                  for k, v in old.items()}
    except Exception:  # noqa: BLE001 - 新增时记录尚不存在，取不到"修改前"快照属正常
        pass
    row = {
        "id": tid, "code": code,
        "batch_id": body.get("batch_id") or None,
        "supplier_id": body.get("supplier_id") or None,
        "payload": dict(body.get("payload") or {}),
        "created_at": body.get("created_at") or datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    state.repo.put_trace_code(code, row)
    _audit(state, session, "trace.code", target=code,
           before=before, after=row, detail="保存溯源码")
    archived = state.journal_config(journal_trace_payload(row))
    return {"code": code, "id": tid,
            "note": "已保存" if archived else "已生效；但配置归档失败，重启会丢失"}


def save_recipe_version(state, session: Session, params, body) -> dict[str, Any]:
    """把一份配方**另存为新版本**（菜品 id 维度）。

    ## 为什么不「原地改」

    订单在创建时快照了 `recipe_id`（见 `OrderItem.recipe_id`）。原地改配方会让
    「正在做的单」和「刚下的单」按两套配料称重——现场表现是「同一个菜，昨天的
    单说 150g、今天的单说 180g，两个都不合格」。版本化之后旧单继续用旧版本，
    新单用新版本，称重判据始终自洽。

    ## 为什么新版本号是「最大 +1」而不是「当前启用 +1」

    如果有人把 v3 停用了、当前启用的是 v2，用「当前 +1」会撞上已存在的 v3
    （`uk_recipes_dish_version`），然后报一个看起来像 bug 的重复错误。
    """
    require(session.role, "recipe.write")
    dish_id = int(params["dish_id"])
    dish = state.repo.dish(dish_id)
    versions = [row for row in state.repo.all_recipes() if row.dish_id == dish_id]
    current = state.repo.recipe_for(dish_id)
    next_version = max((row.version for row in versions), default=0) + 1
    next_recipe_id = max((row.id for row in state.repo.all_recipes()), default=0) + 1

    items = body.get("items")
    steps = body.get("steps")
    if items is None and current is not None:
        items = [
            {"ingredient": row.ingredient, "weight_g": row.weight_g,
             "tolerance_pct": row.tolerance_pct, "sort_no": row.sort_no}
            for row in state.repo.recipe_items(current.id)
        ]
    if steps is None and current is not None:
        steps = [
            {"step_no": row.step_no, "title": row.title, "instruction": row.instruction,
             "heat_level": row.heat_level, "duration_sec": row.duration_sec,
             "ccp": row.ccp, "requires_card": row.requires_card}
            for row in state.repo.sop_steps(current.id)
        ]
    if not items:
        raise ValueError("新版本至少要有一条配料（没有配料的配方无法称重核验）")

    # 配料/步骤 id 全局唯一：扫全部配方拿最大 id（不按单个配方扫，否则换版本会撞 id）
    next_item_id = 1 + max(
        (row.id for recipe in state.repo.all_recipes()
         for row in state.repo.recipe_items(recipe.id)), default=0)
    next_step_id = 1 + max(
        (row.id for recipe in state.repo.all_recipes()
         for row in state.repo.sop_steps(recipe.id)), default=0)

    recipe = state.repo.add_recipe(
        recipe_id=next_recipe_id, dish_id=dish_id, version=next_version,
        name=str(body.get("name") or (current.name if current else dish.name)),
    )
    for offset, row in enumerate(items):
        state.repo.add_recipe_item(
            item_id=next_item_id + offset, recipe_id=recipe.id,
            ingredient=str(row.get("ingredient") or ""),
            weight_g=float(row.get("weight_g") or 0),
            tolerance_pct=float(row.get("tolerance_pct") or 5.0),
            sort_no=int(row.get("sort_no") or offset + 1),
        )
    for offset, row in enumerate(steps or []):
        state.repo.add_sop_step(
            step_id=next_step_id + offset, recipe_id=recipe.id,
            step_no=int(row.get("step_no") or offset + 1),
            title=str(row.get("title") or f"步骤 {offset + 1}"),
            instruction=str(row.get("instruction") or ""),
            heat_level=str(row.get("heat_level") or ""),
            duration_sec=int(row.get("duration_sec") or 0),
            ccp=bool(row.get("ccp")),
            requires_card=bool(row.get("requires_card")),
        )

    _audit(state, session, "recipe.version", target=dish.code,
           before={"version": None if current is None else current.version},
           after={"version": recipe.version, "items": len(items),
                  "steps": len(steps or [])},
           detail=str(body.get("note") or ""))
    state.emit("param", {"changed": [f"{dish.name} 配方 v{recipe.version}"]})
    archived = state.journal_config(journal_recipe_payload(
        dish_id=dish_id,
        name=str(body.get("name") or (current.name if current else dish.name)),
        items=items, steps=steps or [],
    ))
    base_note = "旧版本对已在做的订单继续生效（订单创建时已快照 recipe_id）"
    return {
        "dish_id": dish_id, "dish": dish.name,
        "recipe_id": recipe.id, "version": recipe.version,
        "items": len(items), "steps": len(steps or []),
        "note": base_note if archived else
                "本版本已生效；但配置归档失败，服务重启后会丢失",
    }


# --------------------------------------------------------------------- 刷卡
def scan_card(state, session: Session, params, body) -> dict[str, Any]:
    """刷卡准入（TFT/读卡器上报）。返回 `AdmissionResult` 的完整结论。"""
    require(session.role, "order.write")
    card_uid = str(body.get("card_uid") or "").strip()
    if not card_uid:
        raise ValueError("scan 需要 card_uid")
    station_id = body.get("station_id")
    result = state.admission.scan(
        card_uid,
        station_id=None if station_id is None else int(station_id),
        now=state.to_datetime(),
    )
    # #3 返岗复位：刷卡即“有人在灶”。长烧/离人保护的计时器从这里复位——
    # 不接这一笔，师傅明明回来了、气阀却被 12 秒前的那次离人关掉。
    state.offline.mark_human(source="card")
    payload = {
        "ok": result.ok,
        "staff_id": getattr(result, "staff_id", None),
        "name": getattr(result, "name", ""),
        "reason": getattr(result, "reason", ""),
        "reason_label": getattr(getattr(result, "reason", ""), "label", None),
        "detail": result.describe(),
        "needs_morning_check": result.needs_morning_check,
        "masked": result.masked,
    }
    _audit(state, session, "admission.scan", target=card_uid,
           result="ok" if result.ok else "denied", detail=result.describe())
    state.emit("admission", {"card": card_uid[-4:], "ok": result.ok,
                             "name": payload["name"]})
    return payload


def occupy_station(state, session: Session, params, body) -> dict[str, Any]:
    """上灶刷卡 → 开工位占用会话。资质/忙闲全由 `DispatchBoard` 判定。"""
    require(session.role, "order.write")
    station_id = int(body.get("station_id") or 0)
    staff_id = int(body.get("staff_id") or 0)
    item_id = body.get("item_id")
    if station_id <= 0 or staff_id <= 0:
        raise ValueError("occupy 需要 station_id 与 staff_id")
    occupancy = state.board.occupy(
        station_id, staff_id,
        item_id=None if item_id is None else int(item_id),
        device_id=state.config.device_id,
        now=state.to_datetime(),
    )
    _audit(state, session, "station.occupy", target=f"ST{station_id}",
           after={"staff_id": staff_id, "item_id": item_id})
    state.emit("station", {"station_id": station_id, "staff_id": staff_id,
                           "occupancy_id": occupancy.id})
    return {"occupancy": {
        "id": occupancy.id, "station_id": occupancy.station_id,
        "staff_id": occupancy.staff_id, "item_id": occupancy.order_item_id,
        "started_at": occupancy.started_at.isoformat(timespec="seconds"),
    }}


def release_station(state, session: Session, params, body) -> dict[str, Any]:
    """出餐/离灶 → 关占用会话。"""
    require(session.role, "order.write")
    station_id = int(body.get("station_id") or params.get("station_id") or 0)
    occ = state.board.occupancy_of(station_id) if station_id else None
    if occ is None:
        raise ValueError(f"工位 {station_id} 当前没有占用会话")
    # `close()` 按**工位**关会话（不是按占用 id）：现场是按灶释放的，
    # 让前端记住 occupancy_id 只会多一次「id 过期」的失败。
    closed = state.board.close(
        station_id, reason=str(body.get("reason") or "出餐释放"),
        now=state.to_datetime(),
    )
    _audit(state, session, "station.release", target=f"ST{station_id}",
           before={"state": occ.state}, after={"state": closed.state})
    return {"occupancy": {"id": closed.id, "state": closed.state,
                          "elapsed_sec": round(closed.elapsed_sec(state.to_datetime()), 1)}}


# --------------------------------------------------------------------- 晨检
def submit_morning(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "order.write")
    from ..staff.models import MorningAnswers

    card_uid = str(body.get("card_uid") or "").strip()
    answers = MorningAnswers.from_dict(body.get("answers") or {})
    outcome = state.admission.submit_morning(
        card_uid, answers, now=state.to_datetime(), channel="web"
    )
    check = getattr(outcome, "check", None)
    payload = {
        "ok": bool(getattr(outcome, "ok", False)),
        "message": str(getattr(outcome, "message", "") or ""),
        "locked": bool(getattr(check, "locked", False)),
        "failures": list(getattr(check, "failures", ()) or ()),
    }
    _audit(state, session, "morning.submit", target=card_uid,
           result="ok" if payload["ok"] else "denied", detail=payload["message"])
    return payload


def review_morning(state, session: Session, params, body) -> dict[str, Any]:
    """组长复核：**必须 web_confirmed=True**，只刷卡不算复核（双通道留痕）。"""
    require(session.role, "ticket.write")
    staff_id = int(body.get("staff_id") or 0)
    card = str(body.get("reviewer_card_uid") or "").strip()
    confirmed = bool(body.get("web_confirmed", True))
    outcome = state.admission.review(
        staff_id, reviewer_card_uid=card, web_confirmed=confirmed,
        reviewer_label=session.subject, now=state.to_datetime(),
    )
    payload = {
        "ok": bool(getattr(outcome, "ok", False)),
        "message": str(getattr(outcome, "message", "") or ""),
        "reviewer": str(getattr(outcome, "reviewer_label", "") or ""),
    }
    _audit(state, session, "morning.review", target=f"staff#{staff_id}",
           result="ok" if payload["ok"] else "denied", detail=payload["message"])
    return payload


# --------------------------------------------------------------------- 工序
def cooking_weigh(state, session: Session, params, body) -> dict[str, Any]:
    """配菜称重核验（#12）。超差锁下一步（服务端判，不靠前端）。"""
    require(session.role, "order.write")
    item_id = int(params["item_id"])
    ingredient = str(body.get("ingredient") or "").strip()
    raw = body.get("measured_g")
    if raw is None:
        raise ValueError("weigh 需要 measured_g")
    check = state.cooking.weigh(
        item_id, ingredient, float(raw),
        staff_id=None if body.get("staff_id") is None else int(body["staff_id"]),
    )
    _audit(state, session, "cooking.weigh", target=f"item#{item_id}:{ingredient}",
           after={"measured_g": check.measured_g, "ok": check.ok},
           result="ok" if check.ok else "locked", detail=check.describe(),
           source=str(body.get("channel") or "tft"))
    state.emit("cooking", {
        # 注意键名不用 "kind"：`_emit` 的 row 先放 "kind" 再展开 body，
        # body 里的 "kind" 会把事件种类覆盖掉，订阅端按 kind 过滤就漏了
        "item_id": item_id, "op": "weigh", "ingredient": check.ingredient,
        "measured_g": check.measured_g, "target_g": check.target_g,
        "ok": check.ok, "channel": str(body.get("channel") or "tft"),
    })
    return {"check": {
        "ingredient": check.ingredient, "target_g": check.target_g,
        "measured_g": check.measured_g, "delta_g": check.delta_g,
        "delta_pct": check.delta_pct, "low_g": check.low_g, "high_g": check.high_g,
        "ok": check.ok, "detail": check.describe(),
    }, "locked": state.cooking.ensure(item_id).locked}


def cooking_confirm(state, session: Session, params, body) -> dict[str, Any]:
    """SOP 步骤确认（#13）。顺序、锁定、身份三道检查在域层。"""
    require(session.role, "order.write")
    item_id = int(params["item_id"])
    step_no = int(body.get("step_no") or 0)
    if step_no <= 0:
        raise ValueError("confirm 需要 step_no")
    staff_id = body.get("staff_id")
    result = state.cooking.confirm(
        item_id, step_no, staff_id=None if staff_id is None else int(staff_id),
    )
    _audit(state, session, "cooking.confirm", target=f"item#{item_id}:step{step_no}",
           after={"ccp": result.ccp, "staff_id": result.staff_id},
           detail=result.describe(), source=str(body.get("channel") or "tft"))
    state.emit("cooking", {
        "item_id": item_id, "op": "sop", "step_no": step_no,
        "title": result.title, "ccp": result.ccp, "ok": result.ok,
        "channel": str(body.get("channel") or "tft"),
    })
    detail = state.cooking.detail(item_id)
    return {"confirm": {
        "step_no": result.step_no, "title": result.title, "ccp": result.ccp,
        "staff_id": result.staff_id, "ok": result.ok, "detail": result.detail,
    }, "next_step_no": detail["next_step_no"], "blockers": detail["blockers"]}


def cooking_serve(state, session: Session, params, body) -> dict[str, Any]:
    """出餐（门禁：#12 全部合格 + #13 CCP 全确认，一次列全）。"""
    require(session.role, "order.write")
    item_id = int(params["item_id"])
    staff_id = body.get("staff_id")
    board = state.cooking
    blockers = board.blockers(item_id)
    if blockers:
        # 门禁不满足直接 409：让 TFT 屏把全部原因一次列出（补一样被拒一次
        # 的循环会把师傅逼到打电话叫人关系统）
        from ..process.cooking import NotReadyError

        raise NotReadyError(blockers)
    order = board.serve(
        item_id, staff_id=None if staff_id is None else int(staff_id),
        channel=str(body.get("channel") or "tft"),
    )
    _audit(state, session, "cooking.serve", target=f"item#{item_id}",
           after={"state": order.state.value}, source=str(body.get("channel") or "tft"))
    state.emit("cooking", {
        "item_id": item_id, "op": "serve", "order_no": order.order_no,
        "state": order.state.value, "channel": str(body.get("channel") or "tft"),
    })
    return {"order": _order_brief(state, order.id)}


def cooking_reset(state, session: Session, params, body) -> dict[str, Any]:
    """显式重开会话（进度清零）。称重全错要重来时用。"""
    require(session.role, "order.write")
    item_id = int(params["item_id"])
    state.cooking.reset(item_id)
    _audit(state, session, "cooking.reset", target=f"item#{item_id}",
           after={"progress": "cleared"})
    state.emit("cooking", {"item_id": item_id, "op": "reset"})
    return {"detail": state.cooking.detail(item_id)}


# --------------------------------------------------------------------- 工单
def ack_ticket(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "ticket.write")
    ticket_id = params["ticket_id"]
    if isinstance(ticket_id, str) and ticket_id.startswith(("dis-", "rw-")):
        raise ValueError("消毒类工单由消毒域闭环，不能在这里接单")
    ticket = state.health.ack(int(ticket_id), session.subject)
    _audit(state, session, "ticket.ack", target=f"#{ticket_id}",
           after={"state": ticket.state})
    state.emit("ticket", {"id": ticket_id, "state": ticket.state})
    return {"ticket": ticket.to_payload()}


def close_ticket(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "ticket.write")
    ticket_id = int(params["ticket_id"])
    before = state.health.ticket(ticket_id)
    ticket = state.health.close(
        ticket_id, reviewer=session.subject,
        note=str(body.get("note") or ""), wall_epoch=state.wall_epoch(),
    )
    _audit(state, session, "ticket.close", target=f"#{ticket_id}",
           before={"state": None if before is None else before.state},
           after={"state": ticket.state}, detail=str(body.get("note") or ""))
    state.emit("ticket", {"id": ticket_id, "state": ticket.state})
    return {"ticket": ticket.to_payload()}


# --------------------------------------------------------------------- 通道
def channel_action(state, session: Session, params, body) -> dict[str, Any]:
    """三通道手动动作。触发与恢复都走同一个入口，由互锁层裁决。"""
    action = str(body.get("action") or params.get("action") or "").strip()
    if not action:
        raise ValueError("channel action 需要 action")
    from ..channels import ACTION_BINDINGS, UnknownActionError

    binding = ACTION_BINDINGS.get(action)
    if binding is None:
        raise UnknownActionError(f"未登记的动作 {action!r}")
    require(session.role, "channel.restore" if binding.is_restore else "channel.trip")
    confirmed = bool(body.get("manual_confirmed", False))
    try:
        payload = state.channel_action(
            action, reviewer=session.subject, manual_confirmed=confirmed,
        )
    except Exception as exc:
        # **失败也要留痕**：被拒的复位请求比成功的那次更值得记
        _audit(state, session, "channel.action", target=action,
               result="blocked", detail=str(exc))
        raise
    _audit(state, session, "channel.action", target=action,
           after={"state": payload["state"]}, detail=payload["label"])
    return payload


def channel_review(state, session: Session, params, body) -> dict[str, Any]:
    """双通道复核：只登记一票，不产生动作。"""
    require(session.role, "channel.review")
    channel = str(body.get("channel") or params.get("channel") or "")
    payload = state.review_channel(channel, reviewer=session.subject)
    _audit(state, session, "channel.review", target=channel,
           after={"reviewers": payload["reviewers"]})
    return payload


# --------------------------------------------------------------------- 参数
def save_params(state, session: Session, params, body) -> dict[str, Any]:
    """保存阈值。**走 `KitchenConfig` 热更新**，不重启、不重装去抖计时。"""
    require(session.role, "param.write")
    rows = body.get("rules")
    if not isinstance(rows, list) or not rows:
        raise ValueError("save_params 需要 rules 数组")
    before_rows = {str(row.get("id")): row for row in state.runtime.config.threshold_rows}
    merged = []
    changed: dict[str, Any] = {}
    for row in state.runtime.config.threshold_rows:
        merged.append(dict(row))
    index = {str(row.get("id")): i for i, row in enumerate(merged)}
    for patch in rows:
        key = str(patch.get("id"))
        if key not in index:
            raise ValueError(f"未知阈值 id {key!r}")
        slot = merged[index[key]]
        for field in ("lower_bound", "upper_bound", "duration_sec",
                      "window_sec", "level", "enabled"):
            if field in patch:
                old, new = slot.get(field), patch[field]
                if old != new:
                    changed[f"{key}.{field}"] = [old, new]
                slot[field] = new
    if not changed:
        return {"changed": {}, "note": "没有字段变化"}
    import dataclasses

    config = state.runtime.config
    state.runtime.apply_config(dataclasses.replace(
        config, threshold_rows=tuple(merged), version=int(config.version) + 1,
    ))
    _audit(state, session, "param.save", target="env_thresholds",
           before={key: pair[0] for key, pair in changed.items()},
           after={key: pair[1] for key, pair in changed.items()})
    state.emit("param", {"changed": sorted(changed)})
    return {
        "changed": changed,
        "profile": state.runtime.engine.profile,
        "version": state.runtime.config.version,
        "before_rows": len(before_rows),
        "demo_map": state.demo_map(),
    }


def set_time_scale(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "param.write")
    # **不能用 `or`**：`body.get("time_scale") or 1.0` 会把 0 悄悄变成 1.0，
    # 于是「传 0 应被拒绝」的用例得到 200，而现场表现是「滑杆拖到 0 反而正常」。
    raw = body.get("time_scale")
    if raw is None:
        raise ValueError("需要 time_scale")
    scale = float(raw)
    before = state.config.time_scale
    payload = state.set_time_scale(scale)
    _audit(state, session, "param.time_scale", target="time_scale",
           before={"time_scale": before}, after={"time_scale": scale})
    state.emit("param", {"time_scale": scale})
    return payload


def set_online(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "param.write")
    online = bool(body.get("online", True))
    payload = state.set_online(online)
    _audit(state, session, "network.set", target="link",
           after={"online": online})
    return payload


def flush_queue(state, session: Session, params, body) -> dict[str, Any]:
    require(session.role, "param.write")
    payload = state.flush()
    _audit(state, session, "queue.flush", target="offline_queue",
           after={"queued": payload["queued"], "store_rows": payload["store_rows"]})
    return payload


# --------------------------------------------------------------------- 演示
def seed_orders(state, session: Session, params, body) -> dict[str, Any]:
    """造一批演示订单。答辩演练用；生产环境该接口应被关掉。"""
    require(session.role, "order.write")
    codes = body.get("dish_codes") or []
    payload = state.demo.seed_orders([str(code) for code in codes])
    _audit(state, session, "demo.seed_orders", target="orders",
           after=payload)
    return payload


def inject_signal(state, session: Session, params, body) -> dict[str, Any]:
    """信号注入（A10-06）：现场兜底演示燃气/火焰/超温。

    注入**只影响下一帧的采样值**，不改阈值、不改保护逻辑；注入的帧会被打上
    `demo` 标记进事件流，事后能从审计里看出「这次报警是注入演出来的」。
    """
    require(session.role, "channel.trip")
    frame = body.get("metrics") or {}
    if not isinstance(frame, Mapping) or not frame:
        raise ValueError("inject 需要 metrics 对象")
    step = state.tick({key: float(value) for key, value in frame.items()})
    _audit(state, session, "demo.inject", target="telemetry",
           before={}, after=dict(frame), detail="演示信号注入")
    state.emit("inject", {"metrics": dict(frame), "by": session.subject})
    return {
        "injected": dict(frame),
        "verdicts": [{"rule_id": v.rule_id, "event": v.event,
                      "actions": list(v.actions)} for v in step.verdicts],
        "guards": [{"rule_id": v.rule_id, "event": v.event}
                   for v in step.guard_verdicts],
        "actions": list(step.actions),
    }


# --------------------------------------------------------------------- 路由表
ACTIONS: dict[str, Any] = {
    "order.place": place_order,
    "order.dispatch": dispatch_item,
    "order.urge": urge_item,
    "order.move": move_item,
    "dish.soldout": sold_out,
    "admission.scan": scan_card,
    "cooking.weigh": cooking_weigh,
    "cooking.confirm": cooking_confirm,
    "cooking.serve": cooking_serve,
    "cooking.reset": cooking_reset,
    "station.occupy": occupy_station,
    "station.release": release_station,
    "morning.submit": submit_morning,
    "morning.review": review_morning,
    "ticket.ack": ack_ticket,
    "ticket.close": close_ticket,
    "channel.action": channel_action,
    "channel.review": channel_review,
    "recipe.version": save_recipe_version,
    "recipe.dish": create_dish,
    "trace.supplier": save_supplier,
    "trace.batch": save_batch,
    "trace.ingredient_batch": save_ingredient_batch,
    "trace.code": save_trace,
    "param.save": save_params,
    "param.time_scale": set_time_scale,
    "network.set": set_online,
    "queue.flush": flush_queue,
    "demo.seed_orders": seed_orders,
    "demo.inject": inject_signal,
}


def dispatch(name: str, state, session: Session, params, body) -> dict[str, Any]:
    handler = ACTIONS.get(str(name))
    if handler is None:
        raise KeyError(f"未知操作 {name!r}")
    return handler(state, session, params, body)


# --------------------------------------------------------------------- 小工具
def _order_brief(state, order_id: int) -> dict[str, Any]:
    order = state.orders.order(order_id)
    return {
        "id": order.id, "order_no": order.order_no, "state": order.state.value,
        "due_at": None if order.due_at is None else order.due_at.isoformat(timespec="seconds"),
        "items": [_item_brief(item) for item in order.items],
    }


def _item_brief(item) -> dict[str, Any]:
    return {"id": item.id, "dish": item.dish_name, "state": item.state.value,
            "station_id": item.station_id, "urge_count": item.urge_count}
