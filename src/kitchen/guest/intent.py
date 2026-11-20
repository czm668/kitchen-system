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

## 子意图（`sub`）：一个意图里的六种问法不能共用一句答复

`menu_query` 原来把「推荐 / 多少钱 / 几点开门 / 怎么点 / 忌口 / 热量」全收进来，
`chat._menu_query` 只能统一回一段菜品列表——顾客问营业时间，AI 报了一遍菜单；
问「水煮鱼多少钱」，答的是最便宜的 4 道（见 `16_AI客服FAQ话术库.md` §九）。

所以本层在**不改 `INTENTS` 六个值**的前提下多输出一个 `sub`：
`INTENTS` 是对外契约（单测、前端、审计都在用），`sub` 只是给 `chat` 层选答案用的
内部路由键。这样既有断言零返工，又不会把「问价格」和「问地址」答成同一句。

## 边界
* **确定性优先**：规则层用关键词匹配，离线也能跑；在线模型可 Return 一个
  `intent` 字段覆盖，但取值必须落在本模块白名单里（见 `INTENTS`）。
* **疑似投诉优先于一切**：命中「异物/变质/投诉/态度差」等强信号时，即使
  同时出现「谢谢」也判为 complaint——不能因为礼貌用语漏掉真投诉。
* 本层只输出意图与置信度，**不执行动作、不改状态**，动作由 service 层做。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = ["INTENTS", "SUBS", "classify_intent", "intent_result"]

INTENTS = ("complaint", "urge", "order_query", "menu_query", "chitchat", "other")

#: 子意图白名单。`""` 表示该意图不需要再细分（complaint/chitchat/other 走工单与兜底）。
SUBS = (
    # menu_query
    "list", "recommend", "price", "taste", "allergy", "eta", "soldout",
    "portion", "combo", "nutrition", "store_info", "takeaway", "howto",
    "payment",
    # order_query
    "status", "add", "cancel", "missing", "wrong", "table_change", "lost",
    "queue",
    # urge
    "escalate",
)

#: 强投诉信号：命中任一即判 complaint（优先级最高）
_COMPLAINT_STRONG = (
    "投诉", "客诉", "异物", "头发", "虫子", "钢丝球",
    # 「玻璃」「塑料」已移出：它们是包装材质词，裸词会把「打包用塑料盒吗」
    # 判成投诉。真食安（「菜里有玻璃」「吃出塑料片」）由 handoff 的
    # 语境判定接住——那边要求词附近出现「吃/喝/里/这」等体感词。
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

#: 门店/设施/支付类问法。这些原来既不在 `_MENU_QUERY` 也不在 `_ORDER_QUERY`，
#: 会掉进 `other`（有单时甚至被判成 complaint）——问「你们店在哪」变成投诉单，
#: 是分流层最不该出现的误伤。现在统一归到 `menu_query` 的
#: `store_info` / `payment` 子意图，由 `chat` 层给专门答复。
#: 注意用「店在哪/门店」而不是裸「在哪」：裸词会把「我的菜在哪」也吞进来。
_STORE_INFO_QUERY = (
    "营业", "几点开", "几点关", "打烊", "休息日", "开门",
    "地址", "门店在哪", "店在哪", "地址在哪", "怎么走", "停车",
    "电话", "预订", "订位", "包间", "包厢", "宝宝椅", "儿童椅",
    "宠物", "wifi", "wi-fi", "无线网", "洗手间", "厕所",
)
#: 支付问法。与门店事实分开列，避免「能开发票吗」被 `store_info` 先命中。
_PAYMENT_QUERY = (
    "发票", "刷卡", "团购", "付款", "付钱", "结账", "买单", "支付",
    "扫码付", "支付方式", "怎么付",
)
#: 合并表只用于「是否算 menu_query」的粗判；子意图判定各引各的，不再抄第二份。
_STORE_QUERY = _STORE_INFO_QUERY + _PAYMENT_QUERY

#: 子意图判定表：**顺序即优先级**，先命中的赢。
#: 例：「推荐个不辣的」同时命中 recommend 与 taste，放前面的是推荐（顾客要的是选菜）；
#: 「这个菜还有吗」命中 soldout 而不是 price（问的是有没有，不是多少钱）。
_MENU_SUBS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("store_info", _STORE_INFO_QUERY),
    ("payment", _PAYMENT_QUERY),
    ("howto", ("怎么点", "怎么下单", "如何下单", "哪下单", "在哪点", "怎么买")),
    # 不含「不吃」：把「我不吃辣」判成忌口会让 AI 去念配料表，它其实是在说口味。
    # 不含「咸/淡」：那是投诉信号（太咸/太淡），归 `_COMPLAINT_MID`，别被菜单抢走。
    ("allergy", ("过敏", "忌口", "素食", "清真")),
    ("nutrition", ("热量", "卡路里", "卡路", "低脂", "减肥", "减脂", "健身")),
    ("takeaway", ("打包", "外卖", "带走", "外带")),
    ("price", ("多少钱", "价格", "多贵", "便宜", "价位", "收费")),
    ("soldout", ("还有吗", "卖完", "沽清", "售完", "没了吗")),
    ("eta", ("多久", "等多长", "几分钟", "多长时间", "快吗", "来得及")),
    ("recommend", ("推荐", "招牌", "特色", "好吃", "必点")),
    ("combo", ("几个人", "怎么配", "套餐", "搭配")),
    ("portion", ("分量", "多少克", "几两", "够吃")),
    ("taste", ("辣", "口味", "葱", "蒜", "甜")),
)

#: 订单子意图：同序优先。`cancel` 在 `wrong` 前——「点错了」是要撤单，
#: 「上错菜」才是错菜，前者更常见。
_ORDER_SUBS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("queue", ("排队", "等位", "人多", "满了吗")),
    ("add", ("加菜", "再加", "补一个", "加个", "追加")),
    ("cancel", ("退菜", "取消", "不要了", "点错")),
    ("wrong", ("上错", "不是我点", "上错菜")),
    ("missing", ("少一个", "漏", "没上齐", "差一个", "还差")),
    ("table_change", ("换桌", "并桌", "拼桌", "换到")),
    ("lost", ("找不到订单", "单没了", "看不到订单", "订单没了")),
)

#: 催菜里「已经催过还不来」的说法：答复要提超时升级，不能只回「已催」。
_URGE_ESCALATE = ("催了两次", "催了好几次", "催了没用", "催了也没", "还是没",
                  "一直没上", "还没上", "还没来", "催过")


def _first_sub(text: str, table: tuple[tuple[str, tuple[str, ...]], ...],
               default: str) -> str:
    """按表顺序返回首个命中的子意图，都不命中给 `default`。"""
    for sub, words in table:
        if any(w in text for w in words):
            return sub
    return default


def classify_intent(text: Any, *, has_order: bool = False,
                    force_complaint: bool = False) -> dict[str, Any]:
    """规则意图识别。返回 `{intent, sub, confidence, signals}`。

    `force_complaint` 用于「顾客明确勾选了要补偿/上传了证据」等场景——
    这些动作本身就是投诉意图，直接跳过分流。
    """
    raw = str(text or "").strip()
    low = raw.lower()
    if force_complaint:
        return {"intent": "complaint", "sub": "", "confidence": 1.0,
                "signals": ["force_complaint"]}

    # 1) 强投诉信号优先（防止礼貌用语掩盖真投诉）
    strong = [w for w in _COMPLAINT_STRONG if w in raw or w in low]
    if strong:
        return {"intent": "complaint", "sub": "", "confidence": 0.95,
                "signals": strong[:3]}

    # 2) 明确的功能性请求（催菜/查单/菜单/门店）——命中即返回，不建工单
    #    子意图关键词也算命中：顾客说「能打包吗」「要排队吗」时，
    #    旧词表里一个都没有，会掉进 other 甚至被判成 complaint。
    urge = [w for w in _URGE if w in raw]
    if urge:
        return {"intent": "urge",
                "sub": "escalate" if any(w in raw for w in _URGE_ESCALATE) else "",
                "confidence": 0.9, "signals": urge[:3]}
    order_q = [w for w in _ORDER_QUERY if w in raw]
    order_sub = _first_sub(raw, _ORDER_SUBS, "")
    if order_q or order_sub:
        return {"intent": "order_query", "sub": order_sub or "status",
                "confidence": 0.88, "signals": order_q[:3]}
    menu_q = [w for w in _MENU_QUERY if w in raw]
    store_q = [w for w in _STORE_QUERY if w in raw or w in low]
    menu_sub = _first_sub(raw, _MENU_SUBS, "")
    if menu_q or store_q or menu_sub:
        # 门店类问法也算 menu_query（它答的是「关于门店/菜单的事实」），
        # 靠 sub 区分具体答什么，避免「问地址」被答成菜单列表。
        return {"intent": "menu_query", "sub": menu_sub or "list",
                "confidence": 0.85, "signals": (menu_q or store_q)[:3]}

    # 3) 中等投诉信号 + 不满语气 → 投诉
    mid = [w for w in _COMPLAINT_MID if w in raw]
    if mid and any(w in raw for w in ("不", "太", "很", "没", "错", "差")):
        return {"intent": "complaint", "sub": "", "confidence": 0.75,
                "signals": mid[:3]}

    # 4) 纯闲聊（整句都很短且只含致谢词）
    chit = [w for w in _CHITCHAT if w in raw]
    if chit and len(raw) <= 16:
        return {"intent": "chitchat", "sub": "", "confidence": 0.8,
                "signals": chit[:3]}

    # 5) 拿不准 —— 按投诉处理，宁可多受理
    return {"intent": "complaint" if has_order else "other", "sub": "",
            "confidence": 0.5, "signals": []}


def intent_result(intent: str, *, confidence: float = 0.8,
                  reply: str = "", action: str = "",
                  sub: str = "",
                  data: Mapping[str, Any] | None = None,
                  routed: bool = True) -> dict[str, Any]:
    """统一的意图分流响应形状，供路由层直接返回给前端。"""
    if intent not in INTENTS:
        intent = "other"
    return {
        "routed": routed,            # True=被分流，没建工单
        "intent": intent,
        "sub": sub if sub in SUBS else "",
        "confidence": round(float(confidence), 3),
        "reply": str(reply or ""),
        "action": str(action or ""),  # urge / order / menu / none
        "data": dict(data or {}),
    }
