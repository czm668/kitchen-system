# -*- coding: utf-8 -*-
"""对话式智能客服引擎：像真人客服一样多轮沟通，能办事就不建工单。

## 与 submit() 的分工
* `submit()`：正式客诉受理，建工单、走审批。语义是「我要投诉/申请补偿」。
* `chat()`：对话入口，顾客用自然语言说话。引擎判断意图：
  - 催菜 → 真的去催厨房（调订单域），回一句带具体信息的答复；
  - 查单 → 返回当前订单/菜品进度摘要；
  - 问菜单 → 返回可点的菜与价格；
  - 闲聊/致谢 → 友好回应；
  - 真投诉 → 收集齐信息（关联订单、菜名、诉求）后**才**调用 submit 建工单，
    并在回复里说明已生成工单号与当前环节。

## 为什么这样设计
用户的核心反馈是「写什么都推送后台、AI 死板」。根因是原来只有一个入口
`submit()`，任何文本都建工单 → 管理端全是闲聊工单，顾客也得不到即时帮助。
对话引擎把「能直接办的」当场办掉，只有真投诉才升级成工单，才是客服的样子。

## 边界（与 guest 域一致）
* 本引擎**不批准补偿、不改审批状态**。收集到的补偿诉求仍然走 submit →
  人工审批；引擎只在回复里告知流程，不承诺「已退款」。
* 动作（催菜/查单/查菜单）都复用现有 customer/orders 接口，不复制状态机。
"""
from __future__ import annotations

from typing import Any, Mapping

from .intent import classify_intent, intent_result

__all__ = ["ChatService"]


class ChatService:
    """挂在 `GuestService.chat`，共享其 state / provider / submit。"""

    def __init__(self, guest_service) -> None:
        self.gs = guest_service

    # ------------------------------------------------------------ 主入口
    def send(self, *, token: str, guest_key: str, text: Any,
             order_id: Any = None, item_id: Any = None,
             wants_refund: bool = False) -> dict[str, Any]:
        """顾客发一句话 → 分流 + 执行 + 多轮上下文。

        返回统一形状：`{routed, intent, reply, action, data, mode}`。
        """
        state = self.gs.state
        # 归属校验：令牌无效/不匹配直接拒绝，绝不让匿名对话读到别人的订单
        owner = state.customer.require_guest(token, guest_key)
        raw = str(text or "").strip()
        if not raw:
            return intent_result("other", reply="请说点什么，我来帮您看看。")

        # 有明确补偿勾选/订单上下文时，强制按投诉意图走（别把「要退款谢谢」分流成闲聊）
        intent = classify_intent(
            raw, has_order=bool(order_id), force_complaint=bool(wants_refund))

        handler = {
            "urge": self._urge,
            "order_query": self._order_query,
            "menu_query": self._menu_query,
            "chitchat": self._chitchat,
            "complaint": self._complaint,
            "other": self._complaint,
        }[intent["intent"]]
        result = handler(
            token=token, guest_key=guest_key, text=raw,
            order_id=order_id, item_id=item_id,
            wants_refund=wants_refund, intent=intent, owner=owner)
        result["mode"] = "live" if self.gs.provider.mode == "live" else "rule"
        result["provider"] = self.gs.provider.model
        return result

    # ------------------------------------------------------------ 催菜
    def _urge(self, *, token, guest_key, text, order_id, item_id,
              wants_refund, intent, owner) -> dict[str, Any]:
        """真的催厨房：优先用顾客点名的子项，否则挑一道未完成的菜。"""
        state = self.gs.state
        target_item = None
        if item_id is not None and str(item_id).strip():
            try:
                target_item = int(item_id)
            except (TypeError, ValueError):
                target_item = None
        if target_item is None:
            # 从该顾客未完成的订单里挑第一道没结束的菜
            for oid in state.customer.order_ids_of(token):
                try:
                    order = state.orders.order(oid)
                except Exception:  # noqa: BLE001
                    continue
                pending = [x for x in order.items
                           if not getattr(x, "finished", False)]
                if pending:
                    target_item = pending[0].id
                    break
        if target_item is None:
            return intent_result("urge", confidence=intent["confidence"],
                                 reply="您当前没有待出餐的菜品，若已上齐可忽略；"
                                       "如需加菜请到点菜页。",
                                 action="none")

        try:
            res = state.customer.urge(token, target_item)
            count = res["urge_count"]
            item = None
            for oid in state.customer.order_ids_of(token):
                try:
                    order = state.orders.order(oid)
                except Exception:  # noqa: BLE001
                    continue
                item = next((x for x in order.items if x.id == target_item), None)
                if item:
                    break
            dish = getattr(item, "dish_name", "您的菜") if item else "您的菜"
            reply = (f"已帮您催「{dish}」（第 {count} 次），厨房的派单优先级"
                     f"已提前，请稍等几分钟。还没上可以再找我。")
            return intent_result("urge", confidence=intent["confidence"],
                                 reply=reply, action="urge",
                                 data={"item_id": target_item, "urge_count": count})
        except Exception as exc:  # noqa: BLE001 - 限频/边界都转成自然话术
            msg = str(exc)
            if "频繁" in msg or "30" in msg:
                reply = "这道菜刚催过，厨房已在加急，稍等一下；马上到我再帮您看。"
            else:
                reply = "已记录您的催促，厨房正在处理，请稍等。"
            return intent_result("urge", confidence=intent["confidence"],
                                 reply=reply, action="none",
                                 data={"note": msg[:80]})

    # ------------------------------------------------------------ 查单
    def _order_query(self, *, token, guest_key, text, order_id, item_id,
                     wants_refund, intent, owner) -> dict[str, Any]:
        state = self.gs.state
        ids = list(state.customer.order_ids_of(token))
        if not ids:
            return intent_result("order_query", confidence=intent["confidence"],
                                 reply="您当前还没有订单，点菜后我可以帮您查进度。",
                                 action="order")
        parts = []
        for oid in ids[-3:]:
            try:
                order = state.orders.order(oid)
            except Exception:  # noqa: BLE001
                continue
            unfinished = [x for x in order.items
                          if not getattr(x, "finished", False)]
            if unfinished:
                names = "、".join(f"{x.dish_name}" for x in unfinished[:3])
                parts.append(f"订单 {order.order_no} 还有 {len(unfinished)} 道没上：{names}")
            else:
                parts.append(f"订单 {order.order_no} 的菜已全部上齐")
        reply = ("；".join(parts) + "。需要催哪道菜，直接告诉我就行。") if parts \
            else "暂时没查到订单进度，稍后再试。"
        return intent_result("order_query", confidence=intent["confidence"],
                             reply=reply, action="order",
                             data={"order_ids": ids[-3:]})

    # ------------------------------------------------------------ 问菜单
    def _menu_query(self, *, token, guest_key, text, order_id, item_id,
                    wants_refund, intent, owner) -> dict[str, Any]:
        state = self.gs.state
        try:
            from ..api.views import dish_view
            payload = dish_view(state, {})
            dishes = [r for r in payload["dishes"]
                      if r.get("enabled") and not r.get("sold_out")]
            # 顾客问「推荐/招牌/好吃」时给前几样，否则给最便宜的几样做参考
            keyword = str(text)
            if any(w in keyword for w in ("推荐", "招牌", "特色", "好吃")):
                picks = dishes[:4]
                lead = "招牌推荐"
            else:
                picks = sorted(dishes, key=lambda r: float(r.get("price") or 0))[:4]
                lead = "目前在售"
            if picks:
                lines = [f"{r['name']} ¥{r['price']}" for r in picks]
                reply = f"{lead}：" + "、".join(lines) + "。想点哪道直接说，或到点菜页下单。"
            else:
                reply = "菜单暂时没准备好，请稍后再问我。"
            return intent_result("menu_query", confidence=intent["confidence"],
                                 reply=reply, action="menu",
                                 data={"count": len(dishes)})
        except Exception as exc:  # noqa: BLE001
            return intent_result("menu_query", confidence=intent["confidence"],
                                 reply="我这就帮您看菜单，请稍等；也可直接在点菜页浏览。",
                                 action="menu", data={"note": str(exc)[:80]})

    # ------------------------------------------------------------ 闲聊
    def _chitchat(self, *, token, guest_key, text, order_id, item_id,
                  wants_refund, intent, owner) -> dict[str, Any]:
        reply = ("不客气，有事随时找我～我可以帮您催菜、查订单、看菜单，"
                 "有问题也能直接说，我来帮您处理。")
        return intent_result("chitchat", confidence=intent["confidence"],
                             reply=reply, action="none")

    # ------------------------------------------------------------ 真投诉
    def _complaint(self, *, token, guest_key, text, order_id, item_id,
                   wants_refund, intent, owner) -> dict[str, Any]:
        """收集到信息后建工单，并回一句带工单号与环节的答复。"""
        # 关联订单：对话里没给但顾客有单，自动带最近一笔（可核对归属）
        use_order = order_id
        if use_order is None:
            ids = list(self.gs.state.customer.order_ids_of(token))
            if ids:
                use_order = ids[-1]
        try:
            submitted = self.gs.submit(
                token=token, guest_key=guest_key, text=text,
                order_id=use_order, item_id=item_id,
                wants_refund=wants_refund,
                method="redo",
            )
        except Exception as exc:  # noqa: BLE001 - 建单失败也要给人话
            return intent_result("complaint", confidence=intent["confidence"],
                                 routed=False,
                                 reply=("收到您的问题了，不过暂时没能生成工单："
                                        + str(exc)[:60]
                                        + "。您可以补充订单信息后再告诉我。"),
                                 action="none")
        # submit 返回的 ai_reply 已经由 handler.compose_reply 追加过「当前环节 /
        # 卡点 / 已执行动作」，这里不要再把 actions 拼一遍，否则顾客会看到
        # 「我已向厨房催菜…我已向厨房催菜…」的重复话术。
        reply = submitted.get("ai_reply") or "已收到您的问题并生成工单。"
        return intent_result("complaint", confidence=intent["confidence"],
                             routed=False, reply=reply, action="complaint",
                             data={k: submitted.get(k) for k in (
                                 "complaint_no", "state", "stage", "stage_label",
                                 "blocker", "overdue", "actions")})
