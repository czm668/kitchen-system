# -*- coding: utf-8 -*-
"""A8 后厨 TFT 终端（模拟版）：行为与真机固件同构。

## 与真机的同构关系

| 模拟版 | ESP32 固件 |
|---|---|
| `DirectTransport`（进程内 router） | HTTP 客户端（WiFi + `esp_http_client`） |
| `set_link(False)` | WiFi 断开事件 |
| `LocalQueue`（jsonl 落盘） | NVS/Flash 环形缓冲 |
| `DfPlayerSim` | DFPlayer Mini（UART 指令 0x01 播曲目 N） |
| 文本帧（render.py） | LVGL 页面（design 文档 08 的映射表） |

## 离线策略（A8-05 的论文口径）

**判定永远在服务端，离线只做缓冲**：

- 在线：所有动作直接转发，服务端判定（资质/晨检/顺序/称重）；
- 离线：动作进 `LocalQueue`（jsonl 落盘，断电不丢）；刷卡做**弱校验**
  （上次在线拉的员工白名单：卡号已知 + 证有效 → 黄屏放行「恢复后复核」；
  未知卡/证无效 → 红屏拒绝——**宁可误拒不放行**，证过期的进后厨是食安事故）；
- 恢复：`sync()` 逐条出队提交，服务端照常强校验；被拒的进 `denied`
  并播报 006——师傅听到的是「补卡复核」，而不是静默丢动作。

## 为什么队列屏离线还能看

`poll()` 在线时把 `/api/orders` 的快照存进 `self.snapshot`；离线时屏
渲染**这份缓存**（不请求）。快照带时间戳，状态栏显示「离线 xx 秒」，
师傅能自己判断「这份队列还能不能信」。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Mapping

from ..resilience.queue import LocalQueue
from .audio import DfPlayerSim, announce_events
from .screens import (
    build_card_screen,
    build_status,
    build_steps_screen,
    queue_screen_from_orders,
)

log = logging.getLogger("kitchen.tft.terminal")

__all__ = ["TftTerminal", "DirectTransport"]


class DirectTransport:
    """进程内直连（测试与演示）：把 HTTP 换成 `Router.call`。

    注意登录态拿的是 `Session` 对象而不是 token 字符串——`Router.call`
    收 session；真机走 HTTP 时 transport 内部把 token 放进
    `Authorization: Bearer`。两边对 `TftTerminal` 的接口完全一致。
    """

    def __init__(self, server) -> None:
        self.server = server
        #: 登录后的会话（`Router.call` 收 Session 对象；真机 HTTP 版对应 token）
        self.session = None

    def login(self, subject: str, password: str) -> dict[str, Any]:
        _token, session = self.server.login.login(subject, password, source="tft")
        self.session = session
        return session.to_payload()

    def request(self, method: str, path: str, data: Mapping[str, Any] | None = None,
                session=None) -> tuple[int, dict[str, Any]]:
        # `Router.call` 只认纯路径：query 要在这里拆出来并进 data
        # （HTTP 版由 server 解析，两侧行为对齐）
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(str(path))
        merged = dict(data or {})
        if parsed.query:
            for key, values in parse_qs(parsed.query).items():
                merged.setdefault(key, values[0])
        return self.server.router.call(method, parsed.path, data=merged,
                                       session=session or self.session)


class TftTerminal:
    """后厨终端。持有自己的动作队列与员工白名单缓存。"""

    def __init__(
        self,
        transport: DirectTransport,
        *,
        device_id: str = "tft-node-01",
        queue_dir: str | Path | None = None,
        session=None,
        player: DfPlayerSim | None = None,
    ) -> None:
        self.transport = transport
        self.device_id = device_id
        self.session = session
        self.player = player or DfPlayerSim()
        self.link = True
        self.offline_since: float | None = None
        #: 最近一次在线拉到的订单快照（离线队列屏渲染它）
        self.snapshot: dict[str, Any] = {}
        self.snapshot_at: float | None = None
        #: 员工白名单缓存（离线刷卡弱校验）：card → row
        self.admission_cache: dict[str, dict[str, Any]] = {}
        #: 服务端事件流水位（增量拉事件驱动播报）
        self.event_watermark = 0
        #: 恢复同步时被服务端拒绝的动作（detail 给屏显示）
        self.denied: list[dict[str, Any]] = []
        queue_path = (Path(queue_dir) / "tft-actions.jsonl"
                      if queue_dir is not None else
                      Path(".local/tmp/tft-actions/tft-actions.jsonl"))
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue = LocalQueue(
            queue_path, device_id=device_id, capacity=500,
            wall_clock=time.time,
        )

    # ------------------------------------------------------------ 登录/轮询
    def login(self, subject: str, password: str) -> None:
        """登录并把会话绑到终端（transport 内部也留一份给 request 用）。"""
        self.transport.login(subject, password)
        self.session = self.transport.session

    def bind_session(self, session) -> None:
        """进程内直连模式：绑定已构造的 Session（测试省登录流程）。"""
        self.session = session

    def request(self, method: str, path: str,
                data: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        if not self.link:
            return 503, {"ok": False, "error": "offline",
                         "detail": "终端离线", "status": 503}
        return self.transport.request(method, path, data, session=self.session)

    def poll(self) -> dict[str, Any]:
        """在线时拉快照 + 增量事件（驱动播报）。离线时只推水位不动缓存。

        返回拉到的事件列表（测试断言用）。**离线时静默返回空**——轮询失败
        抛异常会让 demo CLI 崩，而「离线」本身就是一种正常状态。
        """
        if not self.link:
            return {"events": [], "synced": False}
        # 订单快照
        status, payload = self.request("GET", "/api/orders?open=1")
        if status == 200:
            self.snapshot = payload.get("data") or {}
            self.snapshot_at = time.monotonic()
        # 员工白名单（离线弱校验数据源）
        status, payload = self.request("GET", "/api/staff")
        if status == 200:
            rows = (payload.get("data") or {}).get("staff") or []
            self.admission_cache = {
                str(row.get("card_uid") or "").upper(): row
                for row in rows if row.get("card_uid")
            }
        # 增量事件 → 播报
        status, payload = self.request("GET", "/api/events",
                                       {"since": self.event_watermark})
        events: list[dict[str, Any]] = []
        if status == 200:
            data = payload.get("data") or {}
            events = data.get("events") or []
            announce_events(events, self.player)
            if data.get("event_seq") is not None:
                self.event_watermark = int(data["event_seq"])
        return {"events": events, "synced": True}

    # ------------------------------------------------------------ 离线切换
    def set_link(self, online: bool) -> None:
        if online == self.link:
            return
        self.link = bool(online)
        if online:
            self.offline_since = None
            self.sync()
            self.player.play("replay")
        else:
            self.offline_since = time.monotonic()
            self.player.play("offline")

    # ------------------------------------------------------------ 动作
    def scan(self, card_uid: str, station_id: int | None = None) -> dict[str, Any]:
        """刷卡。在线转发服务端；离线走白名单弱校验 + 入队。"""
        card = str(card_uid).strip().upper()
        if self.link:
            status, payload = self.request(
                "POST", "/api/admission/scan",
                {"card_uid": card, "station_id": station_id})
            data = payload.get("data") if status == 200 else {}
            result = dict(data or {})
            result.setdefault("ok", False)
            if status != 200:
                result.setdefault("detail", payload.get("detail", ""))
            return result
        return self._scan_offline(card, station_id)

    def _scan_offline(self, card: str, station_id: int | None) -> dict[str, Any]:
        row = self.admission_cache.get(card)
        if row is None:
            return {
                "ok": False, "name": "", "staff_id": None,
                "detail": f"未知卡 {card}（离线无法核对人员）",
                "needs_morning_check": False, "offline_local": False,
            }
        if not row.get("valid_certificates"):
            return {
                "ok": False, "name": row.get("name", ""),
                "staff_id": row.get("id"),
                "detail": f"{row.get('name', '')}：健康证无效（离线缓存判定）",
                "needs_morning_check": False, "offline_local": False,
            }
        # 弱校验通过：黄屏放行 + 动作入队（服务端恢复后强校验）
        self._enqueue_action("POST", "/api/admission/scan",
                             {"card_uid": card, "station_id": station_id})
        return {
            "ok": True, "name": row.get("name", ""), "staff_id": row.get("id"),
            "detail": f"{row.get('name', '')} 离线放行（恢复后服务端复核）",
            "needs_morning_check": False, "offline_local": True,
        }

    def weigh(self, item_id: int, ingredient: str, measured_g: float,
              *, staff_id: int | None = None) -> dict[str, Any]:
        return self._action(
            ("POST", f"/api/cooking/{item_id}/weigh"),
            {"ingredient": ingredient, "measured_g": measured_g,
             "staff_id": staff_id, "channel": "tft"},
        )

    def confirm(self, item_id: int, step_no: int,
                *, staff_id: int | None = None) -> dict[str, Any]:
        return self._action(
            ("POST", f"/api/cooking/{item_id}/confirm"),
            {"step_no": step_no, "staff_id": staff_id, "channel": "tft"},
        )

    def serve(self, item_id: int, *, staff_id: int | None = None) -> dict[str, Any]:
        return self._action(
            ("POST", f"/api/cooking/{item_id}/serve"),
            {"staff_id": staff_id, "channel": "tft"},
        )

    def _action(self, route: tuple[str, str], body: dict[str, Any]) -> dict[str, Any]:
        method, path = route
        if self.link:
            status, payload = self.request(method, path, body)
            data = payload.get("data") if status == 200 else None
            if data is not None:
                return dict(data)
            return {"ok": False, "status": status,
                    "detail": payload.get("detail", "")}
        # 离线：入队缓冲。本地先把「能自检的」标出来（称重目标/容差在
        # 上次快照里未必有，这里不猜，交给服务端）
        self._enqueue_action(method, path, body)
        return {"ok": None, "queued": True,
                "detail": "离线：动作已入队，恢复后同步"}

    def _enqueue_action(self, method: str, path: str, body: dict[str, Any]) -> None:
        self.queue.append("tft-action", payload={
            "method": method, "path": path, "body": dict(body),
        })

    # ------------------------------------------------------------ 同步
    def sync(self) -> dict[str, Any]:
        """恢复后逐条回放。**业务拒绝（4xx）不重试**（重试还是同样的拒绝）；
        记入 `denied` 并播报。网络错误（5xx）留在队列里等下一次 sync——
        用 `ack` 只确认「发出去且有了结论」的条目。"""
        played = 0
        denied: list[dict[str, Any]] = []
        ack_seqs: list[int] = []
        for entry in self.queue.pending():
            payload = entry.payload or {}
            method = payload.get("method", "POST")
            path = payload.get("path", "")
            body = payload.get("body") or {}
            status, response = self.transport.request(
                method, path, body, session=self.session)
            played += 1
            if status == 200 or 400 <= status < 500:
                # 发出去且有了终局结论：从队列确认掉
                ack_seqs.append(entry.seq)
                if status != 200:
                    denied.append({
                        "path": path, "body": body, "status": status,
                        "detail": response.get("detail", ""),
                    })
            # 5xx/超时：留在队列（不 ack），下次 sync 再试
        if ack_seqs:
            self.queue.ack(ack_seqs)
        self.denied.extend(denied)
        if denied:
            self.player.play("card_denied")
        return {"replayed": len(ack_seqs), "denied": len(denied),
                "remaining": len(self.queue)}

    # ------------------------------------------------------------ 屏
    def status_bar(self) -> dict[str, Any]:
        online = self.link
        offline_sec = (None if self.offline_since is None or online
                       else time.monotonic() - self.offline_since)
        return build_status(
            clock_text=time.strftime("%H:%M:%S"),
            device_id=self.device_id,
            online=online,
            offline_sec=offline_sec,
            queue_pending=len(self.snapshot.get("orders", ())),
            pending_actions=len(self.queue),
        )

    def screen_queue(self) -> dict[str, Any]:
        return queue_screen_from_orders(self.snapshot, status=self.status_bar())

    def screen_steps(self, item_id: int) -> dict[str, Any]:
        if self.link:
            status, payload = self.request("GET", f"/api/cooking/{item_id}")
            if status == 200:
                detail = (payload.get("data") or {}).get("sessions") or [{}]
                return build_steps_screen(detail[0], status=self.status_bar())
        return build_steps_screen({}, status=self.status_bar())

    def screen_card(self, result: Mapping[str, Any]) -> dict[str, Any]:
        return build_card_screen(result, status=self.status_bar(),
                                 offline_local=bool(result.get("offline_local")))

    def close(self) -> None:
        pass  # LocalQueue 每次 append 都同步落盘（jsonl 追加写），无需收尾
