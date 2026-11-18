"""迁移文件与数据库结构的校验。

这里测的不是「代码逻辑」，而是「DDL 有没有写对」。因为本机没有 MySQL 服务端，
全部断言跑在 SQLite 上——先把 MySQL 方言翻译成 SQLite 能吃的形态真执行一遍，
再核对结构。语法错、外键悬空、索引列名拼错、漏建表都会当场暴露。

翻译器覆盖不到的写法必须抛异常（不静默跳过），这一点单独有用例钉住。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from tools import migrate


# ---------------------------------------------------------------- 辅助


@pytest.fixture(scope="module")
def run() -> migrate.SqliteRun:
    return migrate.run_sqlite(passes=2)


@pytest.fixture(scope="module")
def db(run: migrate.SqliteRun) -> sqlite3.Connection:
    """跑完迁移 + 种子数据的连接，供数据层断言用。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_migrations(conn)
    _apply_seed(conn)
    yield conn
    conn.close()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for item in migrate.migration_files():
        for stmt in item.statements:
            for translated in migrate.to_sqlite(stmt):
                if translated.kind == "mysql_only":
                    continue
                if translated.kind == "add_column":
                    match = re.match(
                        r"^ALTER\s+TABLE\s+([\w`\".]+)\s+ADD\s+COLUMN\s+([\w`\"]+)",
                        translated.sql, re.I)
                    assert match
                    table = match.group(1).strip('`"')
                    column = match.group(2).strip('`"')
                    existing = {row[1] for row in conn.execute(
                        f'PRAGMA table_info("{table}")')}
                    if column in existing:
                        continue
                try:
                    conn.execute(translated.sql)
                except sqlite3.OperationalError as exc:
                    if "already exists" in str(exc).lower():
                        continue
                    raise


def _seed_statements() -> list[str]:
    seed_path = migrate.MIGRATIONS_DIR.parent / "fixtures" / "seed_kitchen.sql"
    return migrate.split_statements(seed_path.read_text(encoding="utf-8"))


def _apply_seed(conn: sqlite3.Connection) -> None:
    for stmt in _seed_statements():
        for translated in migrate.to_sqlite(stmt):
            conn.execute(translated.sql)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}


# ---------------------------------------------------------------- 文件层


def test_migration_versions_are_contiguous():
    files = migrate.migration_files()
    numbers = [int(item.version) for item in files]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))
    assert numbers[0] == 1, "版本必须从 001 起"


def test_migration_filenames_are_unique_versions():
    files = migrate.migration_files()
    versions = [item.version for item in files]
    assert len(versions) == len(set(versions))


def test_baseline_001_013_is_byte_identical():
    """001-013 已冻结：动了就说明有人回头改已执行的迁移，会让校验和漂移。"""
    problems = migrate.verify_baseline()
    assert problems == [], "\n".join(problems)


def test_baseline_records_every_file_that_exists():
    baseline = migrate.load_baseline()
    recorded = {row["file"] for row in baseline["files"]}
    on_disk = {p.name for p in migrate.MIGRATIONS_DIR.glob("0*.sql")
               if int(p.name[:3]) <= 13}
    assert recorded == on_disk, "BASELINE.json 与磁盘上的 001-013 不一致"


def test_gap_in_versions_is_rejected(tmp_path: Path):
    """编号断档必须报错——否则迁移执行顺序会静默错位。"""
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "003_c.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrate.MigrateError, match="不连续"):
        migrate.migration_files(tmp_path)


def test_duplicate_version_is_rejected(tmp_path: Path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrate.MigrateError, match="重复"):
        migrate.migration_files(tmp_path)


def test_bad_filename_is_rejected(tmp_path: Path):
    (tmp_path / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrate.MigrateError, match="NNN_name"):
        migrate.migration_files(tmp_path)


# ---------------------------------------------------------------- SQL 切分层


def test_split_statements_ignores_semicolon_inside_string():
    sql = "INSERT INTO t VALUES ('a;b'); SELECT 1;"
    assert migrate.split_statements(sql) == ["INSERT INTO t VALUES ('a;b')", "SELECT 1"]


def test_split_statements_drops_line_comments():
    sql = "-- 注释里有个分号; 不该切开\nSELECT 1;\n-- 尾注释"
    assert migrate.split_statements(sql) == ["SELECT 1"]


def test_split_statements_keeps_double_quoted_semicolon():
    sql = 'UPDATE t SET x = "a;b";'
    assert migrate.split_statements(sql) == ['UPDATE t SET x = "a;b"']


def test_split_statement_ignores_comma_inside_enum():
    """ENUM('admin','viewer') 里的逗号不能把列定义切断。"""
    body = "role ENUM('admin','viewer') DEFAULT 'admin', enabled TINYINT DEFAULT 1"
    parts = migrate._split_top_level(body)
    assert len(parts) == 2
    assert parts[0].startswith("role ENUM")


# ---------------------------------------------------------------- 翻译层


def test_unknown_sql_is_rejected_not_skipped():
    """翻译器必须硬失败。静默跳过会让校验假绿，比不校验更糟。"""
    with pytest.raises(migrate.TranslateError, match="未覆盖的语句类型"):
        migrate.to_sqlite("CREATE PROCEDURE p() BEGIN END")


def test_unknown_column_type_is_rejected():
    with pytest.raises(migrate.TranslateError, match="未覆盖的 MySQL 类型"):
        migrate.to_sqlite("CREATE TABLE t (a GEOMETRY NOT NULL)")


def test_auto_increment_becomes_sqlite_rowid_pk():
    out = migrate.to_sqlite(
        "CREATE TABLE t (id BIGINT AUTO_INCREMENT PRIMARY KEY, n VARCHAR(8))")
    create = out[0].sql
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in create
    assert "AUTO_INCREMENT" not in create.upper()


def test_table_level_key_becomes_separate_index():
    out = migrate.to_sqlite(
        "CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, a INT, KEY idx_a (a)) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
    kinds = [item.kind for item in out]
    assert kinds == ["create_table", "create_index"]
    assert "CREATE INDEX IF NOT EXISTS idx_a ON t (a)" == out[1].sql


def test_unique_key_becomes_table_constraint():
    out = migrate.to_sqlite(
        "CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, a INT, UNIQUE KEY uk_a (a))")
    assert "UNIQUE (a)" in out[0].sql


def test_mysql_only_statement_is_flagged():
    out = migrate.to_sqlite("SET @x = 1")
    assert out[0].kind == "mysql_only"


def test_column_after_clause_is_stripped():
    out = migrate.to_sqlite("ALTER TABLE t ADD COLUMN uid VARCHAR(96) NULL AFTER ts")
    assert "AFTER" not in out[0].sql.upper()
    assert "uid" in out[0].sql


# ---------------------------------------------------------------- 执行层


def test_all_migrations_execute_without_error(run: migrate.SqliteRun):
    assert run.passes == 2, "必须跑两遍验证幂等"
    # 015 新增 A11 扩展模块 4 张表（顾客画像/客诉/证据/补偿凭证），
    # 016 新增 A12 经营账本 3 张表（流水/菜品标准成本/期间费用），
    # 017 新增 A13 合规凭证 1 张表（每日检查表快照）
    assert run.total_tables == 51, (
        f"应为 13 张底座 + 29 张新表 + 4 张 A11 + 3 张 A12 + 1 张 A13 + 1 张原料批次 = 51，实际 {run.total_tables}")


def test_mysql_only_statements_are_limited_to_collation_file():
    """只有 011（排序规则统一）应该是纯 MySQL 语句，别的文件出现就是翻译器漏了。"""
    run = migrate.run_sqlite()
    for report in run.reports:
        if report.mysql_only and report.version != "011":
            pytest.fail(f"{report.version} 有 {report.mysql_only} 条语句被当作 MySQL 专有跳过")


def test_every_expected_table_exists(run: migrate.SqliteRun):
    missing = [t for t in migrate.EXPECTED_TABLES if t not in run.tables]
    assert missing == [], f"设计文档要求的表没建出来：{missing}"


def test_expected_table_count_matches_design_doc():
    assert len(migrate.EXPECTED_TABLES) == 30
    assert len(set(migrate.EXPECTED_TABLES)) == 30, "表名不能重复"


def test_design_doc_table_list_matches_code():
    """03_数据库设计.md 里列出的表名必须与 EXPECTED_TABLES 一致，防止文档与代码脱钩。"""
    # kitchen-system/03_设计文档/03_数据库设计.md
    doc = migrate.MIGRATIONS_DIR.parents[2] / "03_设计文档" / "03_数据库设计.md"
    if not doc.is_file():  # 工程目录被单独拷走时跳过
        pytest.skip("找不到设计文档")
    text = doc.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", text, flags=re.M))
    documented.discard("detector_events")  # 扩字段表，不是新建表
    assert documented == set(migrate.EXPECTED_TABLES), (
        f"文档有代码无：{sorted(documented - set(migrate.EXPECTED_TABLES))}；"
        f"代码有文档无：{sorted(set(migrate.EXPECTED_TABLES) - documented)}")


def test_every_business_table_has_primary_key(db: sqlite3.Connection):
    missing = []
    for table in migrate.EXPECTED_TABLES:
        info = list(db.execute(f'PRAGMA table_info("{table}")'))
        assert info, f"{table} 不存在"
        if not any(row[5] for row in info):
            missing.append(table)
    assert missing == [], f"没有主键的表：{missing}"


def test_foreign_keys_point_at_existing_tables(db: sqlite3.Connection):
    existing = _tables(db)
    dangling = []
    for table in sorted(existing):
        for fk in db.execute(f'PRAGMA foreign_key_list("{table}")'):
            target = fk[2]
            if target not in existing:
                dangling.append(f"{table} → {target}")
    assert dangling == [], f"外键指向不存在的表：{dangling}"


def test_required_indexes_cover_their_columns(db: sqlite3.Connection):
    """设计文档 §4 的六个查询场景必须有索引覆盖（认列，不认索引名）。"""
    problems = []
    for table, hint, cols in migrate.REQUIRED_INDEXES:
        covered = False
        for row in db.execute(f'PRAGMA index_list("{table}")'):
            idx_cols = tuple(r[2] for r in db.execute(
                f'PRAGMA index_info("{row[1]}")'))
            if all(c in idx_cols for c in cols):
                covered = True
                break
        if not covered:
            problems.append(f"{table} 没有覆盖 {cols} 的索引（{hint}）")
    assert problems == [], "\n".join(problems)


def test_detector_events_only_gained_columns(db: sqlite3.Connection):
    """只增列：新字段要在，旧字段一个都不能少。"""
    cols = {row[1] for row in db.execute('PRAGMA table_info("detector_events")')}
    for added in ("evidence", "zone_id", "trace_id", "source_device_id",
                  "disposal_state", "review_channel", "reviewed_by", "reviewed_at",
                  "disposal_by", "disposal_at", "work_order_id", "disposal_note"):
        assert added in cols, f"缺扩字段 {added}"
    for original in ("id", "scenario", "camera_id", "classes", "boxes",
                     "confidence", "snapshot", "duration_ms", "ts"):
        assert original in cols, f"旧列 {original} 被删了"


def test_new_tables_declare_explicit_collation():
    """011 的硬要求：新表只写 CHARSET 会引入 utf8mb4_0900_ai_ci，跨表比较报 1267。"""
    offenders = []
    for item in migrate.migration_files():
        if int(item.version) < 14:
            continue
        for stmt in item.statements:
            upper = stmt.upper()
            if not upper.startswith("CREATE TABLE"):
                continue
            if ("CHARSET" in upper or "CHARACTER SET" in upper) and "COLLATE" not in upper:
                name = re.search(
                    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w`\".]+)", stmt, re.I)
                offenders.append(name.group(1) if name else stmt[:40])
    assert offenders == [], f"缺 COLLATE 的表：{offenders}"


def test_env_thresholds_supports_four_judgement_kinds(db: sqlite3.Connection):
    """规则引擎要的四种判据形态必须都有落点，否则 P2 写不下去。"""
    kinds = {row[0] for row in db.execute("SELECT DISTINCT kind FROM env_thresholds")}
    assert {"range", "duration", "window", "tier"} <= kinds, f"实际只有 {kinds}"


def test_disinfection_can_forbid_outbound(db: sqlite3.Connection):
    """#15 不达标「该批餐具禁止出库」必须有字段落点。"""
    cols = {row[1] for row in db.execute('PRAGMA table_info("disinfection_records")')}
    assert "forbid_outbound" in cols


def test_offline_records_dedupes_by_device_and_seq(db: sqlite3.Connection):
    """#17 去重键必须是 (device_id, seq)，重复插入要被数据库挡住。"""
    db.execute("INSERT INTO offline_records (device_id, seq, ts) "
               "VALUES ('dev-x', 1, '2026-11-12 10:00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO offline_records (device_id, seq, ts) "
                   "VALUES ('dev-x', 1, '2026-11-12 10:00:01')")
    db.rollback()


# ---------------------------------------------------------------- 自检聚合


def test_check_passes():
    result = migrate.check()
    assert result["passed"] is True, "\n".join(result["problems"])
    assert result["stats"]["files"] == 19
    assert result["stats"]["tables_total"] == 51


def test_check_catches_missing_collation(tmp_path: Path):
    """自检必须真能拦住问题——否则它只是个摆设。

    COLLATE 规则只对 014+ 生效（001-013 已冻结），所以这里构造一份 014。
    """
    (tmp_path / "014_t.sql").write_text(
        "CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;", encoding="utf-8")
    result = migrate.check(tmp_path)
    assert result["passed"] is False
    assert any("COLLATE" in item for item in result["problems"]), result["problems"]


# ---------------------------------------------------------------- 种子数据


def test_seed_runs_and_is_idempotent(db: sqlite3.Connection):
    seed_path = migrate.MIGRATIONS_DIR.parent / "fixtures" / "seed_kitchen.sql"
    assert seed_path.is_file(), "缺 fixtures/seed_kitchen.sql"
    statements = migrate.split_statements(seed_path.read_text(encoding="utf-8"))
    assert statements, "种子文件是空的"

    def apply() -> None:
        for stmt in statements:
            for translated in migrate.to_sqlite(stmt):
                db.execute(translated.sql)

    apply()
    counts: dict[str, int] = {}
    for table in ("work_stations", "dishes", "staff", "sop_steps",
                  "env_points", "env_thresholds"):
        counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    apply()  # 第二遍不应报错也不应新增行
    for table, expected in counts.items():
        actual = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert actual == expected, f"{table} 重复执行后行数从 {expected} 变成 {actual}"


def test_seed_covers_demo_basics(db: sqlite3.Connection):
    """演示开场就需要的东西必须齐：两个工位、过期健康证、可派单的菜品配方。"""
    stations = {row[0] for row in db.execute("SELECT code FROM work_stations")}
    assert {"ST-A", "ST-B"} <= stations

    expired = db.execute(
        "SELECT COUNT(*) FROM health_certificates WHERE state='expired'").fetchone()[0]
    assert expired >= 1, "#9 演示需要一张已过期健康证来触发拦截"

    dish = db.execute(
        "SELECT d.name FROM dishes d JOIN dish_recipes r ON r.dish_id = d.id "
        "WHERE r.enabled = 1 LIMIT 1").fetchone()
    assert dish, "没有可派单的菜品配方"

    ccp_steps = db.execute("SELECT COUNT(*) FROM sop_steps WHERE ccp = 1").fetchone()[0]
    assert ccp_steps >= 1, "#13 需要至少一个关键控制点才能演示出餐门禁"

    demo_profile = db.execute(
        "SELECT COUNT(*) FROM env_thresholds WHERE profile = 'demo'").fetchone()[0]
    assert demo_profile >= 1, "演示阈值必须有落点，答辩要展示演示值↔真实值对应关系"


def test_seed_thresholds_match_spec_doc(db: sqlite3.Connection):
    """种子里的关键阈值必须与 04_功能规格 一致，改了实现却忘改文档会被这里抓住。

    演示值一律用 range（区间）而不是 tier（单侧门限），与真实值同构：
    演示只是把探头泡进冰水 / 常温水代替真实柜温，不改变判据形态。
    """
    expectations = [
        # (point_id, profile, 说明, lower, upper)
        (4, "demo", "冷藏柜演示阈值",    0.0,   8.0),
        (5, "demo", "冷冻柜演示阈值",  -30.0,   5.0),
        (6, "demo", "留样柜演示阈值",    0.0,  25.0),
        (4, "real", "冷藏柜真实阈值",    0.0,   8.0),
        (6, "real", "留样柜真实阈值",    0.0,   8.0),
    ]
    for point_id, profile, label, lower, upper in expectations:
        row = db.execute(
            "SELECT lower_bound, upper_bound FROM env_thresholds "
            "WHERE point_id=? AND profile=? AND kind='range'",
            (point_id, profile),
        ).fetchone()
        assert row == (lower, upper), f"{label} 应为 {lower}~{upper}，实际 {row}"

    # #2 排烟三档：营业 280 / 备餐 250 / 收档 200
    # 方向一律 upper_bound：这是「超温」判据，填 lower_bound 会变成低温告警，
    # 规则照样装得进去但永不触发（rules 侧 test_超温类阈值方向正确 也锁了这一条）。
    tiers = dict(db.execute(
        "SELECT profile, upper_bound FROM env_thresholds WHERE point_id=2 AND kind='tier'"
    ).fetchall())
    assert tiers == {"business": 280.0, "prep": 250.0, "closed": 200.0}

    # #15 消毒达标区间 100~120℃，异常超温 150℃
    sterilize = db.execute(
        "SELECT lower_bound, upper_bound FROM env_thresholds "
        "WHERE point_id=3 AND kind='range'").fetchone()
    assert sterilize == (100.0, 120.0)

    # #1 燃气两级处置：联锁级 0.55 单边门限 + 预警级 0.45 持续 30 秒
    gas = dict(db.execute(
        "SELECT kind, upper_bound FROM env_thresholds WHERE point_id=1"
    ).fetchall())
    assert gas == {"range": 0.55, "duration": 0.45}
    gas_duration = db.execute(
        "SELECT duration_sec FROM env_thresholds WHERE point_id=1 AND kind='duration'"
    ).fetchone()[0]
    assert gas_duration == 30

    # #2 排烟 10 分钟温升窗口，三档各一条
    windows = {row[0] for row in db.execute(
        "SELECT profile FROM env_thresholds WHERE point_id=2 AND kind='window'")}
    assert windows == {"business", "prep", "closed"}
    assert db.execute(
        "SELECT window_sec FROM env_thresholds WHERE point_id=2 AND kind='window' "
        "AND profile='business'").fetchone()[0] == 600


def test_seed_device_id_matches_scenario_contract(db: sqlite3.Connection):
    """种子里的 device_id 必须是 kitchen.json 声明的设备，否则演示时数据接不上。"""
    scenario_path = migrate.MIGRATIONS_DIR.parent / "scenarios" / "kitchen.json"
    contract = json.loads(scenario_path.read_text(encoding="utf-8"))
    known = {device["id"] for device in contract["devices"]}

    used = {row[0] for row in db.execute(
        "SELECT DISTINCT device_id FROM env_points WHERE device_id IS NOT NULL")}
    used |= {row[0] for row in db.execute(
        "SELECT DISTINCT device_id FROM device_baselines WHERE device_id IS NOT NULL")}
    used |= {row[0] for row in db.execute(
        "SELECT DISTINCT device_id FROM work_stations WHERE device_id IS NOT NULL")}

    assert used <= known, f"种子用了契约里没有的设备：{sorted(used - known)}"
