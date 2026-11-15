# -*- coding: utf-8 -*-
"""A8 后厨 TFT 终端包（模拟版，与 ESP32+LVGL 真机行为同构）。

模块速览：

| 模块 | 职责 | 真机对应物 |
|---|---|---|
| `screens` | 核心 4 屏的帧模型（结构契约） | LVGL 页面布局 |
| `render` | 帧 → 60x22 文本帧（演示/测试） | LCD 像素绘制 |
| `audio` | DFPlayer 播报编号表与模拟 | DFPlayer Mini + SD 卡曲目 |
| `terminal` | 终端状态机（在线/离线/同步） | 固件主循环 |

真机路线（板子到货后）：固件按 `03_设计文档/08_后厨TFT终端设计.md` 的
映射表实现，接口契约与本包完全一致（HTTP 轮询 + 相同的 JSON 字段）。
"""
from .audio import DfPlayerSim, VOICE_TABLE, announce_events
from .render import render_frame
from .screens import (
    build_card_screen,
    build_queue_screen,
    build_status,
    build_steps_screen,
    queue_screen_from_orders,
)
from .terminal import DirectTransport, TftTerminal

__all__ = [
    "audio", "render", "screens", "terminal",
    "DfPlayerSim", "VOICE_TABLE", "announce_events",
    "render_frame",
    "build_card_screen", "build_queue_screen", "build_status",
    "build_steps_screen", "queue_screen_from_orders",
    "DirectTransport", "TftTerminal",
]
