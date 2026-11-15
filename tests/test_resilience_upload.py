# -*- coding: utf-8 -*-
"""补传、去重与时钟校准（A6-02）。"""
from __future__ import annotations

from resilience_support import make_queue

import pytest

from kitchen.resilience.queue import KIND_TELEMETRY
from kitchen.resilience.upload import (
    ClockCalibrator,
    MemoryOfflineStore,
    ReplayUploader,
    build_batch,
    dedup_rows,
)


def fill(queue, count: int, metric: str = "t") -> None:
    for i in range(count):
        queue.append(KIND_TELEMETRY, metric=metric, value=float(i), ts=100.0 + i)


def uploader(tmp_path, **kwargs) -> tuple[ReplayUploader, MemoryOfflineStore, object]:
    queue = make_queue(tmp_path, **{k: v for k, v in kwargs.pop("queue_kwargs", {}).items()})
    store = MemoryOfflineStore()
    return ReplayUploader(queue, store, device_id="K-TEST", **kwargs), store, queue


class TestReplay:
    def test_补传后队列清空且数据入库(self, tmp_path):
        up, store, queue = uploader(tmp_path)
        fill(queue, 5)
        report = up.replay()
        assert (report.batches, report.sent, report.accepted) == (1, 5, 5)
        assert len(queue) == 0 and len(store) == 5
        assert report.ok and report.remaining == 0

    def test_分批上送(self, tmp_path):
        up, store, queue = uploader(tmp_path, queue_kwargs={"device_id": "K-TEST"},
                                    batch_size=10)
        fill(queue, 25)
        report = up.replay()
        assert report.batches == 3
        assert report.sent == 25
        assert len(store.batches) == 3

    def test_同一条数据送两次不会写两行(self, tmp_path):
        up, store, _queue = uploader(tmp_path)
        queue = make_queue(tmp_path, name="repeat.queue")
        fill(queue, 3)
        batch = build_batch("K-TEST", queue.pending()).to_payload()
        first = store.receive(batch)
        second = store.receive(batch)
        assert (first["accepted"], first["duplicates"]) == (3, 0)
        assert (second["accepted"], second["duplicates"]) == (0, 3)
        assert len(store) == 3

    def test_发送失败保留队列(self, tmp_path):
        queue = make_queue(tmp_path)
        fill(queue, 4)

        class Down:
            def receive(self, payload):
                raise ConnectionError("网线拔了")

        report = ReplayUploader(queue, Down(), device_id="K-TEST").replay()
        assert report.sent == 0
        assert len(queue) == 4
        assert report.stopped and not report.ok
        assert "ConnectionError" in report.stopped

    def test_空队列补传是空操作(self, tmp_path):
        up, store, _queue = uploader(tmp_path)
        report = up.replay()
        assert (report.batches, report.sent) == (0, 0)
        assert report.ok

    def test_最大批次数限制剩余(self, tmp_path):
        up, _store, queue = uploader(tmp_path, batch_size=2, max_batches=1)
        fill(queue, 6)
        report = up.replay()
        assert report.sent == 2
        assert report.remaining == 4


class TestConflict:
    def test_同seq不同内容算冲突且不丢数据(self, tmp_path):
        store = MemoryOfflineStore()
        good = {"device_id": "A", "batch_uid": "b1",
                "records": [{"seq": 1, "ts": 100.0, "kind": "telemetry",
                            "metric": "t", "value_num": 1.0}]}
        bad = {"device_id": "A", "batch_uid": "b2",
               "records": [{"seq": 1, "ts": 100.0, "kind": "telemetry",
                            "metric": "t", "value_num": 99.0}]}
        store.receive(good)
        response = store.receive(bad)
        assert response["accepted"] == 0
        assert response["duplicates"] == 0
        assert len(response["conflicts"]) == 1
        assert store.values("t") == [(100.0, 1.0)]   # 先到的留着，没被覆盖

    def test_冲突时停止补传并保留该条(self, tmp_path):
        queue = make_queue(tmp_path)
        fill(queue, 2)
        store = MemoryOfflineStore()
        # 先让服务端已经有一条 seq=1 但内容不同的记录
        store.receive({"device_id": "K-TEST", "batch_uid": "old",
                       "records": [{"seq": 1, "ts": 100.0, "kind": "telemetry",
                                    "metric": "t", "value_num": 999.0}]})
        report = ReplayUploader(queue, store, device_id="K-TEST").replay()
        assert len(report.conflicts) == 1
        assert report.stopped.startswith("seq 冲突")
        assert [entry.seq for entry in queue.pending()] == [1]   # 冲突那条留着

    def test_乱序补传后按时间线排序(self, tmp_path):
        queue = make_queue(tmp_path)
        queue.append(KIND_TELEMETRY, metric="t", value=30.0, ts=130.0)
        queue.append(KIND_TELEMETRY, metric="t", value=10.0, ts=110.0)
        queue.append(KIND_TELEMETRY, metric="t", value=20.0, ts=120.0)
        store = MemoryOfflineStore()
        ReplayUploader(queue, store, device_id="K-TEST").replay()
        assert [value for _ts, value in store.values("t")] == [10.0, 20.0, 30.0]

    def test_断网重连重复补传不出重复点(self, tmp_path):
        """服务端已写入但响应超时 → 端侧不 ack → 下轮重发，页面仍只有一个点。"""
        queue = make_queue(tmp_path, name="flaky.queue")
        fill(queue, 4)
        store = MemoryOfflineStore()

        class FlakyStore:
            def __init__(self):
                self.inner = store
                self.first = True

            def receive(self, payload):
                self.inner.receive(payload)          # 真的写进去了
                if self.first:                       # 但这一次响应丢了
                    self.first = False
                    raise TimeoutError("响应超时")
                return {"accepted": 0, "duplicates": len(payload["records"]),
                        "conflicts": []}

        up = ReplayUploader(queue, FlakyStore(), device_id="K-TEST")
        assert up.replay().sent == 0                  # 第一次失败，队列保留
        assert len(queue) == 4
        second = up.replay()                          # 重发 → 全部被判重复
        assert second.duplicates == 4 and second.accepted == 0
        assert len(store) == 4 and len(store.values("t")) == 4


class TestClock:
    def test_标准样本算出偏移(self):
        cal = ClockCalibrator(min_samples=1)
        # 端侧 0 发出，服务端 100.05 收到、100.10 回，端侧 0.15 收到 → 偏差约 +100
        state = cal.sample(0.0, 100.05, 100.10, 0.15, now=1.0)
        assert state.stable is True
        assert abs(state.offset - 100.05) < 0.06
        assert abs(state.rtt - 0.10) < 1e-6

    def test_RTT过大丢弃样本(self):
        cal = ClockCalibrator(min_samples=1, max_rtt=1.0)
        state = cal.sample(0.0, 50.0, 50.0, 30.0, now=1.0)   # rtt = 30s
        assert state.stable is False
        assert state.rejected == 1

    def test_连续不一致不采纳(self):
        cal = ClockCalibrator(min_samples=3, tolerance=0.5)
        cal.sample(0.0, 10.0, 10.0, 0.1)     # +9.95
        cal.sample(0.0, 40.0, 40.0, 0.1)     # +39.95：极差远超 0.5s
        state = cal.sample(0.0, 70.0, 70.0, 0.1)
        assert state.stable is False
        assert state.rejected >= 1

    def test_一致三次后才采纳(self):
        cal = ClockCalibrator(min_samples=3, tolerance=1.0)
        assert cal.sample(0.0, 5.0, 5.0, 0.1).stable is False
        assert cal.sample(0.0, 5.0, 5.0, 0.1).stable is False
        assert cal.sample(0.0, 5.0, 5.0, 0.1).stable is True

    def test_跳变超过上限不采纳(self):
        cal = ClockCalibrator(min_samples=1, jump_limit=120.0)
        state = cal.sample(0.0, 1000.0, 1000.0, 0.1, now=1.0)
        assert state.stable is False
        assert "跳变上限" in str(state.rejected) or state.rejected == 1

    def test_未校准时不做时间平移(self):
        cal = ClockCalibrator(min_samples=5)
        assert cal.state.to_server(100.0) == 100.0
        cal.sample(0.0, 10.0, 10.0, 0.1)
        assert cal.state.stable is False
        assert cal.state.to_server(100.0) == 100.0

    def test_校准后补传时间被平移(self, tmp_path):
        queue = make_queue(tmp_path, name="clock.queue")
        fill(queue, 2)
        store = MemoryOfflineStore()
        cal = ClockCalibrator(min_samples=1)
        cal.sample(0.0, 50.0, 50.0, 0.1)
        up = ReplayUploader(queue, store, device_id="K-TEST", calibrator=cal)
        up.replay()
        assert store.values("t")[0][0] > 140.0     # 原 ts=100 → 约 150

    def test_状态可描述(self):
        cal = ClockCalibrator(min_samples=1)
        cal.sample(0.0, 3.0, 3.0, 0.1)
        assert "时钟偏差" in cal.state.describe()
        assert ClockCalibrator().state.describe().startswith("时钟未校准")


class TestBatch:
    def test_批次号自动生成(self, tmp_path):
        queue = make_queue(tmp_path)
        fill(queue, 2)
        batch = build_batch("K-TEST", queue.pending())
        assert batch.batch_uid == "K-TEST-1-2"
        assert batch.to_payload()["count"] == 2

    def test_空批次也有批次号(self):
        assert build_batch("A", []).batch_uid == "A-empty"

    def test_服务端兜底去重保序(self):
        rows = [{"seq": 3, "ts": 30}, {"seq": 1, "ts": 10}, {"seq": 3, "ts": 30}]
        out = dedup_rows(rows, "A")
        assert [row["seq"] for row in out] == [1, 3]
        assert all(row["device_id"] == "A" for row in out)

    @pytest.mark.parametrize("size", [0, -1])
    def test_批大小非法报错(self, tmp_path, size):
        queue = make_queue(tmp_path)
        with pytest.raises(ValueError):
            ReplayUploader(queue, MemoryOfflineStore(), batch_size=size)
