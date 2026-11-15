# -*- coding: utf-8 -*-
"""新工程 pytest 根配置：保证 import 可用，并把临时目录固定到工程外。

- `pyproject.toml` 的 `pythonpath` 已配置，这里再做一层保险（兼容直接跑
  `python -m pytest tests/xxx.py` 与某些 IDE 的默认启动方式）；
- 临时根固定到工程外的独立目录，避免 `tmp*` / `pytest-of-*` 落在工程根
  （旧工程在自动化终端下踩过这个坑：TEMP 不可用时 gettempdir 会回退到 cwd）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (_ROOT, _ROOT / "src"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


def _find_local_tmp() -> Path | None:
    """沿目录树向上找毕设根（带 `.local/` 的那层），临时目录固定到它的 `.local/tmp` 下。

    工程实际位置是 `毕设/kitchen-system/02_项目代码/kitchen`，层级会随目录整理变动，
    所以不硬编码 `parents[N]`，改为向上探测。
    """
    for parent in _ROOT.parents:
        local = parent / ".local"
        if local.is_dir():
            return local / "tmp" / "kitchen-pytest"
    return None


def _pick_tmp_root() -> Path:
    override = os.environ.get("KITCHEN_PYTEST_TMP", "").strip()
    local_tmp = _find_local_tmp()
    candidates = ([Path(override)] if override else []) + [
        # 与旧工程同一约定：毕设目录下的 .local/tmp 一定可写
        local_tmp,
        Path(tempfile.gettempdir()) / "kitchen-system-pytest",
        Path.home() / ".cache" / "kitchen-system-pytest",
    ]
    candidates = [c for c in candidates if c is not None]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            resolved = candidate.resolve()
            if resolved != _ROOT and _ROOT not in resolved.parents:
                return candidate
        except OSError:
            continue
    return Path(tempfile.gettempdir())


_TMP_ROOT = _pick_tmp_root()
try:
    tempfile.tempdir = str(_TMP_ROOT)
    for _key in ("TMP", "TEMP", "TMPDIR"):
        os.environ[_key] = str(_TMP_ROOT)
except OSError:  # 只读盘/权限不足时退回系统默认，不阻断测试
    tempfile.tempdir = None


def pytest_configure(config) -> None:
    """显式接管 pytest 的 basetemp，避免它按 gettempdir() 回退到工程根。"""
    if not config.option.basetemp:
        config.option.basetemp = str(_TMP_ROOT / "pytest-run")


# ---------------------------------------------------------------- 规则测试公共数据
def _parse_insert_rows(sql_text: str, table: str) -> list[list[str]]:
    """从 seed SQL 里抠出某张表 INSERT 的每一行原始字段（不引 sqlite，纯文本解析）。

    目的：规则测试直接跑**真实种子数据**，而不是测试里另抄一份阈值。种子改反了
    upper/lower，规则测试立刻红——这是「文档/数据/代码三方一致」的唯一低成本保证。
    """
    import re

    rows: list[list[str]] = []
    pattern = re.compile(
        rf"INSERT\s+IGNORE\s+INTO\s+{table}\s*\([^)]*\)\s*VALUES(.*?);",
        re.IGNORECASE | re.DOTALL,
    )
    for body in pattern.findall(sql_text):
        for match in re.finditer(r"\(([^()]*)\)", body):
            cells: list[str] = []
            current = ""
            in_quote = False
            for char in match.group(1):
                if char == "'":
                    in_quote = not in_quote
                    current += char
                    continue
                if char == "," and not in_quote:
                    cells.append(current.strip())
                    current = ""
                    continue
                current += char
            cells.append(current.strip())
            rows.append(cells)
    return rows


def _sql_value(cell: str):
    """SQL 字面量 → Python 值；NULL 归 None，带引号的去引号。"""
    text = cell.strip()
    if text.upper() == "NULL":
        return None
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text



@pytest.fixture(scope="session")
def seed_threshold_rows() -> list[dict]:
    """`fixtures/seed_kitchen.sql` 里 env_thresholds 的全部行（已 join env_points.metric）。"""
    sql_text = (_ROOT / "fixtures" / "seed_kitchen.sql").read_text(encoding="utf-8")
    point_metric: dict[int, str] = {}
    for cells in _parse_insert_rows(sql_text, "env_points"):
        # env_points 列序：id, code, device_id, metric, zone, state, latch_key, ...
        body = {"id": _sql_value(cells[0]), "metric": _sql_value(cells[3])}
        point_metric[int(body["id"])] = str(body["metric"])

    columns = (
        "id", "point_id", "kind", "profile", "lower_bound", "upper_bound",
        "duration_sec", "window_sec", "aggregate", "level", "enabled",
    )
    rows: list[dict] = []
    for cells in _parse_insert_rows(sql_text, "env_thresholds"):
        row = {name: _sql_value(value) for name, value in zip(columns, cells)}
        row["metric"] = point_metric[int(row["point_id"])]
        rows.append(row)
    return rows


@pytest.fixture(scope="session")
def scenario_rules() -> dict:
    """`fixtures/kitchen_rules.json` 的场景级补充配置。"""
    import json

    path = _ROOT / "fixtures" / "kitchen_rules.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): dict(body) for name, body in data["rules"].items()}


