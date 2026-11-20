# -*- coding: utf-8 -*-
"""转人工分级接管：这一单 AI 兜不兜得住，谁必须到场，几分钟内给答复。

## 为什么要在 `need_human` 之外再造一层

`llm.need_human` 是**模型**给的判断。提示词里为了不让 AI 什么都推后台，明确
要求「上菜慢、温度、普通菜品质量能当场解决就设 need_human=false」——这条本意
是对的，但它同时留下了一个误判通道：模型完全可能把「菜里有玻璃」也判成
`need_human=false`，然后 `submit()` 按 `AUTO_CLOSE_CATEGORIES` 把它自动结案。
食品安全不该由模型的临场判断决定，所以这里做**确定性下限**：

* 本模块命中 `human_now` 时，**模型无法降级**（`overridden=True` 会记下来，
  审计里能查到「AI 曾判无需人工，被规则层驳回」）；
* 本模块只升不降：模型说 `need_human=true` 而规则层判 `self_service`，
  仍然按 `ai_plus_human` 走，宁可多派一次人。

## 三级（对客承诺的响应时间不同，这才是「分级」的意义）

| level | 含义 | 谁负责 | 对客承诺 |
|---|---|---|---|
| `self_service` | AI 当场办完，无人跟进 | — | 无（已办完） |
| `ai_plus_human` | AI 受理 + 人工跟进核实 | 值班组长 / 值班经理 | 5–15 分钟 |
| `human_now` | 立即人工接管，AI 只做登记与安抚 | 值班经理 / 店长 | 3–5 分钟 |

## 边界（和 handler / service 一致，不越线）

* 本层**只判级别与话术**，不批准补偿、不改 state、不生成凭证；
* 承诺时间是「有人回您」的时间，**不是**「补偿到账」的时间，话术里不得混为一谈；
* 不猜事实：没有订单号就不说订单号，没核实就不说「是我们的问题」。

规则与 `16_AI客服FAQ话术库.md` 里 25 条 `esc=true` 的条目一一对应，
`code` 就是 FAQ 编号的可枚举版本，质检与审计直接按它统计。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = ["LEVELS", "CODES", "RULES", "assess", "needs_human"]

#: 三级接管。顺序即严重程度，前端按它排序展示。
LEVELS = ("self_service", "ai_plus_human", "human_now")

#: 升级原因码。稳定枚举，别随手改——审计、质检、看板都按它聚合。
CODES = (
    # human_now
    "food_safety",        # 异物 / 变质 / 吃坏肚子（FAQ-CMP-001/002）
    "allergy_reaction",   # 已经出现过敏反应（FAQ-CMP-003）
    "legal_threat",       # 报警 / 监管 / 曝光 / 起诉（FAQ-CMP-015）
    "explicit_escalation",  # 明确要求找领导 / 要投诉到人（FAQ-CMP-016）
    "repeat_complaint",   # 带着未结案工单又提新问题（FAQ-CMP-007 的升级形态）
    # ai_plus_human
    "money_claim",        # 退款 / 赔偿 / 免单（FAQ-CMP-009/010）
    "service_attitude",   # 服务态度纠纷（FAQ-CMP-008）
    "quality_check",      # 菜品质量 / 分量，需核对配方与出餐记录（FAQ-CMP-005/006）
    "slow_repeat",        # 等太久且已催多次（FAQ-CMP-007）
    "model_flag",         # 模型自己要求人工，规则层没有反对理由
    # self_service
    "normal",
)

#: 三级 → (责任人, 对客承诺分钟)。承诺的是「有人回您」，不是「赔您」。
_OWNERS: dict[str, tuple[str, int]] = {
    "food_safety": ("门店值班经理", 3),
    "allergy_reaction": ("门店值班经理", 3),
    "legal_threat": ("门店店长", 5),
    "explicit_escalation": ("门店值班经理", 5),
    "repeat_complaint": ("门店值班经理", 5),
    "money_claim": ("值班组长", 10),
    "service_attitude": ("值班组长", 10),
    "quality_check": ("值班组长", 15),
    "slow_repeat": ("值班组长", 10),
    "model_flag": ("值班组长", 15),
    "normal": ("", 0),
}

#: 对客话术。**不得出现「赔/退/免」的承诺**，只承诺「谁、几分钟内、来做什么」。
_LINES: dict[str, str] = {
    "food_safety": ("这个情况我立刻转给值班经理本人，{m} 分钟内到您这桌处理；"
                    "这道菜先别动，值班组长同时封存留样。"),
    "allergy_reaction": ("过敏这事不能等，我立刻叫值班经理过来，{m} 分钟内到您这桌；"
                         "如果出现呼吸不畅、嘴唇发肿，请立刻拨打 120，别等我。"),
    "legal_threat": ("您反映的问题我立刻交给店长本人跟进，{m} 分钟内联系您，"
                     "相关记录我会原样保留。"),
    "explicit_escalation": ("我这就请值班经理过来当面处理，{m} 分钟内到您这桌，"
                            "不用您再重复一遍情况。"),
    "repeat_complaint": ("您这单之前已经登记过，我直接升级给值班经理优先处理，"
                         "{m} 分钟内给您回复，不用再提一次。"),
    "money_claim": ("涉及补偿的部分需要管理人员核实订单与出餐记录后答复，"
                    "我已把这单标为优先，{m} 分钟内给您明确说法。"),
    "service_attitude": ("服务上的问题我记下了，已交值班组长核实，{m} 分钟内跟您确认。"),
    "quality_check": ("菜品问题已记录并通知厨房核对，值班组长 {m} 分钟内跟进您这单。"),
    "slow_repeat": ("这单已经催过不止一次，我把它升级为超时优先，"
                    "{m} 分钟内有人回您具体还要多久。"),
    "model_flag": ("这单我已转人工跟进，{m} 分钟内有人回复您。"),
    "normal": "",
}

#: 食品安全**强信号**：词本身就是事故，出现即 human_now。
#: 注意「玻璃/塑料/酸味」不在此列——它们是包装材质与口味词，见下方弱信号。
_FOOD_SAFETY = (
    "异物", "头发", "虫子", "苍蝇", "蟑螂", "钢丝球",
    "变质", "馊", "发霉", "霉了", "吃坏", "食物中毒",
    "拉肚子", "腹泻", "呕吐", "上医院", "进医院", "急诊", "中毒", "不敢吃",
)
#: 弱信号：脱离"吃进嘴里"的语境就只是材质/口味咨询。
#: 审查 P2-3 实测：旧版把「打包用的是塑料盒吗」「有没有酸味的菜」
#: 「上一单的玻璃瓶可乐没给」都判成 food_safety / human_now，
#: 值班经理被喊来处理包装咨询——正是本模块 docstring 警告的失效形态。
_FOOD_SAFETY_WEAK = ("玻璃", "塑料", "钢丝", "石子", "沙粒", "酸味")
#: 弱信号的语境词（**指示/体感**，不是光有"菜"字就算）。
#: 用「吃/喝/里/吃出/坏」区分「有没有酸味的菜」（咨询）与
#: 「这汤有酸味，是不是坏了」（真事故）。**不放「这/那」**——
#: 「这个塑料盒能回收吗」的「这个」也会触发，泛指示词区分不了。
_FOOD_CONTEXT = (
    "吃", "喝", "里", "中", "咬", "咽", "发现", "吃出", "喝出",
    "舌", "胃", "肚", "坏", "馊", "变质", "扎", "硌",
)
_FOOD_CONTEXT_WINDOW = 8
#: 过敏：只有在「已经吃下去/已经有反应」时才升级。
#: 单纯问「这个菜有花生吗」是菜单问题，不能当过敏事故处理——否则每问一句
#: 忌口都变成一张 human_now 工单，值班经理会被自己的系统喊到麻木。
_ALLERGY = ("过敏", "起疹", "疹子", "痒", "嘴唇肿", "喉咙肿", "呼吸困难", "休克")
_ALLERGY_REACTED = ("吃了", "吃完", "喝完", "入口", "之后", "以后", "现在", "已经",
                    "开始", "反应", "肿", "痒", "呼吸")

#: 法律/舆情风险：这类话一出现，门店必须有人知道，AI 不能自己圆。
_LEGAL = (
    "报警", "警察", "12315", "市场监管", "卫生局", "食药监", "卫健委", "律师",
    "起诉", "法院", "曝光", "发微博", "发抖音", "小红书", "记者", "举报",
)
#: 明确要人：不是投诉内容，是投诉**对象**。
_ESCALATE_TO_HUMAN = (
    "找你们领导", "叫你们经理", "找经理", "叫店长", "找店长", "你们老板",
    "换个能说话的", "叫个真人", "不要机器人", "人工客服", "转人工",
)
#: 涉钱：只要出现诉求，就不能自动结案。
_MONEY = (
    "退款", "退钱", "退我", "赔", "免单", "打折", "折扣", "补偿", "优惠券",
    "代金券", "报销",
)
#: 态度类：需要调排班/工位记录，AI 判不了对错。
_ATTITUDE = ("态度", "不理人", "没人理", "喊了几次", "说话难听", "甩脸", "骂人")
#: 慢：必须带上「极端时长」或「催过多次」才算升级形态。
#: 只写「等了」会把「等了好久还没上菜」也升成人工——那句话的正解是当场催菜
#: 并结案（`test_低风险咨询由AI即时答复结案` 就是这个口径），别抢走它。
_SLOW = ("半小时", "半个小时", "一个小时", "一小时", "一个多小时",
         "催了好几次", "催了两次", "催了三次", "催过几次", "等半天", "一直没上")


#: 人读标签，管理端列表直接显示，不用前端再拼。
_LABELS: dict[str, str] = {
    "food_safety": "食品安全 · 立即人工接管",
    "allergy_reaction": "过敏反应 · 立即人工接管",
    "legal_threat": "法律/舆情风险 · 店长接管",
    "explicit_escalation": "顾客要求人工 · 经理到场",
    "repeat_complaint": "同类问题反复 · 升级经理",
    "money_claim": "补偿诉求 · 人工核实",
    "service_attitude": "服务态度 · 人工核实",
    "quality_check": "菜品质量 · 人工跟进",
    "slow_repeat": "催菜无效 · 超时优先",
    "model_flag": "AI 提请人工",
    "normal": "AI 已当场处理",
}


def _hit(text: str, words: Iterable[str]) -> list[str]:
    return [w for w in words if w and w in text]


def _hit_food_safety(text: str) -> list[str]:
    """强信号直接命中；材质/口味词要有"吃进嘴里"的语境才算食安。

    语境窗口取 ±8 字：一句顾客话里"这汤…酸味"挨得很近，而"有没有酸味的菜"
    含的是"有没有"这种咨询词，不在 `_FOOD_CONTEXT` 里，所以不会误升。
    """
    found = list(_hit(text, _FOOD_SAFETY))
    for word in _FOOD_SAFETY_WEAK:
        start = 0
        while True:
            pos = text.find(word, start)
            if pos < 0:
                break
            lo = max(0, pos - _FOOD_CONTEXT_WINDOW)
            hi = min(len(text), pos + len(word) + _FOOD_CONTEXT_WINDOW)
            if any(ctx in text[lo:hi] for ctx in _FOOD_CONTEXT):
                found.append(word)
                break
            start = pos + 1
    return found


def _allergy_reaction(text: str) -> list[str]:
    """区分「我花生过敏（询问）」与「吃完身上痒（已经出事）」。"""
    allergy = _hit(text, _ALLERGY)
    if not allergy:
        return []
    return allergy if _hit(text, _ALLERGY_REACTED) else []


def assess(*, text: Any, category: str = "", sentiment: str = "",
           wants_refund: bool = False, need_human: bool = False,
           repeat_count: int = 0) -> dict[str, Any]:
    """判定接管级别。返回可直接落进 `handling["escalation"]` 的字典。

    `need_human` 传模型判断（`AiResult.need_human`），`repeat_count` 传同一顾客
    名下**未结案工单数**（由 `GuestService` 数，本层不查库）。
    """
    raw = str(text or "")
    reasons: list[str] = []
    code = ""

    def take(candidate: str, found: Iterable[str], why: str = "") -> None:
        """首个命中即定案：`found` 是关键词命中列表，空列表表示这个规则没命中。"""
        nonlocal code
        hits = list(found)
        if code or not hits:
            return
        code = candidate
        reasons.append(why or f"命中「{'/'.join(hits[:2])}」")

    # 1) 食品安全与过敏反应 —— 最高优先级，模型说什么都不影响
    take("food_safety", _hit_food_safety(raw))
    take("allergy_reaction", _allergy_reaction(raw))
    # 2) 法律/舆情，3) 顾客明确要人
    take("legal_threat", _hit(raw, _LEGAL))
    take("explicit_escalation", _hit(raw, _ESCALATE_TO_HUMAN))
    # 4) 顾客名下还有未结案工单，又开新单 → 上一单没解决，升级经理
    if not code and int(repeat_count or 0) >= 1:
        code = "repeat_complaint"
        reasons.append(f"名下还有 {int(repeat_count)} 单未结案，又提了新问题")
        # 级别已定，但管理端仍需知道这单还带着补偿诉求——只升级不丢信息
        if _hit(raw, _MONEY) or wants_refund:
            reasons.append("同时提到补偿诉求")
    # 5) 涉钱 / 态度 / 慢 / 质量 —— 人工跟进，但不必立刻打断
    take("money_claim",
         _hit(raw, _MONEY) or (["wants_refund"] if wants_refund else []),
         "顾客提出补偿诉求")
    take("service_attitude", _hit(raw, _ATTITUDE))
    take("slow_repeat", _hit(raw, _SLOW))
    if not code and category in ("菜品质量", "服务态度"):
        code = "quality_check" if category == "菜品质量" else "service_attitude"
        reasons = [f"AI 分类为「{category}」"]
    # 6) 模型自己要求人工 —— 尊重它，但不因此把普通问题升成「立即接管」
    if not code and need_human:
        code, reasons = "model_flag", ["AI 判断需人工核实"]
    if not code:
        code = "normal"

    level = ("human_now" if code in ("food_safety", "allergy_reaction",
                                     "legal_threat", "explicit_escalation",
                                     "repeat_complaint")
             else "self_service" if code == "normal"
             else "ai_plus_human")
    owner, promise = _OWNERS[code]
    # 模型说不用人、规则层却判必须人 → 记下来，审计要能查出「AI 判轻了」
    overridden = level == "human_now" and not need_human
    return {
        "level": level,
        "code": code,
        "label": _LABELS.get(code, code),
        "owner": owner,
        "promise_min": promise,
        "reasons": reasons[:3],
        "customer_line": _LINES[code].format(m=promise) if promise else "",
        "model_need_human": bool(need_human),
        "overridden": overridden,
        "category": str(category or ""),
        "sentiment": str(sentiment or ""),
    }


def needs_human(result: Mapping[str, Any] | None) -> bool:
    """该单是否需要真人跟进（`ai_plus_human` 与 `human_now` 都算）。"""
    return str((result or {}).get("level") or "") in ("ai_plus_human", "human_now")
