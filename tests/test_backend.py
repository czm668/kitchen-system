# -*- coding: utf-8 -*-
"""A18 双后端适配层：URL 解析、脱敏、占位符/事务方言、SQLite 零回归。

真 MySQL 连接由 `docker compose up` 后的容器实测覆盖（见
`05_系统记录/20261115_A18MySQL接线落地记录.md`），这里用 fake 连接把
MySQL 分支的方言转换也测到，不需要本机起数据库。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from kitchen.storage import (
    RuntimeDB, SqliteRecorder, mask_url, open_runtime, parse_mysql_url,
)
from kitchen.storage.backend import _ensure_mysql_schema

TS = datetime(2026, 11, 15, 14, 30, 0)


# ---------------------------------------------------------------- URL
def test_URL解析_基本():
    cfg = parse_mysql_url("mysql://kitchen:s3cr3t@mysql:3306/kitchen")
    assert cfg == {
        "host": "mysql", "port": 3306, "user": "kitchen",
        "password": "s3cr3t", "database": "kitchen",
    }


def test_URL解析_默认端口库名与转义():
    cfg = parse_mysql_url("mysql+pymysql://u:p%40ss@10.0.0.1/db2")
    assert cfg["port"] == 3306
    assert cfg["password"] == "p@ss"
    assert cfg["database"] == "db2"


def test_URL解析_口令含未编码的at符号也能容错():
    # compose 用 ${MYSQL_PASSWORD} 拼 URL，口令里带 @ 时不能要求用户手动转义
    cfg = parse_mysql_url("mysql://kitchen:p@ssw0rd@mysql:3306/kitchen")
    assert cfg["host"] == "mysql" and cfg["port"] == 3306
    assert cfg["user"] == "kitchen" and cfg["password"] == "p@ssw0rd"
    assert cfg["database"] == "kitchen"


def test_脱敏_口令含at符号也不会漏出():
    shown = mask_url("mysql://kitchen:p@ssw0rd@mysql:3306/kitchen")
    assert "ssw0rd" not in shown and "***" in shown


def test_URL解析_拒绝非mysql与缺用户():
    with pytest.raises(ValueError):
        parse_mysql_url("sqlite:///var/runtime.db")
    with pytest.raises(ValueError):
        parse_mysql_url("mysql://mysql:3306/kitchen")


def test_脱敏_密码不出现在任何展示():
    url = "mysql://kitchen:SuperSecret123@mysql:3306/kitchen"
    shown = mask_url(url)
    assert "SuperSecret123" not in shown
    assert "***" in shown
    assert shown.endswith("@mysql:3306/kitchen")


# ---------------------------------------------------------------- 方言
class _FakeCursor:
    def __init__(self, raw: "_FakeRaw") -> None:
        self.raw = raw
        self.log = raw.log
        self.closed = False
        self.last_sql = ""

    def execute(self, sql, args=()):
        self.last_sql = sql
        self.log.append(("execute", sql, args))

    def executemany(self, sql, rows):
        self.log.append(("executemany", sql, list(rows)))

    def fetchall(self):
        sql = self.last_sql.strip().upper()
        if sql.startswith("SELECT COUNT(*)"):
            return [(self.raw.count_value,)]
        if "UNION ALL" in sql:
            # stats 的聚合查询：每行 (表名, 行数)，count 用 0 代表空表
            return [("sensor_readings", 0), ("alarm_events", 0)]
        return [("sensor_readings",), ("alarm_events",)]

    def close(self):
        self.closed = True


class _FakeRaw:
    """模拟 pymysql.Connection：__exit__ 是 close（与 sqlite3 不同）。"""

    def __init__(self) -> None:
        self.log: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.close_calls = 0
        self.count_value = 2  # 供 _ensure_mysql_schema 的 COUNT 探测返回

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.close_calls += 1
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _mysql_db():
    raw = _FakeRaw()
    return raw, RuntimeDB(
        raw, dialect="mysql", display="mysql://u:***@h/db",
        errors=(sqlite3.Error,),
    )


def test_MySQL占位符转换():
    raw, db = _mysql_db()
    db.execute("INSERT INTO t (a, b) VALUES (?, 'rule', ?)", (1, 2))
    kind, sql, args = raw.log[-1]
    assert kind == "execute"
    assert sql == "INSERT INTO t (a, b) VALUES (%s, 'rule', %s)"
    assert args == (1, 2)
    assert "%s, 'rule', %s" in sql  # 字面量 'rule' 不被引擎误改


def test_MySQL_executemany与标识符引号():
    raw, db = _mysql_db()
    db.executemany("INSERT INTO t (a) VALUES (?)", [(1,), (2,)])
    assert raw.log[-1][1] == "INSERT INTO t (a) VALUES (%s)"
    assert db._quote("sensor_readings") == "`sensor_readings`"
    assert db.placeholder == "%s"


def test_事务正常提交_异常回滚_且不会误关连接():
    raw, db = _mysql_db()
    with db.transaction():
        db.execute("INSERT INTO t (a) VALUES (?)", (1,))
    assert raw.commits == 1 and raw.rollbacks == 0 and raw.closed is False

    raw2, db2 = _mysql_db()
    with pytest.raises(RuntimeError):
        with db2.transaction():
            raise RuntimeError("boom")
    assert raw2.rollbacks == 1 and raw2.commits == 0
    assert raw2.closed is False  # 关键：pymysql 原生 __exit__ 会 close，这里没有


def test_MySQL_stats查information_schema():
    raw, db = _mysql_db()
    db.stats()
    sqls = [entry[1] for entry in raw.log]
    assert any("information_schema.tables" in s for s in sqls)
    assert any("`sensor_readings`" in s for s in sqls)


def test_MySQL_stats_单次往返聚合而非N加1():
    # 修复点：原先是 N+1（每张表一条 COUNT(*)），改为「取表名 + 一条 UNION ALL」
    # 两次往返。这里验证查询次数固定为 2，且结果精确（非 InnoDB table_rows 估算）。
    raw, db = _mysql_db()
    before = len(raw.log)
    result = db.stats()
    added = raw.log[before:]
    assert len([e for e in added if e[0] == "execute"]) == 2
    assert any("information_schema.tables" in e[1] for e in added)
    assert any("UNION ALL" in e[1] for e in added)
    # 不存在逐表 `SELECT COUNT(*) FROM <表>` 这种 N+1 形态
    assert "SELECT COUNT(*) FROM" not in " | ".join(e[1] for e in added)
    assert result == {"sensor_readings": 0, "alarm_events": 0}


def test_ensure_mysql_schema_表就绪不重跑DDL():
    # 修复点：schema 唯一来源是 compose 挂载的 initdb，不再用 pymysql 重跑一份
    # migrations DDL（消除两份执行通道的潜在分叉）。表就绪时应立即返回，且
    # 不执行任何 CREATE/ALTER 语句。
    raw, db = _mysql_db()
    raw.count_value = 2
    _ensure_mysql_schema(db)
    ddl = [
        e for e in raw.log
        if e[0] == "execute" and (
            "CREATE TABLE" in e[1].upper() or "ALTER TABLE" in e[1].upper()
        )
    ]
    assert ddl == []


def test_ensure_mysql_schema_缺失超时抛错(monkeypatch):
    # 关键表缺失时轮询等待，超时后明确报错指向 initdb（restart policy 会重试）。
    raw, db = _mysql_db()
    raw.count_value = 0
    import time
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError) as exc:
        _ensure_mysql_schema(db, timeout=0.05)
    assert "initdb" in str(exc.value)


# ---------------------------------------------------------------- SQLite 零回归
def test_SQLite仍是默认后端_构造签名与conn兼容(tmp_path):
    rec = SqliteRecorder(tmp_path / "run.db")
    assert rec.conn.dialect == "sqlite"
    assert rec.conn.placeholder == "?"
    assert isinstance(rec.path, Path)
    assert rec.record_frame({"t": 25.5, "bad": "x"}, TS) == 1
    assert rec.readings_history(metric="t")[0]["value"] == 25.5
    assert rec.stats()["backend"] == "sqlite"
    # 既有单测依赖 .conn 可被直接关闭（写失败只计数，不抛）
    rec.conn.close()
    assert rec.record_frame({"t": 1.0}, TS) == 0
    assert rec.write_failures == 1
    rec.close()


def test_open_runtime_按target分流(tmp_path):
    db = open_runtime(tmp_path / "x.db")
    assert db.dialect == "sqlite"
    assert db.display == str(tmp_path / "x.db")
    db.close()


def test_写失败计数_两种异常都兜住(tmp_path):
    rec = SqliteRecorder(tmp_path / "run.db")
    rec.conn.close()
    assert rec.record_event("rule", {"message": "x"}, TS) is False
    assert rec.write_failures == 1
    rec.close()


def test_record_event_source映射到ENUM合法值(tmp_path):
    # alarm_events.source 是 ENUM('rule','detector','llm')：guard/fault-escalate
    # 不能写死 'rule'，否则在 MySQL 严格模式下插 ENUM 非法值会报错。
    rec = SqliteRecorder(tmp_path / "run.db")
    assert rec.record_event("rule", {"message": "r"}, TS) is True
    assert rec.record_event("guard", {"message": "g"}, TS) is True
    assert rec.record_event("fault-escalate", {"message": "f"}, TS) is True
    rows = rec.conn.query(
        "SELECT scenario, source FROM alarm_events ORDER BY id")
    assert rows == [
        ("rule", "rule"),
        ("guard", "rule"),
        ("fault-escalate", "detector"),
    ]
    rec.close()


def test_close幂等_多次调用只关一次底层连接():
    raw, db = _mysql_db()
    db.close()
    db.close()  # 第二次不应重复关闭底层连接
    assert raw.close_calls == 1
    assert raw.closed is True


def test_关闭时提交失败必须抛出而不是静默吞掉():
    """RL001 回归：close() 曾把 commit 异常 `pass` 掉，"写成功"成了假象。

    背景：审查报告 §5.2 第一大根因"静默成功"；本用例钉住"失败可见"这条不变量。
    """
    class _CommitFails(_FakeRaw):
        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

    raw = _CommitFails()
    db = RuntimeDB(
        raw, dialect="mysql", display="mysql://u:***@h/db", errors=(sqlite3.Error,),
    )
    with pytest.raises(sqlite3.OperationalError):
        db.close()
    # 抛异常也必须把底层连接关掉，否则连接泄漏
    assert raw.close_calls == 1 and raw.closed is True
    # 已关闭后再调用不重复抛，close 保持幂等
    db.close()


def test_关闭时提交失败必须抛出而不是静默吞掉():
    """RL001 回归：close() 曾把 commit 异常 `pass` 掉，"写成功"成了假象。

    背景：审查报告 §5.2 第一大根因"静默成功"；本用例钉住"失败可见"这条不变量。
    """
    class _CommitFails(_FakeRaw):
        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

    raw = _CommitFails()
    db = RuntimeDB(
        raw, dialect="mysql", display="mysql://u:***@h/db", errors=(sqlite3.Error,),
    )
    with pytest.raises(sqlite3.OperationalError):
        db.close()
    # 抛异常也必须把底层连接关掉，否则连接泄漏
    assert raw.close_calls == 1 and raw.closed is True
    # 已关闭后再调用不重复抛，close 保持幂等
    db.close()
