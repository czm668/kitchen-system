# -*- coding: utf-8 -*-
"""跨域共用的基础件：时区、日期解析、领域异常。

三个域（`rules` / `staff` / `process`）都要做三件同样的事——判断「今天」、
把 SQL 里的字符串翻成时间、抛同一族错误。每域各抄一份的后果不是省不了几十行，
而是**漂移**：一个域按 UTC 算日期、另一个写 +8，跨零点的排班与 48 小时销毁任务
就会差 8 小时，而这种差错只在演示当天凌晨才现形（评委通常不在那个点盯着）。

## 为什么异常要分这么细

`NotFoundError` 与 `ForeignKeyError` 在 HTTP 层是 404 与 400，在端侧是
「刷了张没登记的卡」与「卡绑到了不存在的人」——两种提示指向完全不同的补救动作。
统一抛 `ValueError` 的做法在单测里最省事，但会把所有区分压平成一个 500。

`DuplicateError` 对应 MySQL 1062，`ForeignKeyError` 对应 1452。内存实现主动
复刻这两种失败，是为了做到「内存里过了 = 库里也会过」。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

__all__ = [
    "CST", "DomainError", "NotFoundError", "DuplicateError", "ForeignKeyError",
    "StaleVersionError", "InvalidTransition", "parse_date", "parse_datetime", "as_bool",
    "now_cst",
]

#: 后厨时区。所有「今天」「这一班」都以它为准。
CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 异常
class DomainError(ValueError):
    """领域错误的基类。业务上「可预期的拒绝」都从这里派。

    继承 `ValueError` 而不是 `Exception`，是为了让「调用方传了非法参数」与
    「系统故障」在 `except` 上天然分开：前者能直接回 4xx，后者必须进日志告警。
    """


class NotFoundError(DomainError):
    """查不到目标行。HTTP 404。"""


class DuplicateError(DomainError):
    """唯一键冲突，对应 MySQL 1062。"""


class ForeignKeyError(DomainError):
    """引用了不存在的行，对应 MySQL 1452。"""


class StaleVersionError(DomainError):
    """乐观锁版本不匹配——这一行已被别人改过。

    订单并发是三端同时改同一张单（Web 改派 / TFT 确认 / 小程序催菜），
    后到的写入必须失败并提示刷新，而不是覆盖掉别人的改动。
    """


class InvalidTransition(DomainError):
    """状态机不允许的迁移。

    单独一类而不是复用 `DomainError`：状态机违例说明**调用顺序**错了，
    而 `DomainError` 多数说明**数据**不对。排查时先看哪一类能省一半时间。
    """


# ---------------------------------------------------------------- 解析
def parse_date(raw: Any) -> date | None:
    """`DATE` 列 → date。空值返回 None（是「缺失」还是「不限」由调用方定）。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw).strip()[:10])


def parse_datetime(raw: Any, *, tz: timezone = CST) -> datetime | None:
    """`DATETIME` 列 → **带时区**的 datetime。

    库里存的是墙上时间（`2026-11-12 09:00:00`），没有偏移量。补上 CST 再比较，
    否则「aware 减 naive」会直接抛 `TypeError`——这类错误在测试里必现，但在
    「只有一条数据恰好是 naive」的线上环境里会一路漏到演示。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=tz)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=tz)
    text = str(raw).strip().replace(" ", "T")
    parsed = datetime.fromisoformat(text)      # 3.11 起支持 "YYYY-MM-DD HH:MM" 与仅日期
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def as_bool(raw: Any, default: bool = True) -> bool:
    """MySQL 的 `TINYINT(1)` 读出来可能是 `0/1`/`'0'`/`b'0'`/`None`。

    只有明确表示「否」的取值才算假，其余一律取 `default`——把脏值当假会让
    「配置没写」变成「功能被关掉」，而这类故障不报错、只是永远不生效。
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    text = str(raw).strip().lower()
    if text.startswith("b'") and text.endswith("'"):    # PyMySQL 的位字面量 b'0'
        text = text[2:-1]
    return text not in ("0", "false", "no", "off", "none")


def now_cst() -> datetime:
    """当前后厨时间。默认时钟，测试里一律注入假时钟。"""
    return datetime.now(CST)
