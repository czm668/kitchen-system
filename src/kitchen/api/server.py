# -*- coding: utf-8 -*-
"""HTTP 服务：标准库 `ThreadingHTTPServer` + REST + SSE + 静态托管。

## 这个文件只管 IO，不管业务

路由、鉴权、业务判定全在 `routes.py` / `actions.py` / `views.py`。这里负责
四件脏活：解析请求、认令牌、发 SSE、发静态文件。分开的好处是**业务逻辑可以
完全脱离 HTTP 单测**（`Router.call()` 直接吃 dict），不用起服务器。

## 六个必须处理的现场问题

1. **SSE 长连接会吃线程**。`ThreadingHTTPServer` 一个连接一个线程，浏览器开
   六个标签页就是六个常驻线程。所以限制 `MAX_SSE_CLIENTS`，超了返回 503 并在
   响应里告诉前端「改用轮询」。不限制的话，内存与线程数会随停留时间线性增长。
2. **SSE 要有寿命上限**。`SSE_MAX_SEC` 到点主动断开，让浏览器重连。这样
   任何一次客户端半死（拔网线、休眠）留下的线程最多活 5 分钟。
3. **`EventSource` 不能带自定义头**，所以令牌必须能从 `?token=` 读。
   只支持 Bearer 头的话，SSE 这条路直接不通。
4. **静态文件要防路径穿越**。`/../../etc/passwd` 必须被拦。用 `resolve()` +
   `relative_to(root)` 判定，不用字符串 `startswith`（大小写、`..`、符号链接
   都能绕过字符串判断）。
5. **请求体要有上限**。没有上限时一个 `Content-Length: 999999999` 就能把内存
   吃穿。1MB 够一份阈值配置。
6. **写响应要 `no-store`**。浏览器缓存一个「复位成功」，用户点十次看起来只
   生效一次。

## 启动

    python -m kitchen.api.server --port 8848 --static web/dist

不带 `--static` 时只提供 API（前端用 `vite dev` 的 proxy 连过来）。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .accounts import AccountStore, LoginService
from .auth import AuthError, Session, TokenInvalid, TokenSigner
from .routes import Router
from .sse import SSE_HEADERS, format_event, hello_frame, ping_frame, retry_frame

log = logging.getLogger("kitchen.api.server")

__all__ = ["ApiServer", "create_server", "MAX_BODY_BYTES", "MAX_SSE_CLIENTS"]

MAX_BODY_BYTES = 1 << 20          # 1MB
MAX_SSE_CLIENTS = 8
SSE_MAX_SEC = 300.0               # 单条 SSE 连接寿命上限
SSE_POLL_SEC = 0.25               # 事件轮询间隔（0.25s 足够「实时」，CPU 几乎为零）
SSE_PING_SEC = 15.0
#: 令牌在 URL 里出现时不要进访问日志（日志会被截图进论文，泄漏令牌很蠢）
_REDACT_KEYS = ("token", "password", "card_uid")


class _HttpServer(ThreadingHTTPServer):
    """带一个 Windows 专属修正的 HTTP 服务。

    **Windows 上 `SO_REUSEADDR` 的语义和 Unix 不同**：它允许第二个 socket
    「抢绑」一个已经在监听的端口，绑的时候不报错，之后连接被系统分给其中一个。
    现场表现是「新版本起在 8848 上，但请求有时打到旧进程」。

    Unix 上相反：关掉 reuse 会让「重启服务」撞上 TIME_WAIT 而失败。所以按平台
    分开设置——这不是可选项，是两种系统上正确性要求正好相反。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"


class _SseBudget:
    """SSE 并发计数。用信号量语义（拿不到就拒绝），不用队列排队。"""

    def __init__(self, limit: int = MAX_SSE_CLIENTS) -> None:
        self.limit = int(limit)
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


class ApiServer:
    """把 state + router 装进一个可启动/可停止的 HTTP 服务。"""

    def __init__(
        self,
        state,
        *,
        host: str = "127.0.0.1",
        port: int = 8848,
        static_dir: str | Path | None = None,
        secret: str | None = None,
        token_ttl_sec: float = 8 * 3600,
        allow_origin: str = "",
        sse_limit: int = MAX_SSE_CLIENTS,
        trust_proxy_headers: bool = False,
        httpd_cls=_HttpServer,
    ) -> None:
        self.state = state
        # 登录限流的来源键取自这里（A5）：默认**只信 TCP 对端地址**——
        # X-Forwarded-For 是请求头，客户端可以随便填；只有在可信反代
        # （nginx 会覆盖/追加 XFF）后面才打开 trust_proxy_headers，
        # 否则攻击者伪造 XFF 每次换一个键，登录锁定形同虚设。
        self.trust_proxy_headers = bool(trust_proxy_headers)
        # 密钥：显式传入 > 环境变量 > 进程启动时随机生成（重启后令牌全部失效）
        secret = secret or os.environ.get("KITCHEN_API_SECRET", "")
        self.secret = secret or f"ephemeral-{os.getpid()}-{int(time.time())}"
        if not secret:
            log.warning(
                "未设置 KITCHEN_API_SECRET，使用进程级临时密钥；"
                "重启后所有令牌失效（多实例部署会互相不认令牌）"
            )
        self.signer = TokenSigner(self.secret, ttl_sec=token_ttl_sec)
        self.accounts = AccountStore(self.signer)
        self.login = LoginService(self.signer, self.accounts)
        self.router = Router(state, login=self.login)
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.allow_origin = str(allow_origin or "")
        self.sse_budget = _SseBudget(sse_limit)
        self._httpd = httpd_cls((host, int(port)), _make_handler(self))
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期
    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def port(self) -> int:
        return self.address[1]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.2)

    def start(self) -> "ApiServer":
        """后台线程起服务（便于演示脚本里同一进程既跑 tick 又跑 API）。"""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self.serve_forever, name="kitchen-api", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ------------------------------------------------------------ 静态文件
    def resolve_static(self, path: str) -> Path | None:
        """把 URL 路径映射到磁盘文件。**穿越防护在这里**。"""
        if self.static_dir is None:
            return None
        rel = unquote(path).lstrip("/")
        if not rel or rel.endswith("/"):
            rel = (rel + "index.html").lstrip("/")
        candidate = (self.static_dir / rel).resolve()
        try:
            candidate.relative_to(self.static_dir)
        except ValueError:
            return None  # 穿越尝试：当作 404，不回 403（不给探测者信息）
        if not candidate.is_file():
            return None
        return candidate

    def index_file(self) -> Path | None:
        if self.static_dir is None:
            return None
        for name in ("index.html",):
            path = self.static_dir / name
            if path.is_file():
                return path
        return None


def _make_handler(server: ApiServer):
    """生成 handler 类（闭包持有 server，避免全局变量）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "kitchen-api/0.1"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------- 工具
        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), _redact(fmt % args))

        def _client_ip(self) -> str:
            # XFF 只在可信反代后面才认（A5）：默认直连场景下攻击者可以
            # 伪造这个头绕过登录限流，所以必须 server 开关控制。
            if server.trust_proxy_headers:
                forwarded = self.headers.get("X-Forwarded-For", "")
                if forwarded:
                    return forwarded.split(",")[0].strip()
            return self.client_address[0] if self.client_address else ""

        def _cors(self) -> None:
            if not server.allow_origin:
                return
            self.send_header("Access-Control-Allow-Origin", server.allow_origin)
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

        def _send_json(self, status: int, payload: dict, *, cache: bool = False) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",
                             "public, max-age=60" if cache else "no-store")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("Content-Length 不是整数") from None
            if length > MAX_BODY_BYTES:
                raise ValueError(f"请求体超过 {MAX_BODY_BYTES // 1024}KB 上限")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return parsed

        def _token(self, query: dict[str, list[str]]) -> str:
            header = self.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            # SSE（EventSource）不能自定义头，只能走 query
            values = query.get("token") or []
            return values[0] if values else ""

        def _session(self, query: dict[str, list[str]]) -> Session | None:
            token = self._token(query)
            if not token:
                return None
            try:
                return server.signer.verify(token)
            except AuthError:
                return None

        # ---------------------------------------------------- 动词
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/api/stream":
                self._stream(query)
                return
            # A9-05 溯源标签：唯一的二进制端点。绕过 Router 的 JSON 包装，
            # 直接从 TraceService 拿 PNG 字节（匿名：标签本身就要让顾客扫）
            if parsed.path == "/api/customer/trace/label.png":
                self._trace_label(query)
                return
            if parsed.path.startswith("/api/"):
                session = self._session(query)
                data = {key: values[0] if len(values) == 1 else values
                        for key, values in query.items() if key != "token"}
                status, payload = server.router.call(
                    "GET", parsed.path, data=data, session=session
                )
                self._send_json(status, payload)
                return
            self._static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            self._mutate("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._mutate("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._mutate("DELETE")

        def _mutate(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                body = self._read_body()
            except ValueError as exc:
                # body 没读完就回错误：这条连接上还堆着未消费的字节，
                # 继续复用会被当成下一个请求行解析。所以明确关掉它。
                self.close_connection = True
                self._send_json(400, {"ok": False, "error": "BadRequest",
                                      "detail": str(exc)})
                return
            # source 是**服务端鉴定的限流键**（A5）：强制覆盖，不让客户端
            # 传值——setdefault 的方向反了的话，攻击者在 body 里每次换一个
            # source 就能绕过登录失败锁定。
            body["source"] = self._client_ip()
            session = self._session(query)
            status, payload = server.router.call(
                method, parsed.path, data=body, session=session
            )
            self._send_json(status, payload)

        # ---------------------------------------------------- 溯源标签 PNG
        def _trace_label(self, query: dict[str, list[str]]) -> None:
            code = (query.get("code") or [""])[0].strip()
            if not code:
                # label_png 对空码会生成一张「指向空码」的图（不报错），
                # 所以空值必须在这里前置拦掉，否则调试时拿到一张能扫的废码
                self._send_json(400, {"ok": False, "error": "BadRequest",
                                      "detail": "缺 ?code= 参数"})
                return
            try:
                raw = server.state.trace.label_png(code)
            except Exception as exc:  # noqa: BLE001
                from .routes import http_status

                status = http_status(exc)
                self._send_json(status, {"ok": False,
                                         "error": type(exc).__name__,
                                         "detail": str(exc),
                                         "status": status})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(raw)))
            # 标签图不变（码即内容），可以缓存；但演示里改 url_template 会重贴
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        # ---------------------------------------------------- 静态
        def _static(self, path: str) -> None:
            target = server.resolve_static(path)
            if target is None:
                # SPA fallback：刷新 /kpi 这类前端路由要能回到 index.html
                target = server.index_file() if "." not in Path(path).name else None
            if target is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": f"没有这个路径：{path}"})
                return
            try:
                raw = target.read_bytes()
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": "io",
                                      "detail": str(exc)})
                return
            guess, _ = mimetypes.guess_type(str(target))
            ctype = guess or "application/octet-stream"
            if ctype.startswith("text/") or ctype in (
                "application/javascript", "application/json"
            ):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            # 带 hash 的 assets 长缓存；index.html 不缓存（否则发版后加载旧壳）
            hashed = "assets" in target.parts
            self.send_header("Cache-Control",
                             "public, max-age=31536000, immutable" if hashed
                             else "no-cache")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        # ---------------------------------------------------- SSE
        def _stream(self, query: dict[str, list[str]]) -> None:
            session = self._session(query)
            if session is None:
                self._send_json(401, {"ok": False, "error": "TokenInvalid",
                                      "detail": "SSE 需要 ?token=<登录令牌>"})
                return
            if not server.sse_budget.acquire():
                self._send_json(503, {
                    "ok": False, "error": "TooManyStreams",
                    "detail": f"实时通道已满（{server.sse_budget.limit} 条），"
                              f"请改用 GET /api/overview 轮询",
                })
                return
            try:
                try:
                    since = int((query.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                self._run_stream(since)
            finally:
                server.sse_budget.release()

        def _run_stream(self, since: int) -> None:
            try:
                self.send_response(200)
                for key, value in SSE_HEADERS:
                    self.send_header(key, value)
                self._cors()
                self.end_headers()
                self.wfile.write(retry_frame(3000))
                self.wfile.write(hello_frame(server.state))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

            started = time.monotonic()
            last_ping = started
            while True:
                if time.monotonic() - started > SSE_MAX_SEC:
                    # 主动断开，让浏览器重连：任何一次半死连接的线程都不会长寿
                    try:
                        self.wfile.write(format_event("bye", {"reason": "rotate"}))
                        self.wfile.flush()
                    except OSError:
                        pass
                    return
                rows = server.state.events(since=since, limit=200)
                try:
                    for row in rows:
                        since = max(since, int(row["seq"]))
                        self.wfile.write(format_event(
                            str(row.get("kind") or "event"), row,
                            event_id=int(row["seq"]),
                        ))
                    if rows:
                        self.wfile.flush()
                    elif time.monotonic() - last_ping >= SSE_PING_SEC:
                        self.wfile.write(ping_frame())
                        self.wfile.flush()
                        last_ping = time.monotonic()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(SSE_POLL_SEC)

    return Handler


def _redact(text: str) -> str:
    for key in _REDACT_KEYS:
        if key not in text:
            continue
        parts = []
        for chunk in text.split("&"):
            if chunk.startswith(key + "="):
                parts.append(f"{key}=***")
            else:
                parts.append(chunk)
        text = "&".join(parts)
    return text


def create_server(
    state,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    static_dir: str | Path | None = None,
    secret: str | None = None,
    allow_origin: str = "",
    trust_proxy_headers: bool = False,
) -> ApiServer:
    """建一个服务（`port=0` 自动选空闲端口，单测用）。"""
    return ApiServer(state, host=host, port=port, static_dir=static_dir,
                     secret=secret or "test-secret", allow_origin=allow_origin,
                     trust_proxy_headers=trust_proxy_headers)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 入口
    import argparse

    from .state import AppState, AppStateConfig

    parser = argparse.ArgumentParser(description="后厨管理端 API 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--static", default="web/dist", help="前端构建产物目录")
    parser.add_argument("--var", default="var/api", help="落盘目录（队列/审计）")
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help="演示加速：1 真实秒 = N 演示秒")
    parser.add_argument("--offline", action="store_true", help="以离线模式启动")
    parser.add_argument("--allow-origin", default="", help="开发期 CORS 白名单")
    parser.add_argument("--no-seed", action="store_true", help="不造演示订单")
    parser.add_argument("--db", default="",
                        help="运行时库路径（A1；空 = 纯内存，如 var/runtime.db）")
    parser.add_argument("--schedule", action="store_true",
                        help="开启定时任务托管（A3：留样到期扫描 + 证件每日扫描）")
    parser.add_argument("--secret", default="",
                        help="令牌签名密钥（A5；也可用 KITCHEN_API_SECRET 环境变量）")
    parser.add_argument("--trust-proxy", action="store_true",
                        help="仅当部署在可信反代（nginx 会覆盖 X-Forwarded-For）后时开启，"
                             "登录限流才按转发链首地址取来源；直连场景务必关闭（默认）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = Path(__file__).resolve().parents[3]
    static = (root / args.static) if not os.path.isabs(args.static) else Path(args.static)
    config = AppStateConfig(
        var_dir=Path(args.var),
        time_scale=args.time_scale,
        db_path=Path(args.db) if args.db else None,
        scheduler_enabled=args.schedule,
    )
    state = AppState(
        config=config,
        online=not args.offline,
    )
    if not args.no_seed:
        state.demo.seed_orders()
    state.start()

    server = ApiServer(
        state, host=args.host, port=args.port,
        static_dir=static if static.is_dir() else None,
        allow_origin=args.allow_origin,
        secret=args.secret or None,
        trust_proxy_headers=args.trust_proxy,
    )
    host, port = server.address
    log.info("管理端已启动：%s（静态目录：%s）", server.url,
             static if static.is_dir() else "未提供，仅 API")
    log.info("演示账号：admin / lead01 / chef01 / guest，默认口令 kitchen2026")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，正在停止")
    finally:
        server.stop()
        state.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
