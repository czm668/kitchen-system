# -*- coding: utf-8 -*-
"""种子相对日期回归（2026-11-13）。

背景：种子原先把 `shifts.shift_date` 写死 `'2026-11-12'`、有效健康证写死
`'2027-01-01'`，于 11-13 复跑全量时三条刷卡用例全挂——当日排班查不到，
`AdmissionController.scan()` 在「排班」这道门直接拒绝，根本不弹晨检问卷。
再往后推到 2027-05 答辩，两位有效员工的健康证也会全部过期。

修法见 `sqlseed.py` 的 `CURDATE()` 解析：MySQL 执行时由服务端算，端侧解析时
按同一语义翻成 ISO 字符串，**一份种子两边同结果**。这里把「答辩当天重新装配」
变成可注入的断言，而不是靠「今天刚好是写种子那天」碰巧通过。
"""
from datetime import date, datetime, timedelta

from kitchen.domain import CST
from kitchen.sqlseed import cell_value, read_inserts, read_table
from kitchen.staff.admission import AdmissionController
from kitchen.staff.models import DenyReason
from kitchen.staff.repository import DEFAULT_SEED, MemoryStaffRepository

#: 答辩期任意一天，离写种子那天足够远，能把任何写死的日期都暴露出来
DEFENSE_DAY = date(2027, 5, 20)
DEFENSE_NOW = datetime(2027, 5, 20, 9, 0, tzinfo=CST)


def test_排班按注入日期生成():
    rows = read_table(DEFAULT_SEED, "shifts", today=DEFENSE_DAY)
    assert len(rows) == 2
    assert {row["shift_date"] for row in rows} == {DEFENSE_DAY.isoformat()}


def test_健康证在答辩日仍有效_过期样本仍过期():
    rows = {
        int(row["staff_id"]): row
        for row in read_table(DEFAULT_SEED, "health_certificates", today=DEFENSE_DAY)
    }
    # 两张有效证刻意错开：+2 年与 +1 年，保留「只有一人临期」的演示语义
    assert rows[1]["valid_to"] == date(2029, 5, 20).isoformat()
    assert rows[3]["valid_to"] == date(2028, 5, 20).isoformat()
    assert rows[2]["valid_to"] == (DEFENSE_DAY - timedelta(days=180)).isoformat()


def test_答辩日重新装配_刷张三卡走晨检而非被排班挡住():
    repo = MemoryStaffRepository.from_seed(DEFAULT_SEED, today=DEFENSE_DAY)
    result = AdmissionController(repo).scan(
        "A1B2C3D4", station_id=1, now=DEFENSE_NOW
    )
    assert result.needs_morning_check is True
    assert DenyReason.SHIFT_BLOCKED not in result.reasons
    assert DenyReason.CERT_EXPIRED not in result.reasons


def test_答辩日李四仍被健康证拦截():
    """相对化不能把 #9 的演示拦截一起抹平。"""
    repo = MemoryStaffRepository.from_seed(DEFAULT_SEED, today=DEFENSE_DAY)
    result = AdmissionController(repo).scan(
        "E5F6A7B8", station_id=1, now=DEFENSE_NOW
    )
    assert result.ok is False
    assert DenyReason.CERT_EXPIRED in result.reasons


def test_相对日期月末闰日收敛_绝对日期原样保留():
    assert cell_value("CURDATE()", today=date(2027, 3, 31)) == "2027-03-31"
    assert cell_value("CURDATE() - INTERVAL 1 MONTH",
                      today=date(2027, 3, 31)) == "2027-02-28"
    assert cell_value("CURDATE() + INTERVAL 1 YEAR",
                      today=date(2028, 2, 29)) == "2029-02-28"
    assert cell_value("'2026-11-12'", today=DEFENSE_DAY) == "2026-11-12"
    assert cell_value("NULL") is None


def test_整份种子共用同一求值基准():
    """逐表各算一次 today 会在跨午夜解析时前表算昨天、后表算今天。"""
    rows = read_inserts(DEFAULT_SEED, today=DEFENSE_DAY)
    assert rows["shifts"][0]["shift_date"] == DEFENSE_DAY.isoformat()
    assert rows["health_certificates"][0]["valid_to"] == date(2029, 5, 20).isoformat()


def test_MySQL相对日期能翻成SQLite可执行语句():
    """`test_migrations.py` 会把种子真跑进 SQLite；翻译器漏了就是一片语法错。"""
    import sqlite3

    from tools import migrate

    sql = ("INSERT IGNORE INTO shifts (id, shift_date) VALUES "
           "(1, CURDATE()), (2, CURDATE() + INTERVAL 1 YEAR)")
    translated = migrate.to_sqlite(sql)[0].sql
    assert "CURDATE" not in translated.upper()
    assert "INTERVAL" not in translated.upper()

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE shifts (id INTEGER PRIMARY KEY, shift_date TEXT)")
        conn.execute(translated)
        rows = conn.execute("SELECT shift_date FROM shifts ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows[0][0] == date.today().isoformat()
    assert len(rows[1][0]) == 10 and rows[1][0] != rows[0][0]


# ================================================================ 链式时刻（#14 批次）
def test_链式INTERVAL求出当天时刻():
    """批次 produced_at/expire_at 锚定「当天 11:30 / +2 天 11:30」。"""
    from kitchen.sqlseed import SeedError

    assert cell_value(
        "CURDATE() + INTERVAL 11 HOUR + INTERVAL 30 MINUTE", today=DEFENSE_DAY
    ) == "2027-05-20 11:30:00"
    assert cell_value(
        "CURDATE() + INTERVAL 2 DAY + INTERVAL 11 HOUR + INTERVAL 30 MINUTE",
        today=DEFENSE_DAY,
    ) == "2027-05-22 11:30:00"
    # 日期单位在前、时间单位在后：跨日取整再升 datetime
    assert cell_value(
        "CURDATE() - INTERVAL 180 DAY + INTERVAL 9 HOUR", today=DEFENSE_DAY
    ) == (DEFENSE_DAY - timedelta(days=180)).isoformat() + " 09:00:00"
    # 时间单位后跟日期单位是非法链式顺序（MySQL 会丢时间/触发月末收敛），
    # 宁可显式报错也不静默给错值。
    import pytest

    with pytest.raises(SeedError):
        cell_value("CURDATE() + INTERVAL 11 HOUR + INTERVAL 2 DAY")


def test_批次在答辩日装配后仍落在当日():
    """#14 应留样清单的跨日修复：写死 2026-11-12 会让清单从 11/13 起恒空。"""
    rows = read_table(DEFAULT_SEED, "dish_batches", today=DEFENSE_DAY)
    assert rows[0]["produced_at"] == "2027-05-20 11:30:00"
    assert rows[0]["expire_at"] == "2027-05-22 11:30:00"
    assert rows[0]["batch_no"] == "B-DEMO-001"
    # 48h 保质期：expire - produced == 2 天整
    produced = datetime.fromisoformat(rows[0]["produced_at"])
    expire = datetime.fromisoformat(rows[0]["expire_at"])
    assert (expire - produced) == timedelta(hours=48)


def test_批次的MySQL链式时刻翻成SQLite可执行语句():
    import sqlite3

    from tools import migrate

    sql = ("INSERT IGNORE INTO dish_batches (id, batch_no, produced_at, expire_at) "
           "VALUES (1, 'B-DEMO-001', "
           "CURDATE() + INTERVAL 11 HOUR + INTERVAL 30 MINUTE, "
           "CURDATE() + INTERVAL 2 DAY + INTERVAL 11 HOUR + INTERVAL 30 MINUTE)")
    translated = migrate.to_sqlite(sql)[0].sql
    assert "CURDATE" not in translated.upper()
    assert "INTERVAL" not in translated.upper()
    # 含时间项必须升级成 datetime(date(...), mods...)，date() 会丢时刻
    assert "datetime(" in translated

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE dish_batches (id INTEGER PRIMARY KEY, batch_no TEXT,"
            " produced_at TEXT, expire_at TEXT)"
        )
        conn.execute(translated)
        row = conn.execute(
            "SELECT produced_at, expire_at FROM dish_batches"
        ).fetchone()
    finally:
        conn.close()
    today = date.today().isoformat()
    assert row[0] == f"{today} 11:30:00"
    assert row[1] == (date.today() + timedelta(days=2)).isoformat() + " 11:30:00"
