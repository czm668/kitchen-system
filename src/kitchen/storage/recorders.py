# -*- coding: utf-8 -*-
"""运行时记录器：把遥测帧与告警事件写进运行时库。

两个写入点（`api/state.py`）：
- `tick()` 里 `telemetry.push(...)` 之后 → `record_frame`（12 指标展开成行）；
- `_emit(...)` 事件出口 → `record_event`（rule / guard / fault-escalate 进
  `alarm_events`，其余业务事件不落库——它们有各自的表，写路径在 A1 第二轮）。

线程模型：采样线程写、请求线程读，`check_same_thread=False` + 自带锁；
写失败只计数不抛异常（采样线程不能被落库拖死）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .db import db_stats, open_runtime_db

log = logging.getLogger("kitchen.storage.recorders")

__all__ = ["SqliteRecorder"]

#: 只有这三类事件是「告警口径」——进 alarm_events。其余 kind 属于业务事件流
#: （订单/刷卡/网络），它们的状态表在 A1 第二轮接写路径，这里不重复记。
_ALARM_KINDS = frozenset({"rule", "guard", "fault-escalate"})

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt_ts(value: datetime) -> str:
    """SQLite DATETIME 与 MySQL DATETIME 同形字符串（migrate.py 同口径）。"""
    return value.strftime(_TS_FMT)


def _clip(value: Any, limit: int) -> str | None:
    """DDL 是 VARCHAR(255)/VARCHAR(48)，超长截断——落库绝不能因长文本而炸。"""
    if value is None:
        return None
    text = str(value)
    return text[:limit]


class SqliteRecorder:
    """运行时库写入器。`close()` 幂等。"""

    def __init__(self, path: str | Path, *, device_id: str = "kitchen-node-01",
                 migrations_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.device_id = str(device_id)
        self.conn: sqlite3.Connection = open_runtime_db(self.path, migrations_dir=migrations_dir)
        self._lock = threading.Lock()
        self.write_failures = 0
        self.frames_written = 0
        self.events_written = 0

    # ------------------------------------------------------------ 遥测帧
    def record_frame(self, metrics: Mapping[str, Any], ts: datetime) -> int:
        """一帧 12 指标 → sensor_readings（跳过坏值，口径同 TelemetryRing.push）。"""
        rows: list[tuple[str, Any, str]] = []
        for metric, raw in metrics.items():
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value != value:  # NaN
                continue
            rows.append((metric, value, _fmt_ts(ts)))
        if not rows:
            return 0
        try:
            with self._lock, self.conn:
                self.conn.executemany(
                    "INSERT INTO sensor_readings (device_id, metric, value, ts) "
                    "VALUES (?, ?, ?, ?)",
                    [(self.device_id, metric, value, ts_text) for metric, value, ts_text in rows],
                )
        except sqlite3.Error:
            self.write_failures += 1
            log.warning("遥测帧落库失败（第 %d 次）", self.write_failures, exc_info=True)
            return 0
        self.frames_written += 1
        return len(rows)

    # ------------------------------------------------------------ 告警事件
    def record_event(self, kind: str, body: Mapping[str, Any], ts: datetime) -> bool:
        if kind not in _ALARM_KINDS:
            return False
        level = body.get("level")
        try:
            level = int(level) if level is not None else 1
        except (TypeError, ValueError):
            level = 1
        atype = _clip(body.get("atype") or body.get("rule_id") or kind, 32)
        message = _clip(body.get("message") or kind, 255)
        snapshot = _clip(json.dumps(body, ensure_ascii=False, default=str), 255)
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    "INSERT INTO alarm_events "
                    "(scenario, device_id, source, level, atype, message, snapshot, handled, ts) "
                    "VALUES (?, ?, 'rule', ?, ?, ?, ?, 0, ?)",
                    (kind, self.device_id, level, atype, message, snapshot, _fmt_ts(ts)),
                )
        except sqlite3.Error:
            self.write_failures += 1
            log.warning("告警事件落库失败（第 %d 次）", self.write_failures, exc_info=True)
            return False
        self.events_written += 1
        return True

    # ------------------------------------------------------------ 读回
    def stats(self) -> dict[str, Any]:
        """库体检：表行数 + 本进程写入计数。"""
        with self._lock:
            tables = db_stats(self.conn)
        return {
            "enabled": True,
            "path": str(self.path),
            "tables": tables,
            "frames_written": self.frames_written,
            "events_written": self.events_written,
            "write_failures": self.write_failures,
        }

    def readings_history(
        self,
        *,
        metric: str | None = None,
        limit: int = 200,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """跨重启的遥测历史（管理端曲线/导出用）。"""
        sql = "SELECT metric, value, ts FROM sensor_readings"
        where: list[str] = []
        args: list[Any] = []
        if metric:
            where.append("metric = ?")
            args.append(metric)
        if since:
            where.append("ts >= ?")
            args.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 2000)))
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [{"metric": m, "value": v, "ts": t} for m, v, t in rows]

    def alarms_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT scenario, device_id, source, level, atype, message, ts "
                "FROM alarm_events ORDER BY ts DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        keys = ("scenario", "device_id", "source", "level", "atype", "message", "ts")
        return [dict(zip(keys, row)) for row in rows]

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            except sqlite3.Error:
                pass
            self.conn.close()

    def __enter__(self) -> "SqliteRecorder":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
