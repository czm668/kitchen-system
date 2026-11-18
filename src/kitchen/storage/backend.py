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
        self._closed = False

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
            rows = cur.fetchall()
        finally:
            cur.close()
            # MySQL 在 autocommit=False 下，SELECT 也会隐式开启事务；只读场景必须
            # 显式收尾，否则事务一直挂着（持有一致性读快照、undo 不回收）。
            # 本模块 query 只用于 stats/history 这类只读查询，不在 transaction()
            # 内调用，rollback 安全；连接已关（_closed）时跳过即可。
            if not self._closed:
                try:
                    self._raw.rollback()
                except Exception:
                    pass
        return rows

    # ------------------------------------------------------------ 幂等写
    def insert_ignore(self, table: str, columns: Sequence[str],
                      row: Sequence[Any]) -> None:
        """冲突即跳过的插入（基线物化用，等价 MySQL `INSERT IGNORE`）。

        基线同步的前提是「内存里有的 = 库里也要有」，已存在的行以库内为准
        （首次物化后再同步就是空操作），所以用 IGNORE 而不是覆盖写。
        """
        if self.dialect == "sqlite":
            verb = "INSERT OR IGNORE INTO"
        else:
            verb = "INSERT IGNORE INTO"
        sql = (f"{verb} {self._quote(table)} ({self._collist(columns)}) "
               f"VALUES ({self._ph(len(columns))})")
        self.execute(sql, row)

    def upsert(self, table: str, columns: Sequence[str], row: Sequence[Any], *,
               conflict: Sequence[str]) -> None:
        """存在即更新的插入（运行期快照写）。

        - SQLite：`ON CONFLICT (...) DO UPDATE SET c = excluded.c`；
        - MySQL：`AS new ON DUPLICATE KEY UPDATE c = new.c`（8.0.19+ 行别名，
          规避 `VALUES(col)` 在 8.0.20+ 的弃用告警；compose 固定 mysql:8.4）。

        为什么不用 REPLACE INTO：REPLACE 的语义是「先 DELETE 再 INSERT」，
        而orders/staff 这类父表被子表外键引用（fk_items_order、fk_cards_staff），
        删除会撞外键 RESTRICT——子表有数据后父表就再也写不进去。UPSERT 原地
        更新，不碰外键。
        """
        if self.dialect == "sqlite":
            sets = ", ".join(f'{self._quote(c)} = excluded.{self._quote(c)}'
                             for c in columns)
            conflict_part = "ON CONFLICT (" + self._collist(conflict) + ") DO UPDATE SET "
        else:
            sets = ", ".join(f"{self._quote(c)} = new.{self._quote(c)}"
                             for c in columns)
            conflict_part = "AS new ON DUPLICATE KEY UPDATE "
        sql = (f"INSERT INTO {self._quote(table)} ({self._collist(columns)}) "
               f"VALUES ({self._ph(len(columns))}) {conflict_part}{sets}")
        self.execute(sql, row)

    def _collist(self, columns: Sequence[str]) -> str:
        return ", ".join(self._quote(c) for c in columns)

    def _ph(self, count: int) -> str:
        return ", ".join([self.placeholder] * count)

    # ------------------------------------------------------------ 事务
    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
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
        """全部业务表行数（按名字排序），供管理端「库体检」接口。

        用一条 UNION ALL 聚合查询**一次往返**拿全表行数（精确计数，区别于
        InnoDB `information_schema.tables.table_rows` 的采样估算）；表名来自
        各自的系统表，可信，用方言对应的标识符引号转义后安全插值。这把原先
        N+1（每张表一条 COUNT(*)）收敛成 2 次查询（1 次取表名 + 1 次 UNION ALL）。
        """
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
        if not names:
            return {}
        parts = []
        for n in names:
            safe = n.replace("`", "``") if self.dialect == "mysql" else n.replace('"', '""')
            parts.append(
                f"SELECT '{safe}' AS t, COUNT(*) AS c FROM {self._quote(safe)}"
            )
        rows = self.query(" UNION ALL ".join(parts))
        return {tbl: cnt for tbl, cnt in rows}


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


def _ensure_mysql_schema(db: RuntimeDB, *, timeout: float = 60.0) -> None:
    """确认 initdb 已建好关键表；**不**重跑 DDL（消除与 initdb 的双执行通道）。

    compose 把 `migrations/` 挂到 `/docker-entrypoint-initdb.d`，MySQL 容器首启
    自动执行 17 份 SQL（建库 + 50 表 + 71 索引 + 种子）——schema 的唯一来源就是
    它。但 mysql 的 healthcheck 用 `mysqladmin ping`，mysqld 一接受连接就 healthy，
    可能早于 initdb 跑完；api 依赖 `service_healthy` 启动，所以这里**轮询等待**关键
    表就绪，而非像旧版那样再用 pymysql 重跑一份 `tools.migrate` 的 DDL（那样会与
    initdb 形成两份执行通道的潜在分叉：镜像内 migrations 与挂载的 migrations 任
    一改了，两边就会不一致）。

    超时即明确报错并指向 initdb 挂载，避免静默卡死（compose 的 restart policy 会重试）。
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        found = db.query(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() "
            "AND table_name IN ('sensor_readings','alarm_events')"
        )[0][0]
        if found >= 2:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "等待 MySQL initdb 完成超时（%.0fs）：kitchen 库缺少 "
                "sensor_readings/alarm_events 表。请确认 compose 的 ../migrations "
                "已挂载到 /docker-entrypoint-initdb.d，且 mysql_data 卷未损坏。"
                % timeout
            )
        time.sleep(2)


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
