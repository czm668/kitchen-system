# -*- coding: utf-8 -*-
"""HTTP 服务：标准库 `ThreadingHTTPServer` + REST + SSE + 静态托管。

## 这个文件只管 IO，不管业务

路由、鉴权、业务判定全在 `routes.py` / `actions.py` / `views.py`。这里负责
四件脏活：解析请求、认令牌、发 SSE、发静态文件。分开的好处是**业务逻辑可以
完全脱离 HTTP 单测**（`Router.call()` 直接吃 dict），不用起服务器。

## 七个必须处理的现场问题

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
7. **静态资源要压缩**。用 `python -m http.server` 那套裸实现发 dist 时，浏览器
   拿到的是未压缩字节：实测首屏 index.js 257KB + echarts 543KB + css 60KB，
   合计 860KB **原样过网**。现场 wifi 通常 2~5Mbps，这就是 1.4~3.4 秒纯下载。
   gzip 后同样三个文件 279KB，压掉 67%。压缩结果按 `(路径, mtime, 大小)` 缓存，
   不重复压同一个文件。

8. **客户端断开不是错误**。浏览器关标签页、按 F5、切地铁隧道，keep-alive 连接
   会被直接 RST，标准库在 `handle_one_request` 的 `readline` 上抛
   `ConnectionResetError`，然后 `socketserver` 打一整段 traceback（实测一次演示
   50 段）。这不影响服务，但后台刷屏会把真正的错误淹掉。`_HttpServer.handle_error`
   只静默「对端已断开」这一类，其余异常照常上报。

## 启动

    python -m kitchen.api.server --port 8848 --static web/dist

不带 `--static` 时只提供 API（前端用 `vite dev` 的 proxy 连过来）。
"""
from __future__ import annotations

import gzip
import json
import logging
import mimetypes
import os
import socket
import sys
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

# ---------------------------------------------------------------- 静态压缩
# 小于这个阈值不压：gzip 头加字典本身就要 18 字节左右，几百字节的响应压缩后
# 反而更大，还多一次 CPU。
GZIP_MIN_BYTES = 1024
# 压缩级别取 6 而不是默认的 9：实测这三个产物 6 与 9 只差 1~2%，耗时却差 3 倍。
# 演示环境是单核小机器，宁可少压 2% 也不要让首个请求卡住。
GZIP_LEVEL = 6
# 缓存条数上限。产物只有几个，这个上限只是防止长时间开发反复重建把内存撑大。
GZIP_CACHE_MAX = 64
# 值得压的类型。图片/字体已经是压缩格式，再压一遍只会变大。
_GZIP_TYPES = frozenset({
    "application/javascript", "application/json", "application/xml",
    "image/svg+xml", "application/manifest+json",
})


def _compressible(ctype: str) -> bool:
    base = ctype.split(";", 1)[0].strip().lower()
    return base.startswith("text/") or base in _GZIP_TYPES


def _accepts_gzip(header: str | None) -> bool:
    """`Accept-Encoding` 里是否真的接受 gzip。

    不能只判 `"gzip" in header`：`gzip;q=0` 是**明确拒绝**，按包含判断会
    给一个声明不要压缩的客户端发 gzip，它只能解出乱码。
    """
    if not header:
        return False
    for part in header.split(","):
        name, _, params = part.partition(";")
        if name.strip().lower() != "gzip":
            continue
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    return float(value) > 0
                except ValueError:
                    return True
        return True
    return False
SSE_PING_SEC = 15.0
#: 令牌在 URL 里出现时不要进访问日志（日志会被截图进论文，泄漏令牌很蠢）
_REDACT_KEYS = ("token", "password", "card_uid")

#: 绕过 Router JSON 包装的二进制/SSE 端点（审查报告 §3）：以前注释写"唯一"，
#: 实际有 4 个且散落在 do_GET/do_POST 里，新人不知道新增该走哪条路。
#: 新增端点只在这里加一行；值 = (Handler 方法名, 是否把 query 传进去)。
#: 语义：都在 `if path.startswith("/api/")` 之前命中，且都不走登录态。
_BINARY_ENDPOINTS: dict[tuple[str, str], tuple[str, bool]] = {
    ("GET", "/api/stream"): ("_stream", True),
    ("GET", "/api/customer/trace/label.png"): ("_trace_label", True),
    ("GET", "/api/customer/guest/evidence-image"): ("_evidence_image", True),
    ("POST", "/api/customer/guest/evidence-upload"): ("_evidence_upload", False),
}


class _HttpServer(ThreadingHTTPServer):
    """带一个 Windows 专属修正的 HTTP 服务。

    **Windows 上 `SO_REUSEADDR` 的语义和 Unix 不同**：它允许第二个 socket
    「抢绑」一个已经在监听的端口，绑的时候不报错，之后连接被系统分给其中一个。
    现场表现是「新版本起在 8848 上，但请求有时打到旧进程」。

    Unix 上相反：关掉 reuse 会让「重启服务」撞上 TIME_WAIT 而失败。所以按平台
    分开设置——这不是可选项，是两种系统上正确性要求正好相反。

    **另外接管 `handle_error` 的日志**：客户端正常断开（关标签页、刷新）时
    keep-alive 连接的 `readline` 会抛 `ConnectionResetError`，标准库把它当
    「处理请求时发生异常」打一整段 traceback。演示一小时能刷出几十屏，真正
    要看的那条错误就淹在里面了。这类异常对服务没有任何影响，降级成 debug。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    #: 对端已断开的三类异常。Windows 上是 WinError 10054（ConnectionReset），
    #: Linux 上常见的是 BrokenPipe / ConnectionAborted。
    _DISCONNECTS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, self._DISCONNECTS):
            peer = client_address[0] if client_address else "-"
            log.debug("客户端已断开（%s）：连接回收，不计错误", peer)
            return
        super().handle_error(request, client_address)


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
        # 压缩结果缓存：key = (绝对路径, mtime_ns, 大小)。用 mtime 参与 key，
        # 重新构建后 hash 变、mtime 也变，天然失效，不需要手动清缓存。
        self._gzip_cache: dict[tuple[str, int, int], bytes] = {}
        self._gzip_lock = threading.Lock()
        # 客诉图片证据落盘目录：优先用 state 的 var_dir，退化到系统临时目录。
        from ..guest.uploads import ImageStore
        var_dir = getattr(getattr(state, "config", None), "var_dir", None)
        upload_root = (Path(var_dir) / "uploads") if var_dir else (
            Path(__import__("tempfile").gettempdir()) / "kitchen-uploads")
        self.images = ImageStore(upload_root)
        self.allow_origin = str(allow_origin or "")
        self.sse_budget = _SseBudget(sse_limit)
        self._httpd = httpd_cls((host, int(port)), _make_handler(self))
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 静态压缩
    def gzipped(self, target: Path, raw: bytes) -> bytes:
        """返回 `raw` 的 gzip 结果，按文件状态缓存。

        首次请求压一次、之后直接命中内存。对已经带 content-hash 的 assets
        来说，一个进程生命周期内每个文件只会压一次。
        """
        try:
            stat = target.stat()
            key = (str(target), stat.st_mtime_ns, stat.st_size)
        except OSError:
            # 文件刚好被删/换掉：压完就返回，不进缓存
            return gzip.compress(raw, GZIP_LEVEL)
        with self._gzip_lock:
            hit = self._gzip_cache.get(key)
        if hit is not None:
            return hit
        packed = gzip.compress(raw, GZIP_LEVEL)
        with self._gzip_lock:
            if len(self._gzip_cache) >= GZIP_CACHE_MAX:
                self._gzip_cache.clear()
            self._gzip_cache[key] = packed
        return packed

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
            # 二进制 / SSE 端点统一在 _BINARY_ENDPOINTS 注册，绕过 Router 的
            # JSON 包装（溯源 PNG、客诉图片、事件流都不是 JSON）。匿名可达。
            binary = _BINARY_ENDPOINTS.get(("GET", parsed.path))
            if binary is not None:
                handler, with_query = binary
                getattr(self, handler)(query) if with_query else getattr(self, handler)()
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
            # 二进制 body 上传同样从 _BINARY_ENDPOINTS 注册，不走 JSON Router。
            binary = _BINARY_ENDPOINTS.get(("POST", urlparse(self.path).path))
            if binary is not None:
                handler, with_query = binary
                if with_query:
                    getattr(self, handler)(parse_qs(urlparse(self.path).query))
                else:
                    getattr(self, handler)()
                return
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

        # ---------------------------------------------------- 客诉图片证据
        def _evidence_upload(self) -> None:
            """上传图片证据：顾客令牌 + guest_key 双因子校验。

            兼容两种 body：小程序 `wx.uploadFile` 的 multipart/form-data
            （取 `image` 字段），以及脚本/测试发的裸二进制 octet-stream。
            """
            from ..guest.uploads import (
                MAX_UPLOAD_BYTES, UploadError, extract_multipart_file,
            )

            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            token = (query.get("customer_token") or query.get("token") or [""])[0]
            guest_key = (query.get("guest_key") or [""])[0]
            # 也认请求头：小程序 wx.uploadFile 的 formData 走 multipart，
            # 令牌放 header 就不必依赖 multipart 解析就能鉴权
            token = self.headers.get("X-Customer-Token", "") or token
            guest_key = self.headers.get("X-Guest-Key", "") or guest_key
            try:
                server.state.customer.require_guest(token, guest_key)
                length = int(self.headers.get("Content-Length") or 0)
                # multipart 有 boundary/头部开销，放宽一点上限；真正落盘前
                # ImageStore.save 仍按 2MB 校验图片本体
                if length <= 0 or length > MAX_UPLOAD_BYTES + 64 * 1024:
                    raise UploadError("图片为空或超过 2MB 上限")
                raw = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                if content_type.lower().startswith("multipart/form-data"):
                    raw = extract_multipart_file(raw, content_type, field="image")
                url = server.images.save(raw)
            except UploadError as exc:
                self._send_json(400, {"ok": False, "error": "BadRequest",
                                      "detail": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - 令牌无效等统一 400
                self._send_json(400, {"ok": False, "error": "Unauthorized",
                                      "detail": str(exc)[:120]})
                return
            self._send_json(200, {"ok": True, "url": url})

        def _evidence_image(self, query: dict[str, list[str]]) -> None:
            name = (query.get("name") or [""])[0]
            found = server.images.read(name)
            if found is None:
                self._send_json(404, {"ok": False, "error": "not_found",
                                      "detail": "图片不存在"})
                return
            raw, ext = found
            ctype = {"jpg": "image/jpeg", "png": "image/png",
                     "webp": "image/webp", "gif": "image/gif"}.get(ext,
                                                                    "image/jpeg")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=86400")
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
            # 压缩判据三件：类型值得压、体积够大、客户端确实接受。
            # 三者任一不满足就发原始字节——发错编码比不压缩严重得多。
            compressible = _compressible(ctype)
            body = raw
            encoded = False
            if (compressible and len(raw) >= GZIP_MIN_BYTES
                    and _accepts_gzip(self.headers.get("Accept-Encoding"))):
                body = server.gzipped(target, raw)
                encoded = True
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if encoded:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            # 同一个 URL 对不同 Accept-Encoding 会给出不同字节，必须声明 Vary，
            # 否则中间的共享缓存可能把 gzip 版本发给不支持它的客户端。
            if compressible:
                self.send_header("Vary", "Accept-Encoding")
            # 带 hash 的 assets 长缓存；index.html 不缓存（否则发版后加载旧壳）
            hashed = "assets" in target.parts
            self.send_header("Cache-Control",
                             "public, max-age=31536000, immutable" if hashed
                             else "no-cache")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

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
    parser.add_argument("--seed-demo", action="store_true",
                        help="即使运行时库已有订单，也再播种一批演示单（仅演示用）")
    parser.add_argument("--db", default="",
                        help="运行时库路径（A1；空 = 纯内存，如 var/runtime.db）")
    parser.add_argument("--db-url", default=os.environ.get("KITCHEN_DB_URL", ""),
                        help="运行时库 URL（A18；mysql://user:pass@host:port/db）。"
                             "也可用 KITCHEN_DB_URL 环境变量；非空时优先于 --db 走 MySQL")
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
        db_url=args.db_url or None,
        scheduler_enabled=args.schedule,
    )
    state = AppState(
        config=config,
        online=not args.offline,
    )
    # 订单是内存态 + 写穿镜像：启动时内存为空、自增游标回到 1，而运行时库里
    # 还留着上次运行的行。**必须先对齐游标**，否则重启后新单会拿 1、2、3 去
    # UPSERT 覆盖历史订单（不报错、不留痕，数据静默丢失）。
    prior_orders = 0
    if state.recorder is not None:
        max_order, max_item = state.recorder.order_id_bounds()
        state.orders.align_ids(max_order, max_item)
        prior_orders = max_order
    if not args.no_seed:
        # 只在「库里还没有订单」时播种演示单（首次启动）。旧行为是每次启动都
        # 造 3 单，于是容器每重启一次就多 3 条假单——答辩当天重启几次，订单
        # 列表里全是时间对不上的脏数据，也不符合「生产系统不自己造假单」。
        # 需要时可在界面点「生成演示订单」，或用 --seed-demo 强制播种。
        if prior_orders == 0 or args.seed_demo:
            state.demo.seed_orders()
        else:
            log.info("运行时库已有 %d 条订单快照，跳过自动播种演示单"
                     "（需要就点「生成演示订单」或加 --seed-demo）", prior_orders)
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
