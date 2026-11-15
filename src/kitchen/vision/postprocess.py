# -*- coding: utf-8 -*-
"""检测框 → 人员状态（A5-02 的判定侧）。

模型只告诉你「画面里有哪些框」，剩下三件事全在这里做：

1. **这个人站在哪个工位**——用脚的位置，不用框中心（见 `BBox.foot`）；
2. **帽子/口罩/工服是不是穿戴在这个人身上**——用「附属框落在目标区域内的
   比例」，不用 IoU（帽框只有头框的 1/3 大，IoU 到不了 0.3）；
3. **一对一分配**——一个帽框不能同时证明两个人戴了帽子。

## 一条最重要的口径：正向证据优先

「画面里 3 个人、检出 3 顶帽子」→ 三个人都合规，**不管几何匹配结果如何**。
理由：几何匹配会因为遮挡、侧身、检测框抖动而失败，而失败的方向永远是
「多报违规」。误报一次违规要切电停线（如果是吸烟/玩手机），代价远大于
漏报一次的代价。所以只要**数量够**就直接判合规，只有数量不足时才逐人几何匹配。

反过来不成立：检出 2 顶帽、画面上 3 个人，不能因为「大部分人戴了」就说都合规——
少的那一顶恰恰是要抓的。这就是为什么只在「数量 ≥ 人数」时才走正向优先。

## 但正向优先只在**多人**场景成立

单人画面里「检出 1 顶帽、1 个人」走正向优先，就等于「只要有帽子框就算他戴了」——
帽子挂在腰上、搭在肩上、放在案板上，都会判成合规。所以 `len(states) >= 2` 才走
数量判定：一个人的框归属没有歧义，几何匹配失败最可能就是**他真的没戴**。

## 置信度分档

`person_conf=0.40` 低于 `behavior_conf=0.50`：宁可多认几个人（多认人只会让
「无人」判定更保守），但「吸烟」这种要切电的结论必须有更高把握。一个通用的
0.25 阈值会同时犯两个错——漏人（于是 #3 判离人关气）和误报吸烟（于是切电）。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .models import (
    CIGARETTE,
    CLASSES,
    HAT,
    MASK,
    PERSON,
    PHONE,
    UNIFORM,
    BBox,
    Detection,
    Frame,
    PersonState,
    StationRoi,
    VisionError,
    VisionFrame,
)

__all__ = ["AnalysisPolicy", "analyze", "assign_stations", "analyze_persons"]


@dataclass(frozen=True)
class AnalysisPolicy:
    """判定参数。改这里之前先看 `03_设计文档/04_功能规格与联动阈值.md` 的 #16。"""

    #: 通用下限：低于此值的框直接丢
    min_conf: float = 0.35
    #: 认人的门槛（比通用下限低，见模块 docstring）
    person_conf: float = 0.40
    #: 判定行为的门槛（吸烟/玩手机会切电，要有更高把握）
    behavior_conf: float = 0.50
    #: 头部区域占人身高的比例。人脸的典型位置在上 45%
    head_ratio: float = 0.45
    #: 口罩比帽子低一点（遮住口鼻），所以匹配区域放宽到上 60%
    mask_ratio: float = 0.60
    #: 工服区域：掐掉头部之后的下半身
    torso_top: float = 0.20
    torso_bottom: float = 0.95
    #: 附属框落在目标区域内的最低比例
    attach_overlap: float = 0.50

    def head_region(self, person: BBox, *, ratio: float | None = None) -> BBox:
        top = person.y1
        bottom = min(person.y2, person.y1 + person.h * (ratio or self.head_ratio))
        return BBox(person.x1, top, person.x2, max(bottom, top + 1e-6))

    def torso_region(self, person: BBox) -> BBox:
        top = person.y1 + person.h * self.torso_top
        bottom = person.y1 + person.h * self.torso_bottom
        return BBox(person.x1, top, person.x2, max(bottom, top + 1e-6))


def _split(detections: Iterable[Detection], policy: AnalysisPolicy
           ) -> dict[str, list[Detection]]:
    """按类别分组并过滤低置信度。"""
    floor = {"person": policy.person_conf,
             "cigarette": policy.behavior_conf,
             "phone": policy.behavior_conf}
    buckets: dict[str, list[Detection]] = {name: [] for name in CLASSES}
    for det in detections:
        if det.conf < floor.get(det.cls, policy.min_conf):
            continue
        buckets[det.cls].append(det)
    for name in buckets:
        buckets[name].sort(key=lambda d: -d.conf)
    return buckets


def _attach(
    persons: Sequence[PersonState],
    items: Sequence[Detection],
    region_of,
    policy: AnalysisPolicy,
) -> dict[int, float]:
    """把附属框一对一挂到人身上。返回 `{人员下标: 附属框置信度}`。

    贪心按置信度从高到低分：高置信度的框先挑人，避免一个 0.42 的误检把
    0.91 的真检挤掉。`used` 保证一顶帽子只证明一个人。
    """
    used: set[int] = set()
    attached: dict[int, float] = {}
    for det in sorted(items, key=lambda d: -d.conf):
        best: int | None = None
        best_ratio = 0.0
        for index, person in enumerate(persons):
            if index in used:
                continue
            ratio = region_of(person).overlap_ratio(det.bbox)
            if ratio >= policy.attach_overlap and ratio > best_ratio:
                best, best_ratio = index, ratio
        if best is not None:
            used.add(best)
            attached[best] = max(attached.get(best, 0.0), det.conf)
    return attached


def assign_stations(
    boxes: Sequence[BBox], rois: Sequence[StationRoi]
) -> list[int | None]:
    """按脚的位置把每个人分到工位；不在任何区域内返回 None（路过的人）。"""
    return [next((roi.station_id for roi in rois if roi.owns(box)), None) for box in boxes]


def analyze_persons(
    persons: Sequence[Detection],
    buckets: dict[str, list[Detection]],
    policy: AnalysisPolicy,
    rois: Sequence[StationRoi] = (),
) -> list[PersonState]:
    """把人身框与附属框合成 `PersonState`。"""
    states = [
        PersonState(person=det.bbox, conf=det.conf) for det in persons
    ]
    if not states:
        return []

    stations = assign_stations([s.person for s in states], rois)
    for state, station_id in zip(states, stations):
        state.station_id = station_id

    # 正向证据优先：**多人且数量够**才直接判合规（见模块 docstring）
    hats, masks, uniforms = buckets["hat"], buckets["mask"], buckets["uniform"]
    positive = len(states) >= 2
    if not (positive and len(hats) >= len(states)):
        for index, conf in _attach(
            states, hats, lambda p: policy.head_region(p.person), policy
        ).items():
            states[index].has_hat = True
    else:
        for state in states:
            state.has_hat = True

    if not (positive and len(masks) >= len(states)):
        for index, _ in _attach(
            states, masks,
            lambda p: policy.head_region(p.person, ratio=policy.mask_ratio), policy,
        ).items():
            states[index].has_mask = True
    else:
        for state in states:
            state.has_mask = True

    if not (positive and len(uniforms) >= len(states)):
        for index, _ in _attach(
            states, uniforms, lambda p: policy.torso_region(p.person), policy
        ).items():
            states[index].has_uniform = True
    else:
        for state in states:
            state.has_uniform = True

    # 行为：主动检出项。**漏检就是不报**，不做「没检出 = 没吸烟」的推断——
    # 那等于要求模型在全店范围内零漏检，做不到。
    for index, _ in _attach(
        states, buckets["cigarette"], lambda p: p.person, policy
    ).items():
        states[index].smoking = True
    for index, _ in _attach(
        states, buckets["phone"], lambda p: p.person, policy
    ).items():
        states[index].using_phone = True
    return states


def analyze(
    detections: Sequence[Detection],
    frame: Frame,
    rois: Sequence[StationRoi] = (),
    policy: AnalysisPolicy | None = None,
) -> VisionFrame:
    """一帧检测结果 → `VisionFrame`。纯函数，不碰时钟、不碰数据库。"""
    if frame.width <= 0 or frame.height <= 0:
        raise VisionError(f"帧尺寸非法：{frame.width}x{frame.height}")
    policy = policy or AnalysisPolicy()
    buckets = _split(detections, policy)
    persons = analyze_persons(buckets["person"], buckets, policy, rois)

    stations: dict[int, int] = {}
    for state in persons:
        if state.station_id is not None:
            stations[state.station_id] = stations.get(state.station_id, 0) + 1

    return VisionFrame(
        frame=frame,
        persons=tuple(persons),
        headcount=len(persons),
        station_headcount=stations,
        empty=not persons,
    )
