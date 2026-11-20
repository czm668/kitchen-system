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
* **菜单意图按 `sub` 分答**（`price / store_info / howto / allergy / nutrition /
  takeaway / payment / recommend / list`）。原来这六种问法共用一句「最便宜的 4 道」，
  问营业时间会被答成菜单列表——答案层缺的正是这一层，`sub` 由 `intent` 层给。
"""
from __future__ import annotations

from typing import Any, Mapping

from . import guard, store
from .intent import ORDER_DISH_VERBS, classify_intent, intent_result

__all__ = ["ChatService"]


def _money(value: Any) -> str:
    """价格显示成 ¥58 而不是 ¥58.0——顾客看的是一句话，不是调试输出。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(num)) if num == int(num) else f"{num:.2f}"


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
        # 菜名由本层从菜品表取（规则层不碰仓储）：否则「青椒肉丝」掉进兜底被判成客诉。
        intent = classify_intent(
            raw, has_order=bool(order_id), force_complaint=bool(wants_refund),
            dish_names=self._dish_names())

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
        # 出口护栏：无论哪条分支，对客文案都不许出现「马上就好 / 已经给您退了」。
        # 规则分支本来就不会说，但这层要挡住以后接在线改写的情况。
        result["reply"], guard_hits = guard.scrub(result.get("reply"))
        if guard_hits:
            result.setdefault("data", {})["guard"] = guard_hits
        result["mode"] = "live" if self.gs.provider.mode == "live" else "rule"
        result["provider"] = self.gs.provider.model
        return result

    # ------------------------------------------------------------ 催菜
    def _urge(self, *, token, guest_key, text, order_id, item_id,
              wants_refund, intent, owner) -> dict[str, Any]:
        """真的催厨房：优先用顾客点名的子项，否则挑一道未完成的菜。

        `sub=escalate` 表示顾客说的是「催了好几次还没上」——除了催，还要把
        超时升级这条讲清楚，否则顾客会觉得催了也没用（FAQ-ORD-005）。
        """
        state = self.gs.state
        sub = str(intent.get("sub") or "")
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
                except Exception:  # noqa: BLE001 - 订单已被清理，跳过该单继续找下一单
                    continue
                pending = [x for x in order.items
                           if not getattr(x, "finished", False)]
                if pending:
                    target_item = pending[0].id
                    break
        if target_item is None:
            return intent_result("urge", sub=sub, confidence=intent["confidence"],
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
                except Exception:  # noqa: BLE001 - 同上：幽灵单跳过，不影响定位目标菜品
                    continue
                item = next((x for x in order.items if x.id == target_item), None)
                if item:
                    break
            dish = getattr(item, "dish_name", "您的菜") if item else "您的菜"
            reply = (f"已帮您催「{dish}」（第 {count} 次），厨房的派单优先级"
                     f"已提前，请稍等几分钟。还没上可以再找我。")
            if sub == "escalate":
                reply += "这道已经催过多次，如果超过 30 分钟还没上，系统会自动升级到值班人员处理。"
            return intent_result("urge", sub=sub, confidence=intent["confidence"],
                                 reply=reply, action="urge",
                                 data={"item_id": target_item, "urge_count": count})
        except Exception as exc:  # noqa: BLE001 - 限频/边界都转成自然话术
            msg = str(exc)
            if "频繁" in msg or "30" in msg:
                reply = ("这道菜刚催过（30 秒内不能重复催，避免干扰厨房排单），"
                         "厨房已在加急，稍等一下；超时会自动升级到值班人员。")
            else:
                reply = "已记录您的催促，厨房正在处理，请稍等。"
            return intent_result("urge", sub=sub, confidence=intent["confidence"],
                                 reply=reply, action="none",
                                 data={"note": msg[:80]})

    # ------------------------------------------------------------ 查单
    def _order_query(self, *, token, guest_key, text, order_id, item_id,
                     wants_refund, intent, owner) -> dict[str, Any]:
        state = self.gs.state
        sub = str(intent.get("sub") or "status")
        if sub == "queue":
            # 系统没有排队/等位数据，答不了「前面还有几桌」——如实说，不编。
            return intent_result(
                "order_query", sub="queue", confidence=intent["confidence"],
                reply=("这会儿的等位情况我这边看不到精确桌数，您到了跟迎宾说一声，"
                       "我帮您留意。"),
                action="none")
        ids = list(state.customer.order_ids_of(token))
        if not ids:
            return intent_result("order_query", sub=sub,
                                 confidence=intent["confidence"],
                                 reply="您当前还没有订单，点菜后我可以帮您查进度。",
                                 action="order")
        parts = []
        for oid in ids[-3:]:
            try:
                order = state.orders.order(oid)
            except Exception:  # noqa: BLE001 - 最近三单里被清理的跳过，不阻断整段回复
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
        return intent_result("order_query", sub=sub,
                             confidence=intent["confidence"],
                             reply=reply, action="order",
                             data={"order_ids": ids[-3:]})

    # ------------------------------------------------------------ 问菜单（按子意图分答）
    def _menu_query(self, *, token, guest_key, text, order_id, item_id,
                    wants_refund, intent, owner) -> dict[str, Any]:
        """菜单意图按 `sub` 选答案，不再一律回一段菜品列表。

        `price / store_info / howto / allergy / nutrition / takeaway / payment`
        各有专门答复；`recommend` 与 `list` 走原来的列表逻辑（保持兼容）。
        任何一条都不承诺折扣、热量、销量——系统里没有这些数据。
        """
        sub = str(intent.get("sub") or "list")
        if sub == "order_dish":
            return self._menu_order_dish(
                text=str(text), intent=intent, token=token, owner=owner)
        handler = {
            "price": self._menu_price,
            "store_info": self._menu_store_info,
            "payment": self._menu_payment,
            "howto": self._menu_howto,
            "allergy": self._menu_allergy,
            "nutrition": self._menu_nutrition,
            "takeaway": self._menu_takeaway,
            "soldout": self._menu_soldout,
        }.get(sub)
        if handler is not None:
            return handler(text=str(text), intent=intent)
        return self._menu_list(text=str(text), intent=intent)

    # -- 菜单子答复的公共件 -------------------------------------------------
    def _dishes(self, *, include_sold_out: bool = False) -> list[dict[str, Any]]:
        """读菜品表。`include_sold_out=True` 用于答「还有吗」这类沽清问题。"""
        from ..api.views import dish_view
        payload = dish_view(self.gs.state, {})
        rows = payload.get("dishes") or []
        if include_sold_out:
            return [r for r in rows if r.get("enabled")]
        return [r for r in rows if r.get("enabled") and not r.get("sold_out")]

    def _dish_names(self) -> tuple[str, ...]:
        """在售菜名，供意图层识别「顾客说的是一道菜」。
        读菜品表失败就返回空元组——点单识别降级，但其他意图分流不受影响。
        """
        try:
            return tuple(str(r["name"]) for r in self._dishes() if r.get("name"))
        except Exception:  # noqa: BLE001
            return ()

    def _match_dish(self, text: str) -> dict[str, Any] | None:
        """从顾客原话里认出菜名。取最长命中，避免「肉丝」抢了「青椒肉丝」。"""
        rows = self._dishes(include_sold_out=True)
        hits = [r for r in rows
                if str(r.get("name") or "") and str(r["name"]) in text]
        return max(hits, key=lambda r: len(str(r["name"]))) if hits else None

    def _menu_list(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问「有什么菜」：推荐位给前 4 道，其余给最便宜的 4 道做参考。"""
        try:
            dishes = self._dishes()
            if any(w in text for w in ("推荐", "招牌", "特色", "好吃")):
                picks, lead, sub = dishes[:4], "招牌推荐", "recommend"
            else:
                picks = sorted(dishes, key=lambda r: float(r.get("price") or 0))[:4]
                lead, sub = "目前在售", "list"
            if picks:
                lines = [f"{r['name']} ¥{_money(r['price'])}" for r in picks]
                reply = (f"{lead}：" + "、".join(lines)
                         + "。想点哪道回我菜名，我帮您下单；或到点菜页选。")
            else:
                reply = "菜单暂时没准备好，请稍后再问我。"
            return intent_result("menu_query", sub=sub,
                                 confidence=intent["confidence"],
                                 reply=reply, action="menu",
                                 data={"count": len(dishes)})
        except Exception as exc:  # noqa: BLE001
            return intent_result("menu_query", sub="list",
                                 confidence=intent["confidence"],
                                 reply="我这就帮您看菜单，请稍等；也可直接在点菜页浏览。",
                                 action="menu", data={"note": str(exc)[:80]})

    def _menu_order_dish(self, *, text: str, intent: Mapping[str, Any],
                         token: str, owner: Mapping[str, Any]) -> dict[str, Any]:
        """顾客提到在售菜名：只说菜名给报价与确认路径，明确说「点」才真下单。

        为什么分两步：菜名本身只是「提到这道菜」，后厨收到订单就会开做，
        擅自下单是不可逆动作。所以裸菜名只报价 + 给出可复制的确认说法；
        顾客带了「点一份 / 来一道 / 我要」这类购买动词，才调用顾客通道下单。
        复用 `customer.place()`——价格快照、配方版本、沽清整单拒绝全走原链路，
        本层零业务逻辑。
        """
        dish = self._match_dish(text)
        if dish is None:
            return intent_result(
                "menu_query", sub="order_dish", confidence=intent["confidence"],
                reply="您说个菜名，我帮您看价格和下单；也可以直接到点菜页选。",
                action="menu")
        name = str(dish.get("name") or "")
        code = str(dish.get("code") or "")
        price = _money(dish.get("price"))
        wants = any(v in text for v in ORDER_DISH_VERBS)
        if not wants:
            return intent_result(
                "menu_query", sub="order_dish", confidence=intent["confidence"],
                reply=(f"{name}¥{price}。想点这道，回我「点{name}」我帮您下单，"
                       "或到点菜页选。"),
                action="menu", data={"code": code, "price": dish.get("price"),
                                     "confirmed": False})

        try:
            res = self.gs.state.customer.place(
                token, [{"code": code, "qty": 1}], owner.get("table_id"))
            reply = (f"已帮您下单：{name} ×1，订单号 {res['order_no']}。"
                     f"厨房已收到，在「订单」页能看到进度；要加量或改动随时告诉我。")
            return intent_result(
                "menu_query", sub="order_dish", confidence=intent["confidence"],
                reply=reply, action="order",
                data={"code": code, "order_id": res["order_id"],
                      "order_no": res["order_no"], "confirmed": True})
        except Exception as exc:  # noqa: BLE001 - 沽清/批次/归属都给人话，不建工单
            msg = str(exc)
            reply = (f"{name}这会儿下单没成功：{msg[:60]}。"
                     "您可以到点菜页看看当天的在售情况，或告诉我换一道。")
            return intent_result(
                "menu_query", sub="order_dish", confidence=intent["confidence"],
                reply=reply, action="none",
                data={"code": code, "note": msg[:80], "confirmed": False})

    def _menu_price(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问「这道菜多少钱」：按菜名精确报单价，不再答「最便宜的 4 道」。"""
        try:
            dish = self._match_dish(text)
            if dish is None:
                return intent_result(
                    "menu_query", sub="price", confidence=intent["confidence"],
                    reply="您说个菜名我给您报价格；也可以直接到点菜页看全部 26 道。",
                    action="menu")
            mins = dish.get("estimate_minutes")
            reply = f"{dish['name']}¥{_money(dish['price'])}"
            if mins:
                reply += f"，参考出餐 {mins} 分钟"
            peers = [r for r in self._dishes()
                     if r.get("category") == dish.get("category")
                     and r.get("code") != dish.get("code")]
            if peers:
                reply += f"。同品类的还有{peers[0]['name']}¥{_money(peers[0]['price'])}"
            # 不要问「要帮您下单吗」——顾客回一个「要」会掉进兜底被判成客诉。
            # 给一句可复制的明确指令，顾客照着说就能命中点单意图。
            reply += f"。想点回我「点{dish['name']}」，我帮您下单。"
            return intent_result("menu_query", sub="price",
                                 confidence=intent["confidence"], reply=reply,
                                 action="menu",
                                 data={"code": dish.get("code"),
                                       "price": dish.get("price")})
        except Exception as exc:  # noqa: BLE001
            return intent_result("menu_query", sub="price",
                                 confidence=intent["confidence"],
                                 reply="我这就帮您查价格，稍等；点菜页上也有每道菜的标价。",
                                 action="menu", data={"note": str(exc)[:80]})

    def _menu_store_info(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问门店事实（营业时间/地址/电话/停车/预订/设施）。

        事实来源只认 `store.info()`；没配置就如实说没同步，**不编营业时间**。
        """
        res = store.answer(text)
        return intent_result("menu_query", sub="store_info",
                             confidence=intent["confidence"],
                             reply=res["reply"], action="menu",
                             data={"field": res["field"],
                                   "configured": res["configured"]})

    def _menu_payment(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问怎么付钱/发票。先说清「小程序只记账不收款」，避免顾客以为付过了。"""
        res = store.answer(text)
        base = "小程序下单只记账不收款，餐后到前台结账。"
        detail = res["reply"] if res["configured"] else \
            "支付方式和发票我请前台同事跟您确认，别让您白跑。"
        return intent_result("menu_query", sub="payment",
                             confidence=intent["confidence"],
                             reply=base + detail, action="none",
                             data={"field": res["field"],
                                   "configured": res["configured"]})

    def _menu_howto(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问怎么点菜。给可执行的路径，不说「请到首页查看」。"""
        return intent_result(
            "menu_query", sub="howto", confidence=intent["confidence"],
            reply=("扫桌上的码就进菜单，选好菜点「加入」，再点右下角「去下单」就行，"
                   "不用注册也不用登录。下完单在「订单」页能看到进度。"),
            action="menu")

    def _menu_allergy(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问忌口/过敏。只念配方主料，**绝不承诺「绝对不含」**。"""
        try:
            dish = self._match_dish(text)
            if dish is None:
                return intent_result(
                    "menu_query", sub="allergy", confidence=intent["confidence"],
                    reply=("您对什么过敏？告诉我菜名，我帮您核对配料；"
                           "严重过敏我会同时叫服务员按后厨流程单独处理。"),
                    action="menu")
            recipe_id = dish.get("recipe_id")
            items = tuple(self.gs.state.repo.recipe_items(recipe_id)) if recipe_id else ()
            main = "、".join(f"{it.ingredient} {int(it.weight_g)}g"
                             for it in items[:3] if it.weight_g)
            if main:
                reply = (f"{dish['name']}的主料是{main}。您对什么过敏？"
                         "我帮您核对，确认不了的会让厨房单独确认，绝不靠猜。")
            else:
                reply = (f"{dish['name']}的配料明细我这边没取到，"
                         "您对什么过敏告诉我，我请厨房确认。")
            return intent_result("menu_query", sub="allergy",
                                 confidence=intent["confidence"], reply=reply,
                                 action="menu",
                                 data={"code": dish.get("code"),
                                       "asked_human": True})
        except Exception as exc:  # noqa: BLE001
            return intent_result("menu_query", sub="allergy",
                                 confidence=intent["confidence"],
                                 reply="过敏这事不能猜，我请厨房确认配料后回您。",
                                 action="menu", data={"note": str(exc)[:80]})

    def _menu_nutrition(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问热量。系统没有营养数据，如实说没有，只给可执行的替代建议。"""
        return intent_result(
            "menu_query", sub="nutrition", confidence=intent["confidence"],
            reply=("菜单目前没有标注热量数据，我不能给您估一个数。清淡些的有凉拌黄瓜、"
                   "紫菜蛋花汤，做法上可以要求少油少盐。"),
            action="menu")

    def _menu_takeaway(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问打包/外卖。外卖明确不做，别让顾客白等。"""
        return intent_result(
            "menu_query", sub="takeaway", confidence=intent["confidence"],
            reply=("可以打包，在备注里写「打包」就行，餐具另收。"
                   "外卖暂不支持，我们只做堂食和到店自取。"),
            action="menu")

    def _menu_soldout(self, *, text: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        """问「还有吗」。按沽清事实答，并给同类替换，不说「明天一定有」。"""
        try:
            rows = self._dishes(include_sold_out=True)
            gone = [r for r in rows if r.get("sold_out")]
            dish = next((r for r in gone
                         if str(r.get("name") or "") and str(r["name"]) in text), None)
            if dish is not None:
                reply = f"{dish['name']}今天已沽清，点不了。"
            elif gone:
                reply = ("今天沽清的是：" + "、".join(str(r["name"]) for r in gone[:3])
                         + "。其余都还能点。")
            else:
                reply = "今天的菜都还能点，没有沽清的。"
            if dish is not None:
                peers = [r for r in self._dishes()
                         if r.get("category") == dish.get("category")]
                if peers:
                    reply += (f"同类的{peers[0]['name']}还有，"
                              f"¥{_money(peers[0]['price'])}，要不要换这道？")
            return intent_result("menu_query", sub="soldout",
                                 confidence=intent["confidence"], reply=reply,
                                 action="menu",
                                 data={"sold_out": [str(r["name"]) for r in gone]})
        except Exception as exc:  # noqa: BLE001
            return intent_result("menu_query", sub="soldout",
                                 confidence=intent["confidence"],
                                 reply="沽清情况我这就帮您确认，稍等。",
                                 action="menu", data={"note": str(exc)[:80]})

    # ------------------------------------------------------------ 闲聊
    def _chitchat(self, *, token, guest_key, text, order_id, item_id,
                  wants_refund, intent, owner) -> dict[str, Any]:
        reply = ("不客气，有事随时找我～我可以帮您点单、催菜、查订单、看菜单，"
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
                                 "blocker", "overdue", "actions", "handoff")})
