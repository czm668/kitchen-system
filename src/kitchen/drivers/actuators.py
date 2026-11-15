# -*- coding: utf-8 -*-
"""后厨场景执行侧驱动（P0 只声明动作与状态回写，不接 GPIO）。

执行通道映射（定稿冻结，详见素材库 docs/kitchen-contract.md §3）：
  CH1 燃气电磁阀  -> gas_valve_close / gas_valve_open   （#1/#2/#3 关阀）
  CH2 排风扇      -> fan_on / fan_off                   （#1/#2/#7）
  CH3 工位插座电源 -> power_cut / power_restore          （#16 视觉违规断电）
  独立 GPIO 声光/语音 -> alarm_light_on / alarm_light_off / voice_play（不占三通道）

硬约束：
- kitchen 场景**不使用** mingchu 的 `valve_close` / `valve_open` 动作名；
  关燃气阀与切工位插座是两种执行对象，动作名必须不同，禁止串路。
- 动作成功（命令 ACK 首次 acked）后按 `drivers[].states` 回写 `devices.state`；
  失败/超时不回写（平台底座能力，通过素材取用器按需接入）。
- `commands` 表没有 scenario 列，靠 `devices.scenario` 设备归属隔离；
  本场景所有命令沿用该约定，不新增场景列。
- CH3 失效安全：断电即断（常闭/失电断开），恢复必须人工确认后手动复位，
  规则引擎不得自动下发 `power_restore`；触点容量建议 10A/250V 继电器或接触器。
"""
from __future__ import annotations

from kitchen.drivers.base import DriverBase


class Relay3ChDriver(DriverBase):
    """继电器 ×3：三路执行末端，互不共用动作名。"""

    id = "relay3ch"
    topic = ""
    metrics = ()
    actions = (
        "gas_valve_close", "gas_valve_open",   # CH1
        "fan_on", "fan_off",                   # CH2
        "power_cut", "power_restore",          # CH3
    )
    aliases = {}


class SignalDriver(DriverBase):
    """声光报警 + DFPlayer 语音：独立 GPIO，不占 CH1–CH3。

    P0 只固定「独立通道」这一口径；具体动作名与语音素材清单在 P1 固件阶段定稿，
    契约文档 §3.4 注明该口径，避免后续误把声光挤进三路继电器。
    """

    id = "signal"
    topic = ""
    metrics = ()
    actions = ("alarm_light_on", "alarm_light_off", "voice_play")
    aliases = {}
