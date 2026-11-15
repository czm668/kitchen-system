# -*- coding: utf-8 -*-
"""供应链与溯源（A4-08）：供应商 → 批次 → 溯源码 → 顾客扫码一屏。

## 一条链，四个查询点

```
供应商 suppliers ──┐
                   ├─→ 批次 dish_batches ─→ 溯源码 trace_codes ─→ 顾客扫码视图
检验记录 payload ──┘                         （贴盒/贴袋）        （小程序 H5）
```

溯源的价值不在「存了什么」，而在**扫出来能不能回答顾客的那三个问题**：
这是哪一批、谁供的、有没有检测记录。所以 `resolve()` 一次性把四张表拼成一屏，
而不是让前端串四个接口——接口串起来必然出现「批次查到了、供应商 404」的半残页面。

## 检测记录存 `trace_codes.payload`，不新建表

`payload` 是 JSON：`{"检测":"合格","检验员":"王组长","报告":"JC-2026-1120"}`。
检测项目在不同品类上差别很大（肉类查瘦肉精、蔬菜查农残），建表就得为每类
建一张明细表或塞一张 `(key, value)` 竖表——前者维护成本高，后者查询要转置。
一张品类内固定的短报告，JSON 足够，且导出时原样进台账。

## 二维码放在这一层，不放前端

标签要能离线打印（后厨那台打印机没网），所以编码必须在服务端/端侧完成。
`qr` 模块是纯标准库实现，`label_png()` 直接出图，扫码枪与手机都能读。

## 出库门禁

`outbound_blockers()` 把 #15 的「该批餐具禁止出库」与「批次过期」并到一起。
门禁放在溯源层而不是消毒层：出库这个动作同时要看**食品安全**（过期）与
**合规**（消毒未达标），分两处判，迟早有一处漏。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from kitchen.domain import CST, DomainError, NotFoundError, now_cst

from . import qr
from .repository import MemoryProcessRepository

log = logging.getLogger(__name__)

__all__ = [
    "TraceError",
    "TRACE_URL_TEMPLATE",
    "TraceView", "TraceService",
]

#: 溯源码对应的顾客端地址。真机部署时从配置读域名，这里给默认值让演示能直接跑
TRACE_URL_TEMPLATE = "https://k.example.com/t/{code}"


class TraceError(DomainError):
    """溯源数据不完整或操作不合法。"""


@dataclass(frozen=True)
class TraceView:
    """扫码结果一屏。字段名与顾客端页面一一对应，不做二次映射。"""

    code: str
    batch_no: str | None
    dish_name: str | None
    produced_at: datetime | None
    expire_at: datetime | None
    supplier: str | None
    license_no: str | None
    qualifications: str | None
    inspection: dict[str, object]
    qr_url: str

    @property
    def expired(self) -> bool:
        return self.expire_at is not None and self.expire_at < now_cst()

    def describe(self) -> str:
        shelf = "已过保质期" if self.expired else (
            f"保质至 {self.expire_at:%Y-%m-%d %H:%M}" if self.expire_at else "未标保质期")
        return (f"{self.code} → {self.batch_no} {self.dish_name or ''}"
                f"｜{self.supplier or '供应商未登记'}（{self.license_no or '无证号'}）"
                f"｜{shelf}")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "batch_no": self.batch_no,
            "dish": self.dish_name,
            "produced_at": self.produced_at.isoformat() if self.produced_at else None,
            "expire_at": self.expire_at.isoformat() if self.expire_at else None,
            "supplier": self.supplier,
            "license_no": self.license_no,
            "qualifications": self.qualifications,
            "inspection": self.inspection,
            "expired": self.expired,
            "qr_url": self.qr_url,
        }


class TraceService:
    """供应商/批次/溯源码的查询与标签生成。"""

    def __init__(
        self,
        repo: MemoryProcessRepository,
        *,
        url_template: str = TRACE_URL_TEMPLATE,
        domain: str | None = None,
    ) -> None:
        self.repo = repo
        self.url_template = url_template
        self.domain = domain

    # ------------------------------------------------------------ 查询
    def view(self, code: str) -> TraceView:
        """扫码 → 一屏溯源信息。缺哪个环节就明确说缺哪个。"""
        text = str(code).strip()
        if not text:
            raise TraceError("扫码内容为空")
        row = self.repo.trace_code(text)          # 不存在抛 NotFoundError
        code_key = text.upper()
        batch_no = dish_name = None
        produced_at = expire_at = None
        batch_id = row.get("batch_id")
        if batch_id is not None:
            batch = self.repo.batch(int(batch_id))
            batch_no = str(batch.get("batch_no") or "") or None
            produced_at = batch.get("produced_at") if isinstance(batch.get("produced_at"), datetime) else None
            expire_at = batch.get("expire_at") if isinstance(batch.get("expire_at"), datetime) else None
            dish_id = batch.get("dish_id")
            if dish_id is not None:
                try:
                    dish_name = self.repo.dish(int(dish_id)).name
                except NotFoundError:
                    dish_name = None
        supplier = license_no = qualifications = None
        supplier_id = row.get("supplier_id")
        if supplier_id is not None:
            info = self.repo.supplier(int(supplier_id))
            supplier = str(info.get("name") or "") or None
            license_no = str(info.get("license_no") or "") or None
            qualifications = info.get("qualifications")
        payload = row.get("payload")
        return TraceView(
            code=code_key,
            batch_no=batch_no,
            dish_name=dish_name,
            produced_at=produced_at,
            expire_at=expire_at,
            supplier=supplier,
            license_no=license_no,
            qualifications=str(qualifications) if qualifications else None,
            inspection=dict(payload) if isinstance(payload, dict) else {},
            qr_url=self.label_url(code_key),
        )

    def batches_of(self, dish_id: int) -> tuple[dict[str, object], ...]:
        """某道菜的所有批次（按生产时间倒序）——出库/召回时按菜反查。"""
        rows = [self.repo.batch(batch_id) for batch_id in self.repo.batch_ids()
                if int(self.repo.batch(batch_id).get("dish_id") or 0) == int(dish_id)]
        rows.sort(key=lambda row: row.get("produced_at") or datetime.min.replace(tzinfo=CST),
                  reverse=True)
        return tuple(rows)

    def codes_of(self, batch_id: int) -> tuple[str, ...]:
        return tuple(code for code, row in self.repo.trace_rows()
                     if int(row.get("batch_id") or 0) == int(batch_id))

    # ------------------------------------------------------------ 出库门禁
    def outbound_blockers(
        self,
        code: str,
        *,
        forbid_outbound: bool = False,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """出库前全部阻塞原因，**一次列全**（同 `CookingSession.blockers()` 口径）。

        `forbid_outbound` 由 #15 消毒会话传入：消毒不达标 → 该批餐具禁止出库。
        """
        reasons: list[str] = []
        view = self.view(code)
        moment = now or now_cst()
        if forbid_outbound:
            reasons.append("消毒未达标：该批餐具禁止出库，需复消合格后放行")
        if view.expire_at is not None and view.expire_at < moment:
            reasons.append(f"已过保质期（{view.expire_at:%Y-%m-%d %H:%M}）")
        if view.supplier is None:
            reasons.append("供应商未登记，追溯链不完整")
        if not view.inspection:
            reasons.append("无检测记录，不能出库")
        return tuple(reasons)

    # ------------------------------------------------------------ 标签
    def label_url(self, code: str) -> str:
        """二维码里放**短链**，不放整串 JSON。

        放 JSON 会让码变密：同样的 3cm 标签上，版本从 v3 涨到 v7，模块边长
        缩到 0.3mm，后厨那台热敏打印机打出来就是一团糊。短链只占 40 字节。
        """
        return self.url_template.format(code=str(code).strip().upper())

    def label_png(self, code: str, *, scale: int = 6, quiet: int = 4) -> bytes:
        return qr.to_png(qr.encode(self.label_url(code)), scale=scale, quiet=quiet)

    def label_ascii(self, code: str, *, quiet: int = 2) -> str:
        return qr.to_ascii(qr.encode(self.label_url(code)), quiet=quiet)

    # ------------------------------------------------------------ 快照
    def snapshot(self) -> dict[str, object]:
        return {
            "suppliers": len(self.repo.suppliers()),
            "batches": len(self.repo.batch_ids()),
            "trace_codes": len(self.repo.trace_rows()),
        }
