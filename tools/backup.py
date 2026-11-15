# -*- coding: utf-8 -*-
"""备份脚本（A4）：运行时库 + 落盘目录打包轮转。

用法::

    python tools/backup.py                      # 默认备份 var/ 到 backups/
    python tools/backup.py --db var/runtime.db  # 连运行时库一起打包
    python tools/backup.py --keep 14            # 保留最近 14 份（默认 7）

设计口径：
- 纯标准库 zipfile——服务器上没有 tar/7z 依赖负担，Windows/Linux 通用；
- 运行时库备份走 SQLite **safe backup**（sqlite3.Connection.backup）：
  直接复制正在写的 .db 文件可能拿到半个事务（写一半），backup API 是
  官方在线备份语义，热备安全；
- 轮转按文件名排序删最旧，保留 --keep 份；
- 退出码 0=成功 1=失败，可直接挂 cron/任务计划。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _backup_sqlite(db_path: Path, workdir: Path) -> Path:
    """在线安全备份：返回快照文件路径（不锁业务库太久）。"""
    snapshot = workdir / (db_path.name + ".snapshot")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)  # 官方在线备份语义：页级复制，写一半也能拿到一致快照
        finally:
            dst.close()
    finally:
        src.close()
    return snapshot


def _add_tree(zf: zipfile.ZipFile, path: Path, arc_prefix: str) -> int:
    """递归打包目录，返回文件数。"""
    count = 0
    if not path.is_dir():
        return 0
    for item in sorted(path.rglob("*")):
        if item.is_file():
            zf.write(item, arc_prefix / item.relative_to(path))
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后厨系统备份（A4）")
    parser.add_argument("--var", default=str(ROOT / "var"),
                        help="落盘目录（队列/审计/运行时库的父目录）")
    parser.add_argument("--db", default=str(ROOT / "var" / "runtime.db"),
                        help="运行时库路径（不存在则跳过）")
    parser.add_argument("--out", default=str(ROOT / "backups"),
                        help="备份输出目录")
    parser.add_argument("--keep", type=int, default=7, help="保留最近 N 份")
    args = parser.parse_args(argv)

    var_dir = Path(args.var)
    db_path = Path(args.db)
    out_dir = Path(args.out)
    if not var_dir.is_dir() and not db_path.is_file():
        print(f"没有可备份的内容：{var_dir} 不存在且 {db_path} 不存在")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = out_dir / f"kitchen_backup_{stamp}.zip"
    workdir = out_dir / f".tmp_{stamp}"
    workdir.mkdir(parents=True, exist_ok=True)

    db_snapshot: Path | None = None
    try:
        if db_path.is_file():
            db_snapshot = _backup_sqlite(db_path, workdir)
        files = 0
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            if var_dir.is_dir():
                files += _add_tree(zf, var_dir, "var")
            if db_snapshot is not None:
                zf.write(db_snapshot, f"db/{db_path.name}")
                files += 1
        size_kb = archive.stat().st_size / 1024
        print(f"✅ 备份完成：{archive}")
        print(f"   {files} 个文件，{size_kb:.1f} KB"
              + ("（运行时库为在线安全快照）" if db_snapshot else ""))
    except (sqlite3.Error, OSError, zipfile.BadZipFile) as exc:
        print(f"❌ 备份失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        if db_snapshot is not None and db_snapshot.exists():
            db_snapshot.unlink()
        workdir.rmdir()

    # 轮转：按文件名（即时间戳）排序删最旧
    keep = max(1, args.keep)
    archives = sorted(out_dir.glob("kitchen_backup_*.zip"))
    removed = 0
    for old in archives[:-keep]:
        old.unlink()
        removed += 1
    if removed:
        print(f"   轮转：删除 {removed} 份旧备份，现保留 {min(len(archives), keep)} 份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
