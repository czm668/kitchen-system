# -*- coding: utf-8 -*-
"""审计日志、路由匹配、SSE 帧格式。

这三块都不碰业务，但都是「出问题很难查」的地方：审计少记一条、路由匹配错一段、
SSE 帧多了个换行，症状分别是「不知道谁改的」「某个页面 404」「数据一直不刷新」。
"""
from __future__ import annotations

import json

import pytest

from kitchen.api.audit import MASK_TEXT, AuditLog
from kitchen.api.routes import NotFound, Router, http_status
from kitchen.api.sse import format_event, hello_frame, ping_frame, retry_frame
from kitchen.channels import InterlockBlocked, UnknownActionError
from kitchen.domain import InvalidTransition, NotFoundError
from kitchen.process.orders import SoldOutError

from api_support import call, force_session, login, make_server, make_state


# =========================================================== 审计
class TestAuditLog:
    def test_记录与自增id(self):
        log = AuditLog(clock=lambda: 1000.0)
        first = log.record(actor="张三", role="lead", action="param.save")
        second = log.record(actor="李四", role="manager", action="ticket.ack")
        assert (first.id, second.id) == (1, 2)
        assert first.ts == 1000.0

    def test_记下改前改后(self):
        log = AuditLog(clock=lambda: 0.0)
        entry = log.record(
            actor="组长", role="lead", action="param.save", target="gas_level",
            before={"upper_bound": 0.55}, after={"upper_bound": 0.65},
        )
        text = entry.describe()
        assert "0.55" in text and "0.65" in text
        payload = entry.to_payload()
        assert payload["before"] == {"upper_bound": 0.55}
        assert payload["after"] == {"upper_bound": 0.65}

    def test_口令字段脱敏(self):
        """审计会被截图进论文、被导出给评审。口令明文出现在里面等于泄漏。"""
        log = AuditLog(clock=lambda: 0.0)
        entry = log.record(
            actor="admin", role="manager", action="auth.login",
            before={"password": "kitchen2026", "new_password": "x"},
            after={"api_token": "abc", "role": "manager"},
        )
        assert entry.before["password"] == MASK_TEXT
        assert entry.before["new_password"] == MASK_TEXT
        assert entry.after["api_token"] == MASK_TEXT
        assert entry.after["role"] == "manager", "非敏感字段不能被连带脱敏"

    def test_嵌套结构也脱敏(self):
        log = AuditLog(clock=lambda: 0.0)
        entry = log.record(actor="a", role="lead", action="x",
                           after={"rows": [{"password": "p"}, {"value": 1}]})
        assert entry.after["rows"][0]["password"] == MASK_TEXT
        assert entry.after["rows"][1]["value"] == 1

    def test_失败的操作也要记(self):
        log = AuditLog(clock=lambda: 0.0)
        entry = log.record(actor="组长", role="lead", action="channel.action",
                           result="blocked", detail="还有触发源未恢复")
        assert entry.result == "blocked"
        assert "blocked" in entry.describe()

    def test_容量满丢最旧(self):
        log = AuditLog(capacity=3, clock=lambda: 0.0)
        for i in range(5):
            log.record(actor="a", role="lead", action=f"a{i}")
        assert len(log.entries()) == 3
        assert log.dropped == 2
        assert log.entries()[-1].action == "a4"

    def test_落盘与重启重载(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, clock=lambda: 1000.0)
        log.record(actor="张三", role="lead", action="param.save",
                   before={"level": 1}, after={"level": 2})
        reloaded = AuditLog(path, clock=lambda: 2000.0)
        assert len(reloaded.entries()) == 1
        entry = reloaded.entries()[0]
        assert entry.actor == "张三"
        assert entry.before == {"level": 1}
        # 重载后 id 游标要接着走，不能从 1 重新开始
        assert reloaded.record(actor="b", role="lead", action="x").id == 2

    def test_坏行跳过不整体崩(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, clock=lambda: 0.0)
        log.record(actor="张三", role="lead", action="ok.action")
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": 99, "ts": "半行')  # 模拟掉电写了半行
        reloaded = AuditLog(path, clock=lambda: 0.0)
        assert [entry.action for entry in reloaded.entries()] == ["ok.action"]

    def test_落盘失败不抛且计数(self, tmp_path, monkeypatch):
        """审计是旁路：磁盘写不进去也不能让「关燃气阀」失败。"""
        log = AuditLog(tmp_path / "audit.jsonl", clock=lambda: 0.0)

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("pathlib.Path.open", boom)
        entry = log.record(actor="a", role="lead", action="x")
        assert entry.id == 1
        assert log.write_failures == 1

    def test_筛选(self):
        log = AuditLog(clock=lambda: 0.0)
        log.record(actor="张三", role="lead", action="param.save")
        log.record(actor="李四", role="manager", action="ticket.ack")
        log.record(actor="张三", role="lead", action="ticket.ack")
        assert len(log.filter(actor="张三")) == 2
        assert len(log.filter(action="ticket.ack")) == 2
        assert len(log.filter(actor="张三", action="ticket.ack")) == 1

    def test_快照字段(self):
        log = AuditLog(capacity=10, clock=lambda: 0.0)
        log.record(actor="a", role="lead", action="x")
        body = log.snapshot()
        assert body["count"] == 1 and body["capacity"] == 10
        assert body["write_failures"] == 0


# =========================================================== 路由
class TestRouteMatch:
    def test_路径参数(self, tmp_path):
        server = make_server(tmp_path)
        route, params = server.router.resolve("POST", "/api/orders/items/42/urge")
        assert params == {"item_id": "42"}
        assert route.pattern == "/api/orders/items/{item_id}/urge"
        assert route.method == "POST"
        assert route.permission == "order.write"

    def test_多段参数(self, tmp_path):
        server = make_server(tmp_path)
        _route, params = server.router.resolve("GET", "/api/stations/gantt")
        assert params == {}

    def test_未知路径404(self, tmp_path):
        server = make_server(tmp_path)
        with pytest.raises(NotFound):
            server.router.resolve("GET", "/api/nope")

    def test_方法不对也是404(self, tmp_path):
        server = make_server(tmp_path)
        with pytest.raises(NotFound):
            server.router.resolve("DELETE", "/api/orders")

    def test_多余层级不匹配(self, tmp_path):
        server = make_server(tmp_path)
        with pytest.raises(NotFound):
            server.router.resolve("GET", "/api/orders/1/2/3/4")

    def test_结尾斜杠容错(self, tmp_path):
        server = make_server(tmp_path)
        assert server.router.resolve("GET", "/api/orders/")[1] == {}


class TestErrorMapping:
    """异常 → 状态码。**顺序敏感**：子类排在父类后面就会被吃成 400。"""

    def test_401与403分开(self):
        from kitchen.api.auth import AuthError, PermissionDenied

        assert http_status(AuthError("x")) == 401
        assert http_status(PermissionDenied("x")) == 403

    def test_冲突类409(self):
        assert http_status(InterlockBlocked("还有触发源")) == 409
        assert http_status(SoldOutError(["宫保鸡丁"])) == 409
        assert http_status(InvalidTransition("ready→cooking")) == 409

    def test_资源不存在404(self):
        assert http_status(NotFoundError("没这单")) == 404
        assert http_status(KeyError("x")) == 404

    def test_参数类400(self):
        assert http_status(ValueError("参数不对")) == 400
        assert http_status(UnknownActionError("valve_close")) == 400

    def test_未知异常500(self):
        assert http_status(RuntimeError("炸了")) == 500


# =========================================================== SSE 帧
class TestSseFrames:
    def test_帧格式(self):
        raw = format_event("rule", {"a": 1}, event_id=7).decode("utf-8")
        assert raw.startswith("id: 7\n")
        assert "event: rule\n" in raw
        assert raw.endswith("\n\n")

    def test_data必须单行(self):
        """JSON 里的换行会把一帧拆成两帧，前端解析直接失败。"""
        raw = format_event("x", {"text": "第一行\n第二行"}).decode("utf-8")
        data_lines = [line for line in raw.splitlines() if line.startswith("data: ")]
        assert len(data_lines) == 1
        assert "\n" not in data_lines[0]
        # 解析回来仍是原文（`\n` 被转义成字面量）
        assert json.loads(data_lines[0][6:])["text"] == "第一行\n第二行"

    def test_事件名非法字符被清掉(self):
        raw = format_event("a b\nc", {}).decode("utf-8")
        assert "event: abc\n" in raw

    def test_事件名为空有兜底(self):
        assert "event: message\n" in format_event("", {}).decode("utf-8")

    def test_心跳是注释行(self):
        assert ping_frame() == b": ping\n\n"

    def test_retry只发一次(self):
        assert retry_frame(3000) == b"retry: 3000\n\n"

    def test_hello带全景(self, tmp_path):
        state = make_state(tmp_path)
        state.tick()
        raw = hello_frame(state).decode("utf-8")
        assert "event: hello" in raw
        body = json.loads(raw.split("data: ")[1].strip())
        assert body["device_id"] == state.config.device_id
        assert body["event_seq"] == state.event_seq


# =========================================================== 端到端小项
class TestRouterBasics:
    def test_健康检查匿名可访问(self, tmp_path):
        server = make_server(tmp_path)
        status, payload = call(server, "GET", "/api/health")
        assert status == 200 and payload["data"]["ok"] is True

    def test_未知路径返回结构化404(self, tmp_path):
        server = make_server(tmp_path)
        status, payload = call(server, "GET", "/api/nope", session=login(server))
        assert status == 404
        assert payload["error"] == "not_found"

    def test_meta给出动作与遥测口径(self, tmp_path):
        server = make_server(tmp_path)
        data = call(server, "GET", "/api/meta", session=login(server))[1]["data"]
        actions = {row["action"]: row for row in data["actions"]}
        assert actions["gas_valve_close"]["channel"] == "CH1"
        assert actions["power_restore"]["manual_confirm"] is True
        assert len(data["metrics"]) == 12, "12 项遥测是口径，不能多也不能少"
        assert data["roles"][-1] == "manager"

    def test_降级报告三端都有文案(self, tmp_path):
        server = make_server(tmp_path)
        data = call(server, "GET", "/api/degrade", session=login(server))[1]["data"]
        assert set(data["channels"]) == {"tft", "web", "miniapp"}
        assert data["severity"] in ("normal", "degraded", "offline")

    def test_审计页能查到登录记录(self, tmp_path):
        """**登录必须走 HTTP 接口**才会写审计：`LoginService` 只在拿到
        `audit` 参数时记录，直调 `login.login()` 是故意留的静默通道。"""
        server = make_server(tmp_path)
        call(server, "POST", "/api/auth/login",
             username="admin", password="kitchen2026")
        session = login(server)
        data = call(server, "GET", "/api/audit", session=session)[1]["data"]
        assert any(row["action"] == "auth.login" for row in data["entries"])
