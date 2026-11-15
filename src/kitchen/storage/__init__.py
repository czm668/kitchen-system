# -*- coding: utf-8 -*-
"""运行时持久化层（A1 / A18）。详见 `db.py` 与 `backend.py` 模块注释。

A18 起支持双后端：文件路径 → SQLite（A1 原行为不变），`mysql://` URL →
MySQL（pymysql）。方言差异收敛在 `backend.py`，`recorders.py` 的 SQL 只有一份。
"""
from .backend import RuntimeDB, mask_url, open_runtime, parse_mysql_url
from .db import db_stats, open_runtime_db
from .recorders import RuntimeRecorder, SqliteRecorder

__all__ = [
    "db_stats", "open_runtime_db", "SqliteRecorder", "RuntimeRecorder",
    "RuntimeDB", "open_runtime", "parse_mysql_url", "mask_url",
]
