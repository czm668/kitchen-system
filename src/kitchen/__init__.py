# -*- coding: utf-8 -*-
"""基于 ESP32 的后厨智能调度与食安合规管理系统 · 新选题业务包。

分层（只放新选题自己的代码，旧工程素材见 `tools/legacy.py`）：

- `kitchen.manifest`  场景契约加载 + 13 类信号唯一事实源
- `kitchen.channels`  执行对象映射与三通道互锁（防误发）
- `kitchen.sim`       13 类信号注入引擎（曲线 / hold / 游走 / 事件回放）
- `kitchen.device`    kitchen-only 模拟器状态机（不背旧场景兼容包袱）
- `kitchen.drivers`   12 遥测指标驱动 + 三通道/独立 GPIO 执行驱动

纯标准库，不连数据库、不发 MQTT、不读 .env；可独立跑测试与离线自检。
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["manifest", "channels", "sim", "device", "drivers"]
