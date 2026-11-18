# -*- coding: utf-8 -*-
"""A1 运行时库：建库同源、遥测/告警落库、跨重启历史、AppState 接线。

口径：
- DDL 必须来自 migrations/001-019（同一份源经 tools.migrate 翻译），
  表数与主控清单口径（42 主线表 + 4 张 A11 + 3 张 A12 + 1 张 A13 + 1 张 A24 = 51）一致；
- `db_path=None` 时行为与旧版完全一致（纯内存）——既有 1242 条用例零影响。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from kitchen.api import AppState
from kitchen.storage import SqliteRecorder, db_stats, open_runtime_db

from api_support import data_of, login, make_server, make_state

TS = datetime(2026, 11, 13, 11, 30, 0)


def test_建库与迁移同源_51表(tmp_path):
    conn = open_runtime_db(tmp_path / "run.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert {"sensor_readings", "alarm_events", "audit_logs", "devices"} <= tables
    assert len(tables) == 51, sorted(tables)
    assert db_stats(conn)["sensor_readings"] == 0
    conn.close()


def test_遥测帧写入与坏值过滤(tmp_path):
    rec = SqliteRecorder(tmp_path / "run.db")
    n = rec.record_frame(
        {"t": 25.5, "h": 60.0, "bad": "x", "nan": float("nan")}, TS
    )
    assert n == 2  # 字符串与 NaN 不进库（口径同 TelemetryRing.push）
    rows = rec.readings_history(metric="t")
    assert rows[0]["value"] == 25.5
    assert rows[0]["ts"].startswith("2026-11-13 11:30:00")
    rec.close()


def test_告警事件进库_业务事件忽略(tmp_path):
    rec = SqliteRecorder(tmp_path / "run.db")
    ok = rec.record_event(
        "rule",
        {"rule_id": "gas_high", "level": 3, "message": "燃气浓度超限",
         "metric": "gas_level"},
        TS,
    )
    assert ok is True
    assert rec.record_event("order", {"rule_id": "x"}, TS) is False  # 业务事件不进 alarm_events
    alarms = rec.alarms_history()
    assert alarms[0]["atype"] == "gas_high"
    assert alarms[0]["level"] == 3
    assert alarms[0]["scenario"] == "rule"
    rec.close()


def test_写失败只计数不抛(tmp_path):
    rec = SqliteRecorder(tmp_path / "run.db")
    rec.conn.close()  # 人为制造库失败
    assert rec.record_frame({"t": 1.0}, TS) == 0
    assert rec.record_event("rule", {"message": "x"}, TS) is False
    assert rec.write_failures == 2
    rec.close()  # 幂等，不抛


def test_跨重启历史仍在(tmp_path):
    path = tmp_path / "run.db"
    rec = SqliteRecorder(path)
    rec.record_frame({"t": 25.5}, TS)
    rec.record_event("rule", {"message": "燃气浓度超限"}, TS)
    rec.close()

    rec2 = SqliteRecorder(path)  # 重开：幂等建库不重建数据
    assert rec2.readings_history(metric="t")[0]["value"] == 25.5
    assert rec2.alarms_history()[0]["message"] == "燃气浓度超限"
    rec2.close()


def test_AppState默认不落库_接口报未启用(tmp_path):
    state = make_state(tmp_path)
    state.tick()
    assert state.recorder is None
    server = make_server(tmp_path, state)
    session = login(server)
    stats = data_of(server, "GET", "/api/admin/db/stats", session=session)
    assert stats["enabled"] is False
    hist = data_of(server, "GET", "/api/admin/db/history", session=session)
    assert hist == {"enabled": False, "readings": [], "alarms": []}
    state.stop()


def test_AppState开启落库_接口可查(tmp_path):
    db_path = tmp_path / "runtime.db"
    state = make_state(tmp_path, db_path=db_path)
    state.tick()  # demo 帧一帧 = 12 指标
    assert state.recorder is not None
    assert state.recorder.frames_written == 1

    server = make_server(tmp_path, state)
    session = login(server)
    stats = data_of(server, "GET", "/api/admin/db/stats", session=session)
    assert stats["enabled"] is True
    assert stats["tables"]["sensor_readings"] == 12
    assert stats["frames_written"] == 1

    hist = data_of(
        server, "GET", "/api/admin/db/history?metric=t&limit=10", session=session
    )
    assert hist["enabled"] is True
    assert hist["readings"] and hist["readings"][0]["metric"] == "t"

    # 业务事件走 emit：rule/guard 类进库（demo 帧自己也会触发规则告警），订单类不进
    state.emit("rule", {"rule_id": "gas_high", "level": 3, "message": "燃气浓度超限"})
    state.emit("order", {"message": "新订单"})
    alarms = state.recorder.alarms_history()
    atypes = {row["atype"] for row in alarms}
    assert "gas_high" in atypes
    assert all(row["scenario"] in ("rule", "guard", "fault-escalate") for row in alarms)

    state.stop()  # stop 关库
    assert db_path.exists()
