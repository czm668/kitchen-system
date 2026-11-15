# -*- coding: utf-8 -*-
"""审计日志：谁、什么时候、对什么、从什么改成什么、结果如何。

## 为什么审计必须单列一个模块

答辩和现场复盘都会问同一个问题：「你凭什么说这个阈值没人改过？」。答不上来
就意味着「合规系统」这个说法站不住。所以凡是**写操作**（改阈值、复位联锁、
关工单、复核、改配方）都必须留痕，且要记**改前改后**——只记「谁改了什么」
在出事故时没用，看不出到底从多少改到了多少。

## 三条硬口径

1. **审计写失败不能阻断业务**。审计是旁路：磁盘满、权限错都不该让「关燃气阀」
   失败。所以 `record()` 捕获异常后记 `write_failures` 计数并继续，由快照把
   这个计数报给运维——**不能静默**，静默等于没有审计。
2. **失败的操作也要记**。被拒的复位请求（`InterlockBlocked`）比成功的那次更
   值得留痕：它证明有人试过强行复位。
3. **口令一律不进审计**。`_MASK_KEYS` 做字段级脱敏，宁可多脱不可漏。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("kitchen.api.audit")

__all__ = ["AuditEntry", "AuditLog", "MASK_TEXT"]

MASK_TEXT = "***"

#: 这些键不落审计原文。名字里带 pass/token/secret 的一律脱敏，
#: 用子串匹配而不是精确匹配：`new_password` / `api_token` 都得覆盖到。
_MASK_KEYS = ("password", "passwd", "token", "secret", "authorization")


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (MASK_TEXT if any(bad in str(key).lower() for bad in _MASK_KEYS)
                  else _mask(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mask(item) for item in value]
    return value


@dataclass
class AuditEntry:
    """一条审计记录。`before`/`after` 只放**变化的那几个字段**，不整份对象。"""

    id: int
    ts: float
    actor: str
    role: str
    action: str
    target: str = ""
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    result: str = "ok"
    detail: str = ""
    source: str = ""

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "ts": round(self.ts, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
            "actor": self.actor,
            "role": self.role,
            "action": self.action,
            "result": self.result,
        }
        if self.target:
            body["target"] = self.target
        if self.before:
            body["before"] = self.before
        if self.after:
            body["after"] = self.after
        if self.detail:
            body["detail"] = self.detail
        if self.source:
            body["source"] = self.source
        return body

    def describe(self) -> str:
        delta = ""
        if self.before or self.after:
            keys = sorted(set(self.before) | set(self.after))
            delta = "；".join(
                f"{key}: {self.before.get(key, '-')} → {self.after.get(key, '-')}"
                for key in keys
            )
        tail = f"（{delta}）" if delta else ""
        return f"[{self.result}] {self.actor}({self.role}) {self.action} {self.target}{tail}"


class AuditLog:
    """有界环形日志 + 可选 JSONL 落盘。

    容量默认 2000 条：一天几百次写操作够用，又不至于把内存吃穿。落盘用
    **追加 + 单行 JSON**，崩溃时最多丢最后半行（读取侧逐行解析跳过坏行）。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        capacity: int = 2000,
        clock: Any = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity 至少为 1")
        self.path = Path(path) if path is not None else None
        self.capacity = int(capacity)
        self._clock = clock or time.time
        self._entries: deque[AuditEntry] = deque(maxlen=self.capacity)
        self._next_id = 1
        self.write_failures = 0
        self.dropped = 0
        if self.path is not None:
            self._load()

    # ------------------------------------------------------------ 读
    def _load(self) -> None:
        """重启后把历史读回来，否则「上次是谁改的」重启一次就查不到。"""
        if self.path is None or not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("审计日志读取失败：%s", exc)
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # 崩溃留下的半行，跳过
            try:
                entry = AuditEntry(
                    id=int(row["id"]), ts=float(row["ts"]),
                    actor=str(row["actor"]), role=str(row["role"]),
                    action=str(row["action"]),
                    target=str(row.get("target") or ""),
                    before=dict(row.get("before") or {}),
                    after=dict(row.get("after") or {}),
                    result=str(row.get("result") or "ok"),
                    detail=str(row.get("detail") or ""),
                    source=str(row.get("source") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._entries.append(entry)
            self._next_id = max(self._next_id, entry.id + 1)

    # ------------------------------------------------------------ 写
    def record(
        self,
        *,
        actor: str,
        role: str,
        action: str,
        target: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        result: str = "ok",
        detail: str = "",
        source: str = "",
    ) -> AuditEntry:
        entry = AuditEntry(
            id=self._next_id,
            ts=float(self._clock()),
            actor=str(actor or "anonymous"),
            role=str(role or "-"),
            action=str(action),
            target=str(target),
            before=_mask(dict(before or {})),
            after=_mask(dict(after or {})),
            result=str(result),
            detail=str(detail),
            source=str(source),
        )
        self._next_id += 1
        if len(self._entries) == self.capacity:
            self.dropped += 1
        self._entries.append(entry)
        self._append_file(entry)
        return entry

    def _append_file(self, entry: AuditEntry) -> None:
        """追加落盘。**任何异常都吞掉并计数**——审计不能拖垮业务。"""
        if self.path is None:
            return
        row = dict(entry.to_payload())
        row.setdefault("before", entry.before)
        row.setdefault("after", entry.after)
        row.setdefault("detail", entry.detail)
        row.setdefault("source", entry.source)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self.write_failures += 1
            log.warning("审计落盘失败（不影响业务）：%s", exc)

    # ------------------------------------------------------------ 查
    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def tail(self, limit: int = 50) -> tuple[AuditEntry, ...]:
        if limit <= 0:
            return ()
        return tuple(list(self._entries)[-limit:])

    def filter(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        result: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> tuple[AuditEntry, ...]:
        out = []
        for entry in self._entries:
            if actor and entry.actor != actor:
                continue
            if action and entry.action != action:
                continue
            if result and entry.result != result:
                continue
            if since is not None and entry.ts < since:
                continue
            out.append(entry)
        return tuple(out[-limit:] if limit > 0 else out)

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": len(self._entries),
            "capacity": self.capacity,
            "dropped": self.dropped,
            "write_failures": self.write_failures,
            "next_id": self._next_id,
            "path": str(self.path) if self.path else "",
        }

    def recent_texts(self, limit: int = 20) -> Iterable[str]:
        return (entry.describe() for entry in self.tail(limit))
