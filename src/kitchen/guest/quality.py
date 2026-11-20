# -*- coding: utf-8 -*-
"""客服质检评分卡：只给「能从工单事实取证」的项打分。

## 为什么不是完整的 100 分

参考质检标准是四维 100 分：服务态度 30 / 业务能力 30 / 规范执行 20 /
问题解决 20。但其中「礼貌用语、同理心、耐心、开场白、结束语」这些项，
工单数据里**没有任何可取证字段**——它们需要听录音或看完整对话由人判断。

如果让代码凭 AI 回复里有没有「您」字就打「礼貌用语 8 分」，那不是质检，
是编数字。所以本模块只评可自动取证的 70 分（规范 20 + 业务 30 + 解决 20），
服务态度 30 分列进 `manual_items` 并标 `pending`，由值班主管按参考表人工补分。
输出里 `coverage=0.7` 明确告诉看板：这不是完整质检分。

## 计分口径

* 100 分制，扣完为止，每项带 `source`（取值字段）和 `why`（扣分理由），
  逐条可复核、可申诉（参考 SOP：结果出来 3 个工作日内可申诉）。
* `grade` 由折算百分制 `normalized` 得出，沿用参考库阈值 90/80/70/60。
* **护栏命中不等于事故**：禁语被 `guard` 改写、模型判轻被 `handoff` 接管，
  顾客侧看到的都是合规结果；扣分评的是 AI 输出质量，不是顾客受损。
  每条这类扣分都会在 `why` 里写明「已拦截 / 已兜住」，避免被误读成服务事故。

## 边界

* 只读：接收 `_view()` 的 row，不改工单、不写库。
* 兼容旧数据：缺字段按「无从取证」处理并降档，不抛异常。
* 人工项不参与 `normalized`，也不能由本模块补分。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "AUTO_MAX", "MANUAL_MAX", "DIMENSIONS", "GRADES", "MANUAL_ITEMS",
    "grade_of", "score_complaint", "summarize",
]

#: 自动可取证满分 / 必须人工评的满分。
AUTO_MAX = 70.0
MANUAL_MAX = 30.0

DIMENSIONS: tuple[tuple[str, str, float], ...] = (
    ("norm", "规范执行", 20.0),
    ("ability", "业务能力", 30.0),
    ("resolve", "问题解决", 20.0),
    ("attitude", "服务态度", 30.0),   # 全部人工
)

#: 等级阈值，降序；与参考库 §1.2 一致。
GRADES: tuple[tuple[str, float], ...] = (
    ("优秀", 90.0), ("良好", 80.0), ("合格", 70.0),
    ("待改进", 60.0), ("不合格", 0.0),
)

#: 服务态度的四个评分项——系统不打分，交由人工质检。
MANUAL_ITEMS: tuple[dict[str, Any], ...] = (
    {"id": "politeness", "name": "礼貌用语", "max": 8,
     "reason": "需看完整对话判断是否全程使用「请/您/谢谢」，工单字段无从取证"},
    {"id": "empathy", "name": "同理心表达", "max": 8,
     "reason": "需判断情绪识别与回应是否恰当，关键词匹配会误判反讽与方言"},
    {"id": "initiative", "name": "主动性", "max": 7,
     "reason": "需结合上下文判断是否主动跟进，工单只记动作不记语气"},
    {"id": "patience", "name": "耐心程度", "max": 7,
     "reason": "需听录音或看多轮对话，单条 ai_reply 不足以判定"},
)

_HUMAN_ACTIONS = frozenset({"review_evidence", "resolve", "approve", "reject"})
_FOOD_CATEGORY = frozenset({"异物", "食品安全"})
_APPEAL_DAYS = 3


def grade_of(score: float) -> str:
    """折算百分制 → 等级。沿参考库阈值，不因覆盖不足而放宽。"""
    value = float(score or 0)
    for name, floor in GRADES:
        if value >= floor:
            return name
    return "不合格"


def _dt(value: Any):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def score_complaint(row: Mapping[str, Any]) -> dict[str, Any]:
    """给一张工单评分。输入是 `GuestService._view()` 的 row。

    返回结构固定：四项汇总 → 逐项扣分明细 → 人工待评项 → 申诉提示。
    `normalized` 是自动分折算百分制后的值，**只代表可取证部分**。
    """
    row = dict(row or {})
    items: list[dict[str, Any]] = []

    def add(dim: str, iid: str, name: str, full: float, got: float,
            why: str, source: str) -> None:
        score = max(0.0, min(float(got or 0), float(full)))
        items.append({
            "dim": dim, "id": iid, "name": name,
            "max": full, "score": round(score, 1),
            "deduct": round(float(full) - score, 1),
            "why": str(why), "source": str(source),
        })

    state = str(row.get("state") or "")
    category = str(row.get("category") or "")
    handling = dict(row.get("handling") or {})
    handoff = dict(row.get("handoff") or {})
    comp = dict(row.get("compensation") or {})
    history = [dict(x) for x in row.get("history") or ()]
    terminal = state in ("approved", "auto_closed")

    # ---------- 规范执行（20） ----------
    # 1) 禁语：guard 命中说明模型说过不该说的话，虽已被出口改写，仍扣输出质量分
    hits = list(handling.get("guard") or [])
    terms = [str(x.get("term") or "") for x in hits if isinstance(x, Mapping)]
    if not hits:
        add("norm", "banned_words", "对客话术合规", 8.0, 8.0,
            "出口禁语扫描零命中", "handling.guard")
    else:
        lost = min(8.0, 4.0 * len(hits))
        add("norm", "banned_words", "对客话术合规", 8.0, 8.0 - lost,
            f"模型输出命中禁语 {len(hits)} 处（{'、'.join(terms[:3])}）；"
            f"已被出口拦截改写，顾客未看到原话，但 AI 输出质量不达标",
            "handling.guard")

    # 2) 驳回对客隔离：内部口径不得原样推给顾客
    if state == "rejected":
        customer = str(row.get("customer_reason") or "")
        internal = str(row.get("reject_reason") or "")
        code = str(row.get("reject_code") or "")
        if not code:
            add("norm", "reject_isolation", "驳回口径隔离", 6.0, 1.0,
                "驳回未使用标准原因码，对客说明无法复核", "reject_code")
        elif customer and customer == internal:
            add("norm", "reject_isolation", "驳回口径隔离", 6.0, 0.0,
                "内部驳回原因被原样推给顾客，泄露风控口径", "customer_reason/reject_reason")
        elif not customer:
            add("norm", "reject_isolation", "驳回口径隔离", 6.0, 2.0,
                "缺少对客标准说明，顾客不知道下一步怎么补证", "customer_reason")
        else:
            add("norm", "reject_isolation", "驳回口径隔离", 6.0, 6.0,
                f"按原因码 {code} 给出对客说明，内部原因未外泄",
                "reject_code/customer_reason")
    # 非驳回单：该项不适用，直接不加——避免「没驳回就白拿 6 分」

    # 3) 记录完整：涉钱单必须有订单快照（补偿核算的唯一依据）
    has_snapshot = float(row.get("order_snapshot_amount") or 0) > 0
    if row.get("wants_refund") and not has_snapshot:
        add("norm", "record_integrity", "信息记录完整", 6.0, 1.0,
            "顾客提了补偿诉求但工单没有订单金额快照，无法核算上限",
            "order_snapshot_amount")
    elif not str(row.get("text") or ""):
        add("norm", "record_integrity", "信息记录完整", 6.0, 0.0,
            "工单没有原始诉求文本", "text")
    else:
        add("norm", "record_integrity", "信息记录完整", 6.0, 6.0,
            "诉求文本与订单快照均在", "text/order_snapshot_amount")

    # ---------- 业务能力（30） ----------
    # 4) 风险识别：食安必须立即接管；模型判轻被规则纠正要记一笔
    level = str(handoff.get("level") or "")
    code = str(handoff.get("code") or "")
    overridden = bool(handoff.get("overridden"))
    if category in _FOOD_CATEGORY and level != "human_now":
        add("ability", "risk_called", "风险识别准确", 12.0, 0.0,
            f"分类为「{category}」却未升级到立即人工接管，食安风险漏判",
            "category/handoff.level")
    elif overridden:
        add("ability", "risk_called", "风险识别准确", 12.0, 8.0,
            f"模型判「无需人工」被规则层纠正为「{code}」；顾客侧已兜住，"
            f"但模型风险识别能力需复盘", "handoff.overridden")
    elif level and code:
        add("ability", "risk_called", "风险识别准确", 12.0, 12.0,
            f"接管级别 {level}（{code}）与风险匹配", "handoff.level/code")
    else:
        add("ability", "risk_called", "风险识别准确", 12.0, 6.0,
            "旧工单无接管记录，无从取证风险识别是否准确", "handoff")

    # 5) 补偿方案合规：偏离标准方案与超额都要留痕，留了痕仍扣分但轻扣
    chosen = str(comp.get("chosen") or row.get("method") or "")
    if terminal and chosen and chosen != "none":
        suggest = str(comp.get("suggest") or "")
        cap = float(comp.get("cap") or 0)
        amount = float(comp.get("amount") or 0)
        over = amount > cap > 0
        deviation = chosen != suggest
        if over and deviation:
            add("ability", "compensation_fit", "补偿方案合规", 10.0, 3.0,
                f"选「{chosen}」偏离标准「{suggest}」且 ¥{amount:.2f} 超上限 ¥{cap:.2f}；"
                f"已留痕可特批，但需店长复核理由", "compensation.chosen/cap/amount")
        elif over:
            add("ability", "compensation_fit", "补偿方案合规", 10.0, 5.0,
                f"面额 ¥{amount:.2f} 超过本级上限 ¥{cap:.2f}，需店长确认",
                "compensation.cap/amount")
        elif deviation:
            add("ability", "compensation_fit", "补偿方案合规", 10.0, 7.0,
                f"选「{chosen}」偏离标准方案「{suggest}」，需说明理由",
                "compensation.chosen/suggest")
        else:
            add("ability", "compensation_fit", "补偿方案合规", 10.0, 10.0,
                f"按分类标准方案「{suggest}」处理", "compensation.chosen/suggest")
    else:
        # 未结案/不涉及补偿，不适用
        pass

    # 6) 分类有效性
    if category and category != "其他":
        add("ability", "category_valid", "问题分类有效", 8.0, 8.0,
            f"归入「{category}」，可直接套用处理 SOP", "category")
    elif category == "其他":
        add("ability", "category_valid", "问题分类有效", 8.0, 3.0,
            "分类落在「其他」，无法套用分类 SOP，需人工重判", "category")
    else:
        add("ability", "category_valid", "问题分类有效", 8.0, 0.0,
            "缺少问题分类", "category")

    # ---------- 问题解决（20） ----------
    # 7) 闭环：rejected 顾客还在补证，不算闭环（与 feedback 的口径一致）
    if terminal:
        add("resolve", "closure", "闭环管理", 7.0, 7.0,
            f"工单已结案（{state}）", "state")
    elif state == "rejected":
        add("resolve", "closure", "闭环管理", 7.0, 2.0,
            "工单处于驳回补证中，尚未形成结论", "state")
    else:
        add("resolve", "closure", "闭环管理", 7.0, 0.0,
            f"工单未结案（{state}）", "state")

    # 8) 时效：30 分钟 SLA，超时即扣
    created = _dt(row.get("created_at"))
    decided = _dt(row.get("decided_at"))
    if decided and created:
        mins = max(0.0, (decided - created).total_seconds() / 60)
        if mins > 30:
            add("resolve", "sla", "处理时效", 7.0, 2.0,
                f"从提交到结案用时 {mins:.0f} 分钟，超过 30 分钟 SLA", "created_at/decided_at")
        else:
            add("resolve", "sla", "处理时效", 7.0, 7.0,
                f"结案用时 {mins:.0f} 分钟，在 SLA 内", "created_at/decided_at")
    elif row.get("overdue"):
        add("resolve", "sla", "处理时效", 7.0, 1.0,
            "当前环节已超 30 分钟 SLA 仍未结案", "handling.overdue")
    else:
        remain = handling.get("sla_remain_min")
        add("resolve", "sla", "处理时效", 7.0, 7.0,
            f"处理中，SLA 剩余约 {remain if remain is not None else 30} 分钟，未超时",
            "handling.sla_remain_min")

    # 9) 首次解决：重复投诉 / 补证都说明第一次没解决
    supplements = sum(1 for x in history if x.get("action") == "supplement")
    if code == "repeat_complaint":
        add("resolve", "first_contact", "首次解决", 6.0, 1.0,
            "同一顾客带着未结案工单又提新问题，首次解决失败", "handoff.code")
    elif supplements:
        add("resolve", "first_contact", "首次解决", 6.0, 3.0,
            f"顾客补证 {supplements} 次后才推进，首次材料不完整", "history.supplement")
    else:
        add("resolve", "first_contact", "首次解决", 6.0, 6.0,
            "一次提交即进入处理，无补证与重复投诉", "history/handoff.code")

    # ---------- 汇总 ----------
    # 不适用项已从 items 中剔除，分母就是实际取证到的满分之和
    applicable_max = sum(float(x["max"]) for x in items)
    auto_score = sum(float(x["score"]) for x in items)
    normalized = round(auto_score / applicable_max * 100, 1) if applicable_max else 0.0
    deductions = [dict(x) for x in items if x["deduct"] > 0]

    human_actors = [str(x.get("actor") or "") for x in history
                    if x.get("action") in _HUMAN_ACTIONS and x.get("actor")]
    return {
        "score": round(auto_score, 1),
        "applicable_max": round(applicable_max, 1),
        "auto_max": AUTO_MAX,
        "manual_max": MANUAL_MAX,
        "normalized": normalized,
        "grade": grade_of(normalized),
        # 覆盖度是「这套评分体系机器能判多少」，固定 0.7；
        # 不能用本单 applicable_max 去除——那会随工单形态乱跳。
        "coverage": round(AUTO_MAX / (AUTO_MAX + MANUAL_MAX), 2),
        "is_full_score": False,       # 人工项未评，永远不是完整质检分
        "items": items,
        "deductions": deductions,
        "manual_items": [dict(x, status="pending", score=None) for x in MANUAL_ITEMS],
        "last_human": human_actors[-1] if human_actors else "",
        "appeal": {"days": _APPEAL_DAYS,
                   "hint": f"对评分有异议可在 {_APPEAL_DAYS} 个工作日内向客服主管申诉，"
                           f"由主管与质检主管联合复核"},
        "note": ("自动分只覆盖可取证的规范/业务/解决三项；服务态度 30 分需人工补评。"
                 "护栏命中已拦截的项仍扣分，评的是 AI 输出质量，不是顾客受损"),
    }


def summarize(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """团队质检汇总：均分、等级分布、TOP 扣分项、按处理人聚合。"""
    cards = [score_complaint(r) for r in rows]
    if not cards:
        return {"count": 0, "avg": 0.0, "pass_rate": 0.0, "excellent_rate": 0.0,
                "coverage": round(AUTO_MAX / (AUTO_MAX + MANUAL_MAX), 2),
                "grades": {}, "top_deductions": [], "by_actor": {},
                "note": "暂无工单可质检"}
    total = len(cards)
    values = [float(c["normalized"]) for c in cards]
    grades: dict[str, int] = {}
    for c in cards:
        grades[c["grade"]] = grades.get(c["grade"], 0) + 1
    lost: dict[str, dict[str, Any]] = {}
    for c in cards:
        for d in c["deductions"]:
            row = lost.setdefault(d["id"], {"id": d["id"], "name": d["name"],
                                            "times": 0, "total_deduct": 0.0})
            row["times"] += 1
            row["total_deduct"] = round(row["total_deduct"] + d["deduct"], 1)
    top = sorted(lost.values(), key=lambda x: (-x["times"], -x["total_deduct"]))[:8]
    actors: dict[str, dict[str, Any]] = {}
    for c in cards:
        actor = c.get("last_human")
        if not actor:
            continue
        row = actors.setdefault(actor, {"count": 0, "sum": 0.0, "fail": 0})
        row["count"] += 1
        row["sum"] += float(c["normalized"])
        if c["grade"] in ("待改进", "不合格"):
            row["fail"] += 1
    by_actor = {
        k: {"count": v["count"], "avg": round(v["sum"] / v["count"], 1),
            "needs_coach": v["fail"]}
        for k, v in actors.items()
    }
    return {
        "count": total,
        "avg": round(sum(values) / total, 1),
        "pass_rate": round(sum(1 for v in values if v >= 70) / total, 3),
        "excellent_rate": round(sum(1 for v in values if v >= 90) / total, 3),
        "coverage": round(AUTO_MAX / (AUTO_MAX + MANUAL_MAX), 2),
        "grades": grades,
        "top_deductions": top,
        "by_actor": by_actor,
        "note": ("自动分折算百分制，仅覆盖可取证 70 分；完整质检需补服务态度 30 分。"
                 "按处理人聚合取该单最后一位人工处理者"),
    }
