# -*- coding: utf-8 -*-
"""低分单当日回访：差评当天就有人联系，不能隔夜。

## 为什么要有这一层

`service.feedback()` 已经在顾客打 1–2 分时发了 `low_score` 事件通知店长，
但**"通知到了"不等于"回访了"**：事件发出去之后没有任何跟进——
没有回访任务、没有责任人、没有"当天到底联系没有"的判据，
看板的 `low_scores` 只是把差评列出来，谁回访、几点回访、说了什么全都没有。
这条链断了，满意度就只能看一个分数，无法闭环改进。

本模块补齐三件事：

1. **当日截止**：低分当天 23:59:59 前必须联系（跨天算超时）；
2. **回访动作**：店长/经理记下 who / when / 说了什么，任务才关；
3. **回访话术**：按客诉分类给 A/B 两套，全部守三条铁律——
   不承诺金额、引工单事实、先共情再方案。

## 边界

* **回访不改评分**：顾客打几分是事实，回访是补救动作，不能回头"洗白"分数；
* **回访不改工单状态**：结案工单仍是终态，回访是它的后续服务动作，重开要走补证；
* **只判低分单**：3–5 分不建回访任务，别把正常评价也变成工单；
* **时间用可控时钟**：截止与超时都基于注入的 `now`，便于答辩演示与单测。
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

__all__ = [
    "LOW_SCORE_MAX", "FOLLOWUP_DUE_TIME", "FOLLOWUP_STATES", "NOTE_MAX",
    "SCRIPTS", "is_low", "due_of", "is_overdue", "script_for",
]

#: 1–2 分算低分，触发当日回访。3 分及以上不建任务。
LOW_SCORE_MAX = 2

#: 当日截止时刻（本地时钟 23:59:59）。写成 time 常量便于单测断言口径。
FOLLOWUP_DUE_TIME = time(23, 59, 59)

#: 回访任务状态。待办 → 完成，没有"取消"——低分单不允许悄悄消失。
FOLLOWUP_STATES = ("pending", "done")

#: 回访说明长度上限，与 feedback_comment 对齐。
NOTE_MAX = 200

#: 按客诉分类给的当日回访话术，A=先共情后事实，B=先事实后共情。
#: 两套都**不提退款/金额**（补偿由工单终审，回访只解释与安抚），
#: 都引工单号（`{no}`）与分类事实，都可复制即用。
SCRIPTS: dict[str, dict[str, str]] = {
    "异物": {
        "A": ("您好，我是值班经理。您反馈的菜里异物我们已经按食安事件受理（工单 {no}），"
              "留样和同批次记录都封存了，先向您道个歉。您现在身体还好吗？有什么需要我们立刻安排的，您直接说。"),
        "B": ("工单 {no} 已按食安事件登记，留样与同批次记录已封存。我知道这事让您很不舒服，"
              "我先确认您的身体状况，后续处理由我本人跟到底，不用您再重复一遍。"),
    },
    "温度": {
        "A": ("您好，我是值班经理。您说菜是凉的（工单 {no}），这确实不该出现，先跟您说声抱歉。"
              "当班出餐记录我会核对，需要的话这道给您重做一份热的，您看可以吗？"),
        "B": ("工单 {no} 记的是菜品温度问题，我已经去看出餐记录。让您吃到凉的菜是我们没做好，"
              "重做和后续我盯着，几分钟内给您回话。"),
    },
    "菜品质量": {
        "A": ("您好，我是值班经理。您反馈的菜品没做好（工单 {no}），我认，先跟您道个歉。"
              "后厨的当班记录我会核对，这道给您重做或换一份，听您的。"),
        "B": ("工单 {no} 已记下菜品质量问题，我已反馈后厨核实。让您不满意这道菜是我们的责任，"
              "重做或换菜我现在就安排，处理结果我再给您回话。"),
    },
    "上菜慢": {
        "A": ("您好，我是值班经理。让您等那么久（工单 {no}），是我们的排单出了问题，先说声抱歉。"
              "我已经查过当时的出餐记录，超时会升级处理，这次我亲自跟。"),
        "B": ("工单 {no} 记的是上菜超时，我核对了当时的派单记录。等这么久换我也会着急，"
              "后续改进我来盯，今天先跟您把话说清楚。"),
    },
    "服务态度": {
        "A": ("您好，我是值班经理。您说服务态度不好（工单 {no}），这不该发生，我先跟您道歉。"
              "当班记录我会调出来核实，不替同事辩解，核实完我给您回复。"),
        "B": ("工单 {no} 记的是服务态度问题，我会调当班记录核实。您的感受我不会打折扣，"
              "也不需要您再复述一遍，结果由我本人回给您。"),
    },
}
#: 未归类或"其他"用的通用话术。
_SCRIPT_DEFAULT = {
    "A": ("您好，我是值班经理。您这次的体验不好（工单 {no}），我先跟您说声抱歉。"
          "您的原话我原样记着，今天我亲自核实，有结果第一时间回您。"),
    "B": ("工单 {no} 已登记您反馈的问题，我现在就核实。让您不满意是我们的责任，"
          "不用您再重复，处理由我跟到底。"),
}


def is_low(score: Any) -> bool:
    """score 是否为低分（1–2）。非整数一律不算低分，避免脏数据建任务。"""
    try:
        value = int(score)
    except (TypeError, ValueError):
        return False
    return 1 <= value <= LOW_SCORE_MAX


def due_of(feedback_at: Any) -> str:
    """按评价时刻算当日截止（当天 23:59:59），返回 ISO 秒串。

    评价时间可能是 ISO 字符串或 datetime；解析失败时退回传入原样字符串，
    让调用方仍能存一个非空 due，而不是在回访链路上抛异常。
    """
    if isinstance(feedback_at, datetime):
        moment = feedback_at
    else:
        try:
            moment = datetime.fromisoformat(str(feedback_at))
        except (TypeError, ValueError):
            return str(feedback_at or "")
    # 保留 tzinfo：state.to_datetime() 返回带时区时间，若 here 丢时区，
    # 后续 is_overdue() 拿 naive due 与 aware now 比较会崩（真实路径也会踩）。
    deadline = moment.replace(
        hour=FOLLOWUP_DUE_TIME.hour, minute=FOLLOWUP_DUE_TIME.minute,
        second=FOLLOWUP_DUE_TIME.second, microsecond=0)
    return deadline.isoformat(timespec="seconds")


def is_overdue(due: str, now: datetime) -> bool:
    """仍待回访且已过当日截止 → 超时。日期比较用可控时钟注入的 now。"""
    if not due:
        return False
    try:
        deadline = datetime.fromisoformat(due)
    except (TypeError, ValueError):
        return False
    return now > deadline


def script_for(category: str = "", *, variant: str = "A", no: str = "") -> dict[str, str]:
    """取某分类的当日回访话术。variant 只认 A/B，其余按 A。

    返回 `{"text": ..., "variant": ..., "forbid": ...}`；
    `forbid` 列出这条话术刻意不说的内容，供讲师讲评与质检对照。
    """
    key = str(category or "").strip()
    script = SCRIPTS.get(key, _SCRIPT_DEFAULT)
    v = "B" if str(variant).strip().upper() == "B" else "A"
    text = script[v].format(no=no or "{no}")
    return {
        "text": text,
        "variant": v,
        "forbid": "不承诺退款/金额、不猜原因、不替员工辩解；补偿以工单终审为准",
    }
