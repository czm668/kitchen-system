# -*- coding: utf-8 -*-
"""补传、去重与时钟校准（A6-02）。

## 三件事，三个不同的失败方式

| 做的事 | 不做的后果 |
|---|---|
| `(device_id, seq)` 去重 | 恢复网络后同一条记录写两次，页面出现重复点，统计指标全部偏高 |
| 批量 + 乱序容忍 | 逐条 POST，2000 条补传要 20 分钟；期间新数据又积压，永远追不上 |
| 时钟校准 | 端侧 RTC 漂移（无 NTP 的 ESP32 一天能差几十秒），补传上来的数据时间错位，趋势图和真实事件对不上 |

## 去重放在服务端，不放在端侧

端侧队列已经在 ack 之后删记录，理论上不会重发。但「理论上」在断网场景里不成立：

- 服务端写库成功、HTTP 响应超时 → 端侧没收到 200，不 ack，下轮重发；
- 端侧重启，`.state` 文件比日志旧 → `seq` 回退，重发旧号。

这两种都是**正常的分布式失败**，不是 bug。所以服务端必须按 `(device_id, seq)`
唯一键去重（对应 `offline_records` 表的 `uk_offline_dev_seq`），端侧只负责
「尽力不重号」。

## 冲突 ≠ 重复，必须分开报

- **重复**：同一个 seq、同样的内容 → 静默 ack（这就是去重要做的事）；
- **冲突**：同一个 seq、**不同的内容** → 端侧 seq 被复用了，这是真 bug。

把冲突当成重复 ack 掉，丢的是数据本身，而且丢得无声无息。所以冲突的记录
**不 ack、保留在队列里**，并让 `replay()` 立刻停下——继续补传只会不断冲突，
把队列越冲越乱。

## 校准用最小 RTT 样本，且要「连续一致」才采纳

`offset = ((t1-t0) + (t2-t3)) / 2`，这是 NTP 的标准估计。两个保护：

1. **只信 RTT 最小的样本**：RTT 大的样本里，排队延迟全被算进了 offset；
2. **连续 `min_samples` 次结果一致才采纳**：一次网络抖动或服务端时钟本身错了，
   就会把整个历史数据的时间轴平移几分钟。端侧时钟跳变比「时间不准」更糟——
   它会让人怀疑数据是假的。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .queue import LocalQueue, QueueEntry, build_replay

log = logging.getLogger("kitchen.resilience.upload")

__all__ = [
    "ClockCalibrator",
    "ClockState",
    "MemoryOfflineStore",
    "ReplayReport",
    "ReplayUploader",
    "UploadBatch",
    "build_batch",
]


# ------------------------------------------------------------------ 时钟校准
@dataclass(frozen=True)
class ClockState:
    """一次校准的结果。`offset` 是「服务端时间 − 端侧时间」。"""

    offset: float = 0.0
    rtt: float = 0.0
    samples: int = 0
    stable: bool = False        # 是否已满足「连续一致」，可以用于校正
    updated_at: float = 0.0
    rejected: int = 0           # 被丢弃的样本数（RTT 过大 / 结果不一致）

    def to_server(self, local_ts: float) -> float:
        """端侧时刻 → 服务端时刻。`stable=False` 时原样返回（不猜）。"""
        return float(local_ts) + (self.offset if self.stable else 0.0)

    @property
    def drift_sec(self) -> float:
        return abs(self.offset) if self.stable else 0.0

    def describe(self) -> str:
        if not self.stable:
            return f"时钟未校准（样本 {self.samples} 个，丢弃 {self.rejected}），按端侧时间上送"
        return (
            f"时钟偏差 {self.offset:+.2f}s（RTT {self.rtt * 1000:.0f}ms，"
            f"样本 {self.samples}）"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "offset": round(self.offset, 4),
            "rtt": round(self.rtt, 4),
            "samples": self.samples,
            "stable": self.stable,
            "drift_sec": round(self.drift_sec, 3),
            "rejected": self.rejected,
        }


class ClockCalibrator:
    """NTP 式偏移估计（最小 RTT + 连续一致）。

    `t0` 端侧发出、`t1` 服务端收到、`t2` 服务端回包、`t3` 端侧收到。
    """

    def __init__(
        self,
        *,
        min_samples: int = 3,
        tolerance: float = 1.0,
        max_rtt: float = 5.0,
        jump_limit: float = 300.0,
        wall_clock=None,
    ) -> None:
        if min_samples < 1:
            raise ValueError(f"min_samples 必须 >= 1，实际 {min_samples}")
        self.min_samples = int(min_samples)
        self.tolerance = float(tolerance)
        self.max_rtt = float(max_rtt)
        self.jump_limit = float(jump_limit)
        self._wall_clock = wall_clock or time.time
        self._state = ClockState(updated_at=float(self._wall_clock()))
        self._recent: list[float] = []
        self._best: tuple[float, float] | None = None   # (rtt, offset)

    @property
    def state(self) -> ClockState:
        return self._state

    def sample(
        self,
        t0: float,
        t1: float,
        t2: float,
        t3: float,
        *,
        now: float | None = None,
    ) -> ClockState:
        """喂一组往返时间戳，返回最新状态（不满足条件时保持原状）。"""
        rtt = (float(t3) - float(t0)) - (float(t2) - float(t1))
        offset = ((float(t1) - float(t0)) + (float(t2) - float(t3))) / 2.0
        moment = float(self._wall_clock() if now is None else now)
        if rtt < 0 or rtt > self.max_rtt:
            self._reject(f"RTT {rtt:.3f}s 不合理（上限 {self.max_rtt}s），样本丢弃")
            return self._state
        if self._best is None or rtt < self._best[0]:
            self._best = (rtt, offset)
        self._recent.append(offset)
        if len(self._recent) > self.min_samples:
            self._recent.pop(0)
        if len(self._recent) < self.min_samples:
            self._state = ClockState(
                offset=self._best[1], rtt=self._best[0], samples=len(self._recent),
                stable=False, updated_at=moment, rejected=self._state.rejected,
            )
            return self._state
        spread = max(self._recent) - min(self._recent)
        if spread > self.tolerance:
            # 样本互相矛盾：别急着采纳，清零重新攒（可能是网络抖动或服务端漂移）
            self._reject(
                f"连续 {len(self._recent)} 次偏差不一致（极差 {spread:.2f}s > "
                f"{self.tolerance}s），等待稳定"
            )
            self._recent.clear()
            return self._state
        if abs(offset) > self.jump_limit:
            self._reject(
                f"偏差 {offset:+.1f}s 超过跳变上限 {self.jump_limit}s："
                "更可能是服务端时钟错，先不校正"
            )
            self._recent.clear()
            return self._state
        previous = self._state
        self._state = ClockState(
            offset=offset,
            rtt=self._best[0] if self._best else rtt,
            samples=len(self._recent),
            stable=True,
            updated_at=moment,
            rejected=previous.rejected,
        )
        if previous.stable and abs(previous.offset - offset) > self.tolerance:
            log.warning(
                "时钟偏移变化较大：%+.2fs → %+.2fs（若端侧 RTC 正常，查服务端时间源）",
                previous.offset, offset,
            )
        return self._state

    def _reject(self, reason: str) -> None:
        log.debug("校准样本丢弃：%s", reason)
        self._state = ClockState(
            offset=self._state.offset or (self._best[1] if self._best else 0.0),
            rtt=self._state.rtt,
            samples=len(self._recent),
            stable=self._state.stable,
            updated_at=self._state.updated_at,
            rejected=self._state.rejected + 1,
        )


# ------------------------------------------------------------------ 批量上送
@dataclass(frozen=True)
class UploadBatch:
    """一次补传会话。`batch_uid` 只用于分组排查，去重靠 `(device_id, seq)`。"""

    device_id: str
    batch_uid: str
    entries: tuple[QueueEntry, ...]
    offset: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "batch_uid": self.batch_uid,
            "count": len(self.entries),
            "clock_offset": round(float(self.offset), 3),
            "records": build_replay(self.entries, offset=self.offset),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)


def build_batch(
    device_id: str,
    entries: Sequence[QueueEntry],
    *,
    offset: float = 0.0,
    batch_uid: str = "",
) -> UploadBatch:
    """组一个补传批次。`batch_uid` 缺省按「设备-首号-末号」生成，便于日志检索。"""
    entries = tuple(entries)
    uid = batch_uid or (
        f"{device_id or 'dev'}-{entries[0].seq}-{entries[-1].seq}" if entries else f"{device_id}-empty"
    )
    return UploadBatch(device_id=device_id, batch_uid=uid, entries=entries, offset=offset)


class OfflineSink(Protocol):
    """服务端接收端契约（HTTP / MQTT / 直接调服务层都能实现）。"""

    def receive(self, payload: dict) -> dict: ...


class MemoryOfflineStore:
    """`offline_records` 的内存替身：按 `(device_id, seq)` 唯一。

    故意把「重复」与「冲突」分开返回：只报 duplicates 的实现会在端侧 seq 复用
    时静默丢数据，而现场只看到「补传成功」。
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], dict] = {}
        self.batches: list[str] = []
        self.received = 0

    def receive(self, payload: dict) -> dict:
        device_id = str(payload.get("device_id") or "")
        self.batches.append(str(payload.get("batch_uid") or ""))
        accepted = duplicates = 0
        conflicts: list[dict] = []
        for row in payload.get("records") or ():
            self.received += 1
            key = (device_id, int(row["seq"]))
            body = {k: v for k, v in row.items() if k != "seq"}
            body["device_id"] = device_id
            body["seq"] = int(row["seq"])
            body["received_at"] = time.time()
            existing = self._rows.get(key)
            if existing is None:
                self._rows[key] = body
                accepted += 1
                continue
            if _same(existing, body):
                duplicates += 1
            else:
                conflicts.append({
                    "device_id": device_id,
                    "seq": int(row["seq"]),
                    "reason": "同一 seq 内容不同（端侧 seq 被复用）",
                })
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "total": len(self._rows),
        }

    # ------------------------------------------------------------ 查询侧
    def records(self, device_id: str | None = None) -> list[dict]:
        """按**校准后的 ts** 排序返回——乱序补传的页面必须按时间线画。"""
        rows = [
            dict(row) for (dev, _seq), row in self._rows.items()
            if device_id is None or dev == device_id
        ]
        rows.sort(key=lambda row: (float(row.get("ts") or 0), int(row["seq"])))
        return rows

    def values(self, metric: str, device_id: str | None = None) -> list[tuple[float, float]]:
        """取某个指标的 (ts, value) 序列，便于断言「补传后曲线没有重复点」。

        队列按**帧**存（`payload.metrics` 放整帧），这里按指标展开——与真实
        服务端入库时的行为一致。按指标拆行存会让 2000 条容量在 1Hz 下只够
        3 分钟，Flash 与带宽都吃不消。
        """
        out: list[tuple[float, float]] = []
        for row in self.records(device_id):
            ts = float(row["ts"])
            if row.get("metric") == metric and row.get("value_num") is not None:
                out.append((ts, float(row["value_num"])))
                continue
            frame = (row.get("payload") or {}).get("metrics") or {}
            if metric in frame:
                out.append((ts, float(frame[metric])))
        return out

    def __len__(self) -> int:
        return len(self._rows)


def _same(left: dict, right: dict) -> bool:
    """内容比对：忽略服务端补的时间字段。"""
    ignore = {"received_at"}
    a = {k: v for k, v in left.items() if k not in ignore}
    b = {k: v for k, v in right.items() if k not in ignore}
    return a == b


@dataclass(frozen=True)
class ReplayReport:
    """一次补传的结果。`remaining` 不为 0 说明还有积压（下次继续）。"""

    batches: int = 0
    sent: int = 0
    accepted: int = 0
    duplicates: int = 0
    conflicts: tuple[dict[str, Any], ...] = ()
    errors: tuple[str, ...] = ()
    remaining: int = 0
    stopped: str = ""        # 提前停止的原因（冲突 / 发送失败）

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.errors

    @property
    def advanced(self) -> bool:
        return self.sent > 0

    def describe(self) -> str:
        parts = [f"{self.batches} 批 / {self.sent} 条"]
        if self.accepted:
            parts.append(f"写入 {self.accepted}")
        if self.duplicates:
            parts.append(f"去重 {self.duplicates}")
        if self.conflicts:
            parts.append(f"**冲突 {len(self.conflicts)}**")
        if self.errors:
            parts.append(f"失败 {len(self.errors)}")
        if self.remaining:
            parts.append(f"剩余 {self.remaining}")
        body = "，".join(parts)
        return f"{body}（已停止：{self.stopped}）" if self.stopped else body

    def to_payload(self) -> dict[str, Any]:
        return {
            "batches": self.batches,
            "sent": self.sent,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
            "remaining": self.remaining,
            "stopped": self.stopped,
            "ok": self.ok,
        }


class ReplayUploader:
    """把本地队列补传上去：批量、去重、遇冲突即停。"""

    def __init__(
        self,
        queue: LocalQueue,
        sink: OfflineSink,
        *,
        device_id: str = "",
        batch_size: int = 100,
        max_batches: int = 20,
        calibrator: ClockCalibrator | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError(f"batch_size 必须 > 0，实际 {batch_size}")
        self.queue = queue
        self.sink = sink
        self.device_id = str(device_id or queue.device_id)
        self.batch_size = int(batch_size)
        self.max_batches = max(1, int(max_batches))
        self.calibrator = calibrator or ClockCalibrator()

    @property
    def offset(self) -> float:
        state = self.calibrator.state
        return state.offset if state.stable else 0.0

    def replay(self, *, limit: int | None = None, batch_uid: str = "") -> ReplayReport:
        """尽量把积压传完。任何一批失败就停（别把队列刷成冲突风暴）。"""
        budget = int(limit) if limit is not None else self.max_batches * self.batch_size
        batches = sent = accepted = duplicates = 0
        conflicts: list[dict] = []
        errors: list[str] = []
        stopped = ""
        while batches < self.max_batches and sent < budget:
            take = min(self.batch_size, budget - sent)
            entries = self.queue.pending(take)
            if not entries:
                break
            batch = build_batch(self.device_id, entries, offset=self.offset, batch_uid=batch_uid)
            try:
                response = self.sink.receive(batch.to_payload())
            except Exception as exc:       # noqa: BLE001 —— 网络层异常一律重试下一轮
                errors.append(f"{batch.batch_uid}：{exc}")
                stopped = f"发送失败（{type(exc).__name__}）"
                log.warning("补传批次失败，保留在队列：%s", exc)
                break
            batches += 1
            sent += len(entries)
            batch_conflicts = list(response.get("conflicts") or ())
            accepted += int(response.get("accepted") or 0)
            duplicates += int(response.get("duplicates") or 0)
            if batch_conflicts:
                conflicts.extend(batch_conflicts)
                # 冲突记录不 ack：丢了就再也找不回来
                bad = {int(item["seq"]) for item in batch_conflicts}
                good = [entry.seq for entry in entries if entry.seq not in bad]
                self.queue.ack(good)
                stopped = f"seq 冲突（{len(batch_conflicts)} 条），已停止补传等待人工介入"
                log.error("补传遇到 seq 冲突，停止本轮：%s", batch_conflicts[:3])
                break
            self.queue.ack([entry.seq for entry in entries])
        return ReplayReport(
            batches=batches,
            sent=sent,
            accepted=accepted,
            duplicates=duplicates,
            conflicts=tuple(conflicts),
            errors=tuple(errors),
            remaining=len(self.queue),
            stopped=stopped,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "batch_size": self.batch_size,
            "max_batches": self.max_batches,
            "clock": self.calibrator.state.to_payload(),
            "queue": self.queue.stats().to_payload(),
        }


def dedup_rows(rows: Iterable[dict], device_id: str) -> list[dict]:
    """服务端兜底去重（不做唯一键的存储层也能用一次）。

    保留**先到**的那条：后到的同 seq 记录若是不同内容，属于冲突，
    由 `MemoryOfflineStore` 单独报，这里只保证不重复。
    """
    seen: dict[int, dict] = {}
    for row in rows:
        key = int(row["seq"])
        if key in seen:
            continue
        body = dict(row)
        body["device_id"] = device_id
        seen[key] = body
    return [seen[key] for key in sorted(seen)]
