# -*- coding: utf-8 -*-
"""场景驱动的最小契约（新工程自带，不依赖素材库 `plugins/base.py`）。

一个驱动 = 一个遥测主题的采样单元；执行类驱动只声明 `actions`，不产生遥测。
场景 manifest 通过 `drivers[].class` 指向本包内的实现，例如
`kitchen.drivers.sensors:AmbientDriver`。
"""
from __future__ import annotations

from collections.abc import Mapping


class DriverBase:
    """驱动基类。子类必须声明 `id`、`topic`（执行类 topic 为空串）。"""

    id: str = ""
    topic: str = ""
    metrics: tuple[str, ...] = ()
    aliases: dict[str, str] = {}
    actions: tuple[str, ...] = ()

    def sample(self, t: float, overrides: Mapping[str, float] | None = None) -> dict:
        """返回本驱动的平铺遥测 payload（不含 ts，由模拟器补充）。"""
        return {}

    @classmethod
    def describe(cls) -> dict:
        """给设备页 / 契约核对用的只读描述。"""
        return {
            "id": cls.id,
            "topic": cls.topic,
            "metrics": list(cls.metrics),
            "actions": list(cls.actions),
            "aliases": dict(cls.aliases),
        }
