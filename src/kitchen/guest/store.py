# -*- coding: utf-8 -*-
"""门店事实：营业时间 / 地址 / 电话 / 停车 / 预订 / 支付 / 设施。

## 为什么单独一层

`intent.py` 把「营业时间 / 几点关门」收进了 `menu_query`，但 `chat._menu_query`
原本只会回一段菜品列表——顾客问几点打烊，AI 报了一遍菜单。根因不是分流，
而是**系统里根本没有门店事实**：代码里搜不到营业时间、地址、电话任何一个字段。

本模块把它收敛成一张可配置的事实表，并定死一条口径：

> **配了就照实说，没配就如实说没同步。任何情况下都不许编。**

这条比「答得漂亮」重要——编一个营业时间让顾客白跑一趟，比说「我帮您确认」
的伤害大得多。演示环境的门店名与价格在 14 号文档里已声明是演示数据，
真营业信息没确认前不该由客服对外播报，所以默认值**全空**。

## 配置方式

环境变量（服务端启动前设置，不入库）：

```
KITCHEN_STORE_NAME      明厨小馆
KITCHEN_STORE_HOURS     11:00-21:30
KITCHEN_STORE_ADDRESS   XX 市 XX 区 XX 路 1 号
KITCHEN_STORE_PHONE     000-00000000
KITCHEN_STORE_PARKING   门口 12 个车位，免费
KITCHEN_STORE_BOOKING   可预订，需提前 2 小时
KITCHEN_STORE_PAYMENT   支持微信/支付宝/银行卡，前台结账
KITCHEN_STORE_WIFI      明厨小馆 / 88888888
KITCHEN_STORE_PET       谢绝宠物入内（导盲犬除外）
KITCHEN_STORE_FACILITY  有宝宝椅、母婴室，洗手间在收银台左侧
```

字段缺失＝该问题走兜底话术，**不给假值**。
"""
from __future__ import annotations

import os
from typing import Any, Mapping

__all__ = ["FIELDS", "info", "missing", "answer"]

#: 字段 → 环境变量名。键是内部名，值是 env 名。
FIELDS: dict[str, str] = {
    "name": "KITCHEN_STORE_NAME",
    "hours": "KITCHEN_STORE_HOURS",
    "address": "KITCHEN_STORE_ADDRESS",
    "phone": "KITCHEN_STORE_PHONE",
    "parking": "KITCHEN_STORE_PARKING",
    "booking": "KITCHEN_STORE_BOOKING",
    "payment": "KITCHEN_STORE_PAYMENT",
    "wifi": "KITCHEN_STORE_WIFI",
    "pet": "KITCHEN_STORE_PET",
    "facility": "KITCHEN_STORE_FACILITY",
}

#: 问法 → 字段。顺序即优先级：「几点开门」算营业时间，不算「开门」这种含糊词。
#: 用「店在哪/门店」而不是裸「在哪」，避免把「我的菜在哪」也吞进来。
_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hours", ("营业", "几点开", "几点关", "几点营业", "打烊", "休息日", "开门")),
    ("address", ("地址", "门店在哪", "店在哪", "怎么走", "位置", "导航")),
    ("phone", ("电话", "联系", "手机号")),
    ("parking", ("停车", "车位")),
    ("booking", ("预订", "订位", "包间", "包厢", "留位", "订座")),
    ("payment", ("发票", "刷卡", "团购", "付款", "付钱", "结账", "买单",
                 "扫码付", "支付方式", "怎么付")),
    ("wifi", ("wifi", "wi-fi", "无线网", "Wi-Fi", "密码")),
    ("pet", ("宠物", "狗", "猫")),
    # 设施类问法：intent 的 _STORE_INFO_QUERY 明确收了这四个词，
    # 答案层必须有同名字段接住，否则「有宝宝椅吗」会白路由到 unknown 兜底。
    ("facility", ("宝宝椅", "儿童椅", "洗手间", "厕所", "母婴室", "无障碍")),
)

#: 未配置时的兜底话术。**每一句都必须把球踢回人**，不能只说「不知道」。
_FALLBACK: dict[str, str] = {
    "hours": "门店营业时间我这边还没同步，我请值班同事回您，别让您白跑。",
    "address": "门店地址我这边还没同步，我请值班同事把定位发给您。",
    "phone": "门店电话我帮您问前台，稍等一下。",
    "parking": "停车位我确认后回您，别让您绕圈。",
    "booking": "预订我帮您记下人数和时间，请值班同事跟您确认。",
    "payment": "支付方式我请前台同事跟您确认，别让您白跑。",
    "wifi": "Wi-Fi 密码我请服务员告诉您。",
    "pet": "能不能带宠物我确认后回您。",
    "facility": "这个我请值班同事确认后回您。",
    "unknown": "这个我帮您问一下值班同事。",
}


def info() -> dict[str, str]:
    """读取门店事实。空字符串视为**未配置**，不进结果。"""
    out: dict[str, str] = {}
    for key, env in FIELDS.items():
        val = str(os.environ.get(env, "") or "").strip()
        if val:
            out[key] = val
    return out


def missing(facts: Mapping[str, str] | None = None) -> list[str]:
    """返回未配置的字段名，供管理端/自检提示「还差哪几项」。"""
    facts = info() if facts is None else facts
    return [key for key in FIELDS if key not in facts]


def _topic(text: str) -> str:
    low = str(text or "").lower()
    for name, words in _TOPICS:
        if any(w in text or w.lower() in low for w in words):
            return name
    return "unknown"


def answer(text: Any, facts: Mapping[str, str] | None = None) -> dict[str, Any]:
    """按问法回答门店事实。

    返回 `{field, reply, configured}`：
    * `configured=True` —— 已配置，`reply` 里是真实值；
    * `configured=False` —— 未配置，`reply` 是如实兜底（**不含任何编造值**）。
    """
    facts = info() if facts is None else dict(facts)
    field = _topic(str(text or ""))
    value = facts.get(field, "")
    if not value:
        return {"field": field, "reply": _FALLBACK.get(field, _FALLBACK["unknown"]),
                "configured": False}

    hours = facts.get("hours", "")
    if field == "hours":
        reply = f"营业时间 {hours}。" if hours else value
    elif field == "address":
        reply = f"门店地址：{value}。" + (f"营业时间 {hours}。" if hours else "")
    elif field == "phone":
        reply = f"门店电话 {value}。" + (f"营业时间 {hours} 有人接。" if hours else "")
    elif field == "payment":
        reply = f"{value}。"
    elif field == "booking":
        reply = f"{value}。需要的话我帮您记下人数和时间。"
    elif field == "wifi":
        reply = f"Wi-Fi：{value}。"
    elif field == "pet":
        reply = f"{value}。导盲犬等工作犬不受限制。"
    else:
        reply = f"{value}。"
    return {"field": field, "reply": reply, "configured": True}
