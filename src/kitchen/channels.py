# -*- coding: utf-8 -*-
"""后厨场景「执行对象映射层」（P1 / T2 可运行实现）。

契约依据：素材库 `docs/kitchen-contract.md` §2.1–§2.6、本工程 `scenarios/kitchen.json` 的
`drivers[relay3ch|signal].actions / states`。

这个模块解决的不是「怎么发 MQTT」，而是**防止把动作发错执行对象**：
- 6 个三通道动作各自绑定唯一通道，业务代码一律通过 `validate_routing` /
  `InterlockController.request` 取绑定，禁止直写 GPIO / channel 号；
- 「关燃气阀」(CH1) 与「切工位插座」(CH3) 动作名不同、通道不同，
  交叉映射在映射层就被拒绝，不会等到继电器动作才出事；
- 互锁优先级、闩锁（任一触发即开、全部恢复才关）、人工确认、CH3 失效安全
  与 Web+TFT 双通道复核统一在这里裁决（契约 §2.3 / §2.4 / §1.4）。

设计约束：
- 纯标准库、纯函数为主，不连数据库、不发 MQTT、不读 .env，便于单测；
- 不依赖旧工程 kernel/、不连数据库、不引用 mingchu 场景；
- 声光 / DFPlayer 走独立 GPIO，不占 CH1–CH3（契约 §2.5）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# kitchen.json 中 relay3ch 绑定的设备；commands 表无 scenario 列，靠设备归属隔离（§2.6）
RELAY_DEVICE_ID = "kitchen-node-01"


class Channel(str, Enum):
    """执行末端。GPIO_SIGNAL 是独立通道，不参与三路互锁。"""

    CH1 = "CH1"  # 燃气电磁阀（12V 常闭）
    CH2 = "CH2"  # 排风扇
    CH3 = "CH3"  # 工位插座电源（10A/250V 独立回路）
    GPIO_SIGNAL = "GPIO_SIGNAL"  # 声光 + DFPlayer


RELAY_CHANNELS: tuple[Channel, ...] = (Channel.CH1, Channel.CH2, Channel.CH3)
ALL_CHANNELS: tuple[Channel, ...] = (*RELAY_CHANNELS, Channel.GPIO_SIGNAL)

# 互锁优先级（契约 §2.3）：数值越小优先级越高
PRIORITY_GAS_LEAK = 0            # P0 燃气泄漏 #1
PRIORITY_FLAME_UNATTENDED = 1    # P1 明火离人 #3
PRIORITY_HOOD_HEAT = 2           # P2 排烟积热 #2
PRIORITY_VISION_VIOLATION = 3    # P3 视觉违规 #16
# 契约 §2.3 只排到 P3。下面两档是 A2 阶段新增的**辅助触发源**：它们只共用
# CH2 排风与声光，从不驱动 CH1 燃气阀、也不切 CH3 工位电，所以插在 P3 之后
# 不改变契约里四档的相对顺序，也不会让设备类异常压过安全类异常。
PRIORITY_EQUIPMENT = 4           # P4 设备类异常 #5/#6/#15（冷链、留样柜、消毒柜）
PRIORITY_ENV_COMFORT = 5         # P5 环境舒适 #7 温湿度（辅助展示，最低）
PRIORITY_MANUAL = 99             # 人工/未知触发源，最低优先级

# 互锁通道状态（契约 §1.4）
NORMAL = "normal"
LATCHED = "latched"
RELEASING = "releasing"
MANUAL_RESET_REQUIRED = "manual_reset_required"
DEGRADED = "degraded"


class ActuationError(ValueError):
    """执行映射/裁决失败的基类。"""


class UnknownActionError(ActuationError):
    """动作未在映射层登记（含 mingchu 的 valve_close 等跨场景动作名）。"""


class MisrouteError(ActuationError):
    """动作 → 通道 / 设备不一致，属于防误发硬拦截。"""


class InterlockBlocked(ActuationError):
    """互锁条件未满足：人工确认、触发源未全恢复、双通道复核、断网降级。"""


@dataclass(frozen=True)
class ActionBinding:
    """动作到执行对象的唯一绑定。"""

    action: str
    channel: Channel
    state_key: str
    state_value: str
    kind: str  # trip=触发保护 / restore=恢复 / signal=独立 GPIO
    label: str = ""
    device_id: str = RELAY_DEVICE_ID
    manual_confirm: bool = False  # 恢复动作是否需要人工确认（契约 §2.3 ③）

    @property
    def is_restore(self) -> bool:
        return self.kind == "restore"

    @property
    def is_signal(self) -> bool:
        return self.kind == "signal"

    def state_effect(self) -> dict[str, str]:
        """动作成功后回写 devices.state 的效果，必须与 kitchen.json 的 states 一致。"""
        return {self.state_key: self.state_value}


def _bindings() -> dict[str, ActionBinding]:
    rows = (
        # action, channel, state_key, state_value, kind, label
        ("gas_valve_close", Channel.CH1, "gas_valve", "closed", "trip", "关闭燃气电磁阀"),
        ("gas_valve_open", Channel.CH1, "gas_valve", "open", "restore", "开启燃气电磁阀"),
        ("fan_on", Channel.CH2, "fan", "on", "trip", "启动排风"),
        ("fan_off", Channel.CH2, "fan", "off", "restore", "停止排风"),
        ("power_cut", Channel.CH3, "station_power", "cut", "trip", "切断工位插座"),
        ("power_restore", Channel.CH3, "station_power", "on", "restore", "恢复工位插座"),
        # 独立 GPIO：不占三通道，不受互锁优先级抑制（§2.5 / §2.3 ④）
        ("alarm_light_on", Channel.GPIO_SIGNAL, "alarm_light", "on", "signal", "声光报警开"),
        ("alarm_light_off", Channel.GPIO_SIGNAL, "alarm_light", "off", "signal", "声光报警关"),
        ("voice_play", Channel.GPIO_SIGNAL, "voice", "played", "signal", "语音播报"),
    )
    result: dict[str, ActionBinding] = {}
    for action, channel, key, value, kind, label in rows:
        result[action] = ActionBinding(
            action=action,
            channel=channel,
            state_key=key,
            state_value=value,
            kind=kind,
            label=label,
            # 三通道恢复动作一律需人工确认；声光开关不需要
            manual_confirm=(kind == "restore"),
        )
    return result


ACTION_BINDINGS: dict[str, ActionBinding] = _bindings()
RELAY_ACTIONS: tuple[str, ...] = tuple(
    action for action, binding in ACTION_BINDINGS.items() if not binding.is_signal
)
SIGNAL_ACTIONS: tuple[str, ...] = tuple(
    action for action, binding in ACTION_BINDINGS.items() if binding.is_signal
)


def resolve_action(action: str) -> ActionBinding:
    """按动作名取唯一绑定；未登记动作直接抛错（禁止隐式兜底到别的通道）。"""
    binding = ACTION_BINDINGS.get(str(action).strip())
    if binding is None:
        raise UnknownActionError(
            f"动作 {action!r} 未在执行对象映射层登记；"
            f"kitchen 场景可用动作：{sorted(ACTION_BINDINGS)}"
        )
    return binding


def validate_routing(
    action: str,
    channel: Channel | str | None = None,
    device_id: str | None = None,
    scenario: str = "kitchen",
) -> ActionBinding:
    """无状态防误发校验：场景 → 动作 → 通道 → 设备四者必须一致。

    供命令下发、模拟器 ack 回放、固件出口共用。调用方传错 channel 或 device_id
    时抛 MisrouteError，而不是「尽量找一个能用的通道」。

    `scenario` 默认值 kitchen：mingchu / lab_safety 也有 `fan_on/fan_off`，
    但它们与 kitchen 的 CH2 排风不是同一套执行对象，跨场景复用映射层必须被拦截。
    """
    if str(scenario).strip() != "kitchen":
        raise MisrouteError(
            f"执行对象映射层只服务 kitchen 场景，收到 {scenario!r}；"
            f"旧场景动作不得经本映射层下发"
        )
    binding = resolve_action(action)
    if channel is not None and Channel(channel) != binding.channel:
        raise MisrouteError(
            f"动作 {action!r} 只能走 {binding.channel.value}，"
            f"收到 {Channel(channel).value}；跨通道下发已被拦截"
        )
    if device_id is not None and device_id != binding.device_id:
        raise MisrouteError(
            f"动作 {action!r} 归属设备 {binding.device_id}，收到 {device_id}"
        )
    return binding


def crosscheck_manifest(manifest) -> list[str]:
    """映射层与场景 manifest 双向核对，返回问题列表（空列表=通过）。

    双向：① 映射层每个动作都要在 manifest 声明且 states 一致；
    ② manifest 声明的动作必须都在映射层登记（防止新增动作绕过映射层）。
    这是 T2 的「三通道互不串路」在配置侧的兜底。
    """
    problems: list[str] = []
    declared: dict[str, tuple[str, dict]] = {}
    for driver in manifest.drivers:
        for action in driver.actions:
            declared[action] = (driver.id, dict(driver.states.get(action) or {}))

    for action, binding in ACTION_BINDINGS.items():
        if action not in declared:
            problems.append(f"映射层动作 {action} 未在 manifest 声明")
            continue
        driver_id, states = declared[action]
        if states != binding.state_effect():
            problems.append(
                f"{action} 的 states 不一致：映射层 {binding.state_effect()} / manifest {states}"
            )
        expected_driver = "signal" if binding.is_signal else "relay3ch"
        if driver_id != expected_driver:
            problems.append(f"{action} 应由 {expected_driver} 驱动声明，实际 {driver_id}")

    for action in declared:
        if action not in ACTION_BINDINGS:
            problems.append(f"manifest 动作 {action} 未在执行对象映射层登记")

    # 6 个三通道动作按「每通道 1 个触发 + 1 个恢复」分布，且不得挤占独立 GPIO
    channels = [ACTION_BINDINGS[a].channel for a in RELAY_ACTIONS]
    if set(channels) != set(RELAY_CHANNELS):
        problems.append("三通道动作未恰好覆盖 CH1–CH3")
    for channel in RELAY_CHANNELS:
        count = channels.count(channel)
        if count != 2:
            problems.append(
                f"{channel.value} 应有且仅有 2 个动作（trip/restore），实际 {count}"
            )
    if any(binding.channel is Channel.GPIO_SIGNAL for binding in ACTION_BINDINGS.values()
           if not binding.is_signal):
        problems.append("三通道动作被错误映射到独立 GPIO")
    if any(binding.channel in RELAY_CHANNELS for action, binding in ACTION_BINDINGS.items()
           if action in SIGNAL_ACTIONS):
        problems.append("声光动作占用了 CH1–CH3")

    return problems


@dataclass
class _ChannelLatch:
    """单通道闩锁：触发源集合 + 状态 + 人工复位复核记录。"""

    sources: dict[str, int] = field(default_factory=dict)  # source -> priority
    state: str = NORMAL
    reset_reviewers: set[str] = field(default_factory=set)


class InterlockController:
    """三通道互锁裁决器（有状态，但完全在内存，不碰数据库与服务）。

    典型用法::

        ctrl = InterlockController()
        ctrl.request("gas_valve_close", source="gas_leak",
                     priority=PRIORITY_GAS_LEAK)          # 允许
        ctrl.request("fan_off", manual_confirmed=True)    # 抛 InterlockBlocked
        ctrl.note_recovered(Channel.CH1, "gas_leak")
        ctrl.request("gas_valve_open", manual_confirmed=True)  # 允许
    """

    def __init__(self, device_id: str = RELAY_DEVICE_ID) -> None:
        self.device_id = device_id
        self._channels: dict[Channel, _ChannelLatch] = {
            channel: _ChannelLatch() for channel in RELAY_CHANNELS
        }
        self.degraded = False  # 断网降级（契约 §1.4 任一 → degraded）

    # ------------------------------------------------------------ 状态查询
    def channel_state(self, channel: Channel | str) -> str:
        return self._channels[Channel(channel)].state

    def is_latched(self, channel: Channel | str) -> bool:
        return bool(self._channels[Channel(channel)].sources)

    def active_sources(self, channel: Channel | str) -> dict[str, int]:
        return dict(self._channels[Channel(channel)].sources)

    def snapshot(self) -> dict[str, dict]:
        """给日志 / 演示页用的只读快照。"""
        return {
            channel.value: {
                "state": latch.state,
                "sources": dict(latch.sources),
                "reset_reviewers": sorted(latch.reset_reviewers),
            }
            for channel, latch in self._channels.items()
        }

    # ------------------------------------------------------------ 触发源管理
    def note_trigger(self, channel: Channel | str, source: str,
                     priority: int = PRIORITY_MANUAL) -> None:
        """登记一个触发源（任一触发即开）。source 为空时用 anonymous。"""
        latch = self._channels[Channel(channel)]
        latch.sources[source or "anonymous"] = int(priority)
        latch.state = LATCHED

    def note_recovered(self, channel: Channel | str, source: str) -> bool:
        """触发源恢复；全部恢复后进入 releasing。返回该通道是否完全无触发源。"""
        latch = self._channels[Channel(channel)]
        latch.sources.pop(source, None)
        if not latch.sources and latch.state in (LATCHED, MANUAL_RESET_REQUIRED):
            latch.state = RELEASING
        return not latch.sources

    def confirm_manual_reset(self, channel: Channel | str, reviewer: str) -> set[str]:
        """CH3 人工复位复核：Web + TFT 双通道齐全才允许 power_restore（§2.4）。"""
        latch = self._channels[Channel(channel)]
        latch.reset_reviewers.add(str(reviewer).strip().lower())
        return set(latch.reset_reviewers)

    def set_degraded(self, degraded: bool) -> None:
        """断网：端侧按 NVS 最后配置执行保守保护，期间禁止自动恢复。"""
        self.degraded = bool(degraded)
        for latch in self._channels.values():
            latch.state = DEGRADED if self.degraded else latch.state

    # ------------------------------------------------------------ 动作裁决
    def request(
        self,
        action: str,
        *,
        channel: Channel | str | None = None,
        device_id: str | None = None,
        manual_confirmed: bool = False,
        source: str = "",
        priority: int = PRIORITY_MANUAL,
        scenario: str = "kitchen",
    ) -> ActionBinding:
        """校验并裁决一个动作；通过返回绑定，不通过抛 ActuationError 子类。

        硬规则（契约 §2.3）：
        1. 动作 → 通道 → 设备必须一致（防串路 / 防误发）；
        2. 声光走独立 GPIO，不受互锁抑制；
        3. 恢复动作必须人工确认，且该通道触发源**全部**恢复；
        4. 高优先级触发源仍占用通道时，低优先级的恢复请求一律拒绝；
        5. CH3 恢复额外要求 Web + TFT 双通道复核；
        6. 断网降级期间禁止下发恢复动作。
        """
        binding = validate_routing(action, channel=channel, device_id=device_id,
                                   scenario=scenario)
        if device_id is None:
            # 未显式指定时按控制器自身归属校验，防止跨设备下发
            validate_routing(action, device_id=self.device_id, scenario=scenario)

        # ② 声光：任何优先级都可驱动，且不占三通道
        if binding.is_signal:
            return binding

        latch = self._channels[binding.channel]

        if binding.is_restore:
            # ⑥ 断网降级
            if self.degraded:
                raise InterlockBlocked(
                    f"{action}：断网降级期间禁止自动恢复，需人工现场处置"
                )
            # ③ 人工确认：规则引擎不得自动下发恢复动作
            if not manual_confirmed:
                raise InterlockBlocked(
                    f"{action} 属于恢复动作，必须人工确认后下发（契约 §2.3 ③）"
                )
            # ④ 任一触发源即开、全部恢复才关；高优先级占用期间不得释放
            if latch.sources:
                blockers = ", ".join(
                    f"{name}(P{level})" for name, level in sorted(
                        latch.sources.items(), key=lambda item: item[1]
                    )
                )
                raise InterlockBlocked(
                    f"{action}：通道 {binding.channel.value} 仍有未恢复触发源 [{blockers}]，"
                    f"高优先级占用期间禁止恢复"
                )
            # ⑤ CH3 失效安全：断电即断，恢复需 Web + TFT 双通道复核
            if binding.channel is Channel.CH3:
                reviewers = {"web", "tft"}
                if not reviewers.issubset(latch.reset_reviewers):
                    missing = sorted(reviewers - latch.reset_reviewers)
                    raise InterlockBlocked(
                        f"power_restore 需 Web + TFT 双通道人工复核，缺少：{missing}"
                    )
            latch.state = NORMAL
            latch.reset_reviewers.clear()
            return binding

        # trip：登记触发源。CH3 断电后立刻进入「待人工复位」状态（失效安全）
        latch.sources[source or "anonymous"] = int(priority)
        latch.state = MANUAL_RESET_REQUIRED if binding.channel is Channel.CH3 else LATCHED
        if binding.channel is Channel.CH3:
            latch.reset_reviewers.clear()  # 每次断电都必须重新复核
        return binding

    def reset_channel(self, channel: Channel | str) -> None:
        """测试 / 人工强制清状态：清空触发源并回到 normal（不产生动作）。"""
        latch = self._channels[Channel(channel)]
        latch.sources.clear()
        latch.reset_reviewers.clear()
        latch.state = NORMAL
