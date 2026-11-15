# -*- coding: utf-8 -*-
"""Server-Sent Events 帧格式（纯函数，方便单测）。

## 为什么用 SSE 而不是 WebSocket

1. **标准库没有 WS 服务端**。自己实现握手 + 掩码 + 分片，是给毕设凭空加一个
   容易出错的协议层；SSE 就是「一条不结束的 HTTP 响应」，`http.server` 直接支持。
2. **方向刚好匹配**：数据全在服务端，浏览器只需要「收」。要下命令走 POST，
   命令天然需要鉴权与审计，走 REST 比走 WS 消息更好写、更好查。
3. **断线重连是浏览器内建行为**：`EventSource` 自动重连并带 `Last-Event-ID`。
   WS 要自己写心跳 + 重连 + 补发，写漏一处就是「页面停在半小时前」。

## 帧格式的四个细节

- **每条消息 `data:` 必须是单行**：JSON 里的换行会让解析器把它当第二条消息。
  所以 `json.dumps(..., ensure_ascii=False)` 之后不能再格式化。
- **`id:` 用事件序号**：浏览器重连时会带 `Last-Event-ID`，服务端据此只补发
  增量。没有这个字段，重连就要么全量重发（曲线抖一下）要么漏一段。
- **注释行 `: ping` 做心跳**：代理与浏览器都会在长时间无数据时掐连接，
  心跳是让「没消息」和「连接死了」区分开的唯一手段。
- **`retry:` 只发一次**：重连间隔交给浏览器，服务端每次都说一遍会让退避策略
  失效（每次都重置成 3 秒，网络真挂了就是每秒重连一次）。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

__all__ = ["SSE_HEADERS", "format_event", "hello_frame", "ping_frame", "retry_frame"]

SSE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Type", "text/event-stream; charset=utf-8"),
    ("Cache-Control", "no-cache, no-store, must-revalidate"),
    ("Connection", "keep-alive"),
    # 关掉 nginx 的缓冲，否则事件会被攒成一坨再发，「实时」变成「每分钟一批」
    ("X-Accel-Buffering", "no"),
)


def hello_frame(state: Any) -> bytes:
    """连接建立时的第一帧：把「当前时刻的全景」一次性给前端。

    不给的话，前端要等下一次有事件才知道连接通了；页面会先空几秒再突然有数据。
    """
    body = {
        "device_id": getattr(state.config, "device_id", ""),
        "server_time": state.to_datetime().isoformat(timespec="seconds"),
        "event_seq": int(getattr(state, "event_seq", 0)),
        "online": bool(getattr(state, "_online", True)),
    }
    return format_event("hello", body, event_id=body["event_seq"])


def retry_frame(milliseconds: int = 3000) -> bytes:
    return f"retry: {int(milliseconds)}\n\n".encode("utf-8")


def ping_frame() -> bytes:
    """心跳：注释行，`EventSource` 会忽略内容但会刷新「连接还活着」的判断。"""
    return b": ping\n\n"


def format_event(event: str, payload: Mapping[str, Any], *, event_id: int | None = None) -> bytes:
    """一帧事件。**`data` 必须是单行 JSON**，见模块说明。"""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {int(event_id)}")
    # event 名只允许 token 字符：带空格/换行会让整帧被解析器丢掉
    safe = "".join(ch for ch in str(event) if ch.isalnum() or ch in "-_.") or "message"
    lines.append(f"event: {safe}")
    text = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    # JSON 里的字面换行只可能来自字符串内容，`json.dumps` 已转义成 `\n`；
    # 这里仍做一次防御：任何残留换行都会破坏帧边界
    text = text.replace("\r", " ").replace("\n", " ")
    lines.append(f"data: {text}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")
