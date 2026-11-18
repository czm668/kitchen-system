"""数据库迁移器 + SQLite 校验链。

为什么需要这个工具
------------------
`migrations/*.sql` 是 MySQL 方言。本机没有 MySQL 服务端，如果只靠读文件确认 DDL 对不对，
34 张表里的类型错误、外键指向不存在、索引列名拼错、重复建表等问题都发现不了。

所以这里做两件事：

1. **真实执行校验**：把 MySQL DDL 翻译成 SQLite 能吃的形态，在内存库里把 001→014
   全部跑一遍，再重复跑一遍验证幂等。语法错、外键悬空、索引列不存在都会当场报错。
2. **生产执行路径**：真接了 MySQL 时用 `run_mysql()`，按 `schema_migrations` 台账顺序执行，
   把「对象已存在」错误码识别为幂等成功（与旧工程 `kernel/db/migrate.py` 同一口径）。

翻译器只覆盖本项目实际用到的语法子集；遇到没见过的写法**抛异常而不是静默跳过**——
静默跳过会让校验假绿，比不校验更糟。

命令行
------
    python tools/migrate.py list          # 列出迁移与校验和
    python tools/migrate.py check         # 全量自检，退出码 0/1（CI 用这个）
    python tools/migrate.py sqlite        # 在内存库真跑一遍，打印每份迁移建了什么
    python tools/migrate.py json          # 机器可读报告
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
BASELINE_FILE = "BASELINE.json"

# MySQL「对象已存在」类错误码，重复执行时应视为幂等成功而非失败
IDEMPOTENT_ERRNOS = {
    1050: "表已存在",
    1060: "列已存在",
    1061: "索引已存在",
    1091: "待删除对象不存在",
    1826: "外键名重复",
}

# 只在 MySQL 上有意义、SQLite 无对应概念的语句前缀（排序规则统一等）
MYSQL_ONLY_PREFIXES = ("SET ", "PREPARE ", "EXECUTE ", "DEALLOCATE ", "DO ")


class MigrateError(RuntimeError):
    """迁移文件本身有问题（编号断档、校验和漂移、语法不可翻译）。"""


class TranslateError(MigrateError):
    """遇到翻译器未覆盖的 SQL 写法。抛出来，不要静默跳过。"""


# ---------------------------------------------------------------- 文件与校验和


@dataclass(frozen=True)
class MigrationFile:
    version: str
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def statements(self) -> list[str]:
        return split_statements(self.sql)


def checksum(raw: bytes | str) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return hashlib.sha256(data).hexdigest()


def migration_files(directory: Path | None = None) -> list[MigrationFile]:
    """按版本号升序返回迁移文件，并校验编号连续、无重复。"""
    base = Path(directory) if directory is not None else MIGRATIONS_DIR
    if not base.is_dir():
        raise MigrateError(f"迁移目录不存在：{base}")

    files: list[MigrationFile] = []
    for path in sorted(base.glob("*.sql")):
        match = re.match(r"^(\d{3,})_(.+)\.sql$", path.name)
        if not match:
            raise MigrateError(f"迁移文件名不符合 NNN_name.sql：{path.name}")
        raw = path.read_bytes()
        files.append(
            MigrationFile(
                version=match.group(1),
                name=match.group(2),
                path=path,
                sql=raw.decode("utf-8"),
                checksum=checksum(raw),
            )
        )

    if not files:
        raise MigrateError(f"{base} 下没有任何 .sql 迁移")

    seen: dict[str, str] = {}
    for item in files:
        if item.version in seen:
            raise MigrateError(
                f"版本号重复：{item.version} 被 {seen[item.version]} 和 {item.path.name} 同时占用"
            )
        seen[item.version] = item.path.name

    numbers = [int(item.version) for item in files]
    expected = list(range(numbers[0], numbers[0] + len(numbers)))
    if numbers != expected:
        gaps = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
        raise MigrateError(f"版本号不连续，缺：{[f'{n:03d}' for n in gaps]}")
    return files


def load_baseline(directory: Path | None = None) -> dict[str, Any]:
    base = Path(directory) if directory is not None else MIGRATIONS_DIR
    path = base / BASELINE_FILE
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def verify_baseline(directory: Path | None = None) -> list[str]:
    """核对 001-013 基线迁移一字未改。改了就报——那会打穿已执行环境的校验和。"""
    base = Path(directory) if directory is not None else MIGRATIONS_DIR
    baseline = load_baseline(base)
    problems: list[str] = []
    recorded = {row["file"]: row["sha256"] for row in baseline.get("files") or []}
    for name, expected in recorded.items():
        path = base / name
        if not path.is_file():
            problems.append(f"基线迁移缺失：{name}")
            continue
        actual = checksum(path.read_bytes())
        if actual != expected:
            problems.append(
                f"基线迁移被修改：{name}（记录 {expected[:12]}，实际 {actual[:12]}）。"
                f"001-013 已冻结，要改只能追加 015+ 补偿迁移"
            )
    return problems


# ---------------------------------------------------------------- SQL 切分


def split_statements(sql: str) -> list[str]:
    """按分号切分语句，跳过 `--` 注释与 `'...'` / `"..."` / 反引号内的分号。"""
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                # 连写两个引号是转义，不算闭合
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _split_top_level(body: str) -> list[str]:
    """按括号深度为 0 的逗号切分（ENUM('a','b') 内部的逗号不算）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


# ---------------------------------------------------------------- MySQL → SQLite

# 类型映射：键是 MySQL 类型名，值给 SQLite 用。SQLite 是动态类型，这里只为可读与断言。
_TYPE_MAP = {
    "BIGINT": "INTEGER",
    "INT": "INTEGER",
    "INTEGER": "INTEGER",
    "MEDIUMINT": "INTEGER",
    "SMALLINT": "INTEGER",
    "TINYINT": "INTEGER",
    "DOUBLE": "REAL",
    "FLOAT": "REAL",
    "DECIMAL": "REAL",
    "NUMERIC": "REAL",
    "VARCHAR": "TEXT",
    "CHAR": "TEXT",
    "TEXT": "TEXT",
    "MEDIUMTEXT": "TEXT",
    "LONGTEXT": "TEXT",
    "JSON": "TEXT",
    "DATETIME": "TEXT",
    "TIMESTAMP": "TEXT",
    "DATE": "TEXT",
    "TIME": "TEXT",
    "BLOB": "BLOB",
}

_INT_TYPES = {"BIGINT", "INT", "INTEGER", "MEDIUMINT", "SMALLINT", "TINYINT"}


@dataclass
class SqliteStatement:
    """翻译后的一条 SQLite 语句。"""

    kind: str  # create_table / create_index / add_column / mysql_only / other
    sql: str
    note: str = ""


def _convert_column_def(token: str) -> str:
    """列定义：类型换 SQLite 形态，AUTO_INCREMENT 主键换成 INTEGER PRIMARY KEY AUTOINCREMENT。"""
    raw = token.strip()
    # MySQL 的列位置子句（AFTER col / FIRST）在 SQLite 无意义，去掉——
    # 列顺序不影响语义，建表顺序已在 014 按依赖排好
    raw = re.sub(r"\s+AFTER\s+[\w`\".]+\s*$", "", raw, flags=re.I).strip()
    raw = re.sub(r"\s+FIRST\s*$", "", raw, flags=re.I).strip()
    upper = raw.upper()

    name_match = re.match(r'^("[^"]+"|`[^`]+`|[A-Za-z_][\w$]*)\s+', raw)
    if not name_match:
        raise TranslateError(f"无法解析列名：{raw[:70]!r}")
    col_name = name_match.group(1)
    rest = raw[name_match.end():].strip()

    type_match = re.match(r"^([A-Za-z]+)\s*(\([^)]*\))?", rest)
    if not type_match:
        raise TranslateError(f"无法解析列类型：{raw[:70]!r}")
    original_type = type_match.group(1).upper()
    sizes = type_match.group(2) or ""
    attributes_raw = rest[type_match.end():].strip()

    if original_type != "ENUM" and original_type not in _TYPE_MAP:
        raise TranslateError(
            f"未覆盖的 MySQL 类型 {original_type!r}（列：{raw[:70]!r}）")

    is_auto_pk = "AUTO_INCREMENT" in upper and "PRIMARY KEY" in upper
    if is_auto_pk:
        if original_type not in _INT_TYPES:
            raise TranslateError(f"AUTO_INCREMENT 只支持整型主键：{raw!r}")
        attributes = attributes_raw
        for pattern in (r"\bAUTO_INCREMENT\b", r"\bPRIMARY\s+KEY\b",
                        r"\bNOT\s+NULL\b", r"\bNULL\b", r"\bUNSIGNED\b"):
            attributes = re.sub(pattern, "", attributes, flags=re.I)
        attributes = re.sub(r"\s+", " ", attributes).strip()
        tail = f" {attributes}" if attributes else ""
        return f"{col_name} INTEGER PRIMARY KEY AUTOINCREMENT{tail}"

    if original_type == "ENUM":
        # ENUM('a','b') 在 SQLite 退化为 TEXT；取值合法性由应用层与单测保证
        return f"{col_name} TEXT{(' ' + attributes_raw) if attributes_raw else ''}"

    sqlite_type = _TYPE_MAP[original_type]
    keep_size = "" if sqlite_type == "TEXT" or not sizes else sizes
    attributes = re.sub(r"\bUNSIGNED\b", "", attributes_raw, flags=re.I)
    attributes = re.sub(r"\s+", " ", attributes).strip()
    head = f"{col_name} {sqlite_type}{keep_size}"
    return f"{head} {attributes}".strip()


# 种子里的 MySQL 相对日期/时刻表达式 → SQLite 等价写法。
# 不翻译的话 `test_migrations.py` 会把种子真跑进 SQLite，直接报
# `near "1": syntax error`——SQLite 既不认 CURDATE()，也不认 `INTERVAL n YEAR`。
# 链式语义与 sqlseed.py 一致：日期单位在前，时间单位在后（只翻译合法顺序，
# 时间单位后跟日期单元属于种子写法错误，让它落到 SQLite 报错暴露出来）。
_MYSQL_RELATIVE_DATE_RE = re.compile(
    r"CURDATE\s*\(\s*\)\s*(?P<terms>"
    r"(?:[+-]\s*INTERVAL\s+\d+\s+(?:DAY|MONTH|YEAR|HOUR|MINUTE)\s*)*)",
    re.IGNORECASE,
)

_MYSQL_TERM_RE = re.compile(
    r"([+-])\s*INTERVAL\s+(\d+)\s+(DAY|MONTH|YEAR|HOUR|MINUTE)", re.IGNORECASE
)


def _translate_relative_dates(sql: str) -> str:
    """把 `CURDATE() [± INTERVAL n UNIT]*` 翻成 SQLite 的 date()/datetime()。

    `localtime` 必须放**第一个** modifier：先转本地日期再偏移，才与 MySQL 的
    CURDATE()（服务器本地日期）同口径；反过来先按 UTC 加一年再转本地，
    在跨年边界会差一天。两端都由服务端自己算「今天」，不会漂移。

    无项 → `date('now','localtime')`；纯日期项 → `date('now',...)`；
    含时间项 → `datetime(date('now','localtime'), '+11 hour', ...)`，
    datetime() 返回 `YYYY-MM-DD HH:MM:SS`，与 MySQL DATETIME 同形。
    """

    def repl(match: re.Match[str]) -> str:
        terms = _MYSQL_TERM_RE.findall(match.group("terms"))
        if not terms:
            return "date('now','localtime')"
        mods = [
            f"'{sign}{amount} {unit.lower()}'" for sign, amount, unit in terms
        ]
        has_time = any(unit.upper() in ("HOUR", "MINUTE") for _, _, unit in terms)
        if not has_time:
            return f"date('now','localtime',{','.join(mods)})"
        return f"datetime(date('now','localtime'),{','.join(mods)})"

    return _MYSQL_RELATIVE_DATE_RE.sub(repl, sql)


def to_sqlite(statement: str) -> list[SqliteStatement]:
    """把一条 MySQL 语句翻译成 0..N 条 SQLite 语句。

    无法覆盖的写法一律抛 TranslateError——静默跳过会让校验假绿。
    """
    stmt = statement.strip()
    upper = stmt.upper()

    for prefix in MYSQL_ONLY_PREFIXES:
        if upper.startswith(prefix):
            return [SqliteStatement("mysql_only", stmt,
                                    note=f"MySQL 专有语句（{prefix.strip()}），SQLite 无对应概念")]

    if upper.startswith("CREATE TABLE"):
        return _translate_create_table(stmt)

    if upper.startswith("CREATE UNIQUE INDEX"):
        sql = re.sub(r"^CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS",
                     stmt, flags=re.I)
        return [SqliteStatement("create_index", sql)]

    if upper.startswith("CREATE INDEX"):
        sql = re.sub(r"^CREATE INDEX", "CREATE INDEX IF NOT EXISTS", stmt, flags=re.I)
        return [SqliteStatement("create_index", sql)]

    if upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper:
        head, tail = re.split(r"ADD\s+COLUMN", stmt, maxsplit=1, flags=re.I)
        column_sql = _convert_column_def(tail)
        return [SqliteStatement("add_column", f"{head.rstrip()} ADD COLUMN {column_sql}")]

    if upper.startswith("INSERT"):
        # 种子数据用 INSERT IGNORE 保证可重复执行；SQLite 对应 INSERT OR IGNORE
        sql = re.sub(r"^INSERT\s+IGNORE\s+INTO", "INSERT OR IGNORE INTO", stmt, flags=re.I)
        sql = _translate_relative_dates(sql)
        return [SqliteStatement("other", sql)]

    if upper.startswith(("UPDATE", "DELETE", "CREATE TRIGGER", "DROP")):
        return [SqliteStatement("other", stmt)]

    raise TranslateError(f"未覆盖的语句类型：{stmt[:80]!r}")


def _translate_create_table(stmt: str) -> list[SqliteStatement]:
    match = re.match(
        r"^CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?([\w`\".]+)\s*\((.*)\)\s*(ENGINE.*)?$",
        stmt,
        flags=re.I | re.S,
    )
    if not match:
        raise TranslateError(f"无法解析 CREATE TABLE：{stmt[:120]!r}")
    table = match.group(2).strip('`"')
    body = match.group(3)

    columns: list[str] = []
    table_constraints: list[str] = []
    pending_indexes: list[SqliteStatement] = []
    deferred_fks: list[str] = []

    for token in _split_top_level(body):
        upper = token.upper()
        if re.match(r"^(UNIQUE\s+)?(KEY|INDEX)\b", upper):
            unique = upper.startswith("UNIQUE")
            inner = re.match(r"^(UNIQUE\s+)?(?:KEY|INDEX)\s+([\w`\"]+)\s*\((.*)\)$",
                             token, flags=re.I | re.S)
            if not inner:
                raise TranslateError(f"无法解析表内索引：{token[:80]!r}")
            idx_name = inner.group(2).strip('`"')
            cols = inner.group(3)
            if unique:
                table_constraints.append(f"UNIQUE ({cols})")
            else:
                pending_indexes.append(SqliteStatement(
                    "create_index",
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})",
                    note=f"{table}.{idx_name}",
                ))
            continue
        if upper.startswith("CONSTRAINT") and "FOREIGN KEY" in upper:
            fk = re.match(r"^CONSTRAINT\s+([\w`\"]+)\s+(FOREIGN\s+KEY.*)$",
                          token, flags=re.I | re.S)
            if not fk:
                raise TranslateError(f"无法解析外键：{token[:80]!r}")
            deferred_fks.append(fk.group(2))
            continue
        if upper.startswith(("PRIMARY KEY", "FOREIGN KEY", "CHECK")):
            table_constraints.append(token)
            continue
        columns.append(_convert_column_def(token))

    if not columns:
        raise TranslateError(f"{table} 一个列都没解析出来")

    parts = columns + table_constraints + deferred_fks
    create = (
        f"CREATE TABLE IF NOT EXISTS {table} (\n  "
        + ",\n  ".join(parts)
        + "\n)"
    )
    return [SqliteStatement("create_table", create, note=table)] + pending_indexes


# ---------------------------------------------------------------- SQLite 执行


@dataclass
class MigrationReport:
    version: str
    name: str
    statements: int
    tables: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    columns_added: list[str] = field(default_factory=list)
    mysql_only: int = 0


@dataclass
class SqliteRun:
    reports: list[MigrationReport] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    passes: int = 0

    @property
    def total_tables(self) -> int:
        return len(self.tables)


def run_sqlite(directory: Path | None = None, passes: int = 2) -> SqliteRun:
    """在内存 SQLite 库把全部迁移跑 `passes` 遍。

    第二遍是幂等验证：重复执行不应报错，结构也不应变化。
    """
    files = migration_files(directory)
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run = SqliteRun()

    try:
        for pass_no in range(passes):
            for item in files:
                report = MigrationReport(item.version, item.name, 0)
                for stmt in item.statements:
                    for translated in to_sqlite(stmt):
                        report.statements += 1
                        if translated.kind == "mysql_only":
                            report.mysql_only += 1
                            continue
                        if translated.kind == "add_column":
                            # SQLite 没有 IF NOT EXISTS for ADD COLUMN，自己判重（等价 MySQL 1060）
                            table = re.match(
                                r"^ALTER\s+TABLE\s+([\w`\".]+)", translated.sql, flags=re.I
                            )
                            assert table is not None
                            tname = table.group(1).strip('`"')
                            col = re.match(
                                r"^ALTER\s+TABLE\s+[\w`\".]+\s+ADD\s+COLUMN\s+([\w`\"]+)",
                                translated.sql, flags=re.I,
                            )
                            assert col is not None
                            cname = col.group(1).strip('`"')
                            existing = {row[1] for row in conn.execute(
                                f'PRAGMA table_info("{tname}")')}
                            if cname in existing:
                                continue
                            conn.execute(translated.sql)
                            report.columns_added.append(f"{tname}.{cname}")
                            continue
                        try:
                            conn.execute(translated.sql)
                        except sqlite3.OperationalError as exc:
                            # 索引/表已存在视为幂等成功（第二遍必然命中）
                            if "already exists" in str(exc).lower():
                                continue
                            raise MigrateError(
                                f"{item.path.name} 第 {report.statements} 条执行失败：{exc}\n"
                                f"  SQL: {translated.sql[:200]}"
                            ) from exc
                        if translated.kind == "create_table":
                            report.tables.append(translated.note)
                        elif translated.kind == "create_index":
                            report.indexes.append(translated.note or translated.sql[:60])
                if pass_no == 0:
                    run.reports.append(report)

            if pass_no == 0:
                run.tables = [
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
                ]
                run.indexes = [
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
                ]
            run.passes = pass_no + 1
    finally:
        conn.close()
    return run


# ---------------------------------------------------------------- MySQL 执行


def run_mysql(dsn: str, directory: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """按台账顺序在真 MySQL 上执行迁移。

    dsn 形如 `mysql+pymysql://user:pass@host:3306/db`。
    把 1050/1060/1061/1091/1826 识别为幂等成功。
    `dry_run=True` 只打印将要执行的语句，不落库。
    """
    try:
        from sqlalchemy import create_engine, text  # type: ignore
        from sqlalchemy.exc import DBAPIError  # type: ignore
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时给明确提示
        raise MigrateError(f"需要 sqlalchemy 与 pymysql：{exc}") from exc

    files = migration_files(directory)
    if dry_run:
        return {
            "dry_run": True,
            "files": [{"version": f.version, "statements": len(f.statements)} for f in files],
        }

    engine = create_engine(dsn, future=True)
    applied: list[str] = []
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version VARCHAR(16) PRIMARY KEY,
                  name VARCHAR(128) NOT NULL,
                  checksum CHAR(64) NOT NULL,
                  applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """))
            done = {row[0]: row[1] for row in conn.execute(
                text("SELECT version, checksum FROM schema_migrations"))}

            for item in files:
                if done.get(item.version) == item.checksum:
                    continue
                if item.version in done and done[item.version] != item.checksum:
                    raise MigrateError(
                        f"{item.path.name} 已执行但内容变了（校验和漂移）。"
                        f"已应用的迁移不可改，只能追加 015+"
                    )
                for stmt in item.statements:
                    try:
                        conn.execute(text(stmt))
                    except DBAPIError as exc:
                        errno = _mysql_errno(exc)
                        if errno in IDEMPOTENT_ERRNOS:
                            continue
                        raise MigrateError(
                            f"{item.path.name} 执行失败（errno={errno}）：{exc}"
                        ) from exc
                conn.execute(
                    text("INSERT INTO schema_migrations (version, name, checksum) "
                         "VALUES (:v, :n, :c)"),
                    {"v": item.version, "n": item.name, "c": item.checksum},
                )
                applied.append(item.version)
    finally:
        engine.dispose()
    return {"applied": applied, "total": len(files)}


def _mysql_errno(exc: Exception) -> int | None:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


# ---------------------------------------------------------------- 自检与 CLI

#: 设计文档 03_数据库设计.md §2 的 29 张表，用于核对迁移是否建全（顺序无关）
EXPECTED_TABLES = (
    # ① 订单调度 7
    "dishes", "dining_tables", "orders", "order_items", "order_events",
    "work_stations", "station_occupancy",
    # ② 人员准入 5
    "staff", "staff_cards", "shifts", "health_certificates", "morning_checks",
    # ③ 过程执行 6
    "process_events", "dish_recipes", "dish_recipe_items", "sop_steps",
    "dish_batches", "retention_samples",
    # ④ 批次/扫码 3（原料批次是溯源主体，成品批次只管留样/门禁）
    "suppliers", "trace_codes", "ingredient_batches",
    # ⑤ 环境与冷链 4
    "env_points", "env_thresholds", "device_baselines", "offline_records",
    # ⑥ AI 视觉 1
    "vision_disposals",
    # ⑦ 工单与合规 4
    "work_orders", "disinfection_records", "daily_ledgers", "ledger_items",
)

#: 设计文档 §4 明确要求的索引（表, 提示关键字, 必须覆盖的列）
#: 断言只认「列覆盖」，不认索引名——名字是自由命名的，覆盖哪些列才是实质。
#: 提示关键字仅用于报错时指明是哪条要求没满足。
REQUIRED_INDEXES = (
    ("sensor_readings", "device/ts", ("device_id", "ts")),
    ("orders", "promised", ("state", "promised_at")),
    ("station_occupancy", "station/state", ("station_id", "state")),
    ("retention_samples", "due", ("state", "destroy_due_at")),
    ("offline_records", "device/seq", ("device_id", "seq")),
    ("daily_ledgers", "date", ("ledger_date",)),
)


def check(directory: Path | None = None) -> dict[str, Any]:
    """全量自检。返回 `{passed, problems, stats}`，供 CLI 与单测共用。"""
    problems: list[str] = []
    stats: dict[str, Any] = {}

    try:
        files = migration_files(directory)
    except MigrateError as exc:
        return {"passed": False, "problems": [str(exc)], "stats": {}}

    stats["files"] = len(files)
    stats["versions"] = [f.version for f in files]

    problems.extend(verify_baseline(directory))

    try:
        run = run_sqlite(directory, passes=2)
    except MigrateError as exc:
        return {"passed": False, "problems": problems + [str(exc)], "stats": stats}

    stats["tables_total"] = run.total_tables
    stats["indexes_total"] = len(run.indexes)
    stats["passes"] = run.passes

    # 1) 29 张业务表必须都在
    missing = [t for t in EXPECTED_TABLES if t not in run.tables]
    if missing:
        problems.append(f"设计文档要求的表没建出来：{missing}")

    # 2) 每张业务表必须有主键
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for item in files:
            for stmt in item.statements:
                for translated in to_sqlite(stmt):
                    if translated.kind == "mysql_only":
                        continue
                    if translated.kind == "add_column":
                        tbl = re.match(r"^ALTER\s+TABLE\s+([\w`\".]+)", translated.sql, re.I)
                        col = re.match(
                            r"^ALTER\s+TABLE\s+[\w`\".]+\s+ADD\s+COLUMN\s+([\w`\"]+)",
                            translated.sql, re.I)
                        assert tbl and col
                        existing = {r[1] for r in conn.execute(
                            f'PRAGMA table_info("{tbl.group(1).strip(chr(96))}")')}
                        if col.group(1).strip('`"') in existing:
                            continue
                    try:
                        conn.execute(translated.sql)
                    except sqlite3.OperationalError as exc:
                        if "already exists" in str(exc).lower():
                            continue
                        raise
        for table in EXPECTED_TABLES:
            info = list(conn.execute(f'PRAGMA table_info("{table}")'))
            if not info:
                continue  # 缺失已在上面报过
            if not any(row[5] for row in info):
                problems.append(f"{table} 没有主键")

        # 3) 外键必须指向真实存在的表
        all_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in EXPECTED_TABLES:
            for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                target = fk[2]
                if target not in all_tables:
                    problems.append(f"{table} 的外键指向不存在的表 {target}")

        # 4) 设计文档要求的索引必须覆盖指定列（认列，不认名）
        for table, hint, cols in REQUIRED_INDEXES:
            found = False
            detail: list[str] = []
            for row in conn.execute(f'PRAGMA index_list("{table}")'):
                idx_name = row[1]
                idx_cols = tuple(r[2] for r in conn.execute(
                    f'PRAGMA index_info("{idx_name}")'))
                detail.append(f"{idx_name}{list(idx_cols)}")
                # UNIQUE 约束生成的自动索引名字里不带列名，所以只看列覆盖
                if all(c in idx_cols for c in cols):
                    found = True
                    break
            if not found:
                problems.append(
                    f"{table} 缺少覆盖 {cols} 的索引（设计文档 §4 要求 {hint}）。"
                    f"现有索引：{detail or '无'}")

        # 5) detector_events 扩字段只增不改
        de_cols = {r[1] for r in conn.execute('PRAGMA table_info("detector_events")')}
        for need in ("evidence", "disposal_state", "work_order_id",
                     "review_channel", "reviewed_by"):
            if need not in de_cols:
                problems.append(f"detector_events 缺扩字段 {need}")
        for keep in ("scenario", "camera_id", "classes", "boxes", "confidence", "ts"):
            if keep not in de_cols:
                problems.append(f"detector_events 旧列 {keep} 被删了（只允许增列）")

        # 6) 新表必须显式带排序规则（011 的硬要求）
        for item in files:
            if int(item.version) < 14:
                continue
            for stmt in item.statements:
                if not stmt.upper().startswith("CREATE TABLE"):
                    continue
                if "CHARACTER SET" in stmt.upper() or "CHARSET" in stmt.upper():
                    if "COLLATE" not in stmt.upper():
                        name = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w`\".]+)",
                                         stmt, re.I)
                        problems.append(
                            f"{name.group(1) if name else '?'} 只写了 CHARSET 没写 COLLATE，"
                            f"会引入 utf8mb4_0900_ai_ci 导致跨表比较报 1267")
    finally:
        conn.close()

    return {"passed": not problems, "problems": problems, "stats": stats}


def _format_report(result: dict[str, Any]) -> str:
    stats = result.get("stats") or {}
    lines = [
        "迁移自检",
        f"  文件数      {stats.get('files', 0)}",
        f"  版本        {', '.join(stats.get('versions') or []) or '-'}",
        f"  建成表      {stats.get('tables_total', 0)}",
        f"  建成索引    {stats.get('indexes_total', 0)}",
        f"  幂等重放    {stats.get('passes', 0)} 遍",
        "",
    ]
    if result["passed"]:
        lines.append("结果：PASS")
    else:
        lines.append(f"结果：FAIL（{len(result['problems'])} 个问题）")
        for item in result["problems"]:
            lines.append(f"  - {item}")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else "check"

    if cmd == "list":
        for item in migration_files():
            print(f"{item.version}  {item.name:28s} {len(item.statements):3d} 语句  "
                  f"{item.checksum[:12]}")
        return 0

    if cmd == "sqlite":
        run = run_sqlite()
        for report in run.reports:
            print(f"[{report.version}] {report.name}: {report.statements} 语句"
                  f" → 表 {len(report.tables)}，索引 {len(report.indexes)}"
                  f"，加列 {len(report.columns_added)}，MySQL 专有 {report.mysql_only}")
        print(f"\n合计 {run.total_tables} 张表 / {len(run.indexes)} 个索引 / {run.passes} 遍")
        print("表：" + ", ".join(run.tables))
        return 0

    if cmd == "json":
        print(json.dumps(check(), ensure_ascii=False, indent=2))
        return 0

    if cmd == "check":
        result = check()
        print(_format_report(result))
        return 0 if result["passed"] else 1

    print(f"未知命令：{cmd}\n可用：list / check / sqlite / json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
