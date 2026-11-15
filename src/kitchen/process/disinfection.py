# -*- coding: utf-8 -*-
"""#15 消毒曲线与复消闭环（A4-06）。

## 判据（`env_thresholds` id=9/10/11 同口径）

| 判据 | 取值 | 处置 |
|---|---|---|
| 达标 | **100℃ 以上累计 ≥ 600 s** | 记录留痕，自动关单 |
| 控温失效 | 连续 **> 120℃ 达 600 s** | 判不达标（温度高但时间白烧，工艺不对） |
| 异常超温 | **> 150℃** | 立即判不达标 + 禁出库 + 声光 |
| 超时 | 会话时长 > **1.5 倍**标准时长仍不达标 | 判不达标（柜子坏了/门没关） |

## 为什么必须做「会话门控」——这是本模块最容易踩的坑

`env_thresholds` 里 id=9 是 `range 100~120℃`。规则引擎是**无状态指标流**，
它不知道消毒柜现在是在消毒还是在待机。常温 25℃ 喂进去，第一个判据就是
「低于下限 100℃」——于是**不消毒的时候反而一直在报警**，现场第一件事就是
把这个点关掉，于是真消毒时也不报了。

所以：#15 的所有判定都只在 `state='running'` 的会话内进行。
`feed()` 在没有运行会话时**直接丢弃**读数并计入 `ignored`，不落事件、不报警。
消毒曲线是「一段过程」，不是「一个瞬时数值」——这是它与 #1/#2/#5 的根本区别。

## 累计时长怎么算

每个采样按 `dt = 采样时刻 - 上次采样时刻` 累加，且 **dt 上限 `max_gap_sec`（120s）**。
不设上限的后果：柜子断电 10 分钟再上电，一次采样的 dt = 600 s，第一个读数就
「达标」了——一台没工作过的柜子发了合格证。

`time_scale` 只压缩演示时间（480℃ 曲线 10 分钟 → 演示 30 秒），
不改变任何判据的**相对**关系，代码路径与真实运行完全一致。

## 复消闭环

不达标 → `forbid_outbound=True`（该批餐具禁止出库）+ 开一张复消工单（
`work_orders.type='disinfection_rework'`）+ 语音提示；复消会话达标 → 自动关单 && 解禁。
「禁出库」与「工单」必须同生同灭：只禁不禁、或只开工单不禁出库，都会出现
「餐具明明没消好，却已经上了桌」。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from kitchen.domain import CST, DomainError, NotFoundError, now_cst

from .models import ProcessEvent
from .repository import MemoryProcessRepository

log = logging.getLogger(__name__)

__all__ = [
    "DisinfectionError", "NotRunningError",
    "DISINFECTION_TARGET_C", "DISINFECTION_HOLD_C", "DISINFECTION_ABNORMAL_C",
    "TARGET_HOLD_SEC", "HOLD_LIMIT_SEC", "TIMEOUT_FACTOR", "MAX_SAMPLE_GAP_SEC",
    "DEMO_TIME_SCALE", "REWORK_TYPE",
    "DisinfectionPolicy", "DisinfectionRecord", "ReworkTicket",
    "DisinfectionSession", "DisinfectionService",
]

#: 达标温度下限（℃）
DISINFECTION_TARGET_C = 100.0
#: 控温上限（℃）：超过它只累计不报警，但连续超时就算控温失效
DISINFECTION_HOLD_C = 120.0
#: 异常超温（℃）：立即判不达标，不等累计
DISINFECTION_ABNORMAL_C = 150.0
#: 达标所需累计秒数（10 分钟）
TARGET_HOLD_SEC = 600.0
#: 连续超 120℃ 的容忍时长（秒）
HOLD_LIMIT_SEC = 600.0
#: 会话时长上限倍数：超过 1.5 倍标准时长仍不达标 → 判超时
TIMEOUT_FACTOR = 1.5
#: 单次采样允许累计的最大间隔（秒）。见模块文档：不设上限会让断电重连直接达标
MAX_SAMPLE_GAP_SEC = 120.0
#: 演示加速：把 600 s 压成 30 s
DEMO_TIME_SCALE = TARGET_HOLD_SEC / 30.0
#: 复消工单在 `work_orders.type` 里的取值
REWORK_TYPE = "disinfection_rework"

#: 判不达标的原因码 → 中文说明
FAIL_REASONS = {
    "abnormal_overtemp": "异常超温（>150℃）",
    "hold_overtime": "控温失效（>120℃ 持续超 600 s）",
    "timeout": "超时（运行时长超过标准 1.5 倍仍未达标）",
    "short": "累计时长不足（未达 100℃ 保持 10 分钟）",
}


class DisinfectionError(DomainError):
    """消毒操作不合法（会话不存在、温度读数异常）。"""


class NotRunningError(DisinfectionError):
    """没有正在运行的消毒会话。**不是**报警条件，是「此刻不判」的门控信号。"""


@dataclass(frozen=True)
class DisinfectionPolicy:
    """判据集合。默认值即 `env_thresholds` id=9/10/11 的口径。"""

    target_c: float = DISINFECTION_TARGET_C
    hold_c: float = DISINFECTION_HOLD_C
    abnormal_c: float = DISINFECTION_ABNORMAL_C
    target_sec: float = TARGET_HOLD_SEC
    hold_limit_sec: float = HOLD_LIMIT_SEC
    timeout_factor: float = TIMEOUT_FACTOR
    max_gap_sec: float = MAX_SAMPLE_GAP_SEC
    time_scale: float = 1.0

    def __post_init__(self) -> None:
        if self.time_scale <= 0:
            raise DisinfectionError(f"time_scale 必须 > 0，实际 {self.time_scale}")
        if not self.target_c < self.hold_c < self.abnormal_c:
            raise DisinfectionError(
                f"温度判据必须递增：达标 {self.target_c} < 控温 {self.hold_c} "
                f"< 超温 {self.abnormal_c}"
            )

    @property
    def timeout_sec(self) -> float:
        return self.target_sec * self.timeout_factor

    @classmethod
    def from_thresholds(
        cls, rows: Iterable[dict[str, object]], *, time_scale: float = 1.0,
    ) -> "DisinfectionPolicy":
        """从 `env_thresholds` 的行构造（`kind` 分 range / duration / tier 三类）。

        不让阈值在代码里写死两份：消毒柜换了工艺（比如 121℃ 高压蒸汽），
        改的应该是配置表，不是重新发一版软件。
        """
        target = hold = abnormal = None
        target_sec = HOLD_LIMIT_SEC
        for row in rows:
            kind = str(row.get("kind") or "")
            lower = row.get("lower_bound")
            upper = row.get("upper_bound")
            if kind == "range" and lower is not None:
                target = float(lower)
                hold = float(upper) if upper is not None else DISINFECTION_HOLD_C
            elif kind == "duration" and row.get("duration_sec") is not None:
                target_sec = float(row["duration_sec"])
                if upper is not None:
                    hold = float(upper)
            elif kind == "tier" and upper is not None:
                abnormal = float(upper)
        return cls(
            target_c=target if target is not None else DISINFECTION_TARGET_C,
            hold_c=hold if hold is not None else DISINFECTION_HOLD_C,
            abnormal_c=abnormal if abnormal is not None else DISINFECTION_ABNORMAL_C,
            target_sec=target_sec,
            time_scale=time_scale,
        )


@dataclass
class ReworkTicket:
    """复消工单（`work_orders.type='disinfection_rework'`）。"""

    id: int
    order_no: str
    record_id: int
    reason: str
    opened_at: datetime
    state: str = "open"
    closed_at: datetime | None = None

    def describe(self) -> str:
        tail = f"已于 {self.closed_at:%m-%d %H:%M} 关单" if self.closed_at else "待复消"
        return f"{self.order_no} 复消（{FAIL_REASONS.get(self.reason, self.reason)}，{tail}）"


@dataclass
class DisinfectionRecord:
    """`disinfection_records` 一行。一次会话一条。"""

    id: int
    session_uid: str
    batch_id: int | None
    start_ts: datetime
    state: str = "running"                     # running / passed / failed
    cumulative_sec: float = 0.0
    peak_temp: float = 0.0
    end_ts: datetime | None = None
    forbid_outbound: bool = False
    rework_order_id: int | None = None
    reason: str = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def passed(self) -> bool:
        return self.state == "passed"

    def describe(self) -> str:
        head = (f"{self.session_uid} 累计 {self.cumulative_sec:.0f}s / "
                f"峰值 {self.peak_temp:.0f}℃")
        if self.running:
            return f"{head}（运行中）"
        if self.passed:
            return f"{head} → 达标"
        return f"{head} → 不达标：{FAIL_REASONS.get(self.reason, self.reason)}"


@dataclass(frozen=True)
class DisinfectionReading:
    """一次采样后的判定快照（TFT 一屏 + 语音提示的输入）。"""

    record_id: int
    state: str
    temp_c: float
    cumulative_sec: float
    remaining_sec: float
    peak_temp: float
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.state == "passed"

    @property
    def failed(self) -> bool:
        return self.state == "failed"

    def describe(self) -> str:
        if self.state == "running":
            return (f"{self.temp_c:.0f}℃ 累计 {self.cumulative_sec:.0f}s，"
                    f"还差 {self.remaining_sec:.0f}s")
        if self.passed:
            return f"{self.temp_c:.0f}℃ 累计 {self.cumulative_sec:.0f}s → 消毒达标"
        return f"{self.temp_c:.0f}℃ → 不达标：{FAIL_REASONS.get(self.reason, self.reason)}"


class DisinfectionSession:
    """一次消毒会话（从程序启动到出柜）。"""

    def __init__(
        self,
        record: DisinfectionRecord,
        policy: DisinfectionPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
        rework: bool = False,
    ) -> None:
        self.record = record
        self.policy = policy
        self._clock = clock or (lambda: datetime.now(CST))
        self.rework = rework
        self._last_ts: datetime | None = None
        self._hold_run_sec = 0.0            # 连续 >120℃ 的时长（虚拟秒）
        self._elapsed_virtual = 0.0         # 会话虚拟总时长

    # ------------------------------------------------------------ 状态
    def now(self) -> datetime:
        return self._clock()

    @property
    def remaining_sec(self) -> float:
        return max(0.0, self.policy.target_sec - self.record.cumulative_sec)

    # ------------------------------------------------------------ 采样
    def observe(self, temp_c: float, *, now: datetime | None = None) -> DisinfectionReading:
        """喂一个温度读数，返回判定结果。"""
        if not self.record.running:
            raise DisinfectionError(f"会话 {self.record.session_uid} 已结束（{self.record.state}）")
        value = float(temp_c)
        if not -40.0 <= value <= 400.0:
            # MAX6675 断线读 0℃、短路读满量程；落到这个区间外一定是接线问题
            raise DisinfectionError(f"温度读数异常：{value}℃（检查 MAX6675 接线）")
        moment = now or self.now()
        gap = 0.0 if self._last_ts is None else (moment - self._last_ts).total_seconds()
        self._last_ts = moment
        # 断点保护：一次采样的累计上限，避免「断电十分钟后一帧达标」
        delta = min(max(gap, 0.0), self.policy.max_gap_sec) * self.policy.time_scale
        self._elapsed_virtual += delta

        if value > self.record.peak_temp:
            self.record.peak_temp = value

        if value >= self.policy.abnormal_c:
            return self._fail("abnormal_overtemp", value, moment)

        # 连续控温失效：跌回 120℃ 以下就清零（工艺要求「持续」，不是「累计」）
        if value > self.policy.hold_c:
            self._hold_run_sec += delta
            if self._hold_run_sec >= self.policy.hold_limit_sec:
                return self._fail("hold_overtime", value, moment)
        else:
            self._hold_run_sec = 0.0

        if self.policy.target_c <= value <= self.policy.hold_c:
            # 只有落在达标区间（100~120℃）才累计。125℃ 挂着 10 分钟不算达标：
            # 那不是热力消毒的工艺温度，时间再长也不产生合格结论——这类「高温
            # 空烧」恰恰是控温失效的表现，累计它等于给故障发合格证。
            self.record.cumulative_sec += delta
            if self.record.cumulative_sec >= self.policy.target_sec:
                self.record.cumulative_sec = self.policy.target_sec
                return self._pass(value, moment)

        if self._elapsed_virtual > self.policy.timeout_sec:
            return self._fail("timeout", value, moment)

        return self._reading(value)

    def finish(self, *, now: datetime | None = None) -> DisinfectionReading:
        """程序结束：累计够就达标，不够判不达标。"""
        if not self.record.running:
            return self._reading(0.0)
        moment = now or self.now()
        if self.record.cumulative_sec >= self.policy.target_sec:
            return self._pass(self.record.peak_temp, moment)
        return self._fail("short", self.record.peak_temp, moment)

    # ------------------------------------------------------------ 内部
    def _reading(self, temp_c: float) -> DisinfectionReading:
        return DisinfectionReading(
            record_id=self.record.id,
            state=self.record.state,
            temp_c=round(temp_c, 1),
            cumulative_sec=round(self.record.cumulative_sec, 1),
            remaining_sec=round(self.remaining_sec, 1),
            peak_temp=round(self.record.peak_temp, 1),
            reason=self.record.reason,
        )

    def _pass(self, temp_c: float, moment: datetime) -> DisinfectionReading:
        self.record.state = "passed"
        self.record.end_ts = moment
        self.record.reason = ""
        log.info("消毒达标：%s", self.record.describe())
        return self._reading(temp_c)

    def _fail(self, reason: str, temp_c: float, moment: datetime) -> DisinfectionReading:
        self.record.state = "failed"
        self.record.end_ts = moment
        self.record.reason = reason
        # 不达标 → 禁出库。这句和下面的复消工单必须成对出现：
        # 只禁不开单，没人去处理；只开单不禁，餐具照样能出库。
        self.record.forbid_outbound = True
        log.info("消毒不达标：%s", self.record.describe())
        return self._reading(temp_c)


class DisinfectionService:
    """消毒会话管理 + 复消闭环 + 出库门禁状态。

    `feed()` 的**门控**是本类的核心：没有 running 会话时，读数被丢弃并计入
    `ignored`。常温 25℃ 在非消毒时段本来就不是异常，见模块文档。
    """

    def __init__(
        self,
        repo: MemoryProcessRepository | None = None,
        *,
        policy: DisinfectionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.policy = policy or DisinfectionPolicy()
        self._clock = clock or (lambda: datetime.now(CST))
        self._records: dict[int, DisinfectionRecord] = {}
        self._sessions: dict[int, DisinfectionSession] = {}
        self._tickets: dict[int, ReworkTicket] = {}
        self._events: list[ProcessEvent] = []
        self._next_record = 1
        self._next_ticket = 1
        self._next_event = 1
        self.ignored = 0

    # ------------------------------------------------------------ 视图
    def now(self) -> datetime:
        return self._clock()

    @property
    def events(self) -> tuple[ProcessEvent, ...]:
        return tuple(self._events)

    @property
    def records(self) -> tuple[DisinfectionRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def tickets(self) -> tuple[ReworkTicket, ...]:
        return tuple(self._tickets[key] for key in sorted(self._tickets))

    def running(self) -> tuple[DisinfectionRecord, ...]:
        return tuple(row for row in self.records if row.running)

    def record(self, record_id: int) -> DisinfectionRecord:
        found = self._records.get(int(record_id))
        if found is None:
            raise NotFoundError(f"消毒记录 {record_id} 不存在")
        return found

    def ticket(self, ticket_id: int) -> ReworkTicket:
        found = self._tickets.get(int(ticket_id))
        if found is None:
            raise NotFoundError(f"复消工单 {ticket_id} 不存在")
        return found

    # ------------------------------------------------------------ 会话
    def start(
        self,
        *,
        batch_id: int | None = None,
        session_uid: str | None = None,
        staff_id: int | None = None,
        now: datetime | None = None,
    ) -> DisinfectionSession:
        moment = now or self.now()
        uid = session_uid or f"DI-{moment:%Y%m%d%H%M%S}"
        if any(row.session_uid == uid for row in self._records.values()):
            raise DisinfectionError(f"会话 {uid} 已存在（uk_dis_session）")
        record = DisinfectionRecord(
            id=self._next_record,
            session_uid=uid,
            batch_id=batch_id,
            start_ts=moment,
        )
        self._next_record += 1
        session = DisinfectionSession(record, self.policy, clock=self._clock)
        session._last_ts = moment
        self._records[record.id] = record
        self._sessions[record.id] = session
        self._emit("started", {
            "session_uid": uid,
            "batch_id": batch_id,
            "policy": {
                "target_c": self.policy.target_c,
                "target_sec": self.policy.target_sec,
                "abnormal_c": self.policy.abnormal_c,
            },
        }, staff_id=staff_id, record=record)
        log.info("消毒开始：%s（批 %s）", uid, batch_id)
        return session

    def feed(
        self,
        temp_c: float,
        *,
        record_id: int | None = None,
        now: datetime | None = None,
        staff_id: int | None = None,
    ) -> DisinfectionReading | None:
        """温度采样入口。**没有运行会话时返回 None 并丢弃**（门控）。"""
        session = self._active(record_id)
        if session is None:
            self.ignored += 1
            return None
        reading = session.observe(temp_c, now=now)
        record = session.record
        self._emit("reading", {
            "temp_c": reading.temp_c,
            "cumulative_sec": reading.cumulative_sec,
            "state": reading.state,
        }, staff_id=staff_id, record=record)
        if reading.failed:
            self._on_failure(record, reading.reason, staff_id=staff_id)
        elif reading.passed:
            self._on_pass(record, staff_id=staff_id)
        return reading

    def finish(
        self, *, record_id: int | None = None, now: datetime | None = None,
        staff_id: int | None = None,
    ) -> DisinfectionReading:
        session = self._active(record_id)
        if session is None:
            raise NotRunningError("没有正在运行的消毒会话，无法结束")
        reading = session.finish(now=now)
        record = session.record
        if reading.failed:
            self._on_failure(record, reading.reason, staff_id=staff_id)
        else:
            self._on_pass(record, staff_id=staff_id)
        return reading

    def _active(self, record_id: int | None) -> DisinfectionSession | None:
        if record_id is not None:
            session = self._sessions.get(int(record_id))
            if session is None:
                raise NotFoundError(f"消毒记录 {record_id} 不存在")
            return session if session.record.running else None
        running = [session for session in self._sessions.values() if session.record.running]
        return running[-1] if running else None

    # ------------------------------------------------------------ 复消闭环
    def _on_failure(self, record: DisinfectionRecord, reason: str,
                    *, staff_id: int | None = None) -> None:
        if record.rework_order_id is not None:
            return                                    # 只开一张单
        moment = self.now()
        ticket = ReworkTicket(
            id=self._next_ticket,
            order_no=f"WO-DI-{self._next_ticket:04d}",
            record_id=record.id,
            reason=reason,
            opened_at=moment,
        )
        self._next_ticket += 1
        self._tickets[ticket.id] = ticket
        record.rework_order_id = ticket.id
        record.forbid_outbound = True
        self._emit("rework_opened", {
            "order_no": ticket.order_no,
            "reason": reason,
            "batch_id": record.batch_id,
        }, staff_id=staff_id, record=record)
        log.info("复消开单：%s", ticket.describe())

    def _on_pass(self, record: DisinfectionRecord, *, staff_id: int | None = None) -> None:
        record.forbid_outbound = False
        if record.rework_order_id is None:
            self._emit("passed", {"cumulative_sec": record.cumulative_sec},
                       staff_id=staff_id, record=record)
            return
        ticket = self._tickets.get(record.rework_order_id)
        if ticket is not None and ticket.state == "open":
            ticket.state = "closed"
            ticket.closed_at = self.now()
            # 解禁要落回**出问题的那条记录**上：禁出库状态记在原记录里，
            # 只解复消记录的禁，`forbid_outbound(batch)` 仍会读到原来的 True，
            # 现场就是「消合格了但还出不了库」。
            origin = self._records.get(ticket.record_id)
            if origin is not None and origin is not record:
                origin.forbid_outbound = False
            self._emit("rework_closed", {
                "order_no": ticket.order_no,
                "cumulative_sec": record.cumulative_sec,
            }, staff_id=staff_id, record=record)
            log.info("复消关单：%s", ticket.describe())

    def start_rework(
        self, ticket_id: int, *, session_uid: str | None = None,
        staff_id: int | None = None, now: datetime | None = None,
    ) -> DisinfectionSession:
        """针对不达标批次重开一次消毒会话（复消）。"""
        ticket = self.ticket(ticket_id)
        if ticket.state != "open":
            raise DisinfectionError(f"复消工单 {ticket.order_no} 已关单，无需再消")
        origin = self.record(ticket.record_id)
        moment = now or self.now()
        session = self.start(
            batch_id=origin.batch_id,
            session_uid=session_uid or f"{origin.session_uid}-R",
            staff_id=staff_id,
            now=moment,
        )
        session.rework = True
        # 把复消会话绑到原工单上：否则复消达标时 `_on_pass` 找不到该关哪张单，
        # 现场表现是「消了两遍、单据还挂着、出库还是禁的」
        session.record.rework_order_id = ticket.id
        self._emit("rework_started", {"order_no": ticket.order_no},
                   staff_id=staff_id, record=session.record)
        return session

    # ------------------------------------------------------------ 门禁与视图
    def forbid_outbound(self, batch_id: int | None = None) -> bool:
        """该批（或当前任一未消好的批）是否禁止出库。"""
        for record in self.records:
            if record.forbid_outbound and (batch_id is None or record.batch_id == batch_id):
                return True
        return False

    def snapshot(self) -> dict[str, object]:
        running = self.running()
        return {
            "running": [row.session_uid for row in running],
            "cumulative_sec": [round(row.cumulative_sec, 1) for row in running],
            "records": [row.describe() for row in self.records],
            "open_tickets": [row.describe() for row in self.tickets if row.state == "open"],
            "ignored_samples": self.ignored,
            "forbid_outbound": self.forbid_outbound(),
        }

    # ------------------------------------------------------------ 内部
    def _emit(self, state: str, payload: dict, *, record: DisinfectionRecord,
              staff_id: int | None = None) -> ProcessEvent:
        event = ProcessEvent(
            id=self._next_event,
            kind="disinfection",
            ts=self.now(),
            payload={**payload, "batch_id": record.batch_id},
            order_item_id=None,
            station_id=None,
            staff_id=staff_id,
            session_uid=record.session_uid,
            state=state,
        )
        self._next_event += 1
        self._events.append(event)
        return event
