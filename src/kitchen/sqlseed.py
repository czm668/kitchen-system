# -*- coding: utf-8 -*-
"""种子 SQL 的极简解析器（只认 `INSERT ... VALUES`，不连数据库）。

## 为什么端侧要能读 SQL

`fixtures/seed_kitchen.sql` 是**唯一事实源**：阈值、人员、卡号、健康证、排班
都写在那里，服务端建库直接执行它。端侧（以及离线单测）需要同一份数据，但
本地没有 MySQL 服务端，也不想维护第二份 JSON 让它慢慢漂移——于是把这个文件
当文本读进来，把需要的表翻成字典列表。

这不是「写了个 SQL 引擎」，只覆盖种子里实际出现的两种形态：

```
INSERT [IGNORE] INTO t (a, b, c) VALUES (...), (...);
```

刻意**不支持**：子查询、`SELECT ... INSERT`、多语句拼接的 VALUES 计算。种子
里没有，将来也不该有——种子要能被任何 MySQL 客户端直接执行，越朴素越好。

## 相对日期/时刻（跨日不失效）

排班、健康证、批次生产时间这类「以运行当天为基准」的列**不能写死日期**：种子写
`'2026-11-12'`，第二天当日排班就查不到，刷卡全被判无排班；写 `'2027-01-01'`
则会在 2027-05 答辩时让全员健康证过期；批次 `produced_at` 写死则 #14 应留样
清单次日恒空。所以这几列写 MySQL 表达式，服务端执行时自己算，端侧解析器按
**同一语义**求值——一份种子，两边同结果。

语法：`CURDATE()` 后跟 0..n 个 `± INTERVAL n UNIT`（链式，不含逗号，
否则 `cells()` 顶层切格会切坏）：

```
CURDATE()                                    -- 当天（DATE）
CURDATE() - INTERVAL 180 DAY                 -- 日期
CURDATE() + INTERVAL 2 DAY + INTERVAL 11 HOUR + INTERVAL 30 MINUTE   -- 时刻
```

约束：**日期单位（DAY/MONTH/YEAR）必须写在时间单位（HOUR/MINUTE）之前**。
MySQL 语义下逐项求值时，时间单位一出现结果就升级为 DATETIME，此后再写
月份单位会触发月末收敛、丢失时间部分——这里直接禁止这种顺序（已由
`test_sqlseed_relative` 覆盖），而不是模拟 MySQL 的完整隐式转换。
端侧求值结果：纯日期 → `YYYY-MM-DD`；含时间单位 → `YYYY-MM-DD HH:MM:SS`。

只认不带逗号的写法：现有 `cells()` 按顶层逗号切格，带 `DATE_ADD(a, b)` 这种
含逗号的函数会被切成两格，改解析器不如约束种子写法（也更贴近「朴素 SQL」）。

## 引号与注释的两个坑

1. 注释必须按「引号外」判断才切：`'a--b'` 里的 `--` 不是注释。种子里的中文
   说明大量出现在行间，直接 `split('--')` 会把数据切坏；
2. 切行/切单元格必须按**顶层**逗号：JSON 值里的逗号（`{"role":"chef","x":1}`）
   有几十个，按普通逗号切会得到一堆残片，而残片被当成「格式不认识」时又很难
   看出是哪个字段的问题。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from pathlib import Path

__all__ = [
    "SeedError", "strip_comments", "insert_body", "split_rows", "cells",
    "cell_value", "read_inserts", "read_table",
]


class SeedError(ValueError):
    """种子结构不符合预期（缺表 / 列数不匹配 / 单元格格式不认识）。"""


def strip_comments(sql_text: str) -> str:
    """去掉 `--` 行注释，**保留引号内的内容**。"""
    out: list[str] = []
    for line in sql_text.splitlines():
        in_quote = False
        cut = len(line)
        for index, char in enumerate(line):
            if char == "'":
                in_quote = not in_quote
            elif not in_quote and char == "-" and line[index:index + 2] == "--":
                cut = index
                break
        out.append(line[:cut])
    return "\n".join(out)


def insert_body(sql_text: str, table: str) -> str:
    """截出某张表 `INSERT ... VALUES` 的 VALUES 段（含注释清理）。"""
    clean = strip_comments(sql_text)
    pattern = re.compile(
        rf"INSERT\s+(?:IGNORE\s+)?INTO\s+{re.escape(table)}\s*\([^)]*\)\s*VALUES\s*(.*?);",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(clean)
    if not match:
        raise SeedError(f"种子里找不到 {table} 的 INSERT 语句")
    return match.group(1)


def split_rows(body: str) -> list[str]:
    """按**顶层**逗号切出 `(...)` 行。"""
    rows: list[str] = []
    depth = 0
    in_quote = False
    buf: list[str] = []
    for char in body:
        if char == "'":
            in_quote = not in_quote
        elif not in_quote and char == "(":
            depth += 1
        elif not in_quote and char == ")":
            depth -= 1
        if char == "," and depth == 0 and not in_quote:
            rows.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    tail = "".join(buf).strip()
    if tail:
        rows.append(tail)
    return [row for row in (item.strip() for item in rows) if row]


def cells(row: str) -> list[str]:
    """把一行 `(a, 'b, c', NULL)` 切成三格（引号内的逗号不切）。"""
    text = row.strip()
    if not (text.startswith("(") and text.endswith(")")):
        raise SeedError(f"种子行格式不认识：{text[:60]}")
    body = text[1:-1]
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    for char in body:
        if char == "'":
            in_quote = not in_quote
        if char == "," and not in_quote:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    out.append("".join(buf).strip())
    return out


#: 种子里允许出现的相对日期/时刻表达式：`CURDATE()` + 0..n 个 ± INTERVAL 项。
#: 与 MySQL 的 `± INTERVAL` 语义一一对应；链式写法必须日期单位在前（见模块注释）。
_RELATIVE_DATE_RE = re.compile(
    r"^CURDATE\s*\(\s*\)\s*(?P<terms>"
    r"(?:[+-]\s*INTERVAL\s+\d+\s+(?:DAY|MONTH|YEAR|HOUR|MINUTE)\s*)*)$",
    re.IGNORECASE,
)

_TERM_RE = re.compile(
    r"([+-])\s*INTERVAL\s+(\d+)\s+(DAY|MONTH|YEAR|HOUR|MINUTE)", re.IGNORECASE
)


def _shift_date(anchor: date, sign: str, amount: int, unit: str) -> date:
    """平移日期，月末/闰日按 MySQL 习惯收敛（1/31 - 1 MONTH → 2/28）。"""
    step = int(amount) * (-1 if sign == "-" else 1)
    if unit == "DAY":
        return anchor + timedelta(days=step)
    if unit == "MONTH":
        total = anchor.year * 12 + (anchor.month - 1) + step
        year, month = divmod(total, 12)
        month += 1
    else:  # YEAR
        year, month = anchor.year + step, anchor.month
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _apply_relative_terms(anchor: date, terms: str):
    """逐项应用 INTERVAL 链，返回 date 或 datetime。

    时间单位（HOUR/MINUTE）出现后值升级为 datetime；此后**再出现日期单位**
    属于非法链式顺序（见模块注释），直接报错而不是静默丢时间部分。
    """
    value: date | datetime = anchor
    for sign, amount, unit in _TERM_RE.findall(terms):
        unit = unit.upper()
        step = int(amount) * (-1 if sign == "-" else 1)
        if unit in ("HOUR", "MINUTE"):
            if not isinstance(value, datetime):
                value = datetime(value.year, value.month, value.day)
            value += timedelta(hours=step) if unit == "HOUR" else timedelta(minutes=step)
        elif isinstance(value, datetime):
            raise SeedError(
                f"非法链式顺序：时间单位后不能再出现日期单位（{sign}{amount} {unit}）；"
                "种子里日期单位必须写在时间单位之前"
            )
        else:
            value = _shift_date(value, sign, int(amount), unit)
    return value


def cell_value(cell: str, *, today: date | None = None):
    """单元格 → Python 值：`NULL`→None，`'x'`→x，相对日期→ISO，其余保持**字符串**。

    数字不在这里转：同一列有的要 `int`（id）、有的要 `float`（阈值），只有
    调用方知道该转成什么。硬转成 float 会让 `'2026-11-12'` 这类日期列报错。
    `today` 供单测注入，默认取运行机本地日期（部署机时区应为 CST）。
    相对表达式含时间单位时返回 `YYYY-MM-DD HH:MM:SS`，`parse_datetime()` 可直接吃。
    """
    text = cell.strip()
    if text.upper() == "NULL":
        return None
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    match = _RELATIVE_DATE_RE.match(text)
    if match:
        anchor = today or date.today()
        value = _apply_relative_terms(anchor, match.group("terms"))
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.isoformat()
    return text


def read_inserts(
    path: str | Path, *, today: date | None = None
) -> dict[str, list[dict[str, object]]]:
    """整份种子 → `{表名: [ {列名: 值}, ... ]}`。

    同一张表有多条 `INSERT` 时按出现顺序拼接（种子里目前没有，但支持）。
    `today` 是相对日期的求值基准，整份文件**共用同一个基准**，避免跨午夜
    解析时前一张表算昨天、后一张表算今天。
    """
    anchor = today or date.today()
    text = Path(path).read_text(encoding="utf-8")
    clean = strip_comments(text)
    pattern = re.compile(
        r"INSERT\s+(?:IGNORE\s+)?INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*(.*?);",
        re.IGNORECASE | re.DOTALL,
    )
    out: dict[str, list[dict[str, object]]] = {}
    for match in pattern.finditer(clean):
        table = match.group(1)
        columns = [item.strip() for item in match.group(2).split(",") if item.strip()]
        bucket = out.setdefault(table, [])
        for row in split_rows(match.group(3)):
            values = cells(row)
            if len(values) != len(columns):
                raise SeedError(
                    f"{table} 列数不匹配（{len(values)} != {len(columns)}）：{row[:80]}"
                )
            bucket.append({
                name: cell_value(value, today=anchor)
                for name, value in zip(columns, values)
            })
    return out


def read_table(
    path: str | Path, table: str, *, today: date | None = None
) -> list[dict[str, object]]:
    rows = read_inserts(path, today=today).get(table)
    if rows is None:
        raise SeedError(f"种子 {Path(path).name} 里没有 {table} 的 INSERT")
    return rows
