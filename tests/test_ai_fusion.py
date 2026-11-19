# -*- coding: utf-8 -*-
"""A28 多融合 AI 中枢回归。

钉死五件事：
1. 四个新场景的离线规则产出**能被 validate_payload 严格接受**的形状；
2. 在线失败时按 spec.rule 降级，不返回 500、留 degraded_reason；
3. AiService 只出建议——结果里没有 approve/decision/amount，且 advice_only=True；
4. overview 只读聚合八个场景、不重复调用模型、不写业务表；
5. 建议端点走权限、过滤隐私字段。
"""
from __future__ import annotations

import pytest

from api_support import data_of, force_session, make_server, make_state
from kitchen.ai import AiService
from kitchen.ai.specs import (
    SCENARIOS,
    rule_anomaly_explain,
    rule_compliance_qa,
    rule_dispatch_hint,
    rule_violation_explain,
)
from kitchen.guest.llm import (
    LLMUnavailable,
    validate_payload,
)


class BrokenProvider:
    """永远失败的在线 provider，用来逼出降级路径。"""
    mode = "live"
    model = "broken-model"

    def complete(self, scenario, facts, *, system_prompt=None):
        raise LLMUnavailable("模拟在线不可用")


class EchoProvider:
    """回显一个符合场景白名单的最小 payload，验证自定义 system 被传进去。"""
    mode = "live"
    model = "echo-model"

    def __init__(self):
        self.seen_prompt = None

    def complete(self, scenario, facts, *, system_prompt=None):
        self.seen_prompt = system_prompt
        category = SCENARIOS[scenario].categories[0]
        return {
            "category": category, "sentiment": "calm",
            "summary": "在线模型给出的一句话结论",
            "reply": "建议：请值班人员复核。", "need_human": True,
            "confidence": 0.9, "tags": [],
        }


# ---------------------------------------------------------------- 离线规则 schema
@pytest.mark.parametrize("rule,facts,spec", [
    (rule_anomaly_explain, {"kind": "燃气", "reading": 900, "threshold": 800,
                            "detail": "浓度持续上升", "advice": "通风并复核"},
     "anomaly_explain"),
    (rule_dispatch_hint, {"queue": [1, 2], "idle_stations": [1], "free_staff": [3]},
     "dispatch_hint"),
    (rule_compliance_qa, {"question": "为什么不能出库", "overall": "warn",
                          "items": [{"label": "出库门禁", "state": "fail",
                                     "passed": 0, "checked": 1, "gaps": ["消毒未达标"]}]},
     "compliance_qa"),
    (rule_violation_explain, {"kind": "smoking", "station_id": 3,
                              "evidence": True, "power_cut": True},
     "violation_explain"),
])
def test_offline_rules_are_strictly_valid(rule, facts, spec):
    raw = rule(facts)
    validated = validate_payload(raw, spec, SCENARIOS[spec])
    assert validated["category"] in SCENARIOS[spec].categories
    assert validated["summary"]
    assert validated["confidence"] == pytest.approx(raw["confidence"])


def test_anomaly_rule_never_invents_when_kind_unknown():
    raw = rule_anomaly_explain({"kind": "火星撞地球", "reading": 1, "threshold": 2})
    assert raw["category"] == "其他"
    assert raw["need_human"] is True   # 安全类恒需人工确认


# ---------------------------------------------------------------- 降级与 system 注入
def test_advise_degrades_to_scene_rule_on_failure():
    svc = AiService(None, provider=BrokenProvider())
    out = svc.advise("anomaly_explain", {"kind": "冷链超温", "reading": 9, "threshold": 8})
    # 降级到本场景规则，不是客服模板
    assert "冷链超温" in out["summary"]
    assert out["mode"] == "rule"
    assert out["model"] == "rule-engine-v1"
    assert out["degraded_reason"]
    assert out["prompt_version"] == "anomaly-explain-v1"


def test_advise_passes_custom_system_prompt():
    provider = EchoProvider()
    svc = AiService(None, provider=provider)
    out = svc.advise("dispatch_hint", {"queue": [1]})
    assert provider.seen_prompt == SCENARIOS["dispatch_hint"].system_prompt
    assert out["mode"] == "live"
    assert out["model"] == "echo-model"


# ---------------------------------------------------------------- 只出建议
def test_advice_only_has_no_decision_fields():
    svc = AiService(None, provider=BrokenProvider())
    out = svc.advise("compliance_qa", {"question": "留样", "items": [], "overall": "ok"})
    assert out["advice_only"] is True
    assert out["slot_boundary"] == "不改门禁与检查结论"
    for forbidden in ("approve", "decision", "amount", "approved", "amount_cents"):
        assert forbidden not in out


def test_unknown_scenario_rejected():
    svc = AiService(None, provider=BrokenProvider())
    with pytest.raises(ValueError, match="未知 AI 场景"):
        svc.advise("refund_now", {})


# ---------------------------------------------------------------- overview 只读聚合
def test_overview_aggregates_eight_scenarios_without_new_calls(tmp_path):
    # 用 BrokenProvider：若 overview 触发任何在线调用会直接抛 LLMUnavailable，
    # 测试随即失败——这是对"只读不重跑模型"最强的证明。
    state = make_state(tmp_path)
    state.ai = AiService(state, provider=BrokenProvider())
    state.ai.advise("anomaly_explain", {"kind": "燃气"})  # 走降级，留一条审计
    ov = state.ai.overview()
    assert ov["totals"]["scenario_count"] == 8
    names = [s["scenario"] for s in ov["scenarios"]]
    assert set(SCENARIOS).issubset(set(names))
    assert {"profile", "support", "finance_daily", "compliance_daily"}.issubset(set(names))
    assert ov["guardrails"]["advice_only"] is True
    assert ov["totals"]["calls"] >= 1


# ---------------------------------------------------------------- 路由与权限
def test_routes_require_permission_and_filter_privacy(tmp_path):
    server = make_server(tmp_path)
    manager = force_session("admin", "manager")
    viewer = force_session("watch", "viewer")

    # 面板：观察员可读（ai.read = 全员）
    ov = data_of(server, "GET", "/api/ai/overview", session=viewer)
    assert ov["totals"]["scenario_count"] == 8

    # 建议：观察员被拒
    status, _ = server.router.call(
        "POST", "/api/ai/advise/anomaly",
        data={"kind": "燃气"}, session=viewer)
    assert status in (401, 403)

    # 组长可请求建议，且只出建议
    adv = data_of(server, "POST", "/api/ai/advise/anomaly",
                  session=force_session("lead", "lead"),
                  kind="排烟积热", reading=70, threshold=60,
                  openid="SENSITIVE-OPENID", phone="13800000000")
    assert adv["advice_only"] is True
    assert "排烟积热" in adv["summary"]
    assert "SENSITIVE-OPENID" not in str(adv)
    assert "13800000000" not in str(adv)

    # 审计留痕
    runs = data_of(server, "GET", "/api/ai/runs", session=manager)
    assert runs["total"] >= 1
    assert runs["runs"][-1]["scenario"] == "anomaly_explain"


# ---------------------------------------------------------------- 视觉违规归因（第 8 场景）
def test_violation_rule_maps_kind_and_never_changes_ticket():
    """英文 kind → 中文白名单分类；只出建议，不碰工单/复核/供电。"""
    for kind, expected in (("no_hat", "未戴厨师帽"), ("smoking", "工位吸烟"),
                           ("phone", "工位玩手机"), ("unknown_kind", "其他")):
        raw = rule_violation_explain({"kind": kind, "station_id": 3,
                                      "evidence": True, "power_cut": True})
        assert raw["category"] == expected
        assert raw["need_human"] is True
        assert "ST3" in raw["summary"]
    out = AiService(None, provider=BrokenProvider()).advise(
        "violation_explain", {"kind": "smoking", "station_id": 2, "power_cut": True})
    assert out["advice_only"] is True
    assert out["slot_boundary"] == "不改工单/不批准复核/不恢复供电"
    assert out["prompt_version"] == "violation-explain-v1"
    for forbidden in ("approve", "decision", "amount", "restore_power", "close_ticket"):
        assert forbidden not in out


def test_violation_route_works_and_filters_privacy(tmp_path):
    server = make_server(tmp_path)
    lead = force_session("lead", "lead")
    adv = data_of(server, "POST", "/api/ai/advise/violation", session=lead,
                  kind="no_hat", station_id=5, evidence=True, power_cut=False,
                  openid="SENSITIVE-OPENID", phone="13800000000")
    assert adv["advice_only"] is True
    assert "未戴厨师帽" in adv["summary"]
    assert "ST5" in adv["summary"]
    assert "SENSITIVE-OPENID" not in str(adv)
    assert "13800000000" not in str(adv)
