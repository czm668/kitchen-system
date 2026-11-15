# -*- coding: utf-8 -*-
"""端侧本地队列（A6-01）：断网/掉电期间的业务数据与日志落脚点。

## 这一层只解决一件事

**判定和执行永远在本地，队列只负责「报上去」这件事。**

断网时规则引擎照常每一帧判定、互锁层照常合继电器，唯一的变化是上送失败的
数据落进本地队列，等恢复再补。反过来（断网就停止判定、等联网再判）等于把
安全保护绑在网络上，拔一根网线就能让燃气阀失效——这是本项目最不能接受的
降级方式。

## 四个必须写死的口径

### 1. `seq` 单调且**跨重启不重号**

服务端的去重键是 `(device_id, seq)`。掉电重启后 `seq` 从 0 开始，服务端会把
新数据当成「已收到过的旧数据」**静默丢弃**，而队列里的数据一条不少地 ack 掉了。
表现是「补传成功、页面数据缺一段、日志全绿」——排查这类问题的成本极高，因为
没有任何一处报错。

所以 `seq` 除了写进每条记录，还要在压缩日志时写进 `.state` 文件：

```
启动时 last_seq = max(队列里最大 seq, state.last_seq)
```

只存队列里最大 seq 不够：压缩会把旧记录删掉，全 ack 之后队列是空的，
最大值就丢了。

### 2. 安全类记录**永不因容量而丢**

队列必须有限（端侧 Flash 就几 MB），满了就得丢。但丢谁不能一视同仁：
遥测帧丢几条只是曲线少几个点，安全事件丢一条就是「燃气泄漏没有记录」。
所以 `critical=True` 的记录走独立配额，普通记录满时**只挤自己人**。

挤不下时（关键配额也满了）宁可丢最旧的关键记录，也要把它**报出来**
（`lost_critical` 计数 + `lossy` 标志），而不是继续吃 Flash 直到文件系统写坏。

### 3. 追加写 + 定期压缩，不做「每条记录重写整个文件」

端侧 Flash 的擦写寿命有限，每条记录都重写整个 JSONL，写 2000 条就是
2000×2000 次写放大。所以正常路径只有 append；确认（ack）之后才按阈值压缩：
未确认条数不变、行数膨胀到 2 倍，或累计 ack 超过 `compact_min_acked`。

### 4. 掉电留下的半行要能被吃掉

`append` 是「写完 flush、按策略 fsync」，最坏情况最后一行只写了一半。
启动时逐行 `json.loads`，解析失败的行**当没写过**（计数 `truncated_lines`），
而不是整个队列报损坏——半行之后的记录本来也没落盘，丢掉它是唯一正确的选择。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("kitchen.resilience.queue")

__all__ = [
    "KIND_ACTION",
    "KIND_ALARM",
    "KIND_EVENT",
    "KIND_LOG",
    "KIND_TELEMETRY",
    "LocalQueue",
    "QueueEntry",
    "QueueFull",
    "QueueStats",
]

#: 记录类型。`critical` 由 `append()` 显式指定，但下面这几类默认就是关键类。
KIND_TELEMETRY = "telemetry"
KIND_EVENT = "event"       # 规则事件（fired / latched / released）
KIND_ACTION = "action"     # 实际下发的动作（继电器状态变化）
KIND_ALARM = "alarm"       # 告警 / 故障
KIND_LOG = "log"

DEFAULT_CRITICAL_KINDS: frozenset[str] = frozenset({KIND_ALARM})


class QueueFull(RuntimeError):
    """容量策略拒绝写入（当前实现不会抛，留给严格模式）。"""


@dataclass(frozen=True)
class QueueEntry:
    """一条待上送记录。字段与 `offline_records` 表一一对应。"""

    seq: int
    ts: float                 # 端侧事件时刻（墙钟，秒）。补传时要按它校准
    kind: str
    metric: str = ""
    value: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    critical: bool = False

    def to_row(self) -> dict[str, Any]:
        """转成 `offline_records` 的一行（JSON 里 value 与 payload 二选一即可）。"""
        row = {
            "seq": self.seq,
            "ts": round(float(self.ts), 3),
            "kind": self.kind,
        }
        if self.metric:
            row["metric"] = self.metric
        if self.value is not None:
            row["value_num"] = round(float(self.value), 4)
        if self.payload:
            row["payload"] = self.payload
        return row

    def to_journal(self) -> str:
        """JSONL 一行（键序固定，便于 diff 与人工排障）。"""
        return json.dumps(
            {
                "seq": self.seq,
                "ts": round(float(self.ts), 3),
                "kind": self.kind,
                "metric": self.metric,
                "value": self.value,
                "critical": self.critical,
                "payload": self.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_journal(cls, line: str) -> QueueEntry:
        body = json.loads(line)
        value = body.get("value")
        return cls(
            seq=int(body["seq"]),
            ts=float(body["ts"]),
            kind=str(body.get("kind") or KIND_LOG),
            metric=str(body.get("metric") or ""),
            value=None if value is None else float(value),
            payload=dict(body.get("payload") or {}),
            critical=bool(body.get("critical")),
        )


@dataclass(frozen=True)
class QueueStats:
    """队列健康度。`lossy=True` 必须被上送，否则「丢了数据」没人知道。"""

    size: int
    critical: int
    capacity: int
    critical_capacity: int
    last_seq: int
    appended: int = 0
    acked: int = 0
    dropped: int = 0            # 普通记录因容量被挤掉
    lost_critical: int = 0      # 关键记录因关键配额满被挤掉（严重）
    truncated_lines: int = 0    # 掉电留下的半行

    @property
    def usage(self) -> float:
        return 0.0 if self.capacity <= 0 else self.size / float(self.capacity)

    @property
    def lossy(self) -> bool:
        return self.lost_critical > 0

    def describe(self) -> str:
        body = f"队列 {self.size}/{self.capacity}（关键 {self.critical}）"
        if self.dropped:
            body += f"，已挤掉普通记录 {self.dropped} 条"
        if self.lost_critical:
            body += f"，**已丢关键记录 {self.lost_critical} 条**"
        if self.truncated_lines:
            body += f"，上电修复半行 {self.truncated_lines} 行"
        return body

    def to_payload(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "critical": self.critical,
            "capacity": self.capacity,
            "usage": round(self.usage, 3),
            "last_seq": self.last_seq,
            "dropped": self.dropped,
            "lost_critical": self.lost_critical,
            "truncated_lines": self.truncated_lines,
            "lossy": self.lossy,
        }


class LocalQueue:
    """有界 + 落盘的本地队列（纯标准库，单文件 JSONL + 侧车 state）。

    用法::

        q = LocalQueue("var/device-A.queue", device_id="A")
        q.append(KIND_TELEMETRY, metric="gas_level", value=0.12)
        q.append(KIND_ALARM, metric="gas_level", payload={"level": 2},
                 critical=True)
        for entry in q.pending(100):
            if send(entry.to_row()):
                q.ack([entry.seq])
    """

    #: 落盘文件默认名，方便现场找
    SUFFIX = ".jsonl"

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        device_id: str = "",
        capacity: int = 2000,
        critical_capacity: int = 500,
        sync_every: int = 8,
        compact_min_acked: int = 64,
        critical_kinds: Iterable[str] = DEFAULT_CRITICAL_KINDS,
        wall_clock=None,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity 必须 > 0，实际 {capacity}")
        if critical_capacity < 0:
            raise ValueError(f"critical_capacity 不能为负，实际 {critical_capacity}")
        self.path = Path(path)
        self.device_id = str(device_id)
        self.capacity = int(capacity)
        self.critical_capacity = int(critical_capacity)
        self.sync_every = max(1, int(sync_every))
        self.compact_min_acked = max(1, int(compact_min_acked))
        self.critical_kinds = frozenset(str(k) for k in critical_kinds)
        self._wall_clock = wall_clock or time.time

        self._entries: list[QueueEntry] = []
        self._next_seq = 1
        self._appended = 0
        self._acked = 0
        self._dropped = 0
        self._lost_critical = 0
        self._truncated = 0
        self._unsynced = 0
        self._acked_since_compact = 0
        self._lines = 0
        self._load()

    # ---------------------------------------------------------------- 装载
    @property
    def state_path(self) -> Path:
        return self.path.with_name(self.path.name + ".state")

    def _load(self) -> None:
        """上电恢复：读 state + 逐行读 journal，跳过坏行。"""
        state_last = 0
        if self.state_path.exists():
            try:
                state_last = int(
                    json.loads(self.state_path.read_text(encoding="utf-8")).get("last_seq", 0)
                )
            except (OSError, ValueError, TypeError) as exc:
                log.warning("队列状态文件不可读，按 0 处理：%s（%s）", self.state_path, exc)
        if not self.path.exists():
            self._next_seq = state_last + 1
            return
        latest = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = QueueEntry.from_journal(line)
                    except (ValueError, KeyError, TypeError):
                        # 掉电留下的半行：当没写过（它本来也没完整落盘）
                        self._truncated += 1
                        continue
                    self._entries.append(entry)
                    self._lines += 1
                    latest = max(latest, entry.seq)
        except OSError as exc:
            log.error("队列日志读取失败：%s（%s）", self.path, exc)
        # 容量自愈：文件被人为拷大过，启动时按策略裁一次，别等下次 append
        self._enforce_capacity()
        self._next_seq = max(latest, state_last, self._max_seq()) + 1

    def _max_seq(self) -> int:
        return max((entry.seq for entry in self._entries), default=0)

    # ---------------------------------------------------------------- 写入
    def append(
        self,
        kind: str,
        *,
        metric: str = "",
        value: float | None = None,
        payload: dict[str, Any] | None = None,
        critical: bool | None = None,
        ts: float | None = None,
    ) -> QueueEntry | None:
        """入队一条。返回落地后的记录；因容量被丢弃时返回 `None`。

        `critical=None` 表示按 `kind` 默认判定（闹警类默认关键）。
        """
        kind = str(kind or KIND_LOG)
        is_critical = (kind in self.critical_kinds) if critical is None else bool(critical)
        if not self._make_room(is_critical):
            self._dropped += 1
            log.debug("队列已满且无普通记录可挤，丢弃本条（kind=%s）", kind)
            return None
        entry = QueueEntry(
            seq=self._next_seq,
            ts=float(self._wall_clock() if ts is None else ts),
            kind=kind,
            metric=str(metric or ""),
            value=None if value is None else float(value),
            payload=dict(payload or {}),
            critical=is_critical,
        )
        self._next_seq += 1
        self._entries.append(entry)
        self._appended += 1
        self._write(entry)
        return entry

    def _make_room(self, critical: bool) -> bool:
        """腾位置。返回 False 表示这条不用写了（普通记录遇到满的关键队列）。"""
        if critical:
            while self._count_critical() >= self.critical_capacity > 0 and self.critical_capacity:
                victim = next((e for e in self._entries if e.critical), None)
                if victim is None:
                    break
                self._entries.remove(victim)
                self._lost_critical += 1
                log.error(
                    "关键记录配额已满（%d），丢弃最旧的关键记录 seq=%s（%s）",
                    self.critical_capacity, victim.seq, victim.kind,
                )
            # 关键记录不受总容量约束：宁可超一点，也不丢安全事件
            return True
        if len(self._entries) < self.capacity:
            return True
        while len(self._entries) >= self.capacity:
            victim = next((e for e in self._entries if not e.critical), None)
            if victim is None:
                return False    # 全是关键记录：这条普通记录放弃
            self._entries.remove(victim)
            self._dropped += 1
        return True

    def _count_critical(self) -> int:
        return sum(1 for entry in self._entries if entry.critical)

    def _write(self, entry: QueueEntry) -> None:
        """追加一行。关键记录强制 fsync（掉电可以丢遥测帧，不能丢告警）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(entry.to_journal() + "\n")
                handle.flush()
                self._lines += 1
                self._unsynced += 1
                if entry.critical or self._unsynced >= self.sync_every:
                    os.fsync(handle.fileno())
                    self._unsynced = 0
        except OSError as exc:
            # 写盘失败不能把安全路径一起带崩：内存里仍然有这条，恢复后再落盘
            log.error("队列落盘失败（记录仍在内存，稍后重试）：%s", exc)

    # ---------------------------------------------------------------- 读取
    def pending(self, limit: int | None = None) -> tuple[QueueEntry, ...]:
        """按入队顺序取待确认记录（不出队）。"""
        items = self._entries if limit is None else self._entries[: max(0, int(limit))]
        return tuple(items)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[QueueEntry]:
        return iter(tuple(self._entries))

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    # ---------------------------------------------------------------- 确认
    def ack(self, seqs: Iterable[int]) -> int:
        """确认已上送。返回真正移出的条数（重复确认返回 0，不是错误）。"""
        wanted = {int(seq) for seq in seqs}
        if not wanted:
            return 0
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if entry.seq not in wanted]
        removed = before - len(self._entries)
        if removed:
            self._acked += removed
            self._acked_since_compact += removed
            self._maybe_compact()
        return removed

    def _maybe_compact(self) -> None:
        """按阈值压缩：重写文件只留未确认记录 + 固化 last_seq。"""
        if (
            self._acked_since_compact < self.compact_min_acked
            and self._lines <= max(16, 2 * len(self._entries))
        ):
            return
        self._compact()

    def _compact(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".part")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                for entry in self._entries:
                    handle.write(entry.to_journal() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            self._lines = len(self._entries)
            self._acked_since_compact = 0
            self._write_state()
        except OSError as exc:
            log.error("队列压缩失败，保留原文件：%s", exc)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_state(self) -> None:
        """侧车 state：**压缩后必须写**，否则 last_seq 随旧记录一起消失。"""
        temp = self.state_path.with_name(self.state_path.name + ".part")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "device_id": self.device_id,
                        "last_seq": self.last_seq,
                        "pending": len(self._entries),
                        "updated_at": int(self._wall_clock()),
                    },
                    handle,
                    ensure_ascii=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_path)
        except OSError as exc:
            log.error("队列状态写盘失败（重启后可能重号）：%s", exc)

    # ---------------------------------------------------------------- 观测
    def stats(self) -> QueueStats:
        return QueueStats(
            size=len(self._entries),
            critical=self._count_critical(),
            capacity=self.capacity,
            critical_capacity=self.critical_capacity,
            last_seq=self.last_seq,
            appended=self._appended,
            acked=self._acked,
            dropped=self._dropped,
            lost_critical=self._lost_critical,
            truncated_lines=self._truncated,
        )

    def snapshot(self) -> dict[str, Any]:
        body = self.stats().to_payload()
        body.update({
            "device_id": self.device_id,
            "path": str(self.path),
            "kinds": self._kind_histogram(),
        })
        return body

    def _kind_histogram(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self._entries:
            out[entry.kind] = out.get(entry.kind, 0) + 1
        return out

    # ---------------------------------------------------------------- 维护
    def reset(self) -> None:
        """清空（出厂复位 / 单测用）。**会删文件**，只在明确调用时发生。"""
        self._entries.clear()
        self._acked_since_compact = 0
        for target in (self.path, self.state_path):
            try:
                Path(target).unlink(missing_ok=True)
            except OSError as exc:
                log.error("队列复位删除 %s 失败：%s", target, exc)
        self._lines = 0
        self._write_state()

    def enforce_capacity(self) -> int:
        """外部（管理端）要求立刻裁剪，返回被挤掉的条数。"""
        before = len(self._entries)
        self._enforce_capacity()
        return before - len(self._entries)

    def _enforce_capacity(self) -> None:
        while len(self._entries) > self.capacity:
            victim = next((e for e in self._entries if not e.critical), None)
            if victim is None:
                break
            self._entries.remove(victim)
            self._dropped += 1
        limit = self.critical_capacity
        if limit > 0:
            while self._count_critical() > limit:
                victim = next((e for e in self._entries if e.critical), None)
                if victim is None:
                    break
                self._entries.remove(victim)
                self._lost_critical += 1


def build_replay(entries: Iterable[QueueEntry], *, offset: float = 0.0) -> list[dict]:
    """把记录转成上送行，`offset` 是「服务端时间 - 端侧时间」的校准量。

    单独抽成函数是因为补传与实时上报必须走**同一个**转换（否则两条路径的
    时间戳口径会分叉，页面上同一事件一会儿早一会儿晚）。
    """
    rows: list[dict] = []
    for entry in entries:
        row = entry.to_row()
        row["ts"] = round(float(entry.ts) + float(offset), 3)
        rows.append(row)
    return rows
