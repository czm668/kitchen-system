# -*- coding: utf-8 -*-
"""顾客消息「意图分流」：先把不是客诉的请求摘出来，别什么都建工单。

## 为什么单独一层
原来的客服入口来者不拒：顾客问「菜单有什么」「我的单到哪了」「谢谢」，
只要 ≥5 个字就建一张客诉工单，管理端后台被闲聊淹没，真正的客诉反而被埋掉。
这一层在写工单**之前**判断意图：

* `complaint`：真实投诉 → 建工单，走 handler 的阶段/卡点分析。
* `urge`：催菜 → 直接调订单域催厨房，不建工单。
* `order_query`：查订单进度 → 直接返回订单状态摘要。
* `menu_query`：问菜单/价格/推荐 → 返回可点的菜。
* `chitchat`：致谢/问候/闲聊 → 友好回应，不建工单。
* `other`：拿不准 → 按客诉处理（宁可多受理，不漏掉真实投诉）。

## 边界
* **确定性优先**：规则层用关键词匹配，离线也能跑；在线模型可 Return 一个
  `intent` 字段覆盖，但取值必须落在本模块白名单里（见 `INTENTS`）。
* **疑似投诉优先于一切**：命中「异物/变质/投诉/态度差」等强信号时，即使
  同时出现「谢谢」也判为 complaint——不能因为礼貌用语漏掉真投诉。
* 本层只输出意图与置信度，**不执行动作、不改状态**，动作由 service 层做。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = ["INTENTS", "classify_intent", "intent_result"]

INTENTS = ("complaint", "urge", "order_query", "menu_query", "chitchat", "other")

#: 强投诉信号：命中任一即判 complaint（优先级最高）
_COMPLAINT_STRONG = (
    "投诉", "客诉", "异物", "头发", "虫子", "钢丝球", "玻璃", "塑料",
    "变质", "馊", "发霉", "吃坏", "食物中毒", "拉肚子", "过敏", "进医院",
    "态度差", "态度恶劣", "骂人", "凶", "不理人", "太差了", "太失望",
    "要退款", "要赔偿", "赔钱", "不敢吃", "卫生",
    # 明确表达「等太久」的不满，是投诉而非普通催菜。注意「还要多久」
    # 单独出现仍是催菜（见 _URGE），这里只收带不满时长的说法。
    "等了很久", "等太久", "很久没上", "很久还没上", "等半天", "催了好几次",
)
#: 中等投诉信号：配合不满语气时算投诉
_COMPLAINT_MID = ("难吃", "咸", "淡", "糊", "不熟", "生的", "凉了", "冷了",
                  "脏", "少辣做错", "上错菜", "漏送", "少上", "等太久", "很久还没上")

_URGE = ("催", "催菜", "快一点", "快点", "还要多久", "还要多长时间", "怎么还不",
         "什么时候上", "催一下", "加急", "赶紧上")
_ORDER_QUERY = ("我的订单", "订单状态", "单到哪", "查订单", "查单", "送到哪",
                "出餐了吗", "做好了没", "还要等几号", "订单进度")
_MENU_QUERY = ("菜单", "有什么菜", "推荐", "多少钱", "价格", "招牌", "特色菜",
               "有什么好吃", " calorie", "热量", "忌口", "素食", "营业时间",
               "几点开门", "几点关门", "怎么点菜", "怎么下单")
_CHITCHAT = ("谢谢", "感谢", "好的", "你好", "您好", "在吗", "嗨", "hi", "hello",
             "辛苦了", "不客气", "再见", "拜拜")


def classify_intent(text: Any, *, has_order: bool = False,
                    force_complaint: bool = False) -> dict[str, Any]:
    """规则意图识别。返回 `{intent, confidence, signals}`。

    `force_complaint` 用于「顾客明确勾选了要补偿/上传了证据」等场景——
    这些动作本身就是投诉意图，直接跳过分流。
    """
    raw = str(text or "").strip()
    low = raw.lower()
    if force_complaint:
        return {"intent": "complaint", "confidence": 1.0,
                "signals": ["force_complaint"]}

    # 1) 强投诉信号优先（防止礼貌用语掩盖真投诉）
    strong = [w for w in _COMPLAINT_STRONG if w in raw or w in low]
    if strong:
        return {"intent": "complaint", "confidence": 0.95,
                "signals": strong[:3]}

    # 2) 明确的功能性请求（催菜/查单/菜单）——命中即返回，不建工单
    urge = [w for w in _URGE if w in raw]
    if urge:
        return {"intent": "urge", "confidence": 0.9, "signals": urge[:3]}
    order_q = [w for w in _ORDER_QUERY if w in raw]
    if order_q:
        return {"intent": "order_query", "confidence": 0.88, "signals": order_q[:3]}
    menu_q = [w for w in _MENU_QUERY if w in raw]
    if menu_q:
        return {"intent": "menu_query", "confidence": 0.85, "signals": menu_q[:3]}

    # 3) 中等投诉信号 + 不满语气 → 投诉
    mid = [w for w in _COMPLAINT_MID if w in raw]
    if mid and any(w in raw for w in ("不", "太", "很", "没", "错", "差")):
        return {"intent": "complaint", "confidence": 0.75, "signals": mid[:3]}

    # 4) 纯闲聊（整句都很短且只含致谢词）
    chit = [w for w in _CHITCHAT if w in raw]
    if chit and len(raw) <= 16:
        return {"intent": "chitchat", "confidence": 0.8, "signals": chit[:3]}

    # 5) 拿不准 —— 按投诉处理，宁可多受理
    return {"intent": "complaint" if has_order else "other",
            "confidence": 0.5, "signals": []}


def intent_result(intent: str, *, confidence: float = 0.8,
                  reply: str = "", action: str = "",
                  data: Mapping[str, Any] | None = None,
                  routed: bool = True) -> dict[str, Any]:
    """统一的意图分流响应形状，供路由层直接返回给前端。"""
    if intent not in INTENTS:
        intent = "other"
    return {
        "routed": routed,            # True=被分流，没建工单
        "intent": intent,
        "confidence": round(float(confidence), 3),
        "reply": str(reply or ""),
        "action": str(action or ""),  # urge / order / menu / none
        "data": dict(data or {}),
    }
