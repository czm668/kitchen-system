# -*- coding: utf-8 -*-
"""真实 HTTP 层：登录、静态托管、路径穿越、SSE、请求体上限。

这一层用**真 socket**，因为要验的东西本身就在 HTTP 语义里：状态码、响应头、
分块读、连接行为。用 `Router.call()` 直调是验不出这些的。
"""
from __future__ import annotations

import gzip
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kitchen.api import ApiServer
from kitchen.api.server import MAX_BODY_BYTES, MAX_SSE_CLIENTS

from api_support import PASSWORD, make_state


@pytest.fixture()
def dist(tmp_path) -> Path:
    """最小可用的前端产物：index.html + 一个带 hash 的 assets。"""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><html><body><div id=app></div></body></html>",
        encoding="utf-8",
    )
    (root / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("不该被读到", encoding="utf-8")
    return root


@pytest.fixture()
def server(tmp_path, dist):
    state = make_state(tmp_path)
    state.tick()
    api = ApiServer(state, port=0, secret="http-test", static_dir=dist)
    api.start()
    yield api
    api.stop()
    state.stop()


def request(server, path, *, method="GET", body=None, token=None):
    """发一个请求，返回 `(status, headers, payload)`。payload 是 dict 或原文。"""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                                 data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            body_out = json.loads(raw) if "json" in ctype else raw.decode("utf-8", "replace")
            return resp.status, dict(resp.headers), body_out
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, dict(exc.headers), json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, dict(exc.headers), raw.decode("utf-8", "replace")


def fetch_raw(server, path, *, accept_encoding=None):
    """取原始字节 + 响应头。压缩测试不能用 `request()`——它按文本解码，gzip 会乱码。"""
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}")
    if accept_encoding is not None:
        req.add_header("Accept-Encoding", accept_encoding)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, dict(resp.headers), resp.read()


def login(server, subject="admin"):
    _status, _headers, payload = request(
        server, "/api/auth/login", method="POST",
        body={"username": subject, "password": PASSWORD},
    )
    return payload["data"]["token"]


# =========================================================== 基本链路
class TestHttpBasics:
    def test_健康检查(self, server):
        status, headers, payload = request(server, "/api/health")
        assert status == 200
        assert payload["data"]["ok"] is True
        assert headers["Content-Type"].startswith("application/json")

    def test_登录后读数据(self, server):
        token = login(server)
        status, _headers, payload = request(server, "/api/overview", token=token)
        assert status == 200
        assert payload["data"]["device_id"]

    def test_无令牌401(self, server):
        status, _headers, payload = request(server, "/api/overview")
        assert status == 401
        assert payload["status"] == 401

    def test_写接口带no_store(self, server):
        """浏览器缓存一个「复位成功」，用户点十次看起来只生效一次。"""
        token = login(server)
        _status, headers, _payload = request(
            server, "/api/auth/logout", method="POST", body={}, token=token
        )
        assert "no-store" in headers.get("Cache-Control", "")

    def test_非JSON请求体400(self, server):
        token = login(server)
        status, _h, payload = request(server, "/api/orders", method="POST",
                                      body=b"this is not json", token=token)
        assert status == 400
        assert payload["error"] == "BadRequest"

    def test_请求体超限400(self, server):
        """**只发头不发体**：服务端看 `Content-Length` 就该拒。

        如果测试真把 1MB 发出去再读响应，Windows 会因为「服务端提前关连接」
        抛 `ConnectionReset`，让人误以为是服务端 bug。
        """
        token = login(server)
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        try:
            head = (
                "POST /api/orders HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {MAX_BODY_BYTES + 10}\r\n\r\n"
            )
            sock.sendall(head.encode("latin-1"))
            # `recv()` 只保证「至少一个字节」：头和体可能分两次到，
            # 只读一次就断言 body 内容是偶发失败的经典写法。
            chunks = []
            for _ in range(6):
                part = sock.recv(4096)
                if not part:
                    break
                chunks.append(part)
                if b"\r\n\r\n" in b"".join(chunks).split(b"\r\n\r\n", 1)[1]:
                    break
            raw = b"".join(chunks).decode("utf-8", "replace")
        finally:
            sock.close()
        assert "400" in raw.splitlines()[0]
        assert "上限" in raw

    def test_json数组被拒(self, server):
        token = login(server)
        status, _h, payload = request(server, "/api/orders", method="POST",
                                      body=b"[1,2,3]", token=token)
        assert status == 400
        assert "JSON 对象" in payload["detail"]

    def test_OPTIONS预检(self, server):
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        try:
            sock.sendall(b"OPTIONS /api/orders HTTP/1.1\r\nHost: x\r\n\r\n")
            head = sock.recv(200).decode("latin-1")
        finally:
            sock.close()
        assert "204" in head.splitlines()[0]


# =========================================================== 静态
class TestStatic:
    def test_根路径给index(self, server):
        status, headers, body = request(server, "/")
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert "<div id=app>" in body
        assert "no-cache" in headers["Cache-Control"]

    def test_带hash的assets长缓存(self, server):
        status, headers, body = request(server, "/assets/app-abc123.js")
        assert status == 200
        assert "javascript" in headers["Content-Type"]
        assert "immutable" in headers["Cache-Control"]

    def test_前端路由回落到index(self, server):
        """刷新 `/kpi` 必须还能拿到 SPA 壳，否则一刷新就 404。"""
        status, _headers, body = request(server, "/kpi")
        assert status == 200
        assert "<div id=app>" in body

    def test_路径穿越被拦(self, server):
        for path in ("/../secret.txt", "/..%2Fsecret.txt", "/assets/../../secret.txt"):
            status, _headers, payload = request(server, path)
            assert status == 404, f"{path} 不该被读到"
            assert "不该被读到" not in json.dumps(payload, ensure_ascii=False)

    def test_不存在的静态文件404(self, server):
        status, _h, payload = request(server, "/nope.css")
        assert status == 404
        assert payload["error"] == "not_found"

    # -------------------------------------------------------------- 压缩
    def test_大文件按需gzip(self, server, dist):
        """声明接受 gzip 时发压缩字节，且解出来必须和原文件逐字节一致。"""
        origin = "console.log('压缩测试');" * 200      # 约 5KB，过阈值
        target = dist / "assets" / "big-abc999.js"
        target.write_text(origin, encoding="utf-8")

        status, headers, body = fetch_raw(
            server, "/assets/big-abc999.js", accept_encoding="gzip, deflate")
        assert status == 200
        assert headers.get("Content-Encoding") == "gzip"
        assert "Accept-Encoding" in headers.get("Vary", "")
        # 长度头必须是压缩后的长度，否则浏览器会按原始长度截断/挂起
        assert int(headers["Content-Length"]) == len(body)
        assert len(body) < len(origin.encode("utf-8"))
        assert gzip.decompress(body).decode("utf-8") == origin

    def test_不声明接受时不压(self, server, dist):
        """没带 Accept-Encoding（curl 默认、老客户端）必须拿到原始字节。"""
        origin = "console.log('x');" * 200
        (dist / "assets" / "plain-abc999.js").write_text(origin, encoding="utf-8")

        _status, headers, body = fetch_raw(server, "/assets/plain-abc999.js")
        assert "Content-Encoding" not in headers
        assert int(headers["Content-Length"]) == len(body)
        assert body.decode("utf-8") == origin

    def test_gzip_q0视为拒绝(self, server, dist):
        """`gzip;q=0` 是明确拒绝。按「包含 gzip」判断会把乱码发给客户端。"""
        origin = "console.log('x');" * 200
        (dist / "assets" / "noq-abc999.js").write_text(origin, encoding="utf-8")

        _status, headers, body = fetch_raw(
            server, "/assets/noq-abc999.js", accept_encoding="gzip;q=0, identity")
        assert "Content-Encoding" not in headers
        assert body.decode("utf-8") == origin

    def test_小文件不压(self, server):
        """index.html 只有几十字节，压完更大，不该压。"""
        _status, headers, _body = fetch_raw(server, "/", accept_encoding="gzip")
        assert "Content-Encoding" not in headers


# =========================================================== SSE
class TestStream:
    def test_没有令牌401(self, server):
        status, _h, payload = request(server, "/api/stream")
        assert status == 401
        assert "token" in payload["detail"]

    def test_读到hello帧(self, server):
        token = login(server)
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/stream?token={token}"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert "text/event-stream" in resp.headers.get("Content-Type", "")
            chunk = resp.read(400).decode("utf-8")
        assert "retry: 3000" in chunk
        assert "event: hello" in chunk
        assert '"device_id"' in chunk

    def test_增量事件能推下来(self, server):
        token = login(server)
        state = server.state
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/stream?token={token}"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            # 逐行读而不是 `read(n)`：SSE 帧长度不定，`read(200)` 会一直等满
            # 200 字节，而下一批数据可能 0.25 秒后才来、也可能要等心跳。
            resp.readline()         # retry: 3000
            resp.readline()         # 空行
            resp.readline()         # id: N
            resp.readline()         # event: hello
            resp.readline()         # data: {...}
            resp.readline()         # 空行

            state.tick({"gas_level": 0.9, "t": 24.0, "h": 50.0, "hood_temp": 60.0,
                        "sterilize_temp": 40.0, "fridge_temp": 4.0,
                        "freezer_temp": -18.0, "sample_temp": 3.0,
                        "sct_current": 0.5, "weight_g": 100.0,
                        "flame_a": 0.0, "flame_b": 0.0})
            state.tick({"gas_level": 0.92, "t": 24.0, "h": 50.0, "hood_temp": 60.0,
                        "sterilize_temp": 40.0, "fridge_temp": 4.0,
                        "freezer_temp": -18.0, "sample_temp": 3.0,
                        "sct_current": 0.5, "weight_g": 100.0,
                        "flame_a": 0.0, "flame_b": 0.0})
            chunk = ""
            for _ in range(20):
                line = resp.readline().decode("utf-8")
                if not line:
                    break
                chunk += line
                if '"guard' in line or '"rule' in line:
                    break
        assert ("guard" in chunk) or ("rule" in chunk), chunk

    def test_并发上限503(self, tmp_path):
        """SSE 一条连接一个线程，不设上限就是线程泄漏。"""
        state = make_state(tmp_path)
        api = ApiServer(state, port=0, secret="sse-limit", sse_limit=1)
        api.start()
        try:
            token = login(api)
            url = f"http://127.0.0.1:{api.port}/api/stream?token={token}"
            first = urllib.request.urlopen(urllib.request.Request(url), timeout=10)
            try:
                first.read(120)     # 占住那唯一一个名额
                req = urllib.request.Request(url)
                with pytest.raises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(req, timeout=5).read(50)
                assert exc.value.code == 503
                payload = json.loads(exc.value.read() or b"{}")
                assert payload["error"] == "TooManyStreams"
            finally:
                first.close()
        finally:
            api.stop()
            state.stop()

    def test_默认上限是8(self):
        assert MAX_SSE_CLIENTS == 8


# =========================================================== 组合
class TestWiring:
    def test_后台线程真的在采样(self, tmp_path):
        """`start()` 起的是**真线程**：不采样的话页面数据永远停在启动那一刻。"""
        import time

        state = make_state(tmp_path)
        state.config.tick_interval = 0.05
        api = ApiServer(state, port=0, secret="wiring")
        state.start()
        try:
            time.sleep(0.35)
            assert state.ticks >= 2
            assert state.telemetry.latest()["t"]["stale"] is False
        finally:
            state.stop()

    def test_重复start不会起第二个线程(self, tmp_path):
        state = make_state(tmp_path)
        state.start()
        thread = state._thread
        state.start()
        assert state._thread is thread
        state.stop()

    def test_端口冲突时报错而不是静默(self, tmp_path, server):
        """同一个端口被占要抛异常：静默换端口会让前端连到别人的服务上。"""
        state = make_state(tmp_path)
        with pytest.raises(OSError):
            ApiServer(state, port=server.port, secret="dup")
        state.stop()

    def test_没有静态目录时只提供API(self, tmp_path):
        state = make_state(tmp_path)
        api = ApiServer(state, port=0, secret="no-static")
        api.start()
        try:
            status, _h, payload = request(api, "/")
            assert status == 404
            assert payload["error"] == "not_found"
        finally:
            api.stop()
            state.stop()


class Test客户端断开:
    """浏览器关标签页 / 刷新会让 keep-alive 连接直接 RST。

    标准库把这件事当「处理请求时发生异常」，打一整段 traceback（实测一次
    演示 50 段），把真正的错误淹掉。这类异常对服务没有任何影响，必须降级成
    debug；但**其他异常仍要照常上报**，不能顺手把真错误一起吃掉。
    """

    def test_断开异常只记debug(self, server, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="kitchen.api.server"):
            try:
                raise ConnectionResetError(
                    10054, "远程主机强迫关闭了一个现有的连接")
            except ConnectionResetError:
                server._httpd.handle_error(None, ("127.0.0.1", 5000))
        rows = [r for r in caplog.records if r.name == "kitchen.api.server"]
        assert rows, "断开也要留一条 debug 痕迹，否则排查时看不出发生过"
        assert all(r.levelno == logging.DEBUG for r in rows)

    def test_其他异常仍然打堆栈(self, server, capsys):
        try:
            raise ValueError("真错误")
        except ValueError:
            server._httpd.handle_error(None, ("127.0.0.1", 5000))
        assert "Exception occurred" in capsys.readouterr().err
