# -*- coding: utf-8 -*-
"""客诉「处理决策」层：判断阶段 / 卡点 / 超时，并生成有具体信息的回复。

## 为什么单独一层

原来 AI 层只输出分类与安抚话术，所有转人工的工单共用一句
「已转交值班组长核实」——顾客看不到卡在哪、超时没有、已经做了什么，
客服也没真的做任何动作。这一层把「事实判断」和「文案生成」分开：

* **事实判断是确定性的**：阶段、卡点、SLA 剩余时间、订单号、菜品、催菜
  次数，全部从真实数据算出来，不交给模型自由发挥（模型会幻觉订单号）。
* **文案才有 AI 参与**：在线 provider 拿到这些 facts 后写回复；离线规则层
  用同样的 facts 拼出带具体信息的答复。两边都禁止再说空泛的「转交值班」。

## 边界（和 guest/service.py 一致，别越线）

本层**不批准补偿、不改审批状态机的 state、不生成凭证**。所谓「推进」指：
催厨房（上菜慢且菜品未结束）、催办当前责任人、超时升级、把可自动完成的
信息补齐并如实告知顾客。退款终审仍然只有 manager 能做，且必须双人复核。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

__all__ = [
    "STAGES", "ACTION_LABELS", "analyze", "compose_reply",
    "reply_has_concrete_info", "urgent_actions",
]

#: 状态 → 当前环节 / 卡点 / 责任人。责任人是「催办」动作的目标。
STAGES: dict[str, dict[str, str]] = {
    "submitted": {
        "label": "已受理 · 待组长核实证据",
        "blocker": "组长核实证据",
        "owner": "值班组长",
        "next": "组长勾选现场证据并确认订单快照金额",
    },
    "evidence_ok": {
        "label": "证据已核实 · 待管理人员终审",
        "blocker": "管理人员终审",
        "owner": "值班经理",
        "next": "经理在补偿审批台终审并生成凭证",
    },
    "rejected": {
        "label": "已驳回 · 待顾客补充证据",
        "blocker": "顾客补充证据",
        "owner": "顾客",
        "next": "顾客补充现场证据后工单重新进入核实环节",
    },
    "auto_closed": {
        "label": "智能客服已即时答复结案",
        "blocker": "",
        "owner": "",
        "next": "无需跟进，如有新情况可重新提交",
    },
    "approved": {
        "label": "已批准 · 补偿凭证已生成",
        "blocker": "",
        "owner": "",
        "next": "凭证已生成，演示环境不发生实际退款",
    },
}

#: 已执行动作 → 对顾客可见的措辞。键和 service._execute_actions 的 type 对齐。
ACTION_LABELS: dict[str, str] = {
    "urge_kitchen": "向厨房发起催单",
    "nudge_reviewer": "催办当前环节负责人",
    "escalate_overdue": "升级为超时优先处理",
    "evidence_linked": "关联订单快照与出餐信息",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """同时兼容 Complaint dataclass 与 dict（视图快照）。"""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def analyze(complaint: Any, *, order: Any = None, item: Any = None,
            now: datetime | None = None, sla_sec: int = 30 * 60) -> dict[str, Any]:
    """从工单 + 订单事实算出当前阶段、卡点、超时与可引用的具体信息。

    `sla_sec` 由调用方传（guest.service.SLA_SEC），这里不反向 import service，
    避免 service → handler → service 的循环依赖。
    """
    if now is None:
        # 工单时间戳可能带 tzinfo（演示时钟/测试时钟），naive now 会减不过去
        ref = _get(complaint, "updated_at") or _get(complaint, "created_at")
        tz = ref.tzinfo if isinstance(ref, datetime) and ref.tzinfo else None
        moment = datetime.now(tz)
    else:
        moment = now
    state = str(_get(complaint, "state", "") or "")
    stage = STAGES.get(state, {
        "label": state or "未知环节", "blocker": "待确认",
        "owner": "值班人员", "next": "请联络值班人员确认处理环节",
    })
    updated = _get(complaint, "updated_at")
    created = _get(complaint, "created_at")
    age_min = max(0, int((moment - created).total_seconds() // 60)) if created else 0
    elapsed = (moment - updated).total_seconds() if updated else 0.0
    remain_min = max(0, int((sla_sec - elapsed) // 60))
    overdue = state in ("submitted", "evidence_ok") and elapsed > sla_sec

    evidences = list(_get(complaint, "evidences") or ())
    dish_name = str(_get(item, "dish_name", "") or "")
    if not dish_name and evidences:
        # 没关联子项时，从证据的 order_item 说明里取一个可读的菜名，不编造
        for row in evidences:
            if str(_get(row, "kind", "")) == "order_item":
                dish_name = str(_get(row, "content", "") or "")[:40]
                break

    amount = _get(complaint, "amount", 0) or 0
    return {
        "stage": state,
        "stage_label": stage["label"],
        "blocker": stage["blocker"],
        "blocker_owner": stage["owner"],
        "next_step": stage["next"],
        "terminal": state in ("approved", "auto_closed"),
        "overdue": overdue,
        "overdue_min": int(elapsed // 60) - sla_sec // 60 if overdue else 0,
        "sla_remain_min": remain_min,
        "age_min": age_min,
        "complaint_no": str(_get(complaint, "complaint_no", "") or ""),
        "order_no": str(_get(order, "order_no", "") or ""),
        "dish_name": dish_name,
        "item_state": str(_get(item, "state", "") or ""),
        "urge_count": int(_get(item, "urge_count", 0) or 0),
        "evidence_count": len(evidences),
        "verified_count": sum(1 for row in evidences if _get(row, "verified")),
        "amount": round(float(amount), 2),
        "wants_refund": bool(_get(complaint, "wants_refund")),
        "category": str(_get(complaint, "category", "") or ""),
    }


def reply_has_concrete_info(reply: str, analysis: Mapping[str, Any]) -> bool:
    """在线模型的回复是否真的用上了具体事实，没用上就由规则层兜底重写。"""
    text = str(reply or "")
    signals = [analysis.get("complaint_no"), analysis.get("order_no"),
               analysis.get("stage_label"), analysis.get("blocker")]
    hits = sum(1 for s in signals if s and str(s) in text)
    # 至少命中工单号 +（订单号或环节）才算具体；纯安抚话术一律重写
    return bool(analysis.get("complaint_no") and text.find(str(analysis["complaint_no"])) >= 0
                and hits >= 2)


def compose_reply(base_reply: str, analysis: Mapping[str, Any],
                  actions: list[dict[str, Any]] | None = None) -> str:
    """生成对顾客可见的具体回复。

    优先保留在线模型写得好的部分；若它没引用任何具体事实（典型就是旧的
    「已转交值班组长」），用本层按真实数据拼出的版本覆盖。无论哪一层，都
    会追加「已执行动作」与「当前卡点 / 剩余时间」，让顾客看得见进度。
    """
    a = analysis
    done = actions or []
    action_texts = [str(x.get("reply") or ACTION_LABELS.get(
        str(x.get("type", "")), str(x.get("detail", "")))) for x in done]
    action_texts = [t for t in action_texts if t]

    if reply_has_concrete_info(base_reply, a):
        parts = [str(base_reply).strip()]
    else:
        parts = []
        no = a.get("complaint_no")
        order_no = a.get("order_no")
        dish = a.get("dish_name")
        head = f"已收到您的反馈，工单 {no} 已受理" if no else "已收到您的反馈，工单已受理"
        if dish:
            head += f"，涉及「{dish}」"
        if order_no:
            head += f"（订单 {order_no}）"
        parts.append(head + "。")

        stage_label = a.get("stage_label")
        if stage_label:
            parts.append(f"当前环节：{stage_label}。")
        blocker = a.get("blocker")
        if blocker:
            next_step = a.get("next_step")
            parts.append(f"卡点：{blocker}" + (f"，下一步{next_step}" if next_step else "") + "。")
        else:
            parts.append("当前无待跟进环节，本工单已结案。")

    if action_texts:
        parts.append("我已" + "；".join(dict.fromkeys(action_texts)) + "。")

    if not a.get("terminal"):
        if a.get("overdue"):
            parts.append(f"该环节已超出 30 分钟 SLA 约 {a.get('overdue_min', 0)} 分钟，已升级处理。")
        else:
            parts.append(f"该环节 SLA 剩余约 {a.get('sla_remain_min', 0)} 分钟，超时会自动升级。")
        if a.get("wants_refund"):
            parts.append(f"补偿基数按订单快照 ¥{a.get('amount', 0)} 核算，不按当前菜价。")
        parts.append("进度可在「我的工单」查看，无需重复提交。")
    return "".join(parts)


def urgent_actions(analysis: Mapping[str, Any]) -> list[str]:
    """给管理端展示：当前需要谁跟进。空列表表示无需人工催办。"""
    owner = analysis.get("blocker_owner")
    if not owner or analysis.get("terminal"):
        return []
    out = [f"催办{owner}：{analysis.get('next_step', '')}".rstrip("：")]
    if analysis.get("overdue"):
        out.insert(0, f"超时 {analysis.get('overdue_min', 0)} 分钟，优先处理")
    return out
