"""客诉补偿矩阵：把「该赔多少、谁能批、为什么」从人的经验变成代码。

## 为什么要有这一层

`llm.COMPENSATIONS` 是**方案白名单**（八种），但它只回答「可以选什么」，
不回答「这一单该选什么」。原来 `resolve` / `approve` 的 `method` 由管理员
任选，于是出现三种真实问题：

1. **八种里有一种兑不了**：`points`（积分）需要会员体系，本项目没有，
   批了也无法对客兑现——对客话术只能讲五种，这一层直接把 `points` 拦下。
2. **同一类问题赔法不一致**：异物和上菜慢都赔「优惠券」，SOP 上说不通。
   食安问题必须「免单 + 致歉 + 留样」，上菜慢只需「加急 + 小额券」。
3. **上限没人算**：`_order_cap` 只防「赔超订单金额」，但没防「一道 16 元
   的菜赔一张 50 元券」。上限要按分级和方案分别算。

## 三级与权限

| 级别 | 触发 | 决策人 | 金额上限 |
|---|---|---|---|
| L1 | `self_service`（AI 当场处理） | AI | 0（只做重做/加急，不涉及钱） |
| L2 | `ai_plus_human`（涉钱/态度/慢/质量） | 值班经理 | ≤ 订单快照 50%，且 ≤ ¥50 |
| L3 | `human_now`（食安/法律/要人/反复） | 店长（manager） | ≤ 订单快照 100% |

**边界**：这里的「上限」是**补偿凭证面额上限**，不是退款金额——项目无支付
链路，`settled` 恒 0，凭证由门店线下核销。任何对客表述不得写成「已退款」。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = ["TIERS", "BLOCKED", "MATRIX", "assess", "check"]

#: 补偿凭证面额上限（按订单快照金额的比例 + 绝对金额双约束）。
TIERS: dict[str, dict[str, Any]] = {
    "L1": {
        "label": "L1 · AI 即时处理",
        "decide_by": "智能客服",
        "ratio": 0.0, "cap_abs": 0.0, "sla_min": 0,
        "desc": "不涉及金额，只做重做/加急/致歉，当场答复",
    },
    "L2": {
        "label": "L2 · 值班经理可决",
        "decide_by": "值班经理",
        "ratio": 0.5, "cap_abs": 50.0, "sla_min": 30,
        "desc": "小额补偿，经理单人可批；超过部分须升级店长",
    },
    "L3": {
        "label": "L3 · 店长决策",
        "decide_by": "店长",
        "ratio": 1.0, "cap_abs": 0.0, "sla_min": 10,
        "desc": "食安/法律风险，按订单快照全额处理，须留样与书面记录",
    },
}

#: 不可用方案 → 不可用原因。这份清单同时是「对客话术不许提」的依据。
BLOCKED: dict[str, str] = {
    "points": "项目无会员体系，积分无法兑现，对客不得承诺",
}

#: 分类 → (首选方案, 备选方案, 适用说明)。顺序即推荐优先级。
MATRIX: dict[str, tuple[str, tuple[str, ...], str]] = {
    "异物":       ("waive", ("coupon", "apology"),
                   "食品安全，按全单免单处理，同时留样并记录批次"),
    "温度":       ("priority", ("redo", "coupon"),
                   "重做并优先出餐；顾客已食用时改小额券"),
    "菜品质量":   ("redo", ("dish_gift", "coupon"),
                   "先重做，不接受重做时给赠品或券"),
    "上菜慢":     ("priority", ("coupon", "dish_gift"),
                   "加急出餐为主，不轻易免单"),
    "服务态度":   ("apology", ("coupon",),
                   "书面致歉为主；用菜品折扣替代会传递错误信号"),
    "其他":       ("apology", ("coupon",),
                   "先致歉说明，确有过失再给券"),
}

#: 方案的金额性质：full=按订单金额、capped=受上限约束、none=不涉钱
_MONEY_KIND: dict[str, str] = {
    "waive": "full", "coupon": "capped", "voucher": "capped",
    "dish_gift": "capped", "redo": "none", "priority": "none",
    "apology": "none", "points": "capped",
}

_DEFAULT = ("apology", ("coupon",), "先致歉并核实，再决定补偿")


def _tier_of(level: str) -> str:
    return {"self_service": "L1", "ai_plus_human": "L2",
            "human_now": "L3"}.get(str(level or ""), "L2")


def _cap(level: str, amount: float) -> float:
    """本单可批的凭证面额上限。L1 为 0，L3 按订单快照全额。"""
    spec = TIERS[_tier_of(level)]
    snapshot = max(0.0, float(amount or 0))
    if spec["ratio"] <= 0:
        return 0.0
    by_ratio = round(snapshot * float(spec["ratio"]), 2)
    abs_cap = float(spec["cap_abs"] or 0)
    return round(min(by_ratio, abs_cap) if abs_cap else by_ratio, 2)


def assess(*, category: str = "", level: str = "self_service",
           amount: float = 0.0, wants_refund: bool = False,
           handoff_code: str = "") -> dict[str, Any]:
    """给出这一单的补偿建议。只读，不改任何状态。

    `amount` 传**下单快照金额**（`complaint.order_snapshot_amount`），
    不传当前菜价——价格改了不该影响已发生的客诉。
    """
    tier_key = _tier_of(level)
    tier = TIERS[tier_key]
    cap = _cap(level, amount)
    primary, alternatives, why = MATRIX.get(str(category or ""), _DEFAULT)

    # 食安问题必须走 L3：分类是「异物」但接管级别被模型判成 self_service 时，
    # 以分类为准抬到 L3——赔偿口径不能比风险口径更松。
    if category == "异物" and tier_key != "L3":
        tier_key = "L3"
        tier = TIERS[tier_key]
        cap = _cap("human_now", amount)

    reasons: list[str] = [why]
    if handoff_code:
        reasons.append(f"接管原因：{handoff_code}")
    if wants_refund and not amount:
        reasons.append("工单未带订单快照，无法核算金额，只能给不涉钱方案")

    # 无快照金额时，涉钱方案不可用——不许凭感觉报一个数
    available = [m for m in (primary, *alternatives) if m not in BLOCKED]
    if amount <= 0:
        available = [m for m in available if _MONEY_KIND.get(m) == "none"] or ["apology"]
        primary = available[0]
        reasons.append("无金额快照：只能选重做/加急/致歉")

    # L3 的免单是标准动作；L2 的免单受上限约束（可能只够部分免）
    if primary == "waive" and cap and cap < float(amount or 0):
        reasons.append(f"免单面额按上限核减为 ¥{cap:.2f}")

    return {
        "tier": tier_key,
        "tier_label": tier["label"],
        "decide_by": tier["decide_by"],
        "sla_min": tier["sla_min"],
        "category": str(category or ""),
        "suggest": primary,
        "alternatives": [m for m in available if m != primary],
        "cap": cap,
        "cap_label": (f"≤ ¥{cap:.2f}" if cap else "不涉及金额"),
        "amount": round(float(amount or 0), 2),
        "reasons": reasons,
        "blocked": dict(BLOCKED),
        "tier_desc": tier["desc"],
    }


def check(method: str, *, advice: Mapping[str, Any],
          amount: float = 0.0) -> dict[str, Any]:
    """校验管理员选定的方案。返回 `{ok, error, warn, over_tier}`。

    - 硬拒：不可兑现方案（`points`）、涉钱方案但无金额快照
    - 软警告：选的不是首选方案、金额超过本级上限（`over_tier`）
      ——软警告不拦人，因为店长有权超出标准档处理，但必须留痕进审计。
    """
    method = str(method or "")
    if method in BLOCKED:
        return {"ok": False, "error": BLOCKED[method], "warn": "", "over_tier": False}

    kind = _MONEY_KIND.get(method)
    if kind is None:
        return {"ok": False, "error": f"未知补偿方案：{method}",
                "warn": "", "over_tier": False}

    cap = float(advice.get("cap") or 0)
    money = float(amount or 0)
    if kind != "none" and money <= 0:
        return {"ok": False,
                "error": "本工单没有订单金额快照，只能给不涉钱的方案（重做/加急/致歉）",
                "warn": "", "over_tier": False}

    if kind == "full" and cap and money > cap:
        return {"ok": True, "error": "",
                "warn": f"免单面额 ¥{money:.2f} 超过本级上限 ¥{cap:.2f}，需店长确认并记录理由",
                "over_tier": True}

    warn = ""
    if method != advice.get("suggest"):
        warn = (f"标准方案为「{advice.get('suggest')}」，本单选了「{method}」，"
                f"请确认与分类口径一致")
    return {"ok": True, "error": "", "warn": warn, "over_tier": False}
