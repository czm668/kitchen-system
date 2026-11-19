# -*- coding: utf-8 -*-
"""运行时记录器：把遥测帧与告警事件写进运行时库（SQLite / MySQL 双后端）。

两个写入点（`api/state.py`）：
- `tick()` 里 `telemetry.push(...)` 之后 → `record_frame`（12 指标展开成行）；
- `_emit(...)` 事件出口 → `record_event`（rule / guard / fault-escalate 进
  `alarm_events`，其余业务事件不落库——它们有各自的表，写路径在 A1 第二轮）。

线程模型：采样线程写、请求线程读，自带锁；写失败只计数不抛异常
（采样线程不能被落库拖死）。

A18：底层连接改由 `storage/backend.py` 的 `RuntimeDB` 门面提供，方言差异
（占位符、事务上下文、异常类型、表清单查询）都收敛在那一层。SQL 仍统一写
`?`，SQLite 分支零行为变化；类名 `SqliteRecorder` 保留是兼容既有导入与
`.conn` 属性，实际后端由 target 决定。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .backend import open_runtime
from .business import BusinessRecorderMixin

log = logging.getLogger("kitchen.storage.recorders")

__all__ = ["SqliteRecorder", "RuntimeRecorder"]

#: 只有这三类事件是「告警口径」——进 alarm_events。其余 kind 属于业务事件流
#: （订单/刷卡/网络），它们的状态表在 A1 第二轮接写路径，这里不重复记。
_ALARM_KINDS = frozenset({"rule", "guard", "fault-escalate"})

# alarm_events.source 是 ENUM('rule','detector','llm')（见 migrations/002），
# 这里按"触发子系统"归类；精确的告警类型由 scenario 列（VARCHAR(32)）承载。
_SOURCE_BY_KIND = {
    "rule": "rule",
    "guard": "rule",                  # 保守保护（ConservativeGuards）也属规则判定
    "fault-escalate": "detector",     # 传感器健康监视器升级，属 detector 一类
}

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt_ts(value: datetime) -> str:
    """SQLite DATETIME 与 MySQL DATETIME 同形字符串（migrate.py 同口径）。"""
    return value.strftime(_TS_FMT)


def _clip(value: Any, limit: int) -> str | None:
    """DDL 为 message/snapshot VARCHAR(255)、atype VARCHAR(32)，超长截断——落库绝不能因长文本而炸。"""
    if value is None:
        return None
    text = str(value)
    return text[:limit]


class RuntimeRecorder(BusinessRecorderMixin):
    """运行时库写入器（类名保留兼容，实际支持 SQLite / MySQL）。`close()` 幂等。

    `target` 为文件路径时用 SQLite（默认，A1 行为不变）；为
    `mysql://user:pass@host:port/db` 时走 MySQL（A18）。
    A23 起混入 `BusinessRecorderMixin`：业务写路径（订单/人员/过程留痕/审计）
    共用同一连接、同一把锁与同一失败计数——两个锁保护一个连接会出现
    「遥测线程与订单线程同时抢 sqlite 连接」的竞态，必须收敛到一处。
    """

    def __init__(self, target: str | Path, *, device_id: str = "kitchen-node-01",
                 migrations_dir: Path | None = None) -> None:
        self.target = target
        # 兼容既有代码读取的 .path：URL 目标不做 Path 转换（否则 Windows 上
        # 会把 "mysql://..." 变成畸形路径），只在 SQLite 时是 Path。
        self.path = target if isinstance(target, Path) else (
            Path(target) if not str(target).startswith(("mysql://", "mysql+pymysql://"))
            else target
        )
        self.device_id = str(device_id)
        self.conn = open_runtime(target, migrations_dir=migrations_dir)
        self._lock = threading.Lock()
        self.write_failures = 0
        self.frames_written = 0
        self.events_written = 0
        #: 业务写路径成功次数（A23：订单/人员/过程留痕/审计/基线物化）
        self.business_writes = 0

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
            with self._lock, self.conn.transaction():
                self.conn.executemany(
                    "INSERT INTO sensor_readings (device_id, metric, value, ts) "
                    "VALUES (?, ?, ?, ?)",
                    [(self.device_id, metric, value, ts_text) for metric, value, ts_text in rows],
                )
        except self.conn.errors:
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
        # source 必须落在 ENUM('rule','detector','llm') 内（见 migrations/002）；
        # 精确类型由 scenario 列承载，这里只做粗粒度子系统归类。
        source = _SOURCE_BY_KIND.get(kind, "rule")
        try:
            with self._lock, self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO alarm_events "
                    "(scenario, device_id, source, level, atype, message, snapshot, handled, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (kind, self.device_id, source, level, atype, message, snapshot, _fmt_ts(ts)),
                )
        except self.conn.errors:
            self.write_failures += 1
            log.warning("告警事件落库失败（第 %d 次）", self.write_failures, exc_info=True)
            return False
        self.events_written += 1
        return True

    # ------------------------------------------------------------ 读回
    def stats(self) -> dict[str, Any]:
        """库体检：表行数 + 本进程写入计数。"""
        with self._lock:
            tables = self.conn.stats()
        return {
            "enabled": True,
            "backend": self.conn.dialect,
            "path": self.conn.display,
            "tables": tables,
            "frames_written": self.frames_written,
            "events_written": self.events_written,
            "business_writes": self.business_writes,
            "write_failures": self.write_failures,
        }

    def order_id_bounds(self) -> tuple[int, int]:
        """运行时库里 `orders` / `order_items` 的当前最大主键（取不到返回 0,0）。

        启动时用它对齐内存里的自增游标。订单只存在于内存、库是写穿镜像，
        重启后游标从 1 重新开始，会覆盖上次运行留下的行——所以必须先读库。
        库还没建表（首次启动、迁移未跑完）时按 (0, 0) 处理，不影响启动。
        """
        def _max(table: str) -> int:
            rows = self.conn.query(f"SELECT COALESCE(MAX(id), 0) FROM {table}", [])
            return int(rows[0][0] or 0) if rows else 0

        try:
            with self._lock:
                return _max("orders"), _max("order_items")
        except Exception:  # noqa: BLE001 - 读不到只是少对齐一次，不该阻断启动
            return (0, 0)

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
            rows = self.conn.query(sql, args)
        return [{"metric": m, "value": v, "ts": t} for m, v, t in rows]

    def alarms_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.query(
                "SELECT scenario, device_id, source, level, atype, message, ts "
                "FROM alarm_events ORDER BY ts DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            )
        keys = ("scenario", "device_id", "source", "level", "atype", "message", "ts")
        return [dict(zip(keys, row)) for row in rows]

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "RuntimeRecorder":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


#: 旧名保留兼容一版（审查报告 §3）：新代码一律用 RuntimeRecorder。
#: 类名不再暗示只支持 SQLite（实际 SQLite / MySQL 双后端）。
SqliteRecorder = RuntimeRecorder
