# -*- coding: utf-8 -*-
"""视觉域的实体与契约（A5）。

## 这个包为什么单独存在

`rules` 处理数值遥测，`safety` 处理领域算法，`staff` 处理刷卡事件，`process`
处理业务状态机——视觉是第五种输入形态：**每帧一张图，输出一组检测框**。
塞进 `rules` 只能得到「把置信度当遥测值」这种用法，而「连续 N 帧缺帽子」
这种时间维度上的判定，数值规则表达不出来。

## 四层结构（分层是为了每一层都能单独测）

```
帧源 stream.py        → Frame（一张图的元数据，不含像素）
检测器 detector.py    → Detection 列表（模型输出，原样不解释）
判定   postprocess.py → VisionFrame（谁在哪个工位、戴没戴、有没有违规行为）
处置   disposal.py    → 切电 / 语音 / 双通道复核 / 工单
```

**检测器是可替换的**：真机接 YOLOv8（`YoloDetector`，需要 ultralytics），
离线开发与单测接 `ScriptedDetector`（按脚本吐检测框）。判定层只认
`Sequence[Detection]`，所以「模型准不准」和「判得对不对」是两件可以分开验证的事。
这一点是本包最重要的设计，因为答辩现场模型必然有漏检，而漏检不该让判定逻辑
看起来也是错的。

## 坐标口径：一律归一化 0~1

像素坐标在不同分辨率间不能直接比（OV2640 出 640×480，抓拍存 320×240，
训练用 416×416），混用会在「框内是否包含某人」这种地方产生静默错误。
`BBox` 落库/传输前乘回宽高，判定层永远只看 0~1。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..domain import CST

__all__ = [
    "CLASSES",
    "PERSON",
    "HAT",
    "MASK",
    "UNIFORM",
    "CIGARETTE",
    "PHONE",
    "BBox",
    "Detection",
    "Frame",
    "PersonState",
    "VisionFrame",
    "StationRoi",
    "ViolationKind",
    "VIOLATION_LABELS",
    "VIOLATION_ACTIONS",
    "VisionError",
    "FrameStaleError",
]

# ------------------------------------------------------------------ 类别
#: 检测类别表。**顺序即模型输出的 class id**，改顺序必须重训模型，否则
#: 「帽子」会被当成「手机」——这种错误在代码里看不出来，只有现场能看到人
#: 拿手机报警但其实是没戴帽，排查会花掉一整天。
CLASSES: tuple[str, ...] = (
    "person", "hat", "mask", "uniform", "cigarette", "phone",
)

PERSON = 0
HAT = 1
MASK = 2
UNIFORM = 3
CIGARETTE = 4
PHONE = 5


class VisionError(Exception):
    """视觉域异常基类。"""


class FrameStaleError(VisionError):
    """帧太旧（时间戳早于上一帧，或超出允许延迟）。"""


# ------------------------------------------------------------------ 几何
@dataclass(frozen=True)
class BBox:
    """归一化检测框。`x1<x2`、`y1<y2`、全部落在 [0,1]。"""

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise VisionError(f"非法检测框（宽高非正）：{self}")

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def foot(self) -> tuple[float, float]:
        """底边中点。**判定「站在哪个工位」用它，不用中心点**。

        人蹲下或俯身时中心点会跑到相邻工位去，而脚的位置不会。这个区别在
        两灶并排、中间只隔 40cm 的画面里决定「切哪个工位电」。
        """
        return ((self.x1 + self.x2) / 2, self.y2)

    def iou(self, other: BBox) -> float:
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        return inter / (self.area + other.area - inter)

    def contains(self, point: tuple[float, float], *, margin: float = 0.0) -> bool:
        px, py = point
        return (self.x1 - margin <= px <= self.x2 + margin
                and self.y1 - margin <= py <= self.y2 + margin)

    def overlap_ratio(self, other: BBox) -> float:
        """`other` 有多大比例落在 `self` 内（按 other 的面积算）。

        用交并比（IoU）判「帽子在不在头上」是错的：帽框只有头框的 1/3 大，
        IoU 到不了 0.3，于是所有戴帽子的人都被判成没戴。正确的问题是
        「这个帽框落在头框里的比例」，即 `self.overlap_ratio(hat)`。
        """
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        return inter / other.area if other.area else 0.0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (round(self.x1, 4), round(self.y1, 4),
                round(self.x2, 4), round(self.y2, 4))

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (int(round(self.x1 * width)), int(round(self.y1 * height)),
                int(round(self.x2 * width)), int(round(self.y2 * height)))


@dataclass(frozen=True)
class Detection:
    """模型吐出的一个框。`cls` 是类别**名**而不是 id——id 会在重训后变。"""

    cls: str
    conf: float
    bbox: BBox

    def __post_init__(self) -> None:
        if self.cls not in CLASSES:
            raise VisionError(f"未知类别 {self.cls!r}，可用：{CLASSES}")
        if not 0.0 <= self.conf <= 1.0:
            raise VisionError(f"置信度越界：{self.conf}")

    def describe(self) -> str:
        return f"{self.cls} {self.conf:.2f} @{self.bbox.as_tuple()}"


# ------------------------------------------------------------------ 帧
@dataclass(frozen=True)
class Frame:
    """一帧的元数据。**不含像素**：像素只在抓拍时读一次，其余环节都别碰。

    把小图 bytes 在各层之间传会带来两个问题：单测必须造图（于是造出来的图
    跟真帧长得不一样的 bug 永远测不出），以及内存里堆着几百 KB 的旧帧。
    `uri` 指回帧源，需要像素的环节（抓拍存证）自己去拿。
    """

    seq: int
    ts: datetime
    width: int
    height: int
    uri: str = ""
    #: 帧源标识（`esp32cam-01`）。多摄像头时用来把 ROI 表分开
    device_id: str = "esp32cam-01"

    def age_sec(self, now: datetime | None = None) -> float:
        moment = now or datetime.now(CST)
        return (moment - self.ts).total_seconds()


@dataclass(frozen=True)
class StationRoi:
    """工位在画面里的区域。**取脚所在的矩形，不取整个工位桌面**。

    桌面会把相邻工位的餐具也框进来，于是「谁没戴帽子」分不清是谁。
    """

    station_id: int
    bbox: BBox
    name: str = ""

    def owns(self, bbox: BBox) -> bool:
        return self.bbox.contains(bbox.foot)

    def describe(self) -> str:
        return f"ST{self.station_id}{'(' + self.name + ')' if self.name else ''}"


# ------------------------------------------------------------------ 观察结果
@dataclass
class PersonState:
    """一个人的着装与行为状态（由检测框推算，不是模型直接给的）。"""

    person: BBox
    has_hat: bool = False
    has_mask: bool = False
    has_uniform: bool = False
    smoking: bool = False
    using_phone: bool = False
    #: 该人归属的工位（None = 不在任何工位区域内，比如路过）
    station_id: int | None = None
    conf: float = 0.0

    def missing(self) -> tuple[str, ...]:
        out: list[str] = []
        if not self.has_hat:
            out.append("hat")
        if not self.has_mask:
            out.append("mask")
        if not self.has_uniform:
            out.append("uniform")
        return tuple(out)

    def behaviors(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.smoking:
            out.append("smoking")
        if self.using_phone:
            out.append("phone")
        return tuple(out)


@dataclass(frozen=True)
class VisionFrame:
    """一帧画面的聚合结论。这是视觉域对外的唯一输出形态。"""

    frame: Frame
    persons: tuple[PersonState, ...] = ()
    #: 画面内（不限工位）的人数。给 #3 用：**只要有一个人就不算离人**
    headcount: int = 0
    #: 各工位的人数
    station_headcount: dict[int, int] = field(default_factory=dict)
    #: 模型未检出任何人（可能是真没人，也可能是曝光过度/镜头被挡）
    empty: bool = True

    def at_station(self, station_id: int) -> tuple[PersonState, ...]:
        return tuple(p for p in self.persons if p.station_id == station_id)

    def occupied(self, station_id: int) -> bool:
        return self.station_headcount.get(int(station_id), 0) > 0

    def describe(self) -> str:
        if self.empty:
            return f"帧#{self.frame.seq} 无人"
        seats = "、".join(f"ST{k}:{v}人" for k, v in sorted(self.station_headcount.items()))
        return f"帧#{self.frame.seq} {self.headcount}人（{seats or '不在工位'}）"


# ------------------------------------------------------------------ 违规类型
class ViolationKind:
    """#16 的五类违规。**用常量而不是 Enum**：落库要写字符串，Enum 到处 `.value`。"""

    NO_HAT = "no_hat"
    NO_MASK = "no_mask"
    NO_UNIFORM = "no_uniform"
    SMOKING = "smoking"
    PHONE = "phone"

    ALL: tuple[str, ...] = (NO_HAT, NO_MASK, NO_UNIFORM, SMOKING, PHONE)


#: 违规人话术（语音播报与工单标题共用一份，改一处不会两处不一致）
VIOLATION_LABELS: dict[str, str] = {
    ViolationKind.NO_HAT: "未戴厨师帽",
    ViolationKind.NO_MASK: "未戴口罩",
    ViolationKind.NO_UNIFORM: "未穿工服",
    ViolationKind.SMOKING: "工位吸烟",
    ViolationKind.PHONE: "工位玩手机",
}

#: 违规 → 是否允许断电。**着装类不切电**。

#: 这条区分是答辩必问的：没戴帽子是卫生违规，切断插座既不影响他戴帽子，
#: 又让整条线停摆；而吸烟在明火厨房是火灾隐患，必须立刻停设备。
#: 一刀切全切电的版本，现场会演变成「把摄像头遮住」。
VIOLATION_ACTIONS: dict[str, str] = {
    ViolationKind.NO_HAT: "voice",
    ViolationKind.NO_MASK: "voice",
    ViolationKind.NO_UNIFORM: "voice",
    ViolationKind.SMOKING: "power",
    ViolationKind.PHONE: "power",
}


def label_of(kind: str) -> str:
    return VIOLATION_LABELS.get(kind, kind)


def cuts_power(kind: str) -> bool:
    """该违规是否触发断电。未知类型按**不切电**处理：认错类型就断电比漏切危险。"""
    return VIOLATION_ACTIONS.get(kind, "voice") == "power"


def to_payload(frame: VisionFrame) -> dict[str, Any]:
    """`VisionFrame` → 可落库/MQTT 的扁平结构（与契约的展平风格一致）。"""
    return {
        "frame_seq": frame.frame.seq,
        "ts": int(frame.frame.ts.timestamp() * 1000),
        "device_id": frame.frame.device_id,
        "headcount": frame.headcount,
        "empty": frame.empty,
        "stations": {str(k): v for k, v in sorted(frame.station_headcount.items())},
        "persons": [
            {
                "bbox": list(p.person.as_tuple()),
                "station_id": p.station_id,
                "hat": p.has_hat,
                "mask": p.has_mask,
                "uniform": p.has_uniform,
                "smoking": p.smoking,
                "phone": p.using_phone,
                "conf": round(p.conf, 3),
            }
            for p in frame.persons
        ],
    }
