# -*- coding: utf-8 -*-
"""A5 prod 安全回归：登录限流键的「服务端权威」。

背景（11/13 排雷）：`_mutate` 原来用 `body.setdefault("source", ip)`——
客户端在 body 里自带 `source` 会覆盖服务端 IP，攻击者每次换一个随机值
就绕过 `LoginLimiter`。修后服务端**强制覆盖**，本文件用真 HTTP（urllib）
钉死这三条：

1. 客户端伪造 body.source 不影响锁定 —— 键永远是服务端看到的对端地址；
2. 默认不信 X-Forwarded-For（直连场景可伪造）；
3. `trust_proxy_headers=True`（可信反代后）XFF 才生效。

单元层的锁定语义（同源累计/换源隔离）在 `test_api_auth.py`，不重复。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from api_support import PASSWORD, make_state


def _post_login(url_base: str, *, source: str | None = None,
                xff: str | None = None, password: str = "wrong") -> tuple[int, dict]:
    """发一次登录请求，返回 (状态码, 响应体)。默认错口令。"""
    body: dict[str, object] = {"username": "admin", "password": password}
    if source is not None:
        # 攻击载荷：试图用随机 source 顶替服务端限流键（修复前能绕过锁定）
        body["source"] = source
    headers = {"Content-Type": "application/json"}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    req = urllib.request.Request(
        f"{url_base}/api/auth/login",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _start_server(tmp_path: Path, **kwargs):
    from api_support import create_server

    state = make_state(tmp_path)
    server = create_server(state, port=0, **kwargs)
    server.start()
    return server


class TestSourceAuthority:
    """限流键必须由服务端决定（body.source 不可信）。"""

    def test_客户端伪造source不能绕过锁定(self, tmp_path: Path) -> None:
        """连错 5 次（每次换个伪造 source）→ 第 6 次必须 429。

        修复前的行为：每次伪造 source 落不同的限流键，永远锁不住。
        """
        server = _start_server(tmp_path)
        try:
            for i in range(5):
                status, _ = _post_login(server.url, source=f"spoof-{i}")
                assert status == 401, f"第 {i + 1} 次错口令应 401，实际 {status}"
            # 第 6 次：再换一个没见过的伪造 source，仍应被锁
            status, payload = _post_login(server.url, source="spoof-new")
            assert status == 429, payload
            assert payload["error"] == "LoginThrottled"
        finally:
            server.stop()

    def test_锁定按真实对端地址生效且能被正确口令前的锁定拦截(self, tmp_path: Path) -> None:
        """锁定后换**正确口令**也进不来——锁定键是对端地址，不管凭据对错。"""
        server = _start_server(tmp_path)
        try:
            for _ in range(5):
                _post_login(server.url, source="x")
            status, _ = _post_login(server.url, source="x",
                                    password=PASSWORD)
            assert status == 429
        finally:
            server.stop()


class TestTrustProxy:
    """X-Forwarded-For 只在可信反代开关打开后才生效。"""

    def test_默认不信任XFF_伪造头不影响限流键(self, tmp_path: Path) -> None:
        server = _start_server(tmp_path)
        try:
            # 带不同 XFF 的请求 = 都来自同一个真实对端 127.0.0.1 → 一起累计
            for i in range(5):
                status, _ = _post_login(server.url, xff=f"10.0.0.{i}")
                assert status == 401
            status, _ = _post_login(server.url, xff="10.9.9.9")
            assert status == 429
        finally:
            server.stop()

    def test_开启trust_proxy后XFF生效_不同来源互不影响(self, tmp_path: Path) -> None:
        server = _start_server(tmp_path, trust_proxy_headers=True)
        try:
            # 同一伪造源 10.0.0.7 连错 5 次 → 只锁 10.0.0.7
            for _ in range(5):
                status, _ = _post_login(server.url, xff="10.0.0.7")
                assert status == 401
            status, _ = _post_login(server.url, xff="10.0.0.7")
            assert status == 429
            # 换一个 XFF（模拟另一个真实客户端）不受影响
            status, _ = _post_login(server.url, xff="10.0.0.8",
                                    password=PASSWORD)
            assert status == 200, "反代模式下不同来源不应被连带锁定"
        finally:
            server.stop()
