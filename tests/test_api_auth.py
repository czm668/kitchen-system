# -*- coding: utf-8 -*-
"""鉴权、账号表与登录限速。

这些用例守的是「谁能做什么」这条线。写错的表现**不是报错**，而是「谁都能复位
燃气阀」——系统一切正常，只是权限表少了一行。
"""
from __future__ import annotations

import json
import time

import pytest

from kitchen.api.accounts import DEFAULT_PASSWORD, AccountStore, LoginService
from kitchen.api.auth import (
    ROLES,
    AuthError,
    LoginLimiter,
    LoginThrottled,
    PermissionDenied,
    TokenExpired,
    TokenInvalid,
    TokenSigner,
    can,
    require,
)

from api_support import PASSWORD, force_session, login, token_of


# =========================================================== 令牌
class TestTokenSigner:
    def setup_method(self):
        self.signer = TokenSigner("unit-secret", ttl_sec=3600)

    def test_签发再验证(self):
        token = self.signer.issue("张三", "lead")
        session = self.signer.verify(token)
        assert session.subject == "张三"
        assert session.role == "lead"

    def test_同一个claims签出同一个串(self):
        """`sort_keys` + 固定分隔符：签名必须是确定性函数。

        不确定的话，「同一秒签两次」得到的两个串在服务端看起来是两个令牌，
        幂等与去重全部失效。
        """
        first = self.signer.issue("李四", "operator", now=1000.0)
        second = self.signer.issue("李四", "operator", now=1000.0)
        assert first == second

    def test_不同nonce不同串(self):
        first = self.signer.issue("李四", "operator", now=1000.0, nonce="a")
        second = self.signer.issue("李四", "operator", now=1000.0, nonce="b")
        assert first != second

    def test_空密钥直接拒绝(self):
        for bad in ("", b"", None):
            with pytest.raises(ValueError):
                TokenSigner(bad)

    def test_空subject拒绝(self):
        with pytest.raises(ValueError):
            self.signer.issue("   ", "lead")

    def test_未知角色拒绝(self):
        with pytest.raises(ValueError):
            self.signer.issue("张三", "root")

    def test_改一个字节就验不过(self):
        token = self.signer.issue("张三", "lead")
        head, sig = token.split(".")
        # 换签名最后一个字符：长度不变，只改内容
        tampered = head + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")
        with pytest.raises(TokenInvalid):
            self.signer.verify(tampered)

    def test_换payload也验不过(self):
        token = self.signer.issue("张三", "viewer")
        head, sig = token.split(".")
        # 直接把角色改成 manager 再拼回原签名
        import base64

        pad = "=" * (-len(head) % 4)
        claims = json.loads(base64.urlsafe_b64decode(head + pad))
        claims["role"] = "manager"
        forged = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        with pytest.raises(TokenInvalid):
            self.signer.verify(f"{forged}.{sig}")

    def test_过期(self):
        token = self.signer.issue("张三", "lead", now=1000.0, ttl_sec=10)
        assert self.signer.verify(token, now=1005.0).subject == "张三"
        with pytest.raises(TokenExpired):
            self.signer.verify(token, now=1011.0)

    def test_格式错误(self):
        for bad in ("", None, "abc", "a.b.c", "!!!.###"):
            with pytest.raises((TokenInvalid, TokenExpired)):
                self.signer.verify(bad)

    def test_别家密钥签的不认(self):
        other = TokenSigner("another-secret")
        with pytest.raises(TokenInvalid):
            self.signer.verify(other.issue("张三", "lead"))

    def test_口令摘要不是裸hash(self):
        signer = TokenSigner("s")
        first = signer.digest_password("kitchen2026", "salt-a")
        second = signer.digest_password("kitchen2026", "salt-b")
        assert first != second, "不同 salt 必须得到不同摘要（否则彩虹表直接命中）"
        assert len(first) == 64
        assert signer.check_password("kitchen2026", "salt-a", first) is True
        assert signer.check_password("kitchen2027", "salt-a", first) is False

    def test_salt不能为空(self):
        with pytest.raises(ValueError):
            TokenSigner("s").digest_password("x", "")


# =========================================================== 权限
class TestPermission:
    def test_未登记权限一律拒绝(self):
        """白名单语义：写错权限名得到「拒绝」而不是「放行」。"""
        assert can("manager", "not.a.real.permission") is False

    def test_角色阶梯(self):
        assert can("viewer", "state.read") is True
        assert can("viewer", "order.write") is False
        assert can("operator", "order.write") is True
        assert can("operator", "param.write") is False
        assert can("lead", "param.write") is True
        # 复位只有 manager：组长有「投一票」的权利，没有单方面复位的权利
        assert can("lead", "channel.review") is True
        assert can("lead", "channel.restore") is False
        assert can("manager", "channel.restore") is True

    def test_require抛具体异常(self):
        require("manager", "param.write")
        with pytest.raises(PermissionDenied) as exc:
            require("viewer", "param.write")
        assert exc.value.status == 403
        assert "viewer" in str(exc.value)

    def test_roles顺序供前端下拉框(self):
        assert ROLES[0] == "viewer" and ROLES[-1] == "manager"

    def test_会话带权限清单(self):
        session = force_session("admin", "manager")
        assert "channel.restore" in session.to_payload()["permissions"]
        guest = force_session("guest", "viewer")
        assert guest.to_payload()["permissions"] == ["state.read"]


# =========================================================== 账号与登录
class TestAccounts:
    def test_四个演示账号(self):
        store = AccountStore(TokenSigner("s"))
        assert [acc.subject for acc in store.all()] == [
            "admin", "chef01", "guest", "lead01"
        ]
        assert store.source == "default"

    def test_口令正确才过(self):
        store = AccountStore(TokenSigner("s"))
        assert store.check("admin", DEFAULT_PASSWORD) is not None
        assert store.check("admin", "wrong") is None
        # 账号不存在与口令错返回同一个值，避免账号枚举
        assert store.check("nobody", DEFAULT_PASSWORD) is None

    def test_不存明文口令(self):
        store = AccountStore(TokenSigner("s"))
        for account in store.all():
            assert DEFAULT_PASSWORD not in (account.digest or "")
            assert len(account.digest) == 64

    def test_停用账号登不上(self, monkeypatch):
        table = [{"subject": "ops", "role": "manager", "password": "x" * 8,
                  "enabled": False}]
        monkeypatch.setenv("KITCHEN_ACCOUNTS", json.dumps(table))
        store = AccountStore(TokenSigner("s"))
        assert store.source == "env"
        assert store.check("ops", "x" * 8) is None

    def test_env账号表用摘要(self, monkeypatch):
        signer = TokenSigner("s")
        digest = signer.digest_password("secret-pass", "pepper")
        table = [{"subject": "ops", "role": "lead", "salt": "pepper",
                  "digest": digest}]
        monkeypatch.setenv("KITCHEN_ACCOUNTS", json.dumps(table))
        store = AccountStore(signer)
        assert store.check("ops", "secret-pass").role == "lead"

    def test_env写坏了回落默认并可用(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_ACCOUNTS", "{不是 json")
        store = AccountStore(TokenSigner("s"))
        assert store.source == "default"
        assert store.check("admin", DEFAULT_PASSWORD) is not None

    def test_env里角色非法要拒绝(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_ACCOUNTS",
                           json.dumps([{"subject": "x", "role": "root",
                                        "password": "12345678"}]))
        store = AccountStore(TokenSigner("s"))
        # 非法表整体回落默认（并打 ERROR），不会带着半张表跑起来
        assert store.source == "default"

    def test_改管理员口令(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_ADMIN_PASSWORD", "much-better-pass")
        store = AccountStore(TokenSigner("s"))
        assert store.check("admin", "much-better-pass") is not None
        assert store.check("admin", DEFAULT_PASSWORD) is None
        # 其它账号不受影响
        assert store.check("chef01", DEFAULT_PASSWORD) is not None


class TestLoginService:
    def setup_method(self):
        self.signer = TokenSigner("login-secret")
        self.service = LoginService(self.signer, AccountStore(self.signer))

    def test_登录拿到令牌(self):
        token, session = self.service.login("admin", PASSWORD, source="1.1.1.1")
        assert session.role == "manager"
        assert self.signer.verify(token).subject == "admin"

    def test_口令错抛PermissionError(self):
        with pytest.raises(PermissionError):
            self.service.login("admin", "wrong", source="1.1.1.1")

    def test_连续失败锁定(self):
        for _ in range(self.service.limiter.max_failures):
            with pytest.raises(PermissionError):
                self.service.login("admin", "wrong", source="9.9.9.9")
        # 再试一次，哪怕口令对了也先被限速拦下
        with pytest.raises(LoginThrottled):
            self.service.login("admin", PASSWORD, source="9.9.9.9")

    def test_别的来源不受影响(self):
        for _ in range(self.service.limiter.max_failures):
            with pytest.raises(PermissionError):
                self.service.login("admin", "wrong", source="9.9.9.9")
        token, _ = self.service.login("admin", PASSWORD, source="8.8.8.8")
        assert token

    def test_成功登录清零失败计数(self):
        for _ in range(self.service.limiter.max_failures - 1):
            with pytest.raises(PermissionError):
                self.service.login("admin", "wrong", source="1.2.3.4")
        self.service.login("admin", PASSWORD, source="1.2.3.4")
        # 再错一次不该被立刻锁
        with pytest.raises(PermissionError):
            self.service.login("admin", "wrong", source="1.2.3.4")


class TestLoginLimiter:
    def test_窗口内计数窗口外清理(self):
        now = [0.0]
        limiter = LoginLimiter(max_failures=3, window_sec=10.0, lock_sec=5.0,
                               clock=lambda: now[0])
        limiter.fail("ip")
        now[0] = 5.0
        limiter.fail("ip")
        assert limiter.locked("ip") == 0.0
        now[0] = 20.0            # 两个失败都出了窗口
        limiter.fail("ip")
        limiter.fail("ip")
        assert limiter.locked("ip") == 0.0, "窗口外的旧失败不能累加"

    def test_锁到期自动解除(self):
        now = [0.0]
        limiter = LoginLimiter(max_failures=2, window_sec=100.0, lock_sec=5.0,
                               clock=lambda: now[0])
        limiter.fail("ip")
        limiter.fail("ip")
        assert limiter.locked("ip") == 5.0
        with pytest.raises(LoginThrottled):
            limiter.check("ip")
        now[0] = 6.0
        limiter.check("ip")      # 解锁后不抛

    def test_解锁后不会一错就再锁(self):
        now = [0.0]
        limiter = LoginLimiter(max_failures=2, window_sec=100.0, lock_sec=5.0,
                               clock=lambda: now[0])
        limiter.fail("ip")       # 1
        limiter.fail("ip")       # 2 → 锁
        now[0] = 6.0
        limiter.fail("ip")       # 解锁后第一次失败
        assert limiter.locked("ip") == 0.0, "锁定后窗口要清空，否则等于无限锁"

    def test_快照带剩余锁定秒(self):
        limiter = LoginLimiter(max_failures=1, lock_sec=30.0, clock=lambda: 0.0)
        limiter.fail("ip")
        snapshot = limiter.snapshot()
        assert "ip" in snapshot["locked"]


# =========================================================== 端到端
class TestAuthEndpoints:
    def test_登录接口返回令牌与会话(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        status, payload = call(server, "POST", "/api/auth/login",
                               username="admin", password=PASSWORD)
        assert status == 200
        data = payload["data"]
        assert data["session"]["role"] == "manager"
        assert server.signer.verify(data["token"]).subject == "admin"

    def test_登录失败是401且不进审计成功(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        status, payload = call(server, "POST", "/api/auth/login",
                               username="admin", password="nope")
        assert status == 401
        entries = server.state.audit.filter(action="auth.login")
        assert [entry.result for entry in entries] == ["denied"]

    def test_缺字段也是401(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        assert call(server, "POST", "/api/auth/login", username="admin")[0] == 401

    def test_匿名接口不需要令牌(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        assert call(server, "GET", "/api/health")[0] == 200

    def test_受保护接口没令牌是401(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        status, payload = call(server, "GET", "/api/orders")
        assert status == 401
        assert payload["error"] == "AuthError"

    def test_令牌走查询串也能用(self, tmp_path):
        """SSE 的 `EventSource` 不能带自定义头，所以服务端必须支持 `?token=`。"""
        from api_support import call, make_server

        server = make_server(tmp_path)
        token = token_of(server)
        session = server.signer.verify(token)
        assert call(server, "GET", "/api/overview", session=session)[0] == 200

    def test_me返回权限清单(self, tmp_path):
        from api_support import data_of, make_server

        server = make_server(tmp_path)
        session = login(server, "chef01")
        data = data_of(server, "GET", "/api/auth/me", session=session)
        assert data["session"]["role"] == "operator"
        assert "order.write" in data["session"]["permissions"]

    def test_退出登录留审计(self, tmp_path):
        from api_support import make_server, call

        server = make_server(tmp_path)
        session = login(server)
        assert call(server, "POST", "/api/auth/logout", session=session)[0] == 200
        assert server.state.audit.filter(action="auth.logout")


class TestRoleGateOnRoutes:
    """路由级的权限门：**权限不是只写在文档里，要在真实请求上拦住**。"""

    def test_viewer改不了阈值(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        guest = login(server, "guest")
        status, payload = call(server, "POST", "/api/params/time-scale",
                               session=guest, time_scale=10)
        assert status == 403
        assert "param.write" in payload["detail"]

    def test_viewer能看不能写(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        guest = login(server, "guest")
        assert call(server, "GET", "/api/kpi", session=guest)[0] == 200
        assert call(server, "POST", "/api/orders", session=guest,
                    items=[{"code": "D-QJRS", "qty": 1}])[0] == 403

    def test_lead能改阈值不能复位(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        lead = login(server, "lead01")
        assert call(server, "POST", "/api/params/time-scale", session=lead,
                    time_scale=1)[0] == 200
        status, payload = call(server, "POST", "/api/channels/CH1/actions",
                               session=lead, action="gas_valve_open",
                               manual_confirmed=True)
        assert status == 403

    def test_审计接口只有能改参数的人能看(self, tmp_path):
        from api_support import call, make_server

        server = make_server(tmp_path)
        assert call(server, "GET", "/api/audit", session=login(server, "chef01"))[0] == 403
        assert call(server, "GET", "/api/audit", session=login(server, "lead01"))[0] == 200
