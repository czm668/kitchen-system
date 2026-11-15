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
    def __init__(self, log: list) -> None:
        self.log = log
        self.closed = False

    def execute(self, sql, args=()):
        self.log.append(("execute", sql, args))

    def executemany(self, sql, rows):
        self.log.append(("executemany", sql, list(rows)))

    def fetchall(self):
        return [("sensor_readings",)]

    def close(self):
        self.closed = True


class _FakeRaw:
    """模拟 pymysql.Connection：__exit__ 是 close（与 sqlite3 不同）。"""

    def __init__(self) -> None:
        self.log: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
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
