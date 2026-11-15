# -*- coding: utf-8 -*-
"""视觉测试的共用夹具。

画面按 640×480、两个工位左右并排来构造：ST1 占左半（脚点 x<0.5），
ST2 占右半。这和 `fixtures/seed_kitchen.sql` 里的两个 `kind='stove'` 工位对应。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from kitchen.domain import CST
from kitchen.vision.models import BBox, Detection, Frame, StationRoi

#: 演示基准时刻（与其它模块的固定时刻一致，避免依赖机器时钟）
T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)

#: 两个工位的区域。**中间必须留过道（0.42~0.58），不能左右对半分。**
#:
#: 对半分的话画面里每个脚点都落在某个工位内，「路过的人」会被算成
#: 「站在 ST1」，于是他的着装违规算到 ST1 头上、他的存在让 #3 以为灶前有人。
#: 现场 ROI 按地面标线划，过道是真实存在的区域。
ROIS: tuple[StationRoi, ...] = (
    StationRoi(station_id=1, bbox=BBox(0.0, 0.0, 0.42, 1.0), name="灶台A"),
    StationRoi(station_id=2, bbox=BBox(0.58, 0.0, 1.0, 1.0), name="灶台B"),
)

#: 站在 ST1 的一个人（脚点 (0.20, 0.90)）
P1 = BBox(0.10, 0.20, 0.30, 0.90)
#: 站在 ST2 的一个人（脚点 (0.70, 0.90)）
P2 = BBox(0.60, 0.20, 0.80, 0.90)
#: 从两灶之间过道走过的人（脚点 (0.455, 0.90)，落在过道里）
P_PASS = BBox(0.42, 0.25, 0.49, 0.90)


def frame(seq: int = 1, *, ts: datetime | None = None, width: int = 640,
          height: int = 480, device_id: str = "esp32cam-01") -> Frame:
    return Frame(seq=seq, ts=ts or (T0 + timedelta(seconds=seq * 0.2)),
                 width=width, height=height, device_id=device_id)


def person(box: BBox = P1, conf: float = 0.92) -> Detection:
    return Detection("person", conf, box)


def hat(box: BBox) -> Detection:
    return Detection("hat", 0.88, box)


def mask(box: BBox) -> Detection:
    return Detection("mask", 0.86, box)


def uniform(box: BBox) -> Detection:
    return Detection("uniform", 0.84, box)


def phone(box: BBox) -> Detection:
    return Detection("phone", 0.72, box)


def cigarette(box: BBox) -> Detection:
    return Detection("cigarette", 0.66, box)


def head_of(box: BBox) -> BBox:
    """一个人的帽子框（上 45% 的上半部）。"""
    return BBox(box.x1 + 0.01, box.y1 + 0.01, box.x2 - 0.01, box.y1 + box.h * 0.30)


def mask_of(box: BBox) -> BBox:
    """口罩框（脸部偏下）。"""
    top = box.y1 + box.h * 0.22
    return BBox(box.x1 + 0.03, top, box.x2 - 0.03, top + box.h * 0.16)


def torso_of(box: BBox) -> BBox:
    """工服框（躯干）。"""
    top = box.y1 + box.h * 0.32
    return BBox(box.x1 + 0.005, top, box.x2 - 0.005, box.y1 + box.h * 0.92)


def hand_of(box: BBox) -> BBox:
    """手机/香烟框（手部区域）。"""
    top = box.y1 + box.h * 0.50
    return BBox(box.x1 + 0.06, top, box.x2 - 0.06, top + box.h * 0.12)


def full_kit(box: BBox, *, conf: float = 0.92) -> list[Detection]:
    """一个穿戴齐全、没有违规行为的人。"""
    return [person(box, conf), hat(head_of(box)), mask(mask_of(box)),
            uniform(torso_of(box))]


def bare(box: BBox, *, conf: float = 0.92) -> list[Detection]:
    """只有一个人，什么都没戴。"""
    return [person(box, conf)]
