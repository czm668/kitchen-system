# -*- coding: utf-8 -*-
"""顾客档案 / 画像 / 客诉 / 补偿审批域（A11 扩展模块）。

## 这个域和订单域的关系

**不复制订单状态机**。客诉只**引用** `order_id / item_id`，金额一律从
`OrderItem.amount`（下单时的单价快照）算，绝不自己存一份价格。顾客下单时
`Order.guest_key` 落档，画像聚合就是遍历 `state.orders.orders()` 过滤同一
`guest_key`——订单是唯一事实源，画像只是它的派生视图。

## 状态机（两级双人复核）

```
                    ┌─ 低风险且无补偿 → AUTO_CLOSED（AI 秒级答复，终态）
顾客提交 → AI 受理 ─┤
                    └─ 其余 → SUBMITTED ──lead 核证据──→ EVIDENCE_OK
                                                            │
                                              manager 终审 ─┼→ APPROVED（凭证，终态）
                                                            └→ REJECTED ─补证→ SUBMITTED
```

- `lead` 核证据：至少 1 条 `verified=True`、必须关联订单、金额 ≤ 订单快照。
- `manager` 终审：权限只有 manager，且 `approver != reviewer`（双人复核）。
- **AI 不参与任何状态迁移**，它的输出只写进 `complaint.ai_suggestion`。
- 乐观锁 `version`：所有审批动作都要带 `expected_version`，经理双击两次
  第二次必须 409，不能重复生成凭证。

## 资金边界（答辩必答）

项目没有支付（`09_顾客端小程序设计.md` §一明确不做），所以这里叫**补偿审批
台账**：审批通过只生成 `voucher_no`（重做/免单/优惠券凭证），不调支付网关、
不产生资金流、不改订单金额。界面和文档必须如实标注，不能叫「已退款」。

## 持久化

客诉是凭证，重启丢了没法解释。复用 `process/journal.py` 的追加日志同构思路：
每次状态变更后追加一条**整单快照**，重放时按 `complaint_no` 后写覆盖。
选快照而不是操作序列，是为了重放时**不重新调用 AI**（AI 结果已存进快照，
重新生成既慢又可能和当时不一样）。`var_dir=None`（纯内存测试）自动跳过。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from kitchen.domain import (
    DomainError, InvalidTransition, NotFoundError, StaleVersionError,
)
from kitchen.process.journal import append_journal

from .handler import analyze, compose_reply, urgent_actions
from .llm import (
    COMPENSATIONS, TAGS, LLMProvider, RuleProvider, llm_run_row,
    run_guarded, sanitize_user_text,
)

log = logging.getLogger("kitchen.guest")

__all__ = ["GuestError", "ComplaintState", "Evidence", "Complaint",
           "GuestProfile", "GuestService"]

#: 客诉 SLA（真实秒，不受演示 time_scale 影响——演示加速不该让工单瞬间超时）
SLA_SEC = 30 * 60
#: 自动结案允许的分类：涉及食安/异物/服务态度一律转人工
AUTO_CLOSE_CATEGORIES = ("上菜慢", "温度", "其他")
EVIDENCE_KINDS = ("photo", "text", "order_item", "log")
#: 双人复核开关。默认关闭——管理员可单人核实+终审（单人直通 resolve）。
#: 生产场景可设 KITCHEN_DUAL_REVIEW=1 恢复「核实人≠终审人」的强约束。
DUAL_REVIEW = str(__import__("os").environ.get(
    "KITCHEN_DUAL_REVIEW", "")).strip().lower() in ("1", "true", "yes", "on")


class GuestError(DomainError):
    """顾客服务域的可预期拒绝（400）。"""


class ComplaintState:
    SUBMITTED = "submitted"
    EVIDENCE_OK = "evidence_ok"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_CLOSED = "auto_closed"
    TERMINAL = frozenset({APPROVED, AUTO_CLOSED})


@dataclass
class Evidence:
    id: int
    kind: str
    content: str
    url: str = ""
    verified: bool = False
    verified_by: str = ""
    verified_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(id=self.id, kind=self.kind, content=self.content,
                    url=self.url, verified=self.verified,
                    verified_by=self.verified_by, verified_at=self.verified_at,
                    note=self.note)

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Evidence":
        return cls(id=int(row["id"]), kind=str(row["kind"]),
                   content=str(row.get("content") or ""),
                   url=str(row.get("url") or ""),
                   verified=bool(row.get("verified")),
                   verified_by=str(row.get("verified_by") or ""),
                   verified_at=str(row.get("verified_at") or ""),
                   note=str(row.get("note") or ""))


@dataclass
class Complaint:
    id: int
    complaint_no: str
    guest_key: str
    customer_ref: str
    table_id: int | None
    order_id: int | None
    item_id: int | None
    category: str
    text: str
    wants_refund: bool
    amount: float
    #: 下单时的订单/子项价格快照，随工单一起落盘。资金护栏以此为准，
    #: 绝不读运行时 OrderService——订单只写库、启动不回读，服务重启后查不到，
    #: 旧实现把「查不到」兜底成 cap=0，导致每笔带补偿的工单都被判「超额」。
    order_snapshot_amount: float
    method: str
    state: str
    version: int
    created_at: datetime
    updated_at: datetime
    evidences: list[Evidence] = field(default_factory=list)
    ai_suggestion: dict[str, Any] = field(default_factory=dict)
    reviewer: str = ""
    approver: str = ""
    reviewed_at: str = ""
    decided_at: str = ""
    reject_reason: str = ""
    voucher_no: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.state in ComplaintState.TERMINAL

    @property
    def verified_count(self) -> int:
        return sum(1 for row in self.evidences if row.verified)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "complaint_no": self.complaint_no,
            "guest_key": self.guest_key, "customer_ref": self.customer_ref,
            "table_id": self.table_id, "order_id": self.order_id,
            "item_id": self.item_id, "category": self.category,
            "text": self.text, "wants_refund": self.wants_refund,
            "amount": self.amount,
            "order_snapshot_amount": self.order_snapshot_amount,
            "method": self.method, "state": self.state,
            "version": self.version,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
            "evidences": [row.to_dict() for row in self.evidences],
            "ai_suggestion": dict(self.ai_suggestion),
            "reviewer": self.reviewer, "approver": self.approver,
            "reviewed_at": self.reviewed_at, "decided_at": self.decided_at,
            "reject_reason": self.reject_reason, "voucher_no": self.voucher_no,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Complaint":
        return cls(
            id=int(row["id"]), complaint_no=str(row["complaint_no"]),
            guest_key=str(row["guest_key"]),
            customer_ref=str(row.get("customer_ref") or ""),
            table_id=row.get("table_id"), order_id=row.get("order_id"),
            item_id=row.get("item_id"), category=str(row["category"]),
            text=str(row["text"]), wants_refund=bool(row["wants_refund"]),
            amount=float(row.get("amount") or 0),
            # 兼容改造前落盘的旧工单：那时没有独立快照字段，amount 本身就是快照
            order_snapshot_amount=float(
                row.get("order_snapshot_amount")
                if row.get("order_snapshot_amount") is not None
                else (row.get("amount") or 0)),
            method=str(row.get("method") or "none"), state=str(row["state"]),
            version=int(row.get("version") or 0),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            evidences=[Evidence.from_dict(x) for x in row.get("evidences") or ()],
            ai_suggestion=dict(row.get("ai_suggestion") or {}),
            reviewer=str(row.get("reviewer") or ""),
            approver=str(row.get("approver") or ""),
            reviewed_at=str(row.get("reviewed_at") or ""),
            decided_at=str(row.get("decided_at") or ""),
            reject_reason=str(row.get("reject_reason") or ""),
            voucher_no=str(row.get("voucher_no") or ""),
            history=[dict(x) for x in row.get("history") or ()],
        )


@dataclass
class GuestProfile:
    guest_key: str
    tags: list[str] = field(default_factory=list)
    openid: str | None = None          # 生产接微信登录后由服务端绑定
    created_at: str = ""
    portrait: str = ""
    portrait_mode: str = ""
    portrait_updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(guest_key=self.guest_key, tags=list(self.tags),
                    openid=self.openid, created_at=self.created_at,
                    portrait=self.portrait, portrait_mode=self.portrait_mode,
                    portrait_updated_at=self.portrait_updated_at)

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "GuestProfile":
        return cls(guest_key=str(row["guest_key"]),
                   tags=[str(t) for t in row.get("tags") or ()],
                   openid=row.get("openid"),
                   created_at=str(row.get("created_at") or ""),
                   portrait=str(row.get("portrait") or ""),
                   portrait_mode=str(row.get("portrait_mode") or ""),
                   portrait_updated_at=str(row.get("portrait_updated_at") or ""))


class GuestService:
    """挂在 `AppState.guest`，单实例。所有写操作都过 `_persist()`。"""

    def __init__(self, state, *, provider: LLMProvider | None = None,
                 journal_path: Path | None = None) -> None:
        self.state = state
        self.provider: LLMProvider = provider or RuleProvider()
        self._journal_path = journal_path
        self._profiles: dict[str, GuestProfile] = {}
        self._complaints: dict[int, Complaint] = {}
        self._llm_runs: list[dict[str, Any]] = []
        self._next_id = 1
        self._next_evidence = 1
        self._idem: dict[tuple[str, str], dict[str, Any]] = {}
        self._applied, self._skipped = self._replay()
        # 对话式客服引擎：能直接办的（催菜/查单/问菜单）当场办，真投诉才建单
        from .chat import ChatService
        self.chat = ChatService(self)

    # ------------------------------------------------------------ 持久化
    def _persist(self, op: str, data: dict[str, Any]) -> bool:
        ok = append_journal(self._journal_path, {"op": op, "data": data})
        if not ok:
            log.warning("guest journal 写入失败：%s（内存态已生效）", op)
        return ok

    def save_profile(self, profile: GuestProfile) -> None:
        self._profiles[profile.guest_key] = profile
        self._persist("profile", profile.to_dict())

    def save_complaint(self, complaint: Complaint) -> None:
        self._complaints[complaint.id] = complaint
        self._persist("complaint", complaint.to_dict())

    def _replay(self) -> tuple[int, int]:
        """整单快照按序覆盖：后写覆盖先写，天然幂等，不重放 AI 调用。"""
        if self._journal_path is None or not self._journal_path.exists():
            return (0, 0)
        applied = skipped = 0
        by_no: dict[str, dict[str, Any]] = {}
        profiles: dict[str, dict[str, Any]] = {}
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                op, data = record.get("op"), record.get("data")
                if not isinstance(data, dict):
                    raise ValueError("data 不是对象")
                if op == "complaint":
                    by_no[str(data["complaint_no"])] = data
                elif op == "profile":
                    profiles[str(data["guest_key"])] = data
                else:
                    raise ValueError(f"未知 op {op}")
                applied += 1
            except (ValueError, KeyError, TypeError, OSError):
                skipped += 1
        for key, row in profiles.items():
            try:
                self._profiles[key] = GuestProfile.from_dict(row)
            except (ValueError, KeyError, TypeError):
                skipped += 1
        for row in by_no.values():
            try:
                item = Complaint.from_dict(row)
                self._complaints[item.id] = item
                self._next_id = max(self._next_id, item.id + 1)
                for ev in item.evidences:
                    self._next_evidence = max(self._next_evidence, ev.id + 1)
            except (ValueError, KeyError, TypeError):
                skipped += 1
        return (applied, skipped)

    @property
    def journal_stats(self) -> dict[str, int]:
        return {"applied": self._applied, "skipped": self._skipped}

    # ------------------------------------------------------------ 档案 / 画像
    def bind(self, guest_key: str, tags: Iterable[str] = ()) -> GuestProfile:
        """开桌时绑定档案。`tags` 只收白名单（忌口/过敏标签不能让前端乱传）。"""
        key = str(guest_key or "").strip()
        if not key or len(key) > 64:
            raise GuestError("guest_key 缺失或过长")
        profile = self._profiles.get(key)
        if profile is None:
            profile = GuestProfile(
                guest_key=key,
                created_at=self.state.to_datetime().isoformat(timespec="seconds"),
            )
        cleaned = [t for t in {str(x).strip() for x in tags} if t in TAGS]
        if cleaned:
            profile.tags = sorted(set(profile.tags) | set(cleaned))
        self.save_profile(profile)
        return profile

    def profile(self, guest_key: str) -> GuestProfile:
        found = self._profiles.get(str(guest_key or ""))
        if found is None:
            raise NotFoundError("没有该顾客档案，请先开桌")
        return found

    def stats_of(self, guest_key: str) -> dict[str, Any]:
        """从订单聚合画像事实。**只出统计，不出身份**。"""
        orders = [o for o in self.state.orders.orders()
                  if o.guest_key == str(guest_key)]
        counter: dict[str, int] = {}
        hours: list[int] = []
        urge_total = 0
        amount = 0.0
        for order in orders:
            amount += order.amount
            if order.created_at:
                hours.append(order.created_at.hour)
            for item in order.items:
                counter[item.dish_name] = counter.get(item.dish_name, 0) + item.qty
                urge_total += item.urge_count
        top = [name for name, _ in sorted(counter.items(),
                                          key=lambda kv: (-kv[1], kv[0]))[:3]]
        lunch = sum(1 for h in hours if 10 <= h < 15)
        dinner = sum(1 for h in hours if 17 <= h < 22)
        daypart = "午餐" if lunch > dinner else ("晚餐" if dinner > lunch else "")
        return {
            "order_count": len(orders), "top_dishes": top, "daypart": daypart,
            "urge_total": urge_total,
            "complaint_count": self._complaint_count(guest_key),
            "total_amount": round(amount, 2),
            "tags": list(self._profiles.get(str(guest_key), GuestProfile("")).tags),
            "text": "",
        }

    def _complaint_count(self, guest_key: str) -> int:
        return sum(1 for c in self._complaints.values()
                   if c.guest_key == str(guest_key))

    def portrait(self, guest_key: str, *, refresh: bool = False) -> dict[str, Any]:
        """生成/读缓存画像。样本不足时 AI 层会明确输出「样本不足」。"""
        profile = self.profile(guest_key)
        stats = self.stats_of(guest_key)
        if profile.portrait and not refresh:
            return {"portrait": profile.portrait, "mode": profile.portrait_mode,
                    "updated_at": profile.portrait_updated_at,
                    "facts": stats, "cached": True}
        result = run_guarded(self.provider, "profile", stats)
        self._record_llm_run(result, "ok")
        profile.portrait = result.summary
        profile.portrait_mode = result.mode
        profile.portrait_updated_at = self.state.to_datetime().isoformat(timespec="seconds")
        self.save_profile(profile)
        return {"portrait": result.summary, "mode": result.mode,
                "updated_at": profile.portrait_updated_at,
                "facts": stats, "cached": False,
                "ai": result.to_dict()}

    def _record_llm_run(self, result, status: str, error: str = "") -> None:
        self._llm_runs.append({
            **llm_run_row(result, status=status, error=error),
            "ts": self.state.to_datetime().isoformat(timespec="seconds"),
        })
        if len(self._llm_runs) > 200:      # 定长，防止长跑内存无界增长
            del self._llm_runs[:-200]

    def llm_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._llm_runs[-max(1, int(limit)):])

    # ------------------------------------------------------------ 读
    def complaint(self, complaint_id: int) -> Complaint:
        found = self._complaints.get(int(complaint_id))
        if found is None:
            raise NotFoundError(f"客诉 {complaint_id} 不存在")
        return found

    @staticmethod
    def _timeliness_key(c: Complaint) -> tuple[float, int]:
        """时效性排序键：越新提交越靠前。

        以前按 `id` 升序排，id 单调递增，结果新客诉沉在列表底部、旧的霸占
        顶部——和「时效性是第一优先」完全相反。这里改成 created_at **降序**：
        元组按升序比较，故取负时间戳；同一秒提交时 id 大的（后提交）在前。

        刻意不用 updated_at：老工单被审批一下就把 updated_at 刷成当前时间、
        冒到队首，看起来像「新投诉」，反而误导。要的是「新提交的在上面」。
        """
        try:
            ts = float(c.created_at.timestamp())
        except (AttributeError, OSError, ValueError, TypeError):
            ts = 0.0
        return (-ts, -int(c.id))

    def complaints_of(self, guest_key: str) -> tuple[Complaint, ...]:
        return tuple(c for c in sorted(self._complaints.values(),
                                       key=self._timeliness_key)
                     if c.guest_key == str(guest_key))

    def all_complaints(self) -> tuple[Complaint, ...]:
        return tuple(sorted(self._complaints.values(), key=self._timeliness_key))

    @property
    def pending_count(self) -> int:
        return sum(1 for c in self._complaints.values()
                   if c.state in (ComplaintState.SUBMITTED,
                                 ComplaintState.EVIDENCE_OK))

    def overdue(self) -> list[str]:
        """SLA 超时未结案的客诉编号。用真实墙钟，不吃演示 time_scale。"""
        from datetime import datetime as _dt
        now = _dt.now().timestamp()
        out = []
        for c in self._complaints.values():
            if c.state in (ComplaintState.SUBMITTED, ComplaintState.EVIDENCE_OK):
                if now - c.updated_at.timestamp() > SLA_SEC:
                    out.append(c.complaint_no)
        return out

    # ------------------------------------------------------------ 写：提交客诉
    def submit(
        self, *, token: str, guest_key: str, text: Any,
        order_id: Any = None, item_id: Any = None,
        wants_refund: bool = False, method: str = "redo",
        evidences: list[dict[str, Any]] | None = None,
        client_event_id: str | None = None,
    ) -> dict[str, Any]:
        """顾客提交客诉 → AI 秒级受理 → 低风险自动答复 / 其余转人工。

        归属校验走 `CustomerService`：令牌无效、订单不属于本令牌、
        `guest_key` 与令牌绑定不一致，任一不满足直接拒绝。只传一个
        伪造的 `guest_key` 是查不到也提交不了的。
        """
        if client_event_id:
            cached = self._idem.get(("submit", str(client_event_id)))
            if cached is not None:
                return cached
        owner = self.state.customer.require_guest(token, guest_key)
        clean_text, suspicious = sanitize_user_text(text)
        if len(clean_text) < 5:
            raise GuestError("请至少填写 5 个字的问题描述")
        if method not in COMPENSATIONS and wants_refund:
            raise GuestError("补偿方式只能是 redo / waive / coupon")

        order = None
        amount = 0.0
        if order_id is not None and str(order_id).strip():
            oid = int(order_id)
            if oid not in self.state.customer.order_ids_of(token):
                raise NotFoundError("订单不存在或不属于当前桌台")
            order = self.state.orders.order(oid)
            if order.guest_key != owner["guest_key"]:
                raise GuestError("订单档案与当前顾客不一致")
            amount = order.amount
            if item_id is not None and str(item_id).strip():
                iid = int(item_id)
                item = next((x for x in order.items if x.id == iid), None)
                if item is None:
                    raise NotFoundError("订单子项不存在")
                amount = item.amount
        elif wants_refund:
            raise GuestError("补偿申请必须关联一笔订单，便于核实金额与出餐记录")

        moment = self.state.to_datetime()
        evidence_rows = self._build_evidences(evidences or ())
        # 关联子项拿到的具体事实只用于「说人话」：菜名、状态、催菜次数都来自
        # 订单域真实数据，模型/规则层不得自己编订单号或金额。
        item_obj = None
        if order is not None and item_id is not None and str(item_id).strip():
            iid = int(item_id)
            item_obj = next((x for x in order.items if x.id == iid), None)
        facts = {
            "text": clean_text, "wants_refund": bool(wants_refund),
            "injection_suspected": suspicious,
            "category_hint": "", "order_amount": amount,
            "order_no": order.order_no if order else "",
            "dish_name": item_obj.dish_name if item_obj else "",
            "item_state": str(item_obj.state or "") if item_obj else "",
            "urge_count": int(item_obj.urge_count or 0) if item_obj else 0,
        }
        result = run_guarded(self.provider, "support", facts)
        self._record_llm_run(result, "ok", result.degraded_reason)

        # 上菜慢但顾客没点名子项：挑一道还没结束、催得最少的菜代他催厨房，
        # 别让「已受理」停在纸面上。有明确 item_id 时绝不替顾客猜。
        if (order is not None and item_obj is None
                and result.category == "上菜慢"):
            pending = [x for x in order.items
                       if not getattr(x, "finished", False)]
            if pending:
                item_obj = min(pending, key=lambda x: (int(x.urge_count or 0), x.id))

        can_auto = (not wants_refund and not result.need_human
                    and not suspicious
                    and result.category in AUTO_CLOSE_CATEGORIES)
        state = (ComplaintState.AUTO_CLOSED if can_auto
                 else ComplaintState.SUBMITTED)
        complaint = Complaint(
            id=self._next_id,
            complaint_no=f"C{moment.strftime('%m%d')}-{self._next_id:03d}",
            guest_key=owner["guest_key"], customer_ref=owner["customer_ref"],
            table_id=owner.get("table_id"),
            order_id=order.id if order else None,
            item_id=int(item_id) if item_id is not None and str(item_id).strip() else None,
            category=result.category, text=clean_text,
            wants_refund=bool(wants_refund),
            amount=round(amount, 2) if wants_refund else 0.0,
            order_snapshot_amount=round(amount, 2),
            method=method if wants_refund else "none",
            state=state, version=1, created_at=moment, updated_at=moment,
            evidences=evidence_rows,
        )

        # ---- 处理决策：判断阶段/卡点/超时 → 实际执行能自动完成的动作 ----
        handling = analyze(complaint, order=order, item=item_obj,
                           now=moment, sla_sec=SLA_SEC)
        actions = self._execute_actions(complaint, handling, item=item_obj)
        handling["actions"] = actions
        handling["urgent"] = urgent_actions(handling)
        # AI 若只给了「已转交值班人员」这类空话，用真实数据重写成具体答复
        reply = compose_reply(result.reply, handling, actions)
        result = dc_replace(result, reply=reply)
        complaint.ai_suggestion = {
            **result.to_dict(), "handling": dict(handling),
        }
        self._next_id += 1
        self._record(complaint, "submit", actor="customer",
                     detail={"ai_mode": result.mode,
                             "auto_closed": can_auto,
                             "injection_suspected": suspicious,
                             "actions": [x["type"] for x in actions]})
        self.save_complaint(complaint)

        payload = {
            "complaint_no": complaint.complaint_no, "state": complaint.state,
            "category": complaint.category, "ai_reply": result.reply,
            "ai_summary": result.summary, "ai_mode": result.mode,
            "need_human": result.need_human,
            "sla_minutes": SLA_SEC // 60,
            "stage": handling["stage"], "stage_label": handling["stage_label"],
            "blocker": handling["blocker"], "blocker_owner": handling["blocker_owner"],
            "overdue": handling["overdue"],
            "sla_remain_min": handling["sla_remain_min"],
            "actions": actions, "urgent": handling["urgent"],
            "note": ("已由智能客服即时答复结案" if can_auto else
                     f"当前环节：{handling['stage_label']}；"
                     f"卡点：{handling['blocker'] or '无'}；"
                     f"已执行 {len(actions)} 项处理动作"),
            "amount": complaint.amount,
        }
        self.state.emit("complaint", {
            "op": "submit", "complaint_no": complaint.complaint_no,
            "guest_key": complaint.guest_key,
            "customer_ref": complaint.customer_ref,
            "category": complaint.category, "state": complaint.state,
            "stage_label": handling["stage_label"],
            "blocker": handling["blocker"],
            "overdue": handling["overdue"],
            "actions": [x["type"] for x in actions],
            "message": result.reply or "客诉已受理",
        })
        self.state.audit.record(
            actor="customer", role="customer", action="guest.complaint",
            target=complaint.complaint_no,
            after={"category": complaint.category,
                   "wants_refund": complaint.wants_refund,
                   "ai_mode": result.mode, "auto_closed": can_auto})
        if client_event_id:
            self._idem[("submit", str(client_event_id))] = payload
        return payload

    def _build_evidences(self, rows: list[dict[str, Any]]) -> list[Evidence]:
        out: list[Evidence] = []
        for row in rows[:5]:                    # 一次最多 5 条，防刷
            kind = str(row.get("kind") or "text")
            if kind not in EVIDENCE_KINDS:
                raise GuestError(f"证据类型 {kind} 不支持")
            content = sanitize_user_text(row.get("content"), 300)[0]
            url = str(row.get("url") or "")[:300]
            # 图片证据有 url 即可，content 允许为空（照片本身即内容）
            if not content and not (kind == "photo" and url):
                continue
            if kind == "photo" and url and not url.startswith("/api/"):
                raise GuestError("图片证据 url 非法")
            out.append(Evidence(id=self._next_evidence, kind=kind,
                                content=content or "[图片证据]", url=url))
            self._next_evidence += 1
        return out

    # ------------------------------------------------------------ 自动处理
    def _execute_actions(self, complaint: Complaint, handling: dict[str, Any],
                         *, item: Any = None) -> list[dict[str, Any]]:
        """执行客诉里能自动完成的动作，返回对顾客可见的动作清单。

        刻意**不碰审批状态机**：不核实证据、不批准补偿、不改 version，
        退款终审仍由 manager 按双人复核规则执行。这里推进的是「催厨房 /
        催办责任人 / 超时升级 / 关联订单快照」这类不需要人做决定的步骤。
        """
        actions: list[dict[str, Any]] = []

        # 1) 上菜慢且菜品未结束 → 直接催厨房，走订单域累加 urge_count
        if (handling["category"] == "上菜慢" and item is not None
                and not getattr(item, "finished", False)):
            try:
                self.state.orders.urge(item.id, actor="guest-ai",
                                       now=self.state.to_datetime())
                count = int(getattr(item, "urge_count", 0) or 0)
                actions.append({
                    "type": "urge_kitchen", "item_id": item.id,
                    "urge_count": count,
                    "detail": f"已向厨房催「{item.dish_name}」（第 {count} 次）",
                    "reply": f"向厨房催了「{item.dish_name}」"
                             f"（第 {count} 次，派单优先级已提前）",
                })
            except Exception as exc:  # noqa: BLE001 - 菜品刚做完就别让客诉提交失败
                log.info("客诉自动催菜跳过：%s", exc)

        # 2) 关联订单 → 订单快照落进处理记录，人工核证据时直接可用
        if handling["order_no"]:
            actions.append({
                "type": "evidence_linked", "order_no": handling["order_no"],
                "detail": f"已关联订单 {handling['order_no']} 的下单快照",
            })

        # 3) 有人工卡点 → 发起催办，顾客侧不再只是「等通知」
        owner = handling["blocker_owner"]
        if owner and owner != "顾客":
            self.state.emit("complaint", {
                "op": "nudge", "complaint_no": complaint.complaint_no,
                "guest_key": complaint.guest_key,
                "owner": owner, "blocker": handling["blocker"],
                "message": (f"请{owner}处理工单 {complaint.complaint_no}："
                            f"{handling['blocker']}"),
            })
            actions.append({
                "type": "nudge_reviewer", "owner": owner,
                "detail": f"已催办{owner}处理「{handling['blocker']}」",
                "reply": f"催办{owner}处理「{handling['blocker']}」",
            })

        # 4) 超时 → 升级优先处理
        if handling["overdue"]:
            self.state.emit("complaint", {
                "op": "escalate", "complaint_no": complaint.complaint_no,
                "overdue_min": handling["overdue_min"],
                "message": (f"工单 {complaint.complaint_no} 超时 "
                            f"{handling['overdue_min']} 分钟，请优先处理"),
            })
            actions.append({
                "type": "escalate_overdue",
                "detail": f"超时 {handling['overdue_min']} 分钟，已升级",
            })
        return actions

    # ------------------------------------------------------------ 写：审批
    def _guard_version(self, complaint: Complaint,
                       expected_version: int | None) -> None:
        if expected_version is not None and int(expected_version) != complaint.version:
            raise StaleVersionError(
                f"客诉 {complaint.complaint_no} 已被修改（本地 v{expected_version}，"
                f"当前 v{complaint.version}），请刷新后重试")

    def _record(self, complaint: Complaint, action: str, *, actor: str,
                detail: dict[str, Any] | None = None) -> None:
        complaint.history.append({
            "action": action, "actor": actor, "version": complaint.version,
            "time": self.state.to_datetime().isoformat(timespec="seconds"),
            "state": complaint.state, **(detail or {}),
        })

    def review_evidence(self, complaint_id: int, *, actor: str,
                        evidence_ids: list[Any],
                        expected_version: int | None = None,
                        note: str = "") -> dict[str, Any]:
        """组长核实证据（lead）。核实 ≠ 批准，这里只把证据置为可信。"""
        complaint = self.complaint(complaint_id)
        self._guard_version(complaint, expected_version)
        if complaint.state != ComplaintState.SUBMITTED:
            raise InvalidTransition(
                f"当前 {complaint.state}，只有 submitted 能核实证据")
        if complaint.wants_refund and complaint.order_id is None:
            raise GuestError("补偿申请缺少关联订单，无法核实金额")
        wanted = {int(x) for x in evidence_ids if str(x).strip()}
        if not wanted:
            raise GuestError("请至少勾选一条证据进行核实")
        moment = self.state.to_datetime().isoformat(timespec="seconds")
        matched = 0
        for row in complaint.evidences:
            if row.id in wanted:
                row.verified = True
                row.verified_by = actor
                row.verified_at = moment
                row.note = str(note or "")[:200]
                matched += 1
        if not matched:
            raise NotFoundError("所选证据不存在于本工单")
        if complaint.wants_refund and complaint.amount > self._order_cap(complaint):
            raise GuestError("申请金额超过订单快照金额，请核实后调整")
        complaint.state = ComplaintState.EVIDENCE_OK
        complaint.reviewer = actor
        complaint.reviewed_at = moment
        complaint.version += 1
        complaint.updated_at = self.state.to_datetime()
        self._record(complaint, "review_evidence", actor=actor,
                     detail={"verified": matched})
        self.save_complaint(complaint)
        self.state.emit("complaint", {
            "op": "evidence_ok", "complaint_no": complaint.complaint_no,
            "guest_key": complaint.guest_key, "state": complaint.state,
            "message": "证据已核实，等待管理人员终审"})
        self.state.audit.record(actor=actor, role="lead",
                                action="guest.evidence_review",
                                target=complaint.complaint_no,
                                after={"verified": matched,
                                       "amount": complaint.amount})
        return self._view(complaint)

    def _order_cap(self, complaint: Complaint) -> float:
        """补偿上限 = 提交时落盘的下单价格快照。

        绝不读 `state.orders`：订单只写库、启动不回读，服务一重启运行时订单
        就是空的。旧实现查不到时兜底 0.0，于是每笔带补偿的工单（无论选哪个
        方案）都被判「金额超过快照」，管理员完全无法处理。

        快照随工单持久化（见 `order_snapshot_amount`），重启/退菜都不受影响。
        旧工单缺该字段时 `from_dict` 已用 amount 回填；极端情况下（手工改坏
        journal）两值都缺失，退回快照字段而非又拿运行时订单赌运气。
        """
        cap = float(getattr(complaint, "order_snapshot_amount", 0) or 0)
        if cap > 0:
            return cap
        # 兜底：老数据两值都无，用 amount；仍无则 0（此时确实无从核算）
        return float(getattr(complaint, "amount", 0) or 0)

    def approve(self, complaint_id: int, *, actor: str, role: str,
                expected_version: int | None = None,
                method: str = "redo", note: str = "") -> dict[str, Any]:
        """经理终审（仅 manager）。**双人复核 + 证据前置 + 乐观锁**。

        生成的是补偿凭证，不是退款流水——项目没有支付，见模块头资金边界。
        """
        if role != "manager":
            raise GuestError("补偿终审只有管理人员（manager）可以执行")
        complaint = self.complaint(complaint_id)
        self._guard_version(complaint, expected_version)
        if complaint.state != ComplaintState.EVIDENCE_OK:
            raise InvalidTransition(
                f"当前 {complaint.state}，必须先由组长核实证据才能终审")
        if DUAL_REVIEW and complaint.reviewer and complaint.reviewer == actor:
            raise GuestError("双人复核：核实人与终审人不能是同一人")
        if complaint.verified_count < 1:
            raise GuestError("至少一条证据需经核实")
        if method not in COMPENSATIONS:
            raise GuestError(f"补偿方式只能是 {' / '.join(COMPENSATIONS)}")
        if complaint.wants_refund and complaint.amount > self._order_cap(complaint):
            raise GuestError("补偿金额超过订单快照金额")

        moment = self.state.to_datetime()
        complaint.state = ComplaintState.APPROVED
        complaint.method = method
        complaint.approver = actor
        complaint.decided_at = moment.isoformat(timespec="seconds")
        complaint.voucher_no = f"V{moment.strftime('%m%d')}-{complaint.id:03d}"
        complaint.version += 1
        complaint.updated_at = moment
        self._record(complaint, "approve", actor=actor,
                     detail={"method": method, "note": str(note)[:200],
                             "voucher_no": complaint.voucher_no})
        self.save_complaint(complaint)
        self.state.emit("complaint", {
            "op": "approved", "complaint_no": complaint.complaint_no,
            "guest_key": complaint.guest_key, "state": complaint.state,
            "message": f"补偿申请已批准，凭证 {complaint.voucher_no}（演示环境不发生实际退款）"})
        self.state.audit.record(actor=actor, role=role,
                                action="guest.refund_approve",
                                target=complaint.complaint_no,
                                after={"voucher_no": complaint.voucher_no,
                                       "method": method,
                                       "amount": complaint.amount,
                                       "reviewer": complaint.reviewer})
        return self._view(complaint)

    def resolve(self, complaint_id: int, *, actor: str, role: str,
                method: str = "redo", note: str = "",
                evidence_ids: list[Any] | None = None,
                expected_version: int | None = None) -> dict[str, Any]:
        """管理员单人直通：核实证据 + 终审一步完成（无需等组长/另一人）。

        可选 `evidence_ids` 指定核实哪些证据；不传则核实全部已有证据。
        若无任何证据且工单关联了订单，自动补一条 order_item 快照证据并核实，
        保证「管理员能直接处理」不会卡在「至少一条证据」的硬校验上。
        选中补偿方案仍受资金边界约束——生成凭证，不发生实际退款。
        """
        if role != "manager":
            raise GuestError("单人直通处理只有管理人员（manager）可以执行")
        complaint = self.complaint(complaint_id)
        self._guard_version(complaint, expected_version)
        if complaint.terminal:
            raise InvalidTransition(f"工单已结案（{complaint.state}），不能重复处理")
        if method not in COMPENSATIONS:
            raise GuestError(f"补偿方式只能是 {' / '.join(COMPENSATIONS)}")
        if complaint.wants_refund and complaint.amount > self._order_cap(complaint):
            raise GuestError("补偿金额超过订单快照金额")

        moment_dt = self.state.to_datetime()
        moment = moment_dt.isoformat(timespec="seconds")
        # 1) 选证据：显式指定则只核这些；否则核全部；没有则按订单快照补一条
        wanted = {int(x) for x in (evidence_ids or [])
                  if str(x).strip()} if evidence_ids is not None else None
        matched = 0
        for row in complaint.evidences:
            if wanted is None or row.id in wanted:
                row.verified = True
                row.verified_by = actor
                row.verified_at = moment
                row.note = str(note or "")[:200]
                matched += 1
        if matched == 0 and complaint.evidences:
            raise NotFoundError("所选证据不存在于本工单")
        if not complaint.evidences and complaint.order_id is not None:
            order = None
            try:
                order = self.state.orders.order(complaint.order_id)
            except Exception:  # noqa: BLE001 - 重启后运行时订单为空，属预期
                log.info("单人直通：运行时订单不可读，改用客诉自带快照补证")
            if order is not None:
                snapshot = "、".join(
                    f"{it.dish_name}×{it.qty}" for it in order.items[:5]
                ) or order.order_no
                content = f"订单 {order.order_no} 快照：{snapshot}"
            else:
                # 用随工单落盘的快照兜底，保证重启后管理员依然能直接处理，
                # 不因「运行时订单被清空」卡在「没有可核实的证据」上。
                order_no = str((complaint.ai_suggestion or {}).get("handling", {})
                               .get("order_no") or "")
                content = (f"订单 {order_no or '#' + str(complaint.order_id)} "
                           f"下单快照：¥{complaint.order_snapshot_amount:.2f}"
                           f"（服务重启后按工单快照恢复）")
            complaint.evidences.append(Evidence(
                id=self._next_evidence, kind="order_item",
                content=content,
                url="", verified=True, verified_by=actor,
                verified_at=moment, note="管理员单人直通自动关联",
            ))
            self._next_evidence += 1
            matched = 1
        if complaint.verified_count < 1:
            raise GuestError("工单没有可核实的证据，请先补充证据后再处理")

        # 2) 核实落轨迹
        complaint.reviewer = actor
        complaint.reviewed_at = moment
        self._record(complaint, "review_evidence", actor=actor,
                     detail={"verified": matched, "via": "resolve"})
        # 3) 终审出凭证
        complaint.state = ComplaintState.APPROVED
        complaint.method = method
        complaint.approver = actor
        complaint.decided_at = moment
        complaint.voucher_no = f"V{moment_dt.strftime('%m%d')}-{complaint.id:03d}"
        complaint.version += 1
        complaint.updated_at = moment_dt
        self._record(complaint, "resolve", actor=actor,
                     detail={"method": method, "note": str(note)[:200],
                             "voucher_no": complaint.voucher_no,
                             "mode": "single_step"})
        self.save_complaint(complaint)
        self.state.emit("complaint", {
            "op": "approved", "complaint_no": complaint.complaint_no,
            "guest_key": complaint.guest_key, "state": complaint.state,
            "message": (f"管理员已直接处理，凭证 {complaint.voucher_no}"
                        f"（演示环境不发生实际退款）")})
        self.state.audit.record(actor=actor, role=role,
                                action="guest.resolve",
                                target=complaint.complaint_no,
                                after={"voucher_no": complaint.voucher_no,
                                       "method": method, "amount": complaint.amount,
                                       "verified_count": complaint.verified_count})
        return self._view(complaint)

    def reject(self, complaint_id: int, *, actor: str, role: str,
               reason: str, expected_version: int | None = None) -> dict[str, Any]:
        """驳回（lead/manager）。顾客可补证后重新提交，approved 不可驳回。"""
        complaint = self.complaint(complaint_id)
        self._guard_version(complaint, expected_version)
        if complaint.terminal:
            raise InvalidTransition("已结案工单不能驳回")
        if not str(reason).strip():
            raise GuestError("驳回必须填写原因")
        complaint.state = ComplaintState.REJECTED
        complaint.reject_reason = str(reason).strip()[:200]
        complaint.approver = actor
        complaint.decided_at = self.state.to_datetime().isoformat(timespec="seconds")
        complaint.version += 1
        complaint.updated_at = self.state.to_datetime()
        self._record(complaint, "reject", actor=actor,
                     detail={"reason": complaint.reject_reason, "role": role})
        self.save_complaint(complaint)
        self.state.emit("complaint", {
            "op": "rejected", "complaint_no": complaint.complaint_no,
            "guest_key": complaint.guest_key, "state": complaint.state,
            "message": f"工单已驳回：{complaint.reject_reason}（可补充证据后重新提交）"})
        return self._view(complaint)

    def supplement(self, complaint_id: int, *, token: str, guest_key: str,
                   evidences: list[dict[str, Any]],
                   expected_version: int | None = None) -> dict[str, Any]:
        """顾客补证重开。只从 REJECTED 回到 SUBMITTED，已批准是终态。"""
        self.state.customer.require_guest(token, guest_key)
        complaint = self.complaint(complaint_id)
        if complaint.guest_key != str(guest_key):
            raise NotFoundError("工单不存在或不属于当前顾客")
        self._guard_version(complaint, expected_version)
        if complaint.state != ComplaintState.REJECTED:
            raise InvalidTransition("只有被驳回的工单可以补充证据")
        rows = self._build_evidences(evidences)
        if not rows:
            raise GuestError("请至少补充一条证据")
        complaint.evidences.extend(rows)
        complaint.state = ComplaintState.SUBMITTED
        complaint.reject_reason = ""
        complaint.reviewer = ""
        complaint.version += 1
        complaint.updated_at = self.state.to_datetime()
        self._record(complaint, "supplement", actor="customer",
                     detail={"added": len(rows)})
        self.save_complaint(complaint)
        return self._view(complaint)

    # ------------------------------------------------------------ 视图
    def _view(self, complaint: Complaint) -> dict[str, Any]:
        row = complaint.to_dict()
        row["verified_count"] = complaint.verified_count
        row["needs_evidence"] = complaint.state == ComplaintState.SUBMITTED
        row["needs_approval"] = complaint.state == ComplaintState.EVIDENCE_OK
        row["can_supplement"] = complaint.state == ComplaintState.REJECTED
        row["compensation_note"] = (
            "演示环境无支付链路，审批通过仅生成补偿凭证，不发生实际退款"
            if complaint.wants_refund else "本工单不涉及补偿")

        # 处理决策：提交时的快照 + 随时间推进的 SLA 实时刷新（不用重调 AI）
        handling = dict((complaint.ai_suggestion or {}).get("handling") or {})
        if handling and handling.get("stage") in (ComplaintState.SUBMITTED,
                                                   ComplaintState.EVIDENCE_OK):
            tzinfo = complaint.updated_at.tzinfo if complaint.updated_at else None
            elapsed = (datetime.now(tzinfo) - complaint.updated_at).total_seconds()
            handling["overdue"] = elapsed > SLA_SEC
            handling["overdue_min"] = max(0, int(elapsed // 60) - SLA_SEC // 60)
            handling["sla_remain_min"] = max(0, int((SLA_SEC - elapsed) // 60))
            handling["urgent"] = urgent_actions(handling)
        row["handling"] = handling
        row["stage"] = handling.get("stage", complaint.state)
        row["stage_label"] = handling.get("stage_label", "")
        row["blocker"] = handling.get("blocker", "")
        row["blocker_owner"] = handling.get("blocker_owner", "")
        row["overdue"] = bool(handling.get("overdue"))
        row["actions"] = handling.get("actions") or []
        row["urgent"] = handling.get("urgent") or []
        row["ai_reply"] = str((complaint.ai_suggestion or {}).get("reply") or "")
        return row

    def complaint_view(self, *, guest_key: str | None = None,
                       state: str | None = None) -> dict[str, Any]:
        rows = (self.complaints_of(guest_key) if guest_key
                else self.all_complaints())
        if state:
            rows = tuple(c for c in rows if c.state == state)
        return {"total": len(rows), "pending": self.pending_count,
                "overdue": self.overdue(),
                "complaints": [self._view(c) for c in rows]}

    def profile_view(self, guest_key: str, *, refresh: bool = False) -> dict[str, Any]:
        profile = self.profile(guest_key)
        return {"profile": profile.to_dict(),
                "portrait": self.portrait(guest_key, refresh=refresh),
                "complaints": [self._view(c) for c in self.complaints_of(guest_key)],
                "privacy_note": "本机匿名档案，仅聚合订单统计，不含姓名/手机号/openid"}
