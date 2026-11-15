# -*- coding: utf-8 -*-
"""运行时持久化层（A1）。

目标：回答答辩硬伤「数据存在哪 / 重启即丢」。这一层把运行数据写进
**与 migrations/001-014 完全同一套 DDL** 的库——dev 用 SQLite 文件
（`tools/migrate.to_sqlite` 已验证的 DDL 翻译，列名与 MySQL 逐列一致），
prod 换 MySQL 连接串即可，schema 不分叉。

范围口径（本轮）：只落**追加型**运行数据——
- `sensor_readings`：12 遥测帧（重启后历史曲线仍可查）
- `alarm_events`：规则/保护/升级告警事件
配置与业务状态（菜品/订单/人员）启动时从种子装配，写路径落库放 A1 第二轮。

设计约束：
- `db_path=None` = 纯内存（现状），全部既有测试零影响；
- 写失败**不抛**：只计数告警（口径同 `AuditLog.write_failures`），
  落库是锦上添花，不能把采样线程打挂；
- 复用 `tools/migrate.py` 而不是复制翻译逻辑——同一份代码保证
  「校验过的 DDL」和「运行时建出来的库」永远一致。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

try:
    from tools.migrate import migration_files, to_sqlite
except ImportError:
    # 直跑入口（python tools/xxx.py）不经过 pytest 的 pythonpath 配置，
    # 工程根不在 sys.path——按本文件相对位置（src/kitchen/storage/ 上三层）补上重试。
    # 同源口径优先于导入优雅性：翻译逻辑必须复用 tools/migrate.py 这一份。
    import sys

    _ROOT = Path(__file__).resolve().parents[3]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    # namespace 包残片防御：若 sys.path 里已有一个**别的** tools 目录
    # （无 __init__.py），失败的 import 会把残片缓存进 sys.modules，
    # 之后即使补对了 path，子模块搜索仍卡在旧 __path__ 上。
    # 实例：便携解释器 ._pth 里曾写死旧工程 proj，tools 命中 proj/tools。
    sys.modules.pop("tools", None)
    from tools.migrate import migration_files, to_sqlite

log = logging.getLogger("kitchen.storage")

__all__ = ["open_runtime_db", "db_stats"]


def open_runtime_db(path: str | Path, *, migrations_dir: Path | None = None) -> sqlite3.Connection:
    """打开（必要时创建）运行时库，并应用全部迁移的 schema。

    - 文件库：WAL 模式（采样线程写 + 请求线程读并发友好）；
    - DDL 来自 migrations/001-014 的 MySQL 语句经 `to_sqlite` 翻译，
      与 `tools/migrate.py check` 校验的是**同一份源**；
    - 幂等：表/索引已存在视为成功（等价第二遍迁移）。
    """
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")

    for item in migration_files(migrations_dir):
        for stmt in item.statements:
            for translated in to_sqlite(stmt):
                if translated.kind == "mysql_only":
                    continue
                if translated.kind == "add_column":
                    # SQLite 无 ADD COLUMN IF NOT EXISTS，自己判重（等价 MySQL 1060）
                    head = translated.sql
                    table = head.split("ADD COLUMN")[0].strip().rstrip()
                    tname = table.split()[-1].strip('`"')
                    col = head.split("ADD COLUMN", 1)[1].strip().split()[0].strip('`"')
                    existing = {row[1] for row in conn.execute(
                        f'PRAGMA table_info("{tname}")')}
                    if col in existing:
                        continue
                try:
                    conn.execute(translated.sql)
                except sqlite3.OperationalError as exc:
                    if "already exists" in str(exc).lower():
                        continue
                    conn.close()
                    raise RuntimeError(
                        f"运行时建库失败 {item.path.name}: {translated.sql[:120]!r}: {exc}"
                    ) from exc
    conn.commit()
    log.info("运行时库已就绪：%s", path)
    return conn


def db_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """全部业务表的行数（按名字排序），供管理端「库体检」接口展示。"""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    ]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
