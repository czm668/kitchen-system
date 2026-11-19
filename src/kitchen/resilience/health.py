# -*- coding: utf-8 -*-
"""传感器故障判定、故障工单与超时升级（A6-04）。

## 「读数不正常」和「传感器坏了」是两件事

阈值规则管前者：温度超了报警。它管不了后者，因为坏的传感器往往**报出完全
合法但错误的值**：

| 现象 | 真实原因 | 阈值规则的表现 |
|---|---|---|
| 冷冻柜固定报 `-18.0` 不动 | DS18B20 卡在最后一次读数 | 一切正常，**冷库化了也不知道** |
| 冷藏柜报 `85.0` | DS18B20 上电默认值（未完成转换） | 阈值 8℃ 会报警，但报的是「温度过高」而不是「传感器坏了」 |
| 留样柜报 `-127` | 总线开路 / 探头没插好 | 报「温度过低」，运维去修压缩机 |
| 某一路整段没有数据 | 线断了 / 分线器坏了 | **什么都不报**（缺数据不算越界） |

最后一行是最要命的：`#6 留样` 的合规记录会一直显示「正常」，因为根本没有
读数来证明它不正常。所以故障判定必须独立于阈值判定，而且要覆盖「没数据」。

## 三类判据，各有各的时间尺度

- **读数卡死（stuck）**：连续 N 个样本完全相同且跨越 `stuck_window_sec`。
  用「完全相同」而不是「变化很小」：DS18B20 是 0.0625℃ 分辨率、MAX6675 是
  0.25℃，正常工况下不可能连续 5 分钟一个 bit 都不变。
- **超量程（out_of_range）**：超出物理可能范围。这不是「报警阈值」，是
  「这个值不可能是真的」——`85.0` 对冷藏柜就是与阈值 8℃ 无关的另一类事实。
- **心跳丢失（lost）**：`now - last_seen > heartbeat_timeout`。缺数据也算故障，
  这是把「沉默」变成「事件」的唯一办法。

## 工单与升级

三件事必须分开记：`opened_at`（什么时候坏的）、`due_at`（什么时候该修好）、
`escalated_at`（什么时候升级过）。只记一个 `created_at` 会出这个 bug：
工单在待办里躺了三天，看起来和刚开的单一模一样，运维的排序永远是先来先做，
于是「修好一个坏传感器」被排到「三天前就该升级的那张单」后面。

升级用**轮询**（`due_escalations()`）而不是定时器：端侧没有 APScheduler，
也不想为了一个升级引入线程。服务端拿到 `due_escalations()` 的结果后自己决定
推给谁——职责分开，端侧只负责「谁超时了」这个事实。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("kitchen.resilience.health")

__all__ = [
    "DEFAULT_RANGES",
    "FaultKind",
    "FaultTicket",
    "SensorFault",
    "SensorHealthMonitor",
    "SensorPolicy",
    "TICKET_TYPE",
]

#: 故障工单在 `work_orders.type` 里的取值
TICKET_TYPE = "sensor_fault"


class FaultKind(str, Enum):
    """三类判据（见模块 docstring）。"""

    STUCK = "stuck"
    OUT_OF_RANGE = "out_of_range"
    LOST = "lost"

    @property
    def label(self) -> str:
        return {
            FaultKind.STUCK: "读数卡死",
            FaultKind.OUT_OF_RANGE: "超量程",
            FaultKind.LOST: "心跳丢失",
        }[self]

    @property
    def severity(self) -> int:
        """1=提示 2=警告 3=严重。丢数据比读数卡死更严重（后者至少还有数据）。"""
        return {FaultKind.STUCK: 2, FaultKind.OUT_OF_RANGE: 2, FaultKind.LOST: 3}[self]


#: 物理可能范围（min, max）。**不是报警阈值**，是「这个值不可能是真的」。
#: 数值取自器件手册与本项目实际工况（见 03_设计文档/04 的遥测表）。
DEFAULT_RANGES: dict[str, tuple[float, float]] = {
    "t": (-10.0, 60.0),
    "h": (0.0, 100.0),
    "gas_level": (0.0, 1.0),
    "hood_temp": (-20.0, 500.0),
    "sterilize_temp": (-20.0, 300.0),
    "fridge_temp": (-30.0, 30.0),
    "freezer_temp": (-45.0, 0.0),
    "sample_temp": (-30.0, 30.0),
    "sct_current": (0.0, 30.0),
    "weight_g": (0.0, 30000.0),
    "flame_a": (0.0, 1.0),
    "flame_b": (0.0, 1.0),
}

#: DS18B20 的两个典型异常值：85.0 是上电未完成转换，-127 是总线开路。
#: 单独列出来是为了让工单文案能直接说「探头没插好」，而不是让人去猜。
DS18B20_SENTINELS: dict[float, str] = {
    85.0: "DS18B20 上电默认值（85℃），通常是探头未完成转换或供电不足",
    -127.0: "DS18B20 开路值（-127℃），通常是探头没插好或线断了",
}


@dataclass(frozen=True)
class SensorPolicy:
    """故障判定参数。默认值按「1 秒一采、5 分钟内必须出单」定。"""

    ranges: Mapping[str, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_RANGES)
    )
    #: 卡死判据：连续相同样本数
    stuck_repeat: int = 60
    #: 卡死判据：还要跨越这么多秒（防「开机 3 秒内读数本来就没变」）
    stuck_window_sec: float = 300.0
    #: 心跳丢失：超过这么多秒没读数
    heartbeat_timeout_sec: float = 60.0
    #: 工单默认处理时限
    due_sec: float = 1800.0
    #: 超时升级到上一级的时间（从 due_at 算起）
    escalate_after_sec: float = 900.0
    #: 同一指标的同一类故障在这么多秒内不重复开单（防刷单）
    reopen_cooldown_sec: float = 1800.0


@dataclass(frozen=True)
class SensorFault:
    """一次故障判定结果（不持久化，工单才是持久化对象）。"""

    metric: str
    kind: FaultKind
    message: str
    since: float
    value: float | None = None
    samples: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.metric, self.kind.value)

    @property
    def severity(self) -> int:
        return self.kind.severity

    def describe(self) -> str:
        where = self.metric
        if self.value is not None:
            where += f"={self.value:g}"
        return f"{where} {self.kind.label}：{self.message}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "kind": self.kind.value,
            "label": self.kind.label,
            "severity": self.kind.severity,
            "message": self.message,
            "since": round(self.since, 3),
            "value": self.value,
            "samples": self.samples,
        }


@dataclass
class FaultTicket:
    """故障工单（内存版；A7 落 `work_orders`，`type='sensor_fault'`）。"""

    id: int
    metric: str
    kind: FaultKind
    message: str
    opened_at: float
    due_at: float
    level: int = 1
    state: str = "open"          # open / acknowledged / closed
    reviewer: str = ""
    escalated_at: float | None = None
    closed_at: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.state != "closed"

    def is_overdue(self, now: float) -> bool:
        return self.open and now >= self.due_at

    def is_due_for_escalation(self, now: float, after: float) -> bool:
        """超期 `after` 秒**还没人接单** → 该升级了。

        判据是 `state == "open"` 而不是 `open` 属性：接单（acknowledged）之后
        再升级，等于运维已经在修了还把单往上捅，升级链会变成噪声。
        """
        return (
            self.state == "open"
            and now >= self.due_at + float(after)
            and self.escalated_at is None
        )

    def describe(self) -> str:
        body = f"#{self.id} {self.metric} {self.kind.label}（L{self.level}，{self.state}）"
        if self.escalated_at is not None:
            body += "，已升级"
        return body

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": TICKET_TYPE,
            "metric": self.metric,
            "kind": self.kind.value,
            "message": self.message,
            "state": self.state,
            "level": self.level,
            "reviewer": self.reviewer,
            "opened_at": round(self.opened_at, 3),
            "due_at": round(self.due_at, 3),
            "escalated_at": None if self.escalated_at is None else round(self.escalated_at, 3),
            "closed_at": None if self.closed_at is None else round(self.closed_at, 3),
            "notes": list(self.notes),
        }


@dataclass
class _Series:
    last_value: float | None = None
    last_at: float | None = None
    #: 连续与 `last_value` 相同的样本数（含首个）
    repeat: int = 0
    repeat_since: float = 0.0
    fault: SensorFault | None = None


class SensorHealthMonitor:
    """逐指标判定三类故障，并开/关故障工单。

    用法::

        mon = SensorHealthMonitor()
        for frame in stream:                    # 每秒一帧
            mon.observe(frame.metrics, now=frame.ts)
            for fault in mon.new_faults():
                open_ticket(fault)
            for ticket in mon.due_escalations(now=frame.ts):
                escalate(ticket)
    """

    def __init__(
        self,
        policy: SensorPolicy | None = None,
        *,
        metrics: Iterable[str] | None = None,
        clock=None,
        wall_clock=None,
    ) -> None:
        self.policy = policy or SensorPolicy()
        # 全模块只用**一个**时钟，而且默认是墙钟：工单有 `due_at`，
        # 单调时钟算出来的到期时间没法给人看，也没法跨重启比较。
        # 混用两个时钟域会让「活 5 分钟」和「超期 30 分钟」永远不能同时成立。
        self._clock = wall_clock or clock or time.time
        self._series: dict[str, _Series] = {
            str(metric): _Series() for metric in (metrics or self.policy.ranges)
        }
        self._tickets: dict[int, FaultTicket] = {}
        self._by_key: dict[tuple[str, str], int] = {}
        self._next_id = 1
        self._new: list[SensorFault] = []
        self._closed_faults: list[SensorFault] = []

    # ---------------------------------------------------------------- 观测
    def observe(
        self,
        metrics: Mapping[str, float],
        *,
        now: float | None = None,
    ) -> tuple[SensorFault, ...]:
        """喂一帧遥测，返回本帧**新出现**的故障（已有的故障不重复返回）。"""
        moment = float(self._clock() if now is None else now)
        self._check_lost(moment)
        for metric, value in metrics.items():
            self._observe_metric(str(metric), float(value), moment)
        out = tuple(self._new)
        self._new.clear()
        return out

    def observe_metric(
        self, metric: str, value: float, *, now: float | None = None
    ) -> SensorFault | None:
        """单指标喂一个样本（服务端按 topic 逐条消费时用）。"""
        moment = float(self._clock() if now is None else now)
        self._observe_metric(str(metric), float(value), moment)
        out = self._new
        self._new = []
        return out[0] if out else None

    def _observe_metric(self, metric: str, value: float, now: float) -> None:
        series = self._series.setdefault(metric, _Series())
        policy = self.policy
        series.last_at = now
        if series.last_value is not None and value == series.last_value:
            series.repeat += 1
        else:
            series.repeat = 1
            series.repeat_since = now
            # 值变了 → 卡死故障自动解除（只是值变了，不代表别的判据也好了）
            if series.fault is not None and series.fault.kind is FaultKind.STUCK:
                self._resolve(series, now, "读数恢复变化")
        series.last_value = value

        # ① 超量程（先判，因为它的证据最强：这个值不可能存在）
        bounds = policy.ranges.get(metric)
        if bounds is not None:
            low, high = float(bounds[0]), float(bounds[1])
            if value < low or value > high:
                hint = DS18B20_SENTINELS.get(value, "")
                message = f"读数 {value:g} 超出物理范围 [{low:g}, {high:g}]"
                if hint:
                    message += f"；{hint}"
                self._raise(series, SensorFault(
                    metric=metric, kind=FaultKind.OUT_OF_RANGE, message=message,
                    since=now, value=value, samples=series.repeat,
                ), now)
                return
        # ② 读数卡死
        if (
            series.repeat >= policy.stuck_repeat
            and now - series.repeat_since >= policy.stuck_window_sec
        ):
            self._raise(series, SensorFault(
                metric=metric, kind=FaultKind.STUCK,
                message=(
                    f"连续 {series.repeat} 个样本都是 {value:g}"
                    f"（已 {now - series.repeat_since:.0f}s 无变化）"
                ),
                since=series.repeat_since, value=value, samples=series.repeat,
            ), now)
            return
        # 回到正常：卡死/超量程故障解除
        if series.fault is not None:
            self._resolve(series, now, f"读数回到正常范围（{value:g}）")

    def _check_lost(self, now: float) -> None:
        limit = self.policy.heartbeat_timeout_sec
        for metric, series in self._series.items():
            if series.last_at is None:
                continue    # 从未上报过：由「心跳」之外的机制（设备上线检查）负责
            idle = now - series.last_at
            if idle > limit:
                if series.fault is None or series.fault.kind is not FaultKind.LOST:
                    self._raise(series, SensorFault(
                        metric=metric, kind=FaultKind.LOST,
                        message=f"{idle:.0f}s 无读数（心跳阈值 {limit:.0f}s），疑似掉线或线断",
                        since=series.last_at, value=series.last_value,
                    ), now)

    def _raise(self, series: _Series, fault: SensorFault, now: float) -> None:
        """登记新故障。同 key 的工单还在冷却期内不重复开单。"""
        if series.fault is not None and series.fault.key == fault.key:
            return      # 同一故障持续中
        series.fault = fault
        self._new.append(fault)
        existing = self._by_key.get(fault.key)
        if existing is not None:
            ticket = self._tickets[existing]
            if ticket.open and now - ticket.opened_at < self.policy.reopen_cooldown_sec:
                ticket.notes.append(f"{now:.0f}s 复发：{fault.message}")
                return
        self.open_ticket(fault, now=now)

    def _resolve(self, series: _Series, now: float, reason: str) -> None:
        fault = series.fault
        series.fault = None
        if fault is None:
            return
        self._closed_faults.append(fault)
        ticket_id = self._by_key.get(fault.key)
        if ticket_id is None:
            return
        ticket = self._tickets[ticket_id]
        if ticket.state != "open":
            return
        # **不自动关单**：故障消失不等于处理完成，得有人来说「我看过了」。
        # 设备自己把单关掉，现场就没有任何记录证明这次故障发生过。
        ticket.notes.append(f"{now:.0f}s 读数恢复：{reason}（待人工确认关闭）")
        log.info("传感器 %s 故障消失（%s），工单 #%d 等待人工关闭",
                 fault.metric, reason, ticket.id)

    # ---------------------------------------------------------------- 工单
    def open_ticket(self, fault: SensorFault, *, now: float | None = None) -> FaultTicket:
        moment = float(self._clock() if now is None else now)
        ticket = FaultTicket(
            id=self._next_id,
            metric=fault.metric,
            kind=fault.kind,
            message=fault.message,
            opened_at=moment,
            due_at=moment + self.policy.due_sec,
            level=1 if fault.kind.severity < 3 else 2,
        )
        self._next_id += 1
        self._tickets[ticket.id] = ticket
        self._by_key[fault.key] = ticket.id
        log.warning("开故障工单 #%d：%s %s — %s",
                    ticket.id, fault.metric, fault.kind.label, fault.message)
        return ticket

    def ack(self, ticket_id: int, reviewer: str) -> FaultTicket:
        ticket = self._require(ticket_id)
        if ticket.state == "closed":
            raise ValueError(f"工单 #{ticket_id} 已关闭，不能再接单")
        ticket.state = "acknowledged"
        ticket.reviewer = str(reviewer or "")
        return ticket

    def close(
        self, ticket_id: int, *, reviewer: str = "", note: str = "",
        wall_epoch: float | None = None,
    ) -> FaultTicket:
        ticket = self._require(ticket_id)
        if ticket.state == "closed":
            return ticket
        moment = float(self._clock() if wall_epoch is None else wall_epoch)
        ticket.state = "closed"
        ticket.closed_at = moment
        if reviewer:
            ticket.reviewer = str(reviewer)
        if note:
            ticket.notes.append(str(note))
        # 关单后清掉 key 映射，允许下一次故障开新单
        self._by_key.pop((ticket.metric, ticket.kind.value), None)
        return ticket

    def due_escalations(self, *, now: float | None = None) -> tuple[FaultTicket, ...]:
        """**只读**：列出该升级的工单，由调用方（服务端调度器）执行升级。"""
        moment = float(self._clock() if now is None else now)
        return tuple(
            ticket for ticket in self._tickets.values()
            if ticket.is_due_for_escalation(moment, self.policy.escalate_after_sec)
        )

    def escalate(self, ticket_id: int, *, now: float | None = None) -> FaultTicket:
        """升级：等级 +1，记 `escalated_at`，并追加一条说明。"""
        ticket = self._require(ticket_id)
        moment = float(self._clock() if now is None else now)
        ticket.escalated_at = moment
        ticket.level += 1
        overdue = moment - ticket.due_at
        ticket.notes.append(
            f"{moment:.0f}s 超时未处理（超期 {overdue / 60:.0f} 分钟），升级至 L{ticket.level}"
        )
        log.warning("故障工单 #%d 超时升级至 L%d（%s）", ticket.id, ticket.level, ticket.metric)
        return ticket

    def tickets(self, *, open_only: bool = False) -> tuple[FaultTicket, ...]:
        return tuple(
            ticket for ticket in self._tickets.values() if ticket.open or not open_only
        )

    def ticket(self, ticket_id: int) -> FaultTicket | None:
        return self._tickets.get(int(ticket_id))

    def _require(self, ticket_id: int) -> FaultTicket:
        ticket = self._tickets.get(int(ticket_id))
        if ticket is None:
            raise KeyError(f"工单 #{ticket_id} 不存在")
        return ticket

    # ---------------------------------------------------------------- 观测
    def faulty(self) -> tuple[SensorFault, ...]:
        """当前处于故障中的指标（含尚未恢复的）。"""
        return tuple(
            series.fault for series in self._series.values() if series.fault is not None
        )

    def healthy(self, metric: str) -> bool:
        series = self._series.get(str(metric))
        return series is None or series.fault is None

    def usable(self) -> dict[str, bool]:
        """每个指标当前是否可信——视觉/规则层据此决定「这条读数能不能用」。"""
        return {metric: series.fault is None for metric, series in self._series.items()}

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        moment = float(self._clock() if now is None else now)
        return {
            "faults": [fault.to_payload() for fault in self.faulty()],
            "open_tickets": len([t for t in self._tickets.values() if t.open]),
            "escalations_due": [
                ticket.id for ticket in self.due_escalations(now=moment)
            ],
            "series": {
                metric: {
                    "last_value": series.last_value,
                    "idle_sec": None if series.last_at is None else round(moment - series.last_at, 1),
                    "repeat": series.repeat,
                    "healthy": series.fault is None,
                }
                for metric, series in self._series.items()
            },
        }
