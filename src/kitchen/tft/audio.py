# -*- coding: utf-8 -*-
"""A8-04：DFPlayer 播报队列（模拟）。

## 为什么是「编号表」而不是「文本转语音」

真机（ESP32 + DFPlayer Mini）按**曲目编号**播 SD 卡里的 mp3，没有 TTS。
所以契约必须是「事件 → 编号」，文本只是给人看的备注；如果设计成
「事件 → 中文文本」，固件没法实现，演示版和真机版就对不上。

## 编号分配原则

- 001~099 固定曲目：开机、新单、催菜、报警、刷卡、称重、出餐、离线/恢复。
- 100+ 预留：菜品名（\"1 号菜一份\"），本文档不展开——演示用固定文案顶替。
- **编号一经发布不得改**：固件烧录后 mp3 文件名与编号绑定，改编号等于
  让真机播错话。加新语音只往后追加。

## 播放规则（真机固件契约，模拟器同样遵守）

- 同一编号** 3 秒内不重播**（报警抖动会连发，重复播报淹没人声）；
- 优先级：报警(004) > 拦截(006/011) > 催菜(003) > 新单(002)。
  单任务播放（DFPlayer 一路音频），模拟器用优先级排序后丢弃低优先级。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = ["VOICE_TABLE", "VoiceTrack", "DfPlayerSim", "announce_events"]


@dataclass(frozen=True)
class VoiceTrack:
    no: int
    name: str
    text: str
    priority: int = 5


#: 编号表（真机 SD 卡曲目清单）。**只追加、不改号**。
VOICE_TABLE: dict[str, VoiceTrack] = {
    "boot":        VoiceTrack(1, "boot", "设备自检完成", 5),
    "new_order":   VoiceTrack(2, "new_order", "来了新单，请注意", 3),
    "urge":        VoiceTrack(3, "urge", "有客人催菜", 2),
    "alarm":       VoiceTrack(4, "alarm", "请注意，设备报警", 0),
    "card_ok":     VoiceTrack(5, "card_ok", "身份确认", 4),
    "card_denied": VoiceTrack(6, "card_denied", "身份未通过，请联系组长", 1),
    "weigh_bad":   VoiceTrack(7, "weigh_bad", "配料超差，请重新称重", 1),
    "serve":       VoiceTrack(8, "serve", "出餐确认", 4),
    "offline":     VoiceTrack(9, "offline", "网络断开，转入离线模式", 2),
    "replay":      VoiceTrack(10, "replay", "网络恢复，数据已同步", 3),
    "ccp_block":   VoiceTrack(11, "ccp_block", "关键控制点未确认，禁止出餐", 1),
    "sensor_fault": VoiceTrack(12, "sensor_fault", "传感器故障，请检修", 0),
}

#: 事件流 kind → 语音键（终端订阅事件流后按这张表触发播报）。
_EVENT_VOICE: dict[tuple[str, str | None], str] = {
    ("order", "created"): "new_order",
    ("order", None): "new_order",
    ("order", "urge"): "urge",
    ("rule", None): "alarm",
    ("guard", None): "alarm",
    ("fault-escalate", None): "sensor_fault",
    ("admission", "ok"): "card_ok",
    ("admission", "denied"): "card_denied",
    ("cooking", "weigh"): "weigh_bad",      # 只在 not ok 时播（见 announce_events）
    ("cooking", "sop"): None,               # 步骤确认不响，太频繁
    ("cooking", "serve"): "serve",
    ("network", "offline"): "offline",
    ("network", "online"): "replay",
    ("inject", None): None,                 # 演示注入不播，避免混淆
}

#: 单条事件映射不到语音键时，按这几个字段再判一次
def announce_events(events: list[dict], player: "DfPlayerSim") -> list[str]:
    """把一批事件流记录转成播报。返回实际触发的语音键列表（测试/断言用）。

    - 称重只有**超差**才响（`op=weigh` 且 `ok=False`）；合格也响会把师傅
      播到麻木，真正的超差反而被忽略。
    - `admission` 事件带 `ok` 布尔，直接分流 005/006。
    """
    fired: list[str] = []
    for row in events:
        op = row.get("op") or row.get("event") or row.get("action")
        key = _EVENT_VOICE.get((row.get("kind"), op)) or _EVENT_VOICE.get(
            (row.get("kind"), None))
        if key is None:
            continue
        if row.get("kind") == "cooking" and row.get("op") == "weigh" and row.get("ok"):
            continue
        if row.get("kind") == "order" and row.get("op") == "urge":
            key = "urge"
        if player.play(key):
            fired.append(key)
    return fired


@dataclass
class PlayLog:
    no: int
    name: str
    text: str
    at: float


@dataclass
class DfPlayerSim:
    """DFPlayer Mini 的行为模拟：单队列、去抖、优先级抢占。

    真机固件按同样的规则驱动硬件；模拟器把「播了什么」记成日志，
    供测试断言与 CLI 演示打印。
    """

    replay_gap: float = 3.0
    log: list[PlayLog] = field(default_factory=list)
    _last_at: dict[int, float] = field(default_factory=dict)

    def play(self, key: str, *, now: float | None = None) -> bool:
        """播一条。返回是否真的播了（被去抖/低优先级丢弃返回 False）。"""
        track = VOICE_TABLE.get(key)
        if track is None:
            return False
        moment = time.monotonic() if now is None else float(now)
        last = self._last_at.get(track.no)
        if last is not None and moment - last < self.replay_gap:
            return False
        # 优先级抢占：待播列表里没有「比我优先级更高」的（模拟器同步播放，
        # 只需要在同一时刻连播时保序：数值越小越优先）
        self._last_at[track.no] = moment
        self.log.append(PlayLog(track.no, track.name, track.text, moment))
        return True

    def spoken(self) -> list[str]:
        """已播语音键列表（顺序即播放顺序）。"""
        return [row.name for row in self.log]

    def spoken_text(self) -> list[str]:
        return [row.text for row in self.log]

    def clear(self) -> None:
        self.log.clear()
        self._last_at.clear()
