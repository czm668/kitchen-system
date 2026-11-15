# -*- coding: utf-8 -*-
"""端侧本地队列（A6-01）。"""
from __future__ import annotations

from resilience_support import FakeClock, make_queue

import pytest

from kitchen.resilience.queue import (
    KIND_ACTION,
    KIND_ALARM,
    KIND_EVENT,
    KIND_TELEMETRY,
    LocalQueue,
    QueueEntry,
    build_replay,
)


class TestBasics:
    def test_入队顺序与seq单调(self, tmp_path):
        queue = make_queue(tmp_path)
        entries = [queue.append(KIND_TELEMETRY, metric="t", value=float(i)) for i in range(3)]
        assert [entry.seq for entry in entries] == [1, 2, 3]
        assert [entry.seq for entry in queue.pending()] == [1, 2, 3]
        assert queue.last_seq == 3

    def test_ack摘除且重复确认无害(self, tmp_path):
        queue = make_queue(tmp_path)
        for i in range(3):
            queue.append(KIND_TELEMETRY, value=float(i))
        assert queue.ack([1, 2]) == 2
        assert [entry.seq for entry in queue.pending()] == [3]
        assert queue.ack([1, 2]) == 0
        assert queue.ack([]) == 0

    def test_时间戳取注入时钟(self, tmp_path):
        clock = FakeClock(start=1000.0)
        queue = make_queue(tmp_path, wall_clock=clock)
        entry = queue.append(KIND_TELEMETRY)
        assert entry.ts == 1000.0
        later = queue.append(KIND_TELEMETRY, ts=2000.0)
        assert later.ts == 2000.0

    def test_闹警类默认关键(self, tmp_path):
        queue = make_queue(tmp_path)
        assert queue.append(KIND_ALARM).critical is True
        assert queue.append(KIND_TELEMETRY).critical is False
        assert queue.append(KIND_EVENT, critical=True).critical is True

    def test_空队列stats正常(self, tmp_path):
        queue = make_queue(tmp_path, capacity=100)
        stats = queue.stats()
        assert (stats.size, stats.usage, stats.lossy) == (0, 0.0, False)
        assert "0/100" in stats.describe()


class TestPersistence:
    def test_重启后待确认数据还在(self, tmp_path):
        first = make_queue(tmp_path)
        first.append(KIND_TELEMETRY, metric="gas_level", value=0.1)
        first.append(KIND_EVENT, payload={"rule_id": "22"})

        second = make_queue(tmp_path)
        assert len(second) == 2
        assert [entry.seq for entry in second.pending()] == [1, 2]

    def test_重启后seq不重号(self, tmp_path):
        """没有这条，服务端会把新数据当旧数据静默丢掉（见模块 docstring 第 1 条）。"""
        first = make_queue(tmp_path)
        for _ in range(3):
            first.append(KIND_TELEMETRY)
        assert first.last_seq == 3

        second = make_queue(tmp_path)
        assert second.append(KIND_TELEMETRY).seq == 4

    def test_全部确认并压缩后seq仍不重号(self, tmp_path):
        """全 ack 之后队列是空的，`last_seq` 只能靠侧车 state 文件活下来。"""
        first = make_queue(tmp_path, compact_min_acked=2)
        for _ in range(5):
            first.append(KIND_TELEMETRY)
        first.ack([1, 2, 3, 4, 5])
        assert len(first) == 0
        assert first.state_path.is_file()

        second = make_queue(tmp_path)
        assert second.append(KIND_TELEMETRY).seq == 6

    def test_半行被吃掉而不是整队报损坏(self, tmp_path):
        queue = make_queue(tmp_path)
        queue.append(KIND_TELEMETRY, value=1.0)
        # 模拟掉电：最后一行只写了一半
        with queue.path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 2, "ts": 17000')

        reloaded = make_queue(tmp_path)
        assert len(reloaded) == 1
        assert reloaded.stats().truncated_lines == 1
        assert "半行" in reloaded.stats().describe()

    def test_关键记录立刻可见于磁盘(self, tmp_path):
        queue = make_queue(tmp_path, sync_every=100)
        queue.append(KIND_ALARM, payload={"level": 2})
        text = queue.path.read_text(encoding="utf-8")
        assert "level" in text and text.count("\n") == 1

    def test_压缩后文件行数等于待确认数(self, tmp_path):
        queue = make_queue(tmp_path, compact_min_acked=2)
        for _ in range(10):
            queue.append(KIND_TELEMETRY)
        queue.ack([1, 2, 3, 4, 5, 6, 7])
        lines = [line for line in queue.path.read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == len(queue) == 3

    def test_reset清空文件(self, tmp_path):
        queue = make_queue(tmp_path)
        queue.append(KIND_TELEMETRY)
        queue.reset()
        assert len(queue) == 0
        assert not queue.path.exists()


class TestCapacity:
    def test_满了挤掉最旧的普通记录(self, tmp_path):
        queue = make_queue(tmp_path, capacity=3)
        for i in range(5):
            queue.append(KIND_TELEMETRY, value=float(i))
        assert [entry.value for entry in queue.pending()] == [2.0, 3.0, 4.0]
        assert queue.stats().dropped == 2

    def test_普通记录再多也不挤关键记录(self, tmp_path):
        queue = make_queue(tmp_path, capacity=3)
        queue.append(KIND_ALARM, payload={"level": 2})
        for i in range(10):
            queue.append(KIND_TELEMETRY, value=float(i))
        assert KIND_ALARM in [entry.kind for entry in queue.pending()]
        assert queue.stats().lost_critical == 0

    def test_队列全是关键记录时普通记录直接丢弃(self, tmp_path):
        queue = make_queue(tmp_path, capacity=2)
        queue.append(KIND_ALARM, payload={"n": 1})
        queue.append(KIND_ALARM, payload={"n": 2})
        assert queue.append(KIND_TELEMETRY) is None
        assert queue.stats().dropped == 1
        assert queue.stats().lossy is False

    def test_关键配额满时丢最旧关键并报出来(self, tmp_path):
        queue = make_queue(tmp_path, capacity=10, critical_capacity=2)
        for i in range(4):
            queue.append(KIND_ALARM, payload={"n": i})
        stats = queue.stats()
        assert stats.critical == 2
        assert stats.lost_critical == 2
        assert stats.lossy is True
        assert "已丢关键记录" in stats.describe()

    def test_启动时按容量自愈(self, tmp_path):
        """文件被外部拷大 / 降过容量之后，装载时裁回容量而不是等下次 append。"""
        grown = make_queue(tmp_path, capacity=50)
        for i in range(12):
            grown.append(KIND_TELEMETRY, value=float(i))
        load = make_queue(tmp_path, capacity=5)
        assert len(load) == 5
        assert load.stats().dropped == 7


class TestEntry:
    def test_JSONL往返(self):
        entry = QueueEntry(seq=7, ts=100.5, kind=KIND_ACTION, metric="fan",
                           value=1.0, payload={"actions": ["fan_on"]}, critical=True)
        again = QueueEntry.from_journal(entry.to_journal())
        assert again == entry

    def test_转库行字段名对齐offline_records(self):
        entry = QueueEntry(seq=1, ts=10.0, kind=KIND_TELEMETRY, metric="t", value=24.5)
        row = entry.to_row()
        assert row["value_num"] == 24.5
        assert "value" not in row
        assert row["metric"] == "t"

    def test_补传行应用时钟偏移(self):
        entry = QueueEntry(seq=1, ts=10.0, kind=KIND_TELEMETRY, metric="t", value=1.0)
        rows = build_replay([entry], offset=-3.0)
        assert rows[0]["ts"] == 7.0


class TestParams:
    @pytest.mark.parametrize("capacity", [0, -1])
    def test_容量非法直接报错(self, tmp_path, capacity):
        with pytest.raises(ValueError):
            LocalQueue(tmp_path / "q.jsonl", capacity=capacity)

    def test_关键配额可以为0表示不限(self, tmp_path):
        queue = make_queue(tmp_path, capacity=2, critical_capacity=0)
        for i in range(5):
            queue.append(KIND_ALARM, payload={"n": i})
        assert queue.stats().critical == 5
        assert queue.stats().lost_critical == 0
