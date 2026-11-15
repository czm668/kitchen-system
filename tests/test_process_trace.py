# -*- coding: utf-8 -*-
"""供应链与溯源 + 标签（A4-08）。

验收口径直译：「扫码能查到批次、供应商、生产时间、检测记录」→ `TestTraceView`。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kitchen.domain import CST, NotFoundError
from kitchen.process import (
    MemoryProcessRepository,
    TraceError,
    TraceService,
    qr,
)

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=CST)


def make(**kwargs) -> TraceService:
    # 种子批次的 produced_at/expire_at 是「装配日 + 相对时刻」：不注入 today
    # 就按运行当天装配，T0（2026-11-12 12:00）与 11:30/14 号到期的断言全落空。
    return TraceService(
        MemoryProcessRepository.from_seed(today=T0.date()), **kwargs
    )


# ================================================================ 扫码一屏
class TestTraceView:
    def test_扫码查到批次供应商生产时间检测记录(self):
        view = make().view("tc-demo-0001")          # 大小写不敏感
        assert view.code == "TC-DEMO-0001"
        assert view.batch_no == "B-DEMO-001"
        assert view.dish_name == "青椒肉丝"
        assert view.supplier == "鲜丰肉联厂"
        assert view.license_no == "SC11110010001"
        assert view.produced_at == datetime(2026, 11, 12, 11, 30, tzinfo=CST)
        assert view.inspection["检测"] == "合格"
        assert view.inspection["检验员"] == "王组长"
        assert view.qr_url.endswith("TC-DEMO-0001")

    def test_描述里带齐关键信息(self):
        text = make().view("TC-DEMO-0001").describe()
        assert "B-DEMO-001" in text and "青椒肉丝" in text and "鲜丰肉联厂" in text

    def test_保质期取自批次(self):
        view = make().view("TC-DEMO-0001")
        assert view.expire_at == datetime(2026, 11, 14, 11, 30, tzinfo=CST)
        # expired 是与机器时钟比的结果，不在此断言（演示机上会随时间漂）
        assert isinstance(view.expired, bool)

    def test_不存在的码报错(self):
        with pytest.raises(NotFoundError):
            make().view("TC-NOT-EXIST")

    def test_空扫码报错(self):
        with pytest.raises(TraceError):
            make().view("  ")

    def test_字典导出供小程序用(self):
        payload = make().view("TC-DEMO-0001").to_dict()
        assert payload["batch_no"] == "B-DEMO-001"
        assert payload["produced_at"].startswith("2026-11-12T11:30")
        assert isinstance(payload["inspection"], dict)


# ================================================================ 批次反查
class TestBatchLookup:
    def test_按菜查批次(self):
        rows = make().batches_of(1)
        assert len(rows) == 1
        assert rows[0]["batch_no"] == "B-DEMO-001"

    def test_按批次查溯源码(self):
        assert make().codes_of(1) == ("TC-DEMO-0001",)


# ================================================================ 出库门禁
class TestOutbound:
    def test_正常批次可以出库(self):
        # 必须显式传 now：批次 expire_at 固定在 T0+2 天，不传 now 用真实时钟，
        # 这条测试会在 11/15 之后跑挂（与种子写死日期是同一类跨日雷）。
        assert make().outbound_blockers("TC-DEMO-0001", now=T0) == ()

    def test_消毒未达标禁止出库(self):
        reasons = make().outbound_blockers("TC-DEMO-0001", forbid_outbound=True)
        assert reasons and "复消" in reasons[0]

    def test_过期禁止出库(self):
        svc = make()
        later = datetime(2026, 11, 20, tzinfo=CST)
        reasons = svc.outbound_blockers("TC-DEMO-0001", now=later)
        assert any("保质期" in reason for reason in reasons)

    def test_阻塞原因一次列全(self):
        svc = make()
        reasons = svc.outbound_blockers(
            "TC-DEMO-0001", forbid_outbound=True,
            now=datetime(2026, 11, 20, tzinfo=CST),
        )
        assert len(reasons) == 2


# ================================================================ 标签
class TestLabel:
    def test_二维码装的是短链(self):
        svc = make()
        url = svc.label_url("tc-demo-0001")
        assert url == "https://k.example.com/t/TC-DEMO-0001"
        assert len(url.encode()) <= 42          # v3 容量内，标签不会过密

    def test_可自定义域名(self):
        svc = make(url_template="http://192.168.1.20:8000/trace?code={code}")
        assert svc.label_url("A1").startswith("http://192.168.1.20")

    def test_png_能出图(self):
        png = make().label_png("TC-DEMO-0001", scale=4)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 200

    def test_终端预览(self):
        art = make().label_ascii("TC-DEMO-0001")
        assert art.count("\n") > 20

    def test_标签内容可被自研解码器解回(self):
        """标签里编的是 URL，解回来必须与 URL 完全一致。"""
        from test_qr import decode

        svc = make()
        code = qr.encode(svc.label_url("TC-DEMO-0001"))
        assert decode(code) == "https://k.example.com/t/TC-DEMO-0001"


def test_快照():
    snap = make().snapshot()
    assert snap == {"suppliers": 2, "batches": 1, "trace_codes": 1}
