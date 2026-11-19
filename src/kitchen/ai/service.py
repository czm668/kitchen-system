# -*- coding: utf-8 -*-
"""AiService：多融合 AI 的统一编排与只读审计聚合。

## 它做什么 / 不做什么

做：
- `advise(scenario, facts)`：按 `specs.SCENARIOS` 里的场景规格调
  `guest/llm.py::run_guarded`，产出统一 `AiResult` + 逐次审计；
- `overview()`：只读聚合**七个 AI 场景**（3 新增 + 4 既有）的调用模式、降级次数、
  提示词版本，配成答辩能一屏讲清的"多融合 AI 面板"；
- `runs()`：返回本中枢的场景调用审计（对齐 `004_llm_reports.sql::llm_runs` 列）。

不做：**不持有业务状态、不写业务表、不改任何规则判定**。派单/关阀/门禁/退款
全部由既有规则与权限层裁决，`AiResult` 里也没有 approve/decision/amount 字段。
它是"建议层"，挂在业务链旁边，摘掉它整条业务链照常运行。

## 为什么既有四场景只读、不重写

guest 的画像/客服、finance 日报、compliance 日报已经各自落了审计
（`guest._llm_runs` / `finance._ai_runs` / `compliance._runs`）。重跑它们会造成
重复调用、重复扣 token，还可能在日报场景改写当天快照。所以 `overview()` 只做
**只读汇总**，各域怎么算还照旧——这也是"不破坏已验证业务链"的具体落实。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Mapping

from ..guest.llm import AiResult, llm_run_row, run_guarded
from .specs import AI_SLOTS, SCENARIOS, SCENARIO_TITLES

log = logging.getLogger("kitchen.ai")

__all__ = ["AiService", "advise", "ai_overview", "ai_runs"]

#: overview 覆盖的既有场景（只读各域已有审计，不重新调用模型）。
_LEGACY_SCENARIOS = ("profile", "support", "finance_daily", "compliance_daily")


class AiService:
    """统一 AI 建议层。无业务状态，仅持有 provider 与定长审计缓冲。"""

    def __init__(self, state, *, provider=None) -> None:
        self.state = state
        # provider 由 AppState 注入（与 guest/finance/compliance 同一个实例，
        # 配 key 后七个场景一起切 live）；没给就复用 state 上的，再兜底现造。
        self.provider = provider if provider is not None else getattr(
            state, "llm_provider", None)
        if self.provider is None:
            from ..guest.llm import make_provider
            self.provider = make_provider()
        self._runs: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 建议入口
    def advise(self, scenario: str, facts: Mapping[str, Any]) -> dict[str, Any]:
        """按场景规格出建议。返回 `AiResult.to_dict()` + 展示元数据。

        未知 scenario 抛 `ValueError`——不静默降级成客服回答，那会让调用方
        以为拿到了 anomaly 结论，实际是客诉模板，答非所问还难排查。
        """
        spec = SCENARIOS.get(str(scenario))
        if spec is None:
            raise ValueError(f"未知 AI 场景：{scenario!r}；"
                             f"可用：{', '.join(SCENARIOS)}")
        result: AiResult = run_guarded(self.provider, spec.scenario, facts, spec)
        status = "degraded" if result.degraded_reason else "ok"
        row = llm_run_row(result, status=status)
        with self._lock:
            self._runs.append({
                **row,
                "ts": (self.state.to_datetime().isoformat(timespec="seconds")
                       if self.state is not None else ""),
                "title": SCENARIO_TITLES.get(spec.scenario, spec.scenario),
            })
            del self._runs[:-200]  # 定长，长跑内存有界（口径同 guest 200 条）
        return {
            **result.to_dict(),
            "title": SCENARIO_TITLES.get(spec.scenario, spec.scenario),
            "slot_boundary": AI_SLOTS.get(spec.scenario, "仅出建议，不改业务状态"),
            "advice_only": True,
        }

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        cap = max(1, min(200, int(limit)))
        with self._lock:
            return [dict(row) for row in self._runs[-cap:]]

    # ------------------------------------------------------------ 只读聚合
    def _legacy_runs(self) -> dict[str, list[dict[str, Any]]]:
        """从各域已有审计缓冲只读取值，绝不重新触发模型调用。"""
        out: dict[str, list[dict[str, Any]]] = {}
        state = self.state
        if state is None:
            return out
        sources = (
            ("profile", getattr(state, "guest", None), "llm_runs"),
            ("support", getattr(state, "guest", None), "llm_runs"),
            ("finance_daily", getattr(state, "finance", None), "ai_runs"),
            ("compliance_daily", getattr(state, "compliance", None), "runs"),
        )
        for scenario, svc, method in sources:
            if svc is None or not hasattr(svc, method):
                out[scenario] = []
                continue
            try:
                rows = list(getattr(svc, method)() or [])
            except Exception:  # pragma: no cover - 审计聚合失败不该拖垮面板
                log.warning("读取 %s 审计失败", scenario, exc_info=True)
                rows = []
            if scenario in ("profile", "support"):
                rows = [row for row in rows if row.get("scenario") == scenario]
            out[scenario] = rows
        return out

    def overview(self) -> dict[str, Any]:
        """一屏多融合 AI 面板：七个场景的模式、调用数、降级数、边界口径。"""
        legacy = self._legacy_runs()
        all_by_scenario: dict[str, list[dict[str, Any]]] = {
            key: list(rows) for key, rows in legacy.items()}
        for row in self._runs:
            all_by_scenario.setdefault(str(row.get("scenario")), []).append(row)

        scenarios_out: list[dict[str, Any]] = []
        total_calls = total_live = total_rule = total_degraded = 0
        for name in (*SCENARIOS, *_LEGACY_SCENARIOS):
            rows = all_by_scenario.get(name, [])
            live = sum(1 for r in rows if r.get("mode") == "live"
                       or r.get("source") == "llm")
            degraded = sum(1 for r in rows
                           if r.get("status") == "degraded" or r.get("degraded_reason"))
            rule = len(rows) - live
            newest = rows[-1] if rows else {}
            scenarios_out.append({
                "scenario": name,
                "title": SCENARIO_TITLES.get(name, name),
                "slot_boundary": AI_SLOTS.get(name, "仅出建议，不改业务状态"),
                "calls": len(rows), "live": max(0, live),
                "rule": max(0, rule), "degraded": degraded,
                "mode": getattr(newest, "get", lambda *_: "")("mode", "rule"),
                "prompt_version": newest.get("prompt_version", ""),
                "is_new": name in SCENARIOS,
            })
            total_calls += len(rows)
            total_live += max(0, live)
            total_rule += max(0, rule)
            total_degraded += degraded

        return {
            "provider": {
                "mode": getattr(self.provider, "mode", "rule"),
                "model": getattr(self.provider, "model", "rule-engine-v1"),
            },
            "scenarios": scenarios_out,
            "totals": {
                "scenario_count": len(scenarios_out),
                "calls": total_calls, "live": total_live,
                "rule": total_rule, "degraded": total_degraded,
            },
            "guardrails": {
                "advice_only": True,
                "offline_fallback": True,
                "whitelist_output": True,
                "per_call_audit": True,
                "privacy_filter": True,
            },
            "note": ("既有四场景只读各域已有审计，不重复调用模型；"
                     "新增三场景由本中枢统一护栏与审计。AI 只出建议，"
                     "派单/关阀/门禁/退款仍由规则与权限层裁决。"),
        }


# ---------------------------------------------------------------- 模块级便捷函数
def advise(state, scenario: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """给路由用的薄封装：从 state 取（或建）AiService 再调 advise。"""
    return _service(state).advise(scenario, facts)


def ai_overview(state) -> dict[str, Any]:
    return _service(state).overview()


def ai_runs(state, limit: int = 50) -> dict[str, Any]:
    svc = _service(state)
    runs = svc.runs(limit)
    return {"total": len(runs), "runs": runs,
            "provider": {"mode": getattr(svc.provider, "mode", "rule"),
                         "model": getattr(svc.provider, "model", "rule-engine-v1")}}


def _service(state) -> AiService:
    """挂在 state 上复用，避免每个请求都新建一个实例丢审计。"""
    existing = getattr(state, "ai", None)
    if isinstance(existing, AiService):
        return existing
    svc = AiService(state)
    try:
        state.ai = svc
    except Exception:  # pragma: no cover - 只读对象没法挂属性时也能工作
        pass
    return svc
