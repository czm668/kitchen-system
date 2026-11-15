# -*- coding: utf-8 -*-
"""运行时库双后端适配（A18）。

背景：A1 的 `storage/db.py` 只支持 SQLite 文件；compose 里虽然起了
MySQL 8.4 且首启执行了 17 份迁移，但 api 的 CMD 写死 `--db ...runtime.db`，
`KITCHEN_DB_URL` 没有任何代码消费——即「MySQL 空跑」。本模块把这件事收掉。

设计约束（改架构时最要紧的三条）：
1. **默认 SQLite 行为零变化**：`db.py` 一行不改，继续用 `to_sqlite` 翻译的
   同源 DDL；`SqliteRecorder` 的构造签名、`.conn` 属性、全部单测保持兼容。
2. **SQL 只写一份**：recorders 里的语句继续用 `?` 占位符，由适配层在
   MySQL 分支转换成 `%s`（本模块所有 SQL 的字面量里不含 `?`）。
3. **不静默降级**：`KITCHEN_DB_URL` 一旦设置，连不上就明确抛错，不偷偷
   退回 SQLite——否则「以为写进 MySQL 其实还在写文件」比空跑更危险。

方言差异收敛点：
- 占位符 `?` / `%s`
- 事务上下文：sqlite3 的 `with conn` 是 commit/rollback；pymysql 的
  `__exit__` 是 **close()**，绝不能套用，这里统一成显式 try/commit/rollback
- 异常类型 `sqlite3.Error` / `pymysql.Error`
- 表清单 `sqlite_master` / `information_schema.tables`
- 标识符引号 `"` / 反引号
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import unquote, urlparse

log = logging.getLogger("kitchen.storage.backend")

__all__ = ["RuntimeDB", "open_runtime", "parse_mysql_url", "mask_url"]

_MYSQL_SCHEMES = ("mysql", "mysql+pymysql")
# 与 tools/migrate.py 的 IDEMPOTENT_ERRNOS 同口径：DDL 重跑时的幂等错误
_IDEMPOTENT_ERRNOS = frozenset({1050, 1060, 1061, 1091, 1826})


def parse_mysql_url(url: str) -> dict[str, Any]:
    """解析 `mysql://user:pass@host:port/db`（也接受 `mysql+pymysql://`）。

    userinfo 用 **最后一个 `@`** 切分，而不是依赖 urlparse 的 `.username`：
    口令里若含未编码的 `@`，`urlparse` 会把 host 段也吞进用户名导致连错库。
    compose 是用 `${MYSQL_PASSWORD}` 拼 URL 的，无法在那里做 percent-encode，
    所以容错放在解析器这一侧。
    """
    p = urlparse(url)
    if p.scheme not in _MYSQL_SCHEMES:
        raise ValueError(f"不支持的 MySQL URL scheme: {p.scheme!r}")

    netloc = p.netloc
    if "@" in netloc:
        userinfo, hostpart = netloc.rsplit("@", 1)
    else:
        userinfo, hostpart = "", netloc
    raw_user, _, raw_pass = userinfo.partition(":")
    if not raw_user:
        raise ValueError("MySQL URL 缺少用户名")

    # hostpart 可能是 host / host:port / [ipv6]:port
    host, _, port_text = hostpart.rpartition(":")
    if not host:  # 没有冒号 = 只有 host
        host, port_text = hostpart, ""
    host = host.strip("[]")
    port = int(port_text) if port_text.isdigit() else 3306

    db = (p.path or "/").lstrip("/").split("/")[0]
    return {
        "host": host or "127.0.0.1",
        "port": port,
        "user": unquote(raw_user),
        "password": unquote(raw_pass),
        "database": db or "kitchen",
    }


def mask_url(url: str) -> str:
    """脱敏展示用：密码永远不进日志/接口响应。

    复用 `parse_mysql_url` 的解析结果重新拼装，避免 urlparse 在口令含
    未编码 `@` 时把密码片断误当成 host 输出。
    """
    cfg = parse_mysql_url(url)
    scheme = urlparse(url).scheme
    return (
        f"{scheme}://{cfg['user']}:***@{cfg['host']}:{cfg['port']}"
        f"/{cfg['database']}"
    )


def _mysql_errno(exc: BaseException) -> int | None:
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


class RuntimeDB:
    """统一 DB-API 门面。`recorders.py` 只依赖这里的方法，不碰底层连接。"""

    def __init__(
        self,
        raw: Any,
        *,
        dialect: str,
        display: str,
        errors: tuple[type[BaseException], ...],
        migrations_dir: Path | None = None,
    ) -> None:
        self._raw = raw
        self.dialect = dialect
        self.display = display
        self.errors = errors
        self._migrations_dir = migrations_dir

    # ------------------------------------------------------------ 方言
    @property
    def placeholder(self) -> str:
        return "?" if self.dialect == "sqlite" else "%s"

    def _sql(self, sql: str) -> str:
        # 约定：本模块 SQL 的字符串字面量里不含 `?`（现有语句均满足）。
        return sql if self.dialect == "sqlite" else sql.replace("?", "%s")

    def _quote(self, ident: str) -> str:
        return f'"{ident}"' if self.dialect == "sqlite" else f"`{ident}`"

    # ------------------------------------------------------------ 读写
    def execute(self, sql: str, args: Sequence[Any] = ()) -> None:
        """写操作；不返回结果集。"""
        if self.dialect == "sqlite":
            self._raw.execute(sql, args)
            return
        cur = self._raw.cursor()
        try:
            cur.execute(self._sql(sql), args)
        finally:
            cur.close()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        if self.dialect == "sqlite":
            self._raw.executemany(sql, rows)
            return
        cur = self._raw.cursor()
        try:
            cur.executemany(self._sql(sql), list(rows))
        finally:
            cur.close()

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        if self.dialect == "sqlite":
            return self._raw.execute(sql, args).fetchall()
        cur = self._raw.cursor()
        try:
            cur.execute(self._sql(sql), args)
            return cur.fetchall()
        finally:
            cur.close()

    # ------------------------------------------------------------ 事务
    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        try:
            self._raw.commit()
        except Exception:
            pass
        self._raw.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """统一事务语义：正常 commit，异常 rollback 后原样抛出。

        不用 `with self._raw:`——pymysql 的 `Connection.__exit__` 是 close()，
        套上去会把连接关掉（A18 实测确认）。
        """
        try:
            yield
        except Exception:
            try:
                self._raw.rollback()
            except Exception:
                pass
            raise
        else:
            self._raw.commit()

    # ------------------------------------------------------------ 体检
    def stats(self) -> dict[str, int]:
        """全部业务表行数（按名字排序），供管理端「库体检」接口。"""
        if self.dialect == "sqlite":
            names = [
                row[0]
                for row in self.query(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
        else:
            names = [
                row[0]
                for row in self.query(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
            ]
        return {
            name: self.query(f"SELECT COUNT(*) FROM {self._quote(name)}")[0][0]
            for name in names
        }


# ---------------------------------------------------------------- 工厂
def _open_sqlite(path: Path, migrations_dir: Path | None) -> RuntimeDB:
    # 直接复用 A1 已验证的建库逻辑（to_sqlite 同源翻译），零改动、零回归。
    from .db import open_runtime_db

    conn = open_runtime_db(path, migrations_dir=migrations_dir)
    return RuntimeDB(
        conn,
        dialect="sqlite",
        display=str(path),
        errors=(sqlite3.Error,),
        migrations_dir=migrations_dir,
    )


def _ensure_mysql_schema(db: RuntimeDB) -> None:
    """表已由 initdb 建好就跳过；否则补执行 migrations 原始 DDL（幂等）。

    compose 的 mysql healthcheck 用 `mysqladmin ping`，它在 mysqld 开始接受
    连接时就 healthy，而 `/docker-entrypoint-initdb.d` 的 17 份 SQL 可能还在
    跑。api 依赖 service_healthy 启动，所以这一层兜底是必须的。
    """
    found = db.query(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() "
        "AND table_name IN ('sensor_readings','alarm_events')"
    )[0][0]
    if found == 2:
        return

    # 与 db.py 相同的工程根补路径逻辑：容器里 PYTHONPATH=/app/src，
    # tools/ 在 /app/tools，不在 import path 上。
    import sys

    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    sys.modules.pop("tools", None)
    from tools.migrate import migration_files  # type: ignore

    cur = db._raw.cursor()
    applied = 0
    try:
        for item in migration_files(db._migrations_dir):
            for stmt in item.statements:
                try:
                    cur.execute(stmt)
                    applied += 1
                except Exception as exc:
                    if _mysql_errno(exc) in _IDEMPOTENT_ERRNOS:
                        continue
                    raise RuntimeError(
                        f"MySQL 补建表失败 {item.path.name}: {stmt[:120]!r}: {exc}"
                    ) from exc
        db._raw.commit()
    finally:
        cur.close()
    log.info("MySQL 首次连接补建表：执行 %d 条 DDL", applied)


def _open_mysql(url: str, migrations_dir: Path | None) -> RuntimeDB:
    try:
        import pymysql  # type: ignore
    except ImportError as exc:  # pragma: no cover - 仅容器/裸机缺依赖时
        raise RuntimeError(
            "启用 KITCHEN_DB_URL 需要 pymysql：pip install pymysql "
            "（SQLite 路径不需要，裸机演示零第三方依赖）"
        ) from exc

    cfg = parse_mysql_url(url)
    # 不静默降级：连不上就抛，让 compose 的 restart policy 重试，
    # 也避免「以为写进 MySQL 其实写了文件」这种更难发现的假象。
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=15,
        write_timeout=15,
    )
    db = RuntimeDB(
        conn,
        dialect="mysql",
        display=mask_url(url),
        errors=(pymysql.Error, sqlite3.Error),
        migrations_dir=migrations_dir,
    )
    _ensure_mysql_schema(db)
    log.info("运行时库已就绪：%s（MySQL）", db.display)
    return db


def open_runtime(target: str | Path, *, migrations_dir: Path | None = None) -> RuntimeDB:
    """按 target 选后端：mysql:// 走 MySQL，其余当 SQLite 文件路径。

    `SqliteRecorder(path)` 的既有调用方式完全不变（Path → SQLite）。
    """
    if isinstance(target, str) and urlparse(target).scheme in _MYSQL_SCHEMES:
        return _open_mysql(target, migrations_dir)
    return _open_sqlite(Path(target), migrations_dir)
