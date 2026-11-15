# -*- coding: utf-8 -*-
"""三端降级标记（A6-05）：把「现在的系统能力」算成一份可渲染的状态。

## 为什么降级要单独算，而不是各端自己拼

TFT、Web、小程序三端的屏幕尺寸、刷新频率、谁在看都不一样，但**它们说的必须
是同一件事**。让三端各自读一堆布尔量自己拼文案，一定会出现：

- Web 显示「设备离线」，TFT 显示「网络异常」，小程序显示「数据更新中」——
  同一时刻三种说法，运维在群里问「到底哪个是真的」；
- 新加一个降级项（比如传感器故障），改了 Web 忘了 TFT，TFT 依然全绿；
- 三端对「离线」的理解不同：Web 认为「MQTT 断了」，TFT 认为「HTTP 请求失败」。

所以降级状态由这一个模块算出来，三端只负责把 `channels` 里属于自己的那段
渲染出来。文案也在这里定，改一处三端同步。

## 两条必须说清的语义

### 1. 断网 ≠ 不安全

这是端侧优先架构最容易被误解的地方。断网时**安全保护照常执行**（规则在本地、
继电器在本地），只是「上报」暂停了。如果界面只显示一个红色的「离线」，
现场第一反应是「这破系统不管用了」——然后去拉总闸，反而制造了停业事故。

所以离线的文案必须写成：

> 离线模式（本地保护仍生效，数据待补传 N 条）

而不是：

> 设备离线

### 2. 视觉离线是**最严重**的一类降级，必须单独点名

摄像头挂了之后，设备上其他一切正常：温度正常、燃气正常、调度正常。
但 `#3 明火离人` 退化成「只看火焰」（见 `safety/flame.py`：视觉过期按「有人」
处理，只单独告警），`#16 违规处置` 完全失效。

只报一句「视觉离线」，没人会想到「后厨的着装/吸烟监管已经没有了」。所以
视觉离线时**必须**把受影响的功能逐条列出来，这是验收里「能说清哪些能力不可用」
的落点。

## `severity` 的口径

- `normal`：全部能力可用；
- `degraded`：有降级，但**安全保护仍然有效**（视觉离线、传感器故障、用缓存配置）；
- `offline`：与平台失联，安全保护在本地仍有效，但上报与远程控制不可用。

`offline` 不是比 `degraded` 更严重，它是**另一个维度**。所以 `severity` 只用于
图标/配色，真正决定「哪些能力不可用」的是 `capabilities`。
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CAP_ALARM",
    "CAP_CONFIG",
    "CAP_GAS",
    "CAP_REPLAY",
    "CAP_SENSOR",
    "CAP_VISION_DISPOSAL",
    "CAP_VISION_FLAME",
    "Capability",
    "DegradeReport",
    "STATE_DEGRADED",
    "STATE_FULL",
    "STATE_OFF",
    "build_degrade",
]

STATE_FULL = "full"
STATE_DEGRADED = "degraded"
STATE_OFF = "off"

CAP_ALARM = "threshold_alarm"
CAP_GAS = "gas_protection"
CAP_VISION_FLAME = "vision_flame"
CAP_VISION_DISPOSAL = "vision_disposal"
CAP_SENSOR = "sensor_integrity"
CAP_REPLAY = "offline_replay"
CAP_CONFIG = "config_sync"

ICONS = {
    STATE_FULL: "●",
    STATE_DEGRADED: "◐",
    STATE_OFF: "○",
}


@dataclass(frozen=True)
class Capability:
    """一项能力的当前状态。`reason` 必须能被人读懂（会直接显示在 TFT 上）。"""

    key: str
    label: str
    state: str = STATE_FULL
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.state == STATE_FULL

    @property
    def broken(self) -> bool:
        return self.state == STATE_OFF

    def describe(self) -> str:
        head = f"{ICONS.get(self.state, '?')} {self.label}"
        if self.state == STATE_FULL:
            return head
        mark = {STATE_DEGRADED: "降级", STATE_OFF: "不可用"}.get(self.state, self.state)
        return f"{head}（{mark}）：{self.reason}" if self.reason else f"{head}（{mark}）"

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DegradeReport:
    """一次降级计算结果。`channels` 是三端的渲染文案，键固定为 tft/web/miniapp。"""

    online: bool = True
    severity: str = "normal"
    headline: str = "运行正常"
    capabilities: tuple[Capability, ...] = ()
    channels: dict[str, dict] = field(default_factory=dict)
    ts: float = 0.0
    queue_pending: int = 0

    @property
    def degraded(self) -> bool:
        return self.severity != "normal"

    def unavailable(self) -> tuple[Capability, ...]:
        return tuple(cap for cap in self.capabilities if not cap.available)

    def capability(self, key: str) -> Capability | None:
        return next((cap for cap in self.capabilities if cap.key == key), None)

    def describe(self) -> str:
        if not self.degraded:
            return "运行正常：全部能力可用"
        lines = [f"【{self.headline}】"]
        lines.extend(f"- 不可用：{cap.describe()}" for cap in self.unavailable())
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "severity": self.severity,
            "headline": self.headline,
            "ts": round(float(self.ts), 3),
            "queue_pending": self.queue_pending,
            "capabilities": [cap.to_payload() for cap in self.capabilities],
            "unavailable": [cap.key for cap in self.unavailable()],
            "channels": self.channels,
        }


def build_degrade(
    *,
    network_online: bool = True,
    vision: Mapping[str, Any] | None = None,
    sensor_faults: Iterable[Mapping[str, Any] | Any] = (),
    config_degraded: bool = False,
    queue_pending: int = 0,
    clock_stable: bool | None = None,
    clock_offset: float = 0.0,
    now: float | None = None,
    wall_clock=None,
) -> DegradeReport:
    """按各子系统的现状算出降级报告（纯函数，不读全局状态）。

    参数尽量只收「事实」（视觉在不在线、哪些传感器坏了），不收子系统对象：
    这一层要能被三端、被单测、被离线演示直接调用，不能让调用方先拼出一整套
    运行时依赖。
    """
    moment = float((wall_clock or time.time)() if now is None else now)
    caps: list[Capability] = []

    # ① 端侧阈值报警：断网也照常，唯一的问题是配置可能停在缓存版本
    if config_degraded:
        caps.append(Capability(
            CAP_CONFIG, "配置同步", STATE_DEGRADED,
            "使用上一次下发的缓存配置，阈值可能不是最新",
        ))
        caps.append(Capability(
            CAP_ALARM, "阈值报警", STATE_DEGRADED, "按缓存配置判定，阈值可能滞后",
        ))
    else:
        caps.append(Capability(CAP_CONFIG, "配置同步", STATE_FULL))
        caps.append(Capability(CAP_ALARM, "阈值报警", STATE_FULL))

    # ② 燃气联锁：纯本地，永不降级（这是本系统「断网也安全」的核心论据）
    caps.append(Capability(CAP_GAS, "燃气/超温联锁", STATE_FULL))

    # ③ 视觉相关
    vision_online = True if vision is None else bool(vision.get("online", True))
    if vision_online:
        caps.append(Capability(CAP_VISION_FLAME, "明火离人（视觉）", STATE_FULL))
        caps.append(Capability(CAP_VISION_DISPOSAL, "着装与行为监管", STATE_FULL))
    else:
        age = vision.get("age_sec") if vision else None
        why = "视觉信号中断" + (f"（已 {float(age):.0f}s）" if isinstance(age, (int, float)) else "")
        caps.append(Capability(
            CAP_VISION_FLAME, "明火离人（视觉）", STATE_DEGRADED,
            f"{why}：明火离人退化为仅看火焰，不会联锁",
        ))
        caps.append(Capability(
            CAP_VISION_DISPOSAL, "着装与行为监管", STATE_OFF,
            f"{why}：未戴帽/口罩/工服、吸烟、玩手机检测全部停止",
        ))

    # ④ 传感器完整性
    faults = list(sensor_faults)
    if faults:
        names = "、".join(_metric_label(item) for item in faults)
        caps.append(Capability(
            CAP_SENSOR, "传感器数据", STATE_DEGRADED, f"{names} 异常，相关判定按「无数据」处理",
        ))
    else:
        caps.append(Capability(CAP_SENSOR, "传感器数据", STATE_FULL))

    # ⑤ 断网补传
    if network_online:
        caps.append(Capability(CAP_REPLAY, "数据上报", STATE_FULL))
    else:
        caps.append(Capability(
            CAP_REPLAY, "数据上报", STATE_OFF,
            f"已断开，{queue_pending} 条数据在本地排队，恢复后自动补传",
        ))

    severity, headline = _severity(network_online, caps)
    channels = _channels(network_online, headline, caps, queue_pending, clock_stable, clock_offset)
    return DegradeReport(
        online=bool(network_online),
        severity=severity,
        headline=headline,
        capabilities=tuple(caps),
        channels=channels,
        ts=moment,
        queue_pending=int(queue_pending),
    )


def _metric_label(item: Mapping[str, Any] | Any) -> str:
    """故障项 → 给人看的短名（`SensorFault` 与已展平的 dict 都吃）。"""
    if isinstance(item, Mapping):
        metric = str(item.get("metric") or item.get("key") or "?")
        label = item.get("label")
        return f"{metric}（{label}）" if label else metric
    metric = getattr(item, "metric", "?")
    kind = getattr(item, "kind", None)
    return f"{metric}（{getattr(kind, 'label', '故障')}）"


def _severity(online: bool, caps: list[Capability]) -> tuple[str, str]:
    broken = [cap for cap in caps if not cap.available]
    if not online:
        return "offline", "离线模式（本地保护仍生效）"
    if not broken:
        return "normal", "运行正常"
    safety_hit = any(cap.key in (CAP_VISION_DISPOSAL, CAP_VISION_FLAME) for cap in broken)
    return "degraded", ("部分安全能力降级" if safety_hit else "部分功能降级")


def _channels(
    online: bool,
    headline: str,
    caps: list[Capability],
    queue_pending: int,
    clock_stable: bool | None,
    clock_offset: float,
) -> dict[str, dict]:
    """三端渲染文案。**同一份 `affected`，三种排版**，不各自判断。"""
    broken = [cap for cap in caps if not cap.available]
    affected = [{"key": cap.key, "label": cap.label, "state": cap.state,
                 "reason": cap.reason} for cap in broken]
    clock_warn = ""
    if clock_stable is False:
        clock_warn = "端侧时钟未校准，补传时间可能偏差"
    elif clock_stable is not None and abs(clock_offset) >= 1.0:
        clock_warn = f"端侧时钟偏差 {clock_offset:+.1f}s，补传已自动校正"

    tft_lines = [headline]
    tft_lines.extend(f"{ICONS.get(cap.state, '?')} {cap.label}" for cap in broken[:3])
    if queue_pending:
        tft_lines.append(f"待补传 {queue_pending} 条")

    web_tooltip = [cap.describe() for cap in broken]
    if clock_warn:
        web_tooltip.append(clock_warn)
    if online:
        web_tooltip.append("本地安全保护始终生效")

    miniapp_items = [
        {"label": cap.label, "state": cap.state, "reason": cap.reason} for cap in broken
    ]
    status = "离线（本地保护生效）" if not online else ("部分降级" if broken else "在线")

    return {
        "tft": {
            # 96px 宽的屏一次最多 4 行，超出的由 Web 承担
            "box": "red" if (not online or broken) else "none",
            "headline": headline,
            "lines": tft_lines[:4],
            "compact": True,
            "note": "本地保护仍生效" if not online else "",
        },
        "web": {
            "badge": "设备离线" if not online else ("降级" if broken else ""),
            "level": "danger" if not online else ("warning" if broken else "ok"),
            "headline": headline,
            "tooltip": web_tooltip,
            "affected": affected,
            "queue_pending": queue_pending,
        },
        "miniapp": {
            "status": status,
            "items": miniapp_items,
            "hint": "恢复网络后自动补传，无需手动操作" if not online else "",
        },
    }
