# -*- coding: utf-8 -*-
"""A13 食安合规凭证域：测的是「不能伪造合格」的几条口径。

- 空数据不是 100% 达标（`na` 不进分母）
- 留样只认 ≥125g 且已落库；消毒只认域层 `passed`，不重算温度
- 晨检待复核、健康证过期、工单未闭环、禁出库都算缺口
- 一页纸的缺失环节如实列出，不静默省略
- 日报规则层如实标 `rule`，在线失败降级，AI 不改检查结论
- 权限、CSV BOM、journal 重放

业务流程判据（#8/#9/#14/#15）由各自域测试覆盖，这里只 stub 数据源验证聚合层。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from api_support import call, data_of, force_session, make_server, make_state
from kitchen.domain import CST, NotFoundError

TODAY = "2026-11-15"


def _moment(hour: int = 10) -> datetime:
    return datetime.fromisoformat(f"{TODAY}T{hour:02d}:00:00").replace(tzinfo=CST)


def _cert(*, valid: bool = True, left: int | None = 365, no: str = "HC-001"):
    return SimpleNamespace(
        cert_no=no, is_valid_on=lambda day: valid,
        expires_in=lambda day: left, state="valid" if valid else "expired")


def _sample(batch_id: int, weight: float):
    return SimpleNamespace(dish_batch_id=batch_id, weight_g=weight,
                           id=1, state="stored", sampled_at=_moment(),
                           destroy_due_at=_moment(23), destroyed_at=None)


def _record(*, state: str = "passed", forbid: bool = False,
            batch_id: int | None = 1, uid: str = "DI-001"):
    return SimpleNamespace(
        id=1, session_uid=uid, batch_id=batch_id, start_ts=_moment(),
        end_ts=_moment(11), state=state, cumulative_sec=600.0,
        peak_temp=110.0, forbid_outbound=forbid, reason="",
        describe=lambda: f"{uid} {state}")


def _ticket(state: str = "open", msg: str = "传感器故障"):
    return SimpleNamespace(
        state=state, describe=lambda: msg,
        to_payload=lambda: {"state": state, "message": msg, "level": 1})


def _item(check, key):
    return next(row for row in check["items"] if row["key"] == key)


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def state(tmp_path):
    return make_state(tmp_path)


class Test检查表口径:

    def test_六项齐全且带判定依据与口径说明(self, server):
        check = data_of(server, "GET", "/api/compliance/checklist",
                        session=force_session())
        assert len(check["items"]) == 6
        assert all(row["basis"] and isinstance(row["gaps"], list)
                   for row in check["items"])
        assert "不计入达标率" in check["discipline_note"]
        assert check["date"] and check["generated_at"]

    def test_无数据项标na且不算通过(self, server):
        check = data_of(server, "GET", "/api/compliance/checklist",
                        session=force_session())
        na = [row for row in check["items"] if row["state"] == "na"]
        assert na, "演示初始应有未采集数据的检查项"
        assert all(row["rate"] is None and row["checked"] == 0 for row in na)
        assert check["summary"]["passed"] + check["summary"]["no_data"] + \
               check["summary"]["failed"] + check["summary"]["warn"] == 6

    def test_晨检未提交与待复核都是缺口(self, server):
        st = server.state
        people = list(st.staff_repo.all_staff())
        fake = {people[0].id: None,
                people[1].id: SimpleNamespace(locked=True, admitted=False),
                people[2].id: SimpleNamespace(locked=False, admitted=True)}
        st.staff_repo.morning_check = lambda sid, day: fake.get(sid)
        row = _item(st.compliance.checklist(TODAY, persist=False), "morning")
        assert (row["checked"], row["passed"], row["state"]) == (3, 1, "fail")
        assert row["gap_total"] == 2

    def test_健康证过期算失败_临期只提醒(self, state):
        st = state
        people = list(st.staff_repo.all_staff())
        # 两张都有效、其中一张 10 天后到期 → 只有提醒，不算未达标
        st.staff_repo.certificates = lambda sid: (
            [_cert(valid=True, left=10, no="HC-WARN")] if sid == people[0].id
            else [_cert(valid=True, no="HC-OK")] if sid == people[1].id
            else [])
        row = _item(st.compliance.checklist(TODAY, persist=False), "certificate")
        assert (row["checked"], row["passed"]) == (2, 2)
        assert row["state"] == "warn"     # 无过期 → 只是提醒
        assert row["warn_count"] == 1
        assert any("到期" in gap for gap in row["gaps"])

        # 出现一张已失效 → 立刻 fail
        st.staff_repo.certificates = lambda sid: (
            [_cert(valid=False, no="HC-EXP")] if sid == people[0].id else [])
        row = _item(st.compliance.checklist(TODAY, persist=False), "certificate")
        assert row["state"] == "fail" and row["passed"] == 0

    def test_留样按当日生产批次且124g不算达标(self, state):
        st = state
        st.repo.batch_ids = lambda: (1, 2)
        st.repo.batch = lambda bid: {
            "id": bid, "batch_no": f"B-{bid}", "dish_id": 1,
            "produced_at": _moment(11).isoformat(), "state": "produced"}
        st.retention._samples = {1: _sample(1, 124.0)}   # 只有不足 125g 的
        row = _item(st.compliance.checklist(TODAY, persist=False), "retention")
        assert (row["checked"], row["passed"], row["state"]) == (2, 0, "fail")
        assert "无 ≥125g" in row["gaps"][0]

        st.retention._samples = {1: _sample(1, 125.0), 2: _sample(2, 140.0)}
        row = _item(st.compliance.checklist(TODAY, persist=False), "retention")
        assert (row["passed"], row["state"]) == (2, "pass")

    def test_留样只认当日生产批次_昨天不重列(self, state):
        st = state
        st.repo.batch_ids = lambda: (1,)
        st.repo.batch = lambda bid: {
            "id": 1, "batch_no": "B-OLD", "dish_id": 1,
            "produced_at": "2026-11-14T11:30:00", "state": "produced"}
        st.retention._samples = {}
        row = _item(st.compliance.checklist(TODAY, persist=False), "retention")
        assert row["checked"] == 0 and row["state"] == "na"

    def test_消毒只认域层passed_运行中不计入判定(self, state):
        st = state
        st.disinfection._records = {
            1: _record(state="passed", uid="DI-OK"),
            2: _record(state="failed", uid="DI-NG"),
            3: _record(state="running", uid="DI-RUN"),
        }
        st.disinfection._tickets = {}
        st.health.tickets = lambda: ()
        row = _item(st.compliance.checklist(TODAY, persist=False), "disinfection")
        assert (row["checked"], row["passed"], row["state"]) == (2, 1, "fail")
        assert row["running"] == 1
        assert any("DI-NG" in gap for gap in row["gaps"])

    def test_工单按closed计闭环(self, state):
        st = state
        st.health.tickets = lambda: (_ticket("closed"), _ticket("open"))
        st.disinfection._records = {}
        # 域层 _tickets 是 dict[int, ReworkTicket]，property 按 key 遍历
        st.disinfection._tickets = {
            1: SimpleNamespace(state="closed", describe=lambda: "复消完成")}
        row = _item(st.compliance.checklist(TODAY, persist=False), "ticket")
        assert (row["checked"], row["passed"], row["state"]) == (3, 2, "fail")
        assert row["gaps"][0].startswith("未关闭")

    def test_禁出库未解除算缺口(self, state):
        st = state
        st.disinfection._records = {
            1: _record(state="failed", forbid=True, uid="DI-BLOCK"),
            2: _record(state="passed", forbid=False, uid="DI-OK"),
        }
        st.disinfection._tickets = {}
        st.health.tickets = lambda: ()
        row = _item(st.compliance.checklist(TODAY, persist=False), "outbound")
        assert (row["checked"], row["passed"], row["state"]) == (2, 1, "fail")

    def test_整体结论未达标优先于提醒(self, state):
        st = state
        st.staff_repo.morning_check = lambda sid, day: None
        st.compliance.checklist(TODAY, persist=False)
        check = st.compliance.checklist(TODAY, persist=False)
        assert check["overall"] == "fail"
        assert check["summary"]["failed"] >= 1


class Test一页纸追溯:

    def test_合并各环节且缺失如实列出(self, server):
        d = data_of(server, "GET", "/api/compliance/dossier?batch_id=1",
                    session=force_session())
        assert d["batch_id"] == 1 and d["batch"]["batch_no"]
        assert isinstance(d["missing"], list)
        assert "不能作为法定追溯凭证" in d["evidence_note"]
        assert all(k in d for k in ("retention", "disinfection", "outbound", "audit"))
        # 缺失环节不能静默省略：空台账必须点名
        assert "留样台账" in d["missing"]

    def test_留样与消毒记录进一页纸(self, server):
        st = server.state
        st.retention._samples = {1: _sample(1, 130.0)}
        st.disinfection._records = {1: _record(state="passed", batch_id=1)}
        d = st.compliance.dossier(1)
        assert len(d["retention"]) == 1 and d["retention"][0]["qualified"] is True
        assert len(d["disinfection"]) == 1
        assert "留样台账" not in d["missing"]

    def test_批次不存在明确报错(self, server):
        status, payload = call(server, "GET", "/api/compliance/dossier?batch_id=9999",
                               session=force_session())
        assert status >= 400
        assert "不存在" in str(payload)


class Test日报与导出:

    def test_规则层如实标注且列出失败项(self, server):
        st = server.state
        st.staff_repo.morning_check = lambda sid, day: None
        row = st.compliance.report(TODAY)
        assert row["scenario"] == "compliance_daily"
        assert row["source"] == "rule" and row["model"] == "rule-engine-v1"
        assert row["degraded_reason"] == ""
        assert "晨检准入" in row["failed_items"]
        assert "非大模型生成" in row["summary"]
        assert "# 食安合规日报" in row["content_md"]

    def test_在线失败自动降级且留痕(self, server):
        class Broken:
            mode, model = "live", "deepseek-test"
            def complete(self, scenario, facts):
                raise RuntimeError("upstream timeout")
        st = server.state
        st.compliance.provider = Broken()
        row = st.compliance.report(TODAY)
        assert row["source"] == "rule"
        assert "upstream timeout" in row["degraded_reason"]
        assert st.compliance.runs()[-1]["status"] == "degraded"

    def test_模型输出夹带动作词被拒并降级(self, server):
        class Naughty:
            mode, model = "live", "deepseek-test"
            def complete(self, scenario, facts):
                return {"summary": "已批准并判定合格，建议退款处理完成"}
        st = server.state
        st.compliance.provider = Naughty()
        row = st.compliance.report(TODAY)
        assert row["source"] == "rule"
        assert "非大模型生成" in row["summary"]

    def test_CSV带BOM且行数等于检查项(self, server):
        out = data_of(server, "GET", "/api/compliance/export",
                      session=force_session())
        assert out["csv"].startswith("\ufeff")
        assert out["rows"] == 6
        assert "无数据" in out["csv"]
        assert out["filename"].endswith(".csv")

    def test_日报接口与运行审计可用(self, server):
        data_of(server, "GET", "/api/compliance/report", session=force_session())
        runs = data_of(server, "GET", "/api/compliance/runs", session=force_session())
        assert runs["total"] >= 1 and runs["provider"]["mode"] == "rule"
        assert runs["journal"]["path"]


class Test持久化与权限:

    def test_重启后检查表与日报不丢(self, tmp_path):
        first = make_state(tmp_path)
        first.compliance.checklist(TODAY)
        first.compliance.report(TODAY)
        second = make_state(tmp_path)
        assert TODAY in second.compliance._checks
        assert TODAY in second.compliance._reports
        assert second.compliance.journal_stats["applied"] >= 3

    def test_损坏journal行只跳过不崩(self, tmp_path):
        var = tmp_path / "var"
        var.mkdir(parents=True, exist_ok=True)
        (var / "compliance_journal.jsonl").write_text(
            '{"op":"unknown"}\nnot-json\n', encoding="utf-8")
        state = make_state(tmp_path)
        assert state.compliance.journal_stats["skipped"] >= 1
        assert state.compliance.checklist(TODAY)

    def test_组长可读_操作员与观察员403(self, server):
        assert call(server, "GET", "/api/compliance/checklist",
                    session=force_session("lead01", "lead"))[0] == 200
        for role in ("operator", "viewer"):
            status, _ = call(server, "GET", "/api/compliance/checklist",
                             session=force_session("u", role))
            assert status == 403, role

    def test_五条路由全部注册(self, server):
        for path in ("/api/compliance/checklist", "/api/compliance/report",
                     "/api/compliance/export", "/api/compliance/runs",
                     "/api/compliance/dossier?batch_id=1"):
            status, _ = call(server, "GET", path, session=force_session())
            assert status == 200, path
