# -*- coding: utf-8 -*-
"""食安合规凭证域（A13）——把 17 条主线的事实变成「能交给监管的东西」。

## 为什么这个域是「必须有」而不是锦上添花

题目是《…后厨智能调度与**食安合规**管理系统》，但核查下来合规侧只有 4 个
**查询页签**（晨检 / 健康证 / 资质 / 批次），全局零导出、零日报、零一页纸追溯。
也就是说系统「能查」，但「交不出凭证」：答辩老师问「合规体现在哪」，只能指屏幕。

本域不新增业务动作，**只读已有事实源**，把分散在 8 个地方的数据聚合成三样交付物：

1. `checklist()`  每日合规检查表（6 项，逐项给事实数 / 达标数 / 缺口）
2. `dossier()`    批次追溯一页纸（原料 → 留样 → 消毒 → 出库门禁 → 工单 → 审计）
3. `report()`     合规日报（复用 `004::reports` 列形状 + A11 的 LLM provider）

## 六条硬口径（写错就是伪造合格）

- **空数据不是 100% 达标**：`checked == 0` 一律标 `na`，不进达标率分母。
  演示环境经常没跑消毒/留样，把这些算「通过」就是假合格证。
- **只信域层结论，不重算判据**：消毒达标认 `DisinfectionRecord.state == 'passed'`，
  留样达标认 `weight_g >= 125` 且已被 `RetentionService` 落库。
  在这里自己按温度累加一遍，等于造出第二套判定逻辑，两套迟早打架。
- **留样清单按当日生产批次**（`produced_at` 的日期），不按 `state='produced'`——
  后者会把昨天的活儿天天重列（#14 踩过的坑）。
- **只有达标的留样才写台账**：不达标的只进 `process_events`，不污染合格率。
- **健康证临期是提醒不是失败**：30 天内到期标 `warn`，过期才 `fail`。
- **门禁是「当前状态」不是历史累计**：`forbid_outbound` 被复消清零后，
  历史记录里查不到「曾经禁过」，所以这一项只能表达「当前是否还有未放行批次」。

## 证据链而非评分

刻意**不做加权总分、不做合规分数**：6 项量纲不同（人数 / 证数 / 批次数 / 会话数），
加权就是拍脑袋。输出只给逐项「事实数 / 达标数 / 缺口明细」和整体
`pass / warn / fail`，让看的人自己判断，论文里也能逐项复现。
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from ..domain import CST, NotFoundError
from ..process.journal import append_journal

__all__ = ["ComplianceService"]

#: #14 硬口径：留样达标门限（含 125）
MIN_SAMPLE_G = 125.0
#: #9 法定提示窗口
CERT_WARN_DAYS = 30

_PROMPT_VERSION = "compliance-daily-v1"
_RULE_MODEL = "rule-engine-v1"
_SCENARIO = "compliance_daily"

#: 检查项元数据。顺序即检查表 / CSV 的行序。
CHECK_META: tuple[tuple[str, str, str], ...] = (
    ("morning", "晨检准入", "#8 当日全员晨检，待复核与未提交均不算放行"),
    ("certificate", "健康证有效期", "#9 在册健康证当日有效，30 天内到期仅提醒"),
    ("retention", "留样达标", "#14 当日生产批次须有 ≥125g 的达标留样记录"),
    ("disinfection", "消毒达标", "#15 已结束会话须达标，运行中不计入判定"),
    ("ticket", "工单处置闭环", "传感器故障 / 复消 / 规则告警工单须关闭"),
    ("outbound", "出库门禁", "#15 消毒不达标批次当前不得处于禁止出库状态"),
)


def _day_of(value: Any) -> str:
    """把 datetime / date / 各种字符串统一成 YYYY-MM-DD。解析不了返回空串。

    seed 里 `produced_at` 是相对时刻，仓储里可能是 datetime 也可能是
    `'2026-11-15 11:30:00'`；不做容错会在演示当天直接抛异常。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(CST).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if len(text) >= 10 and text[4:5] == "-" else ""


def _rate(passed: int, checked: int) -> float | None:
    return round(passed / checked, 4) if checked > 0 else None


class ComplianceService:
    """只读聚合 + 快照持久化。不持有任何业务状态机。"""

    def __init__(self, state, *, provider=None, journal_path: Path | None = None):
        self.state = state
        self.provider = provider
        self._journal = journal_path
        self._checks: dict[str, dict[str, Any]] = {}   # day → 检查表快照
        self._reports: dict[str, dict[str, Any]] = {}  # day → 日报快照
        self._runs: list[dict[str, Any]] = []
        self._applied = 0
        self._skipped = 0
        self._replay()

    # ------------------------------------------------------------ 持久化
    @property
    def journal_stats(self) -> dict[str, Any]:
        return {"path": str(self._journal) if self._journal else None,
                "applied": self._applied, "skipped": self._skipped,
                "checks": len(self._checks), "reports": len(self._reports)}

    def _write(self, record: dict[str, Any]) -> None:
        append_journal(self._journal, record)

    def _replay(self) -> None:
        """逐行重放。装配期**不碰 self.state**（A12 踩过的启动崩溃坑）。"""
        if self._journal is None or not self._journal.exists():
            return
        try:
            lines = self._journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                op = row.get("op")
                if op == "checklist" and isinstance(row.get("check"), dict):
                    self._checks[str(row["check"]["date"])] = row["check"]
                elif op == "report" and isinstance(row.get("report"), dict):
                    self._reports[str(row["report"]["rdate"])] = row["report"]
                elif op == "run" and isinstance(row.get("run"), dict):
                    self._runs.append(row["run"])
                else:
                    self._skipped += 1
                    continue
                self._applied += 1
            except (ValueError, KeyError, TypeError, OSError):
                self._skipped += 1
        del self._runs[:-200]

    def today(self) -> str:
        return self.state.to_datetime().astimezone(CST).date().isoformat()

    @staticmethod
    def _norm_day(day: str | None, fallback: str) -> str:
        text = str(day or "").strip() or fallback
        try:
            date.fromisoformat(text[:10])
        except (TypeError, ValueError):
            raise ValueError("日期须为 YYYY-MM-DD") from None
        return text[:10]

    # ------------------------------------------------------------ 检查表
    def checklist(self, day: str | None = None, *, persist: bool = True) -> dict[str, Any]:
        """生成每日合规检查表。同日重复调用结果一致，快照按日覆盖。"""
        target = self._norm_day(day, self.today())
        items = [
            self._morning(target), self._certificates(target),
            self._retention(target), self._disinfection(target),
            self._tickets(target), self._outbound(target),
        ]
        states = [row["state"] for row in items]
        if "fail" in states:
            overall = "fail"
        elif all(state == "na" for state in states):
            overall = "no_data"
        elif "warn" in states or "na" in states:
            overall = "warn"
        else:
            overall = "pass"
        labels = {"pass": "全部检查项达标", "warn": "存在提醒或未采集到数据",
                  "fail": "存在未达标项，需处置", "no_data": "当日无合规事实数据"}
        result = {
            "date": target,
            "generated_at": self.state.to_datetime().isoformat(timespec="seconds"),
            "overall": overall, "overall_label": labels[overall],
            "summary": {
                "total": len(items),
                "passed": sum(1 for s in states if s == "pass"),
                "warn": sum(1 for s in states if s == "warn"),
                "failed": sum(1 for s in states if s == "fail"),
                "no_data": sum(1 for s in states if s == "na"),
            },
            "items": items,
            "discipline_note": (
                "未采集到事实数据的检查项标记为「无数据」，不计入达标率，也不视为通过；"
                "本表只汇总域层已有结论，不重新判定阈值，不构成法定检验报告。"),
            "journal": self.journal_stats,
        }
        if persist and self._checks.get(target) != result:
            self._checks[target] = result
            self._write({"op": "checklist", "check": result})
        return result

    @staticmethod
    def _item(key: str, label: str, basis: str, checked: int, passed: int,
              gaps: list[str], *, running: int = 0, warn: int = 0) -> dict[str, Any]:
        """统一封装：空数据 = na，缺口 = fail，仅提醒 = warn。"""
        if checked == 0:
            state = "na"
        elif passed < checked:
            state = "fail"
        elif warn:
            state = "warn"
        else:
            state = "pass"
        return {
            "key": key, "label": label, "state": state,
            "checked": checked, "passed": passed, "rate": _rate(passed, checked),
            "basis": basis, "gaps": gaps[:8],
            "gap_total": len(gaps), "running": running, "warn_count": warn,
        }

    def _morning(self, day: str) -> dict[str, Any]:
        _, label, basis = CHECK_META[0]
        today = date.fromisoformat(day)
        checked = passed = 0
        gaps: list[str] = []
        for person in self.state.staff_repo.all_staff():
            checked += 1
            check = self.state.staff_repo.morning_check(person.id, today)
            if check is None:
                gaps.append(f"{person.name}：未提交晨检")
                continue
            if getattr(check, "locked", False):
                gaps.append(f"{person.name}：待组长复核")
                continue
            # admitted（复核放行）与未锁定（无需复核即通过）都算放行
            passed += 1
        return self._item("morning", label, basis, checked, passed, gaps)

    def _certificates(self, day: str) -> dict[str, Any]:
        _, label, basis = CHECK_META[1]
        today = date.fromisoformat(day)
        checked = passed = warn = 0
        gaps: list[str] = []
        for person in self.state.staff_repo.all_staff():
            for cert in self.state.staff_repo.certificates(person.id):
                checked += 1
                if not cert.is_valid_on(today):
                    gaps.append(f"{person.name}：健康证 {cert.cert_no} 已失效")
                    continue
                passed += 1
                left = cert.expires_in(today)
                if left is not None and 0 <= left <= CERT_WARN_DAYS:
                    warn += 1
                    gaps.append(f"{person.name}：健康证 {left} 天后到期（提醒）")
        return self._item("certificate", label, basis, checked, passed, gaps, warn=warn)

    def _retention(self, day: str) -> dict[str, Any]:
        _, label, basis = CHECK_META[2]
        batches = []
        for batch_id in self.state.repo.batch_ids():
            row = self.state.repo.batch(batch_id)
            if _day_of(row.get("produced_at")) == day:
                batches.append(row)
        samples = self.state.retention.samples
        by_batch: dict[int, list[Any]] = {}
        for sample in samples:
            by_batch.setdefault(int(sample.dish_batch_id), []).append(sample)
        checked = len(batches)
        passed = 0
        gaps: list[str] = []
        for row in batches:
            found = [s for s in by_batch.get(int(row["id"]), ())
                     if float(s.weight_g) >= MIN_SAMPLE_G]
            if found:
                passed += 1
            else:
                gaps.append(f"批次 {row.get('batch_no') or row['id']}：无 ≥125g 达标留样")
        return self._item("retention", label, basis, checked, passed, gaps)

    def _disinfection(self, day: str) -> dict[str, Any]:
        _, label, basis = CHECK_META[3]
        records = [r for r in self.state.disinfection.records
                   if _day_of(r.start_ts) == day]
        decided = [r for r in records if r.state in ("passed", "failed")]
        running = len(records) - len(decided)
        passed = sum(1 for r in decided if r.state == "passed")
        gaps = [f"{r.session_uid}：{r.describe()}" for r in decided
                if r.state == "failed"]
        return self._item("disinfection", label, basis, len(decided), passed,
                          gaps, running=running)

    def _tickets(self, day: str) -> dict[str, Any]:
        _, label, basis = CHECK_META[4]
        rows: list[tuple[str, str]] = []
        for ticket in self.state.health.tickets():
            payload = ticket.to_payload()
            rows.append((str(payload.get("state") or "open"),
                         str(payload.get("message") or "传感器故障工单")))
        for record in self.state.disinfection.records:
            if record.state == "failed":
                rows.append(("open", f"复消待处理：{record.describe()}"))
        for rework in self.state.disinfection.tickets:
            rows.append((str(getattr(rework, "state", "open")),
                         rework.describe()))
        closed = sum(1 for state, _ in rows if state == "closed")
        gaps = [f"未关闭：{msg[:60]}" for state, msg in rows if state != "closed"]
        return self._item("ticket", label, basis, len(rows), closed, gaps)

    def _outbound(self, day: str) -> dict[str, Any]:
        _, label, basis = CHECK_META[5]
        records = list(self.state.disinfection.records)
        blocked = [r for r in records if r.forbid_outbound]
        gaps = [f"{r.session_uid}：消毒未达标，该批餐具仍禁止出库" for r in blocked]
        # checked 取全部会话数：每项代表一次门禁校验；没有会话就是「无数据」
        return self._item("outbound", label, basis, len(records),
                          len(records) - len(blocked), gaps)

    # ------------------------------------------------------------ 一页纸追溯
    def dossier(self, batch_id: Any) -> dict[str, Any]:
        """批次追溯一页纸：把分散的合规事实拼成一条可举证链。

        任一环节取不到数据都进 `missing`，**不静默省略**——省略会让一页纸
        看起来「完整」，实际是漏了证据，这正是合规交付物最忌讳的。
        """
        try:
            bid = int(batch_id)
            batch = self.state.repo.batch(bid)
        except (TypeError, ValueError, NotFoundError, KeyError):
            raise NotFoundError(f"批次 {batch_id} 不存在") from None

        missing: list[str] = []
        supplier: dict[str, Any] | None = None
        sid = batch.get("supplier_id")
        if sid is not None:
            try:
                supplier = dict(self.state.repo.supplier(int(sid)))
            except (NotFoundError, KeyError, TypeError, ValueError):
                missing.append("供应商档案")
        else:
            missing.append("供应商档案")

        samples = [s for s in self.state.retention.samples
                   if int(s.dish_batch_id) == bid]
        if not samples:
            missing.append("留样台账")
        disinfections = [r for r in self.state.disinfection.records
                         if int(r.batch_id or 0) == bid]
        if not disinfections:
            missing.append("消毒记录")

        codes: list[str] = []
        outbound: list[dict[str, Any]] = []
        forbid = any(r.forbid_outbound for r in disinfections)
        try:
            codes = list(self.state.trace.codes_of(bid))
        except (NotFoundError, KeyError, TypeError, ValueError):
            missing.append("溯源码")
        if not codes:
            missing.append("溯源码")
        for code in codes:
            try:
                view = self.state.trace.view(code)
                blockers = self.state.trace.outbound_blockers(
                    code, forbid_outbound=forbid)
                outbound.append({
                    "code": code,
                    "blockers": list(blockers),
                    "supplier": getattr(view, "supplier", None),
                    "expire_at": (view.expire_at.isoformat(timespec="seconds")
                                  if getattr(view, "expire_at", None) else ""),
                    "blocked": bool(blockers),
                })
            except (NotFoundError, KeyError, TypeError, ValueError):
                missing.append(f"溯源码 {code} 的追溯视图")
        if not outbound:
            missing.append("出库门禁校验")

        batch_no = str(batch.get("batch_no") or "")
        audit_rows = []
        for entry in self.state.audit.filter(limit=200):
            if batch_no and batch_no in str(getattr(entry, "target", "")):
                audit_rows.append(entry.to_payload())
        if not audit_rows:
            missing.append("操作审计留痕")

        return {
            "batch_id": bid, "batch": dict(batch), "supplier": supplier,
            "trace_codes": codes, "outbound": outbound,
            "forbid_outbound": forbid,
            "retention": [{
                "id": s.id, "weight_g": float(s.weight_g), "state": s.state,
                "sampled_at": _iso(s.sampled_at),
                "destroy_due_at": _iso(s.destroy_due_at),
                "destroyed_at": _iso(s.destroyed_at),
                "qualified": float(s.weight_g) >= MIN_SAMPLE_G,
            } for s in samples],
            "disinfection": [{
                "session_uid": r.session_uid, "state": r.state,
                "start_ts": _iso(r.start_ts), "end_ts": _iso(r.end_ts),
                "cumulative_sec": round(float(r.cumulative_sec), 1),
                "peak_temp": float(r.peak_temp),
                "forbid_outbound": bool(r.forbid_outbound),
                "reason": r.reason, "describe": r.describe(),
            } for r in disinfections],
            "audit": audit_rows[-20:], "missing": sorted(set(missing)),
            "evidence_note": (
                "本页由域层既有记录实时聚合，未重新判定任何阈值；"
                "「缺失环节」如实列出，不代表该环节已合规。演示环境为仿真数据，"
                "不能作为法定追溯凭证。"),
        }

    # ------------------------------------------------------------ 合规日报
    def report(self, day: str | None = None) -> dict[str, Any]:
        """合规日报。列形状对齐 `004_llm_reports.sql::reports`，不另造表。

        AI 只写点评文字：检查结论全部由 `checklist()` 确定性算出，模型输出
        不参与判定、不改结论、不写账。喂给模型的只有聚合数字和缺口标签，
        **不含人员姓名、卡号、顾客 key**（隐私护栏）。
        """
        target = self._norm_day(day, self.today())
        check = self.checklist(target, persist=False)
        started = time.monotonic()
        mode, model, degraded, comment = "rule", _RULE_MODEL, "", ""
        if self.provider is not None and getattr(self.provider, "mode", "") == "live":
            try:
                raw = self.provider.complete(_SCENARIO, {
                    "date": target, "overall": check["overall"],
                    "summary": check["summary"],
                    "items": [{"label": row["label"], "state": row["state"],
                               "checked": row["checked"], "passed": row["passed"]}
                              for row in check["items"]],
                })
                text = str((raw or {}).get("summary") or "").strip()
                banned = ("批准", "退款", "已退", "修改结论", "判定合格", "代替检验")
                if len(text) < 8 or any(word in text for word in banned):
                    raise ValueError("模型输出不符合合规点评约束")
                comment, mode, model = text[:400], "live", self.provider.model
            except Exception as exc:  # 在线失败一律降级，不拖垮交付
                degraded = f"{type(exc).__name__}: {str(exc)[:60]}"
                comment = self._rule_comment(check)
        else:
            comment = self._rule_comment(check)
        latency_ms = int((time.monotonic() - started) * 1000)

        lines = [f"# 食安合规日报 {target}", "",
                 f"- 总体结论：{check['overall_label']}", ""]
        for row in check["items"]:
            rate = "无数据" if row["rate"] is None else f"{row['rate'] * 100:.1f}%"
            lines.append(f"- {row['label']}：{row['passed']}/{row['checked']}（{rate}）"
                         f" · {row['state']}")
            for gap in row["gaps"][:3]:
                lines.append(f"    - {gap}")
        lines += ["", "## 合规点评", "", comment, "",
                  f"> {check['discipline_note']}"]
        failed = [r["label"] for r in check["items"] if r["state"] == "fail"]
        row = {
            "scenario": _SCENARIO, "rdate": target,
            "title": f"食安合规日报 {target}",
            "summary": comment[:512], "content_md": "\n".join(lines),
            "risk_level": "high" if failed else ("medium" if check["overall"] == "warn" else "low"),
            "source": "llm" if mode == "live" else "rule",
            "model": model, "prompt_version": _PROMPT_VERSION,
            "latency_ms": latency_ms, "degraded_reason": degraded[:32],
            "created_at": self.state.to_datetime().isoformat(timespec="seconds"),
            "check": check, "failed_items": failed,
        }
        self._reports[target] = row
        self._write({"op": "report", "report": row})
        run = {"scenario": _SCENARIO, "mode": mode,
               "source": row["source"], "model": model,
               "prompt_version": _PROMPT_VERSION,
               "status": "ok" if not degraded else "degraded",
               "attempts": 1, "latency_ms": latency_ms,
               "degraded_reason": degraded[:32], "error_msg": ""}
        self._runs.append(run)
        self._write({"op": "run", "run": run})
        del self._runs[:-200]
        return row

    @staticmethod
    def _rule_comment(check: Mapping[str, Any]) -> str:
        """离线确定性点评：只复述检查表结论，不推断、不编造。"""
        if check["overall"] == "no_data":
            return "当日未采集到合规事实数据，不做达标推断，请先运行演示数据注入。（离线规则点评，非大模型生成）"
        failed = [r["label"] for r in check["items"] if r["state"] == "fail"]
        na = [r["label"] for r in check["items"] if r["state"] == "na"]
        parts = [f"{check['date']} 共 {check['summary']['total']} 项检查，"
                 f"达标 {check['summary']['passed']} 项、未达标 {check['summary']['failed']} 项。"]
        if failed:
            parts.append("未达标项：" + "、".join(failed) + "，建议按缺口明细逐项处置并复核。")
        if na:
            parts.append("无数据项：" + "、".join(na) + "，不计入达标率。")
        if not failed:
            parts.append("已采集的检查项均达标。")
        return "".join(parts) + "（离线规则点评，非大模型生成）"

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._runs[-max(1, min(200, int(limit))):])

    def snapshots(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = [{"date": day, "overall": row["overall"],
                 "summary": row["summary"]}
                for day, row in sorted(self._checks.items(), reverse=True)]
        return rows[:max(1, min(200, int(limit)))]

    # ------------------------------------------------------------ 导出
    def export_csv(self, day: str | None = None) -> dict[str, Any]:
        """每日合规检查表 CSV。UTF-8 BOM 前缀，Excel 双击不乱码。"""
        target = self._norm_day(day, self.today())
        check = self.checklist(target, persist=False)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(["营业日", "检查项", "结论", "应检数", "达标数", "达标率",
                         "判定依据", "缺口明细", "口径说明"])
        state_label = {"pass": "达标", "warn": "提醒", "fail": "未达标", "na": "无数据"}
        for row in check["items"]:
            writer.writerow([
                target, row["label"], state_label.get(row["state"], row["state"]),
                row["checked"], row["passed"],
                "" if row["rate"] is None else f"{row['rate'] * 100:.1f}%",
                row["basis"], " | ".join(row["gaps"]),
                check["discipline_note"],
            ])
        writer.writerow([])
        writer.writerow(["总体结论", check["overall_label"]])
        return {"filename": f"kitchen-compliance-{target}.csv",
                "csv": "\ufeff" + buf.getvalue(), "rows": len(check["items"]),
                "date": target, "note": check["discipline_note"]}


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")
