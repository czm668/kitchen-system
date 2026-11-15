# -*- coding: utf-8 -*-
"""#4 灶台占用与派单约束（A4-02）。

## 三件事必须分开：派单、占用、点火

| 概念 | 数据 | 含义 |
|---|---|---|
| 派单 | `order_items.station_id` + 状态 `dispatched` | 这张单**预约**了哪个灶 |
| 占用 | `station_occupancy`（`state='open'`） | 厨师已刷卡上灶，灶被人占着 |
| 点火 | 同一行的 `flame_verified` | 火焰传感器确认**真的开火**了 |

把「派单」和「占用」合成一步，就回答不了 #4 要抓的第一个问题：
**派了单但一直没人上灶**（厨师在配菜、或者忘了刷卡）。而「占用」和「点火」
合成一步，就抓不到「刷了卡占住灶、但根本没点火」——这种灶在系统里显示繁忙，
实际是空的，后面的单被白白排队。

## 派单规则（`dispatch_pending`）

1. 候选项：所有未结束订单里 `pending` 的子项；
2. 排序：**交期最近优先**（`Order.due_at`，催菜一次提前 3 分钟），同交期按
   订单号、子项号稳定排序——不稳定排序会让同一批单在两次扫描里换顺序，
   「为什么先做这单」就无法复现；
3. 选灶：按菜品需要的 `kind` 过滤空闲灶，取编号最小的；
4. 全忙：**不派**，留在队列里并给出排队位置。返回「排队中」而不是抛异常——
   营业高峰这是常态，不是错误。

## 火焰校验的时间窗

上灶后 `flame_grace_sec`（默认 30 秒）内必须看到火焰。窗口到期仍没看到，报
`FlameVerdict(ok=False)`。30 秒不是随手定的：主厨从刷卡到拧开灶阀通常 5~15 秒，
超过 30 秒还没火，多半是**刷卡占位但人在别处**，或者灶坏了。

## SCT-013 只看「排风在不在转」

`exhaust` 只做监测，**不参与派单决策**，也不做碳排核算（设计文档 §4 的口径）。
理由：排风停了是 #2 排烟积热的判据，由规则引擎走声光与联锁；派单层再拦一次，
只会让「排风故障时整条线派不出单」，那比积热更危险。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from kitchen.domain import CST, DomainError, InvalidTransition, NotFoundError

from .models import (
    ItemState,
    Order,
    OrderItem,
    STATION_BUSY,
    STATION_IDLE,
    STATION_RESERVED,
    StationOccupancy,
    WorkStation,
)
from .orders import OrderService
from .repository import MemoryProcessRepository

log = logging.getLogger(__name__)

__all__ = [
    "StationBusyError", "StaffBusyError", "QualificationMismatch", "StaffMatcher",
    "FlameVerdict", "ExhaustState",
    "DispatchDecision", "DispatchBoard",
    "FLAME_THRESHOLD", "EXHAUST_CURRENT_MIN",
]

#: 火焰有效的判定门限（与 `env_thresholds` id=22 的 `upper_bound 0.5` 同口径）
FLAME_THRESHOLD = 0.5

#: SCT-013 判定「排风在转」的电流门限（A）。风机铭牌 0.4~1.2A，
#: 待机/无电流读数在 0.02A 以下，0.15A 落在两者中间。
EXHAUST_CURRENT_MIN = 0.15


#: 两个「忙」都是 **409 冲突**，不是 404：「东西存在，但当前状态不允许」。
#: 归到 `NotFoundError` 会让前端把冲突提示成「工位不存在」——现场会去找
#: 根本不存在的配置问题。
class StationBusyError(DomainError):
    """工位已被占（有活动占用或已派单未上灶）。"""


class StaffBusyError(DomainError):
    """同一厨师不能同时占两个灶。"""


@runtime_checkable
class StaffMatcher(Protocol):
    """派单需要的**人员资质视图**（A4-09 联调点）。

    只定两个方法，不让 `process` 直接依赖 `staff` 域：
    `AdmissionController` 的 `candidates()` 返回的是 `Staff` 实体，派单只要
    人员 id。中间那层适配写在 `staff.admission.StaffMatcherAdapter` 里——
    跨域耦合留在被适配方，`process` 只认这个 Protocol，单测里塞个假实现即可。
    """

    def qualifies(self, staff_id: int, requirement: str) -> bool: ...

    def candidates(self, requirement: str) -> tuple[int, ...]: ...


class QualificationMismatch(DomainError):
    """刷卡人不具备该工序资质。

    错误里**必须带候选人**：现场看到「资质不符」而不知道该叫谁，只能干等或
    去翻排班表；带上「可改派 3、5」就能直接把单推给下一个人。
    """

    def __init__(self, staff_id: int, requirement: str,
                 candidates: tuple[int, ...] = ()) -> None:
        self.staff_id = int(staff_id)
        self.requirement = str(requirement)
        self.candidates = tuple(candidates)
        hint = (f"，可改派：{'、'.join(str(c) for c in self.candidates)}"
                if self.candidates else "，当前无合格人选（查排班/健康证/晨检）")
        super().__init__(f"人员 {staff_id} 无「{requirement}」资质{hint}")


@dataclass(frozen=True)
class FlameVerdict:
    """点火校验结论。"""

    station_id: int
    ok: bool
    verified: bool
    waited_sec: float
    detail: str

    def describe(self) -> str:
        mark = "点火确认" if self.verified else "未点火"
        return f"ST{self.station_id} {mark}（{self.waited_sec:.0f}s）：{self.detail}"


@dataclass(frozen=True)
class ExhaustState:
    """排风状态。`unknown` 与 `stopped` 必须分开。"""

    device_id: str
    current_a: float | None
    state: str                       # running / stopped / unknown
    detail: str

    @property
    def running(self) -> bool:
        return self.state == "running"

    def describe(self) -> str:
        icon = {"running": "在转", "stopped": "**停转**", "unknown": "无读数"}[self.state]
        reading = "—" if self.current_a is None else f"{self.current_a:.2f}A"
        return f"{self.device_id} 排风{icon}（{reading}）"


@dataclass(frozen=True)
class DispatchDecision:
    """一次派单尝试的结论。`ok=False` 表示本周期的排队，不是错误。"""

    item_id: int
    order_no: str
    dish: str
    ok: bool
    station_id: int | None = None
    reason: str = ""
    queue_position: int = 0
    #: 派给谁（A4-09）：未接人员池时为 None，只派工位不指派人
    staff_id: int | None = None
    #: 因资质不符被改派时，原定的人
    reassigned_from: int | None = None

    @property
    def reassigned(self) -> bool:
        return self.reassigned_from is not None

    def describe(self) -> str:
        if self.ok:
            who = f" → 厨师{self.staff_id}" if self.staff_id is not None else ""
            moved = (f"（{self.reassigned_from} 资质不符，已改派）"
                     if self.reassigned else "")
            return f"派单 {self.order_no}/{self.dish} → ST{self.station_id}{who}{moved}" \
                   f"（{self.reason}）"
        return f"排队 {self.order_no}/{self.dish}：{self.reason}（第 {self.queue_position} 位）"


class DispatchBoard:
    """工位编排：占用会话、点火校验、派单队列、排风监测。

    与 `OrderService` 分开的理由：派单要读「哪些灶空着」，而灶的忙闲是**运行时
    会话**，不是订单的一部分。两块合在一个类里，单测要造订单才能测「灶被占住」，
    成本全落在无关的地方。
    """

    def __init__(
        self,
        repo: MemoryProcessRepository,
        orders: OrderService,
        *,
        clock: Callable[[], datetime] | None = None,
        flame_threshold: float = FLAME_THRESHOLD,
        flame_grace_sec: float = 30.0,
        exhaust_current_min: float = EXHAUST_CURRENT_MIN,
        kitchen_device_id: str = "kitchen-node-01",
        matcher: "StaffMatcher | None" = None,
    ) -> None:
        self.repo = repo
        self.orders = orders
        self._clock = clock or (lambda: datetime.now(CST))
        self.flame_threshold = float(flame_threshold)
        self.flame_grace_sec = float(flame_grace_sec)
        self.exhaust_current_min = float(exhaust_current_min)
        self.kitchen_device_id = kitchen_device_id
        #: 人员资质池。None = 不接人员域（只派工位，不指派人）——装硬件前的演示
        #: 与「厨师自己找灶」的小店模式都是这种，不该强迫配置人员数据
        self.matcher = matcher

        self._occupancies: dict[int, StationOccupancy] = {}   # 会话 id → 会话
        self._open_by_station: dict[int, int] = {}            # 工位 → 会话 id
        self._reserved: dict[int, int] = {}                   # 工位 → 子项 id
        self._assigned: dict[int, int] = {}                    # 子项 id → 指派人员
        self._flame: dict[int, float] = {}                     # 工位 → 最近火焰值
        self._exhaust: dict[str, float] = {}                   # 设备 → 最近电流
        self._next_occ = 1

    # ------------------------------------------------------------ 时间
    def now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------ 工位视图
    def station_state(self, station_id: int) -> str:
        """工位的**实时**状态，以会话与预约为准。

        不直接信 `work_stations.state` 那一列：它是物化结果，重启后进程内的
        会话已经没了，但列里还写着 `busy`——表现就是「重启后所有灶都占用中」，
        只能人工去库里改。这里的口径是「会话是事实，列是缓存」。
        """
        if station_id in self._open_by_station:
            return STATION_BUSY
        if station_id in self._reserved:
            return STATION_RESERVED
        return STATION_IDLE

    def stations(self) -> tuple[WorkStation, ...]:
        return tuple(replace(station, state=self.station_state(station.id))
                     for station in self.repo.stations())

    def idle_stations(self, kind: str | None = None) -> tuple[WorkStation, ...]:
        return tuple(station for station in self.stations()
                     if self.station_state(station.id) == STATION_IDLE
                     and (kind is None or station.kind == kind))

    def occupancy_of(self, station_id: int) -> StationOccupancy | None:
        session_id = self._open_by_station.get(int(station_id))
        return self._occupancies.get(session_id) if session_id is not None else None

    def occupancies(self) -> tuple[StationOccupancy, ...]:
        return tuple(self._occupancies[key] for key in sorted(self._occupancies))

    # ------------------------------------------------------------ 占用会话
    def occupy(
        self,
        station_id: int,
        staff_id: int,
        *,
        item_id: int | None = None,
        device_id: str = "",
        now: datetime | None = None,
    ) -> StationOccupancy:
        """上灶刷卡 → 开一个占用会话。

        `item_id` 给了就顺带把子项推到 `cooking`（上灶即开工）。不给的场合是
        「先占灶、再挑单」，真实厨房里很常见（师傅先站定，服务员再报菜）。
        """
        moment = now or self.now()
        station = self.repo.station(station_id)
        # 资质在**刷卡这一刻**再校验一次：派单可能漏改（老单、手工建单），
        # 而刷卡是唯一的硬关口，错人在这里拦下比在出餐时发现便宜得多
        if self.matcher is not None:
            requirement = station.kind
            if item_id is not None:
                requirement = self._required_kind(self.orders.item(item_id)[1])
            if requirement and not self.matcher.qualifies(int(staff_id), requirement):
                raise QualificationMismatch(
                    int(staff_id), requirement, self.matcher.candidates(requirement))
        if self._open_by_station.get(station.id) is not None:
            holder = self.occupancy_of(station.id)
            raise StationBusyError(
                f"工位 {station.code} 已被 {holder.staff_id if holder else '?'} 占用"
            )
        occupied = [session for session in self._occupancies.values()
                    if session.open and session.staff_id == int(staff_id)]
        if occupied:
            # 一个人站在两个灶上是不可能的；真出现，说明上一单出餐漏了刷卡
            raise StaffBusyError(
                f"厨师 {staff_id} 已占用 ST{occupied[0].station_id}，"
                f"请先出餐释放（或补刷释放卡）"
            )
        session = StationOccupancy(
            id=self._next_occ,
            station_id=station.id,
            staff_id=int(staff_id),
            started_at=moment,
            order_item_id=item_id,
            device_id=device_id or station.device_id,
            state="open",
            flame_verified=False,
        )
        self._next_occ += 1
        self._occupancies[session.id] = session
        self._open_by_station[station.id] = session.id
        if item_id is not None:
            self._reserved.pop(station.id, None)
            self._assigned.pop(item_id, None)
            self.orders.start_cooking(item_id, actor=str(staff_id), channel="tft", now=moment,
                                      detail={"action": "occupy", "station_id": station.id})
        log.info("上灶：%s", session.describe())
        return session

    def close(
        self,
        station_id: int,
        *,
        reason: str = "出餐释放",
        now: datetime | None = None,
    ) -> StationOccupancy:
        """出餐确认 → 关会话、释放工位。没有活动会话时报错（不静默通过）。"""
        moment = now or self.now()
        session_id = self._open_by_station.pop(int(station_id), None)
        if session_id is None:
            raise NotFoundError(f"工位 {station_id} 没有活动占用，无需释放")
        session = self._occupancies[session_id].close(moment)
        self._occupancies[session_id] = session
        self._flame.pop(int(station_id), None)
        log.info("出餐释放：ST%s（%s，占用 %.0fs）", station_id, reason,
                 session.elapsed_sec(moment))
        return session

    # ------------------------------------------------------------ 点火校验
    def observe_flame(self, station_id: int, value: float) -> None:
        """喂一帧火焰读数。只记原始值，判定放到 `flame_check()`。"""
        self._flame[int(station_id)] = float(value)

    def flame_check(self, station_id: int, *, now: datetime | None = None) -> FlameVerdict:
        """点火校验结论。

        三种情形要分开：

        - 已看到火焰 → `ok=True, verified=True`；
        - 还没到窗口期 → `ok=True, verified=False`（**不要**在这时报未点火，
          否则每次上灶都会先弹一次假告警）；
        - 过窗口期仍无火焰 → `ok=False`。
        """
        moment = now or self.now()
        session = self.occupancy_of(station_id)
        if session is None:
            return FlameVerdict(int(station_id), True, False, 0.0, "无活动占用")
        waited = session.elapsed_sec(moment)
        value = self._flame.get(int(station_id))
        if session.flame_verified or (value is not None and value >= self.flame_threshold):
            if not session.flame_verified:
                self._occupancies[session.id] = replace(session, flame_verified=True)
            return FlameVerdict(int(station_id), True, True, waited,
                                f"火焰 {value:.2f} ≥ {self.flame_threshold}")
        if waited < self.flame_grace_sec:
            return FlameVerdict(int(station_id), True, False, waited,
                                f"等待点火（剩余 {self.flame_grace_sec - waited:.0f}s）")
        reading = "无读数" if value is None else f"{value:.2f} < {self.flame_threshold}"
        return FlameVerdict(
            int(station_id), False, False, waited,
            f"上灶 {waited:.0f}s 仍未检测到火焰（{reading}）——刷卡占位但没开火？",
        )

    def pending_flame_checks(self, *, now: datetime | None = None) -> tuple[FlameVerdict, ...]:
        """到期仍没点火的所有工位（前端轮询这个出提示）。"""
        moment = now or self.now()
        out = []
        for station_id in list(self._open_by_station):
            verdict = self.flame_check(station_id, now=moment)
            if not verdict.ok:
                out.append(verdict)
        return tuple(out)

    # ------------------------------------------------------------ 排风监测
    def observe_exhaust(self, current_a: float, *, device_id: str | None = None) -> None:
        """喂一帧 SCT-013 读数（风机回路电流）。"""
        self._exhaust[device_id or self.kitchen_device_id] = float(current_a)

    def exhaust_state(self, device_id: str | None = None) -> ExhaustState:
        device = device_id or self.kitchen_device_id
        reading = self._exhaust.get(device)
        if reading is None:
            # 没读数就说「不知道」。默认「停转」会让排风正常的今天也弹一条告警，
            # 几天之后没人再看这条提示——护栏被噪音淹死就等于没有。
            return ExhaustState(device, None, "unknown", "尚未收到 sct_current 读数")
        if reading >= self.exhaust_current_min:
            return ExhaustState(device, reading, "running",
                                f"{reading:.2f}A ≥ {self.exhaust_current_min}A")
        return ExhaustState(device, reading, "stopped",
                            f"{reading:.2f}A < {self.exhaust_current_min}A，排风可能未开启")

    # ------------------------------------------------------------ 派单
    def queue(self) -> tuple[OrderItem, ...]:
        """待派单子项，按「交期最近优先」排好序。"""
        rows: list[tuple[tuple, Order, OrderItem]] = []
        for order in self.orders.open_orders():
            for item in order.items:
                if item.state is not ItemState.PENDING:
                    continue
                key = (order.due_at, order.id, item.id)
                rows.append((key, order, item))
        rows.sort(key=lambda row: row[0])
        return tuple(item for _key, _order, item in rows)

    def _required_kind(self, item: OrderItem) -> str:
        return self.repo.dish(item.dish_id).station_kind

    def _pick_station(self, item: OrderItem) -> WorkStation | None:
        return next(iter(self.idle_stations(self._required_kind(item))), None)

    # ------------------------------------------------------------ 人员匹配（A4-09）
    def busy_staff(self) -> frozenset[int]:
        """当前占着灶的人。一个人两灶已在 `occupy` 拦掉，这里用于派单避让。"""
        return frozenset(session.staff_id for session in self._occupancies.values()
                         if session.open)

    def assigned_staff(self) -> dict[int, int]:
        """已派出但还没上灶的预约：子项 id → 人员 id。"""
        return {item_id: staff for item_id, staff in self._assigned.items()}

    # ------------------------------------------------------------ 离岗转派（A5-05）
    def station_staff(self, station_id: int) -> int | None:
        """该工位当前的上灶人。没有活动会话返回 None。"""
        session = self.occupancy_of(int(station_id))
        return session.staff_id if session is not None and session.open else None

    def reserved_staff(self, station_id: int) -> int | None:
        """该工位上「已派单但还没上灶」那一项的指派人。

        离岗判定要用它，因为它跟 `station_staff()` 回答的是**两个时刻**：
        前者是「灶上有活儿挂着、人还没到」，后者是「人已经在灶上」。
        只看后者的话，人派了单却没走到灶前就跑了，系统认为「这个灶没人占用」，
        于是既不报离岗也不转派——单子就挂在空气里。
        """
        item_id = self._reserved.get(int(station_id))
        return None if item_id is None else self._assigned.get(item_id)

    def assignments_of(self, staff_id: int) -> tuple[int, ...]:
        """该人名下已派出、还没上灶的子项（离岗时要转走的就是这些）。"""
        return tuple(item_id for item_id, staff in self._assigned.items()
                     if int(staff) == int(staff_id))

    def takeover(
        self,
        item_id: int,
        new_staff: int,
        *,
        reason: str = "",
    ) -> dict[str, object]:
        """换人：#11 离岗转派的落点。**只换预约，不动工位与订单状态。**

        三条拒绝，理由都不是「保守起见」：

        1. `COOKING` 的子项**不能换人**——炒到一半换人等于重做，而且锅里
           的东西没人认账。离岗时正在烧的菜只能由组长接手看火，这条要报到
           界面上，不能悄悄跳过；
        2. 新人资质不符 → `QualificationMismatch`（与派单、刷卡同一套判据）；
        3. 新人正忙 → `StaffBusyError`。一个人两个灶是不可能的。
        """
        _order, item = self.orders.item(int(item_id))
        if item.state is ItemState.COOKING:
            raise InvalidTransition(
                f"{item.dish_name} 已开火，不能中途换人（等于重做）；"
                f"离岗时该菜需组长接手看火"
            )
        if item.state not in (ItemState.PENDING, ItemState.DISPATCHED):
            raise InvalidTransition(
                f"{item.dish_name} 当前状态 {item.state.value}，不接受转派"
            )
        if self.matcher is not None:
            requirement = self._required_kind(item)
            if requirement and not self.matcher.qualifies(int(new_staff), requirement):
                raise QualificationMismatch(
                    int(new_staff), requirement, self.matcher.candidates(requirement))
        if int(new_staff) in self.busy_staff():
            raise StaffBusyError(f"{new_staff} 已在灶上，不能接手 {item.dish_name}")

        old = self._assigned.get(int(item_id))
        self._assigned[int(item_id)] = int(new_staff)
        record = {
            "item_id": int(item_id),
            "dish": item.dish_name,
            "from": old,
            "to": int(new_staff),
            "reason": reason,
        }
        log.info("转派：%s → %s（%s）", old, new_staff, reason)
        return record

    def release_assignment(self, item_id: int) -> int | None:
        """撤销预约。**无人可接时用它**，不要留着指向离岗者的预约：

        留着的话，那张单会一直显示「已派给张三」，而张三已经不在工位——
        下次派单还会因为「该工位被预约」而跳过它，单子就卡死了。
        """
        return self._assigned.pop(int(item_id), None)

    def assign_staff(
        self,
        item_id: int,
        station_id: int,
        *,
        preferred: int | None = None,
    ) -> tuple[int | None, int | None, str]:
        """给一个子项选人。返回 `(人员 id, 被改派掉的原定人员, 说明)`。

        规则（与 #10 的候选集同源，不在派单里另立一套）：

        1. 没接人员池 → `(None, None, "")`，只派工位；
        2. 指定了 `preferred` 且资质匹配、当前空闲 → 就用他；
        3. 否则从候选里挑第一个**空闲**的人，并把 `preferred` 记为改派来源；
        4. 一个都没有 → `(None, preferred, 原因)`，调用方据此排队（不派人也不占灶）。

        第 3 条是本模块存在的理由：派了单却派给一个没资质的人，他刷卡时会被
        `AdmissionController` 挡在门外，而单子已经挂在灶上了——只能人工改派，
        甘特图与 TFT 队列全都对不上。
        """
        if self.matcher is None:
            return None, None, ""
        item = self.orders.item(int(item_id))[1]
        requirement = self._required_kind(item)
        busy = self.busy_staff()
        taken = set(self._assigned.values())

        # 候选集是「四查都过」的人（资质 + 健康证 + 晨检 + 排班），与刷卡准入同源。
        # **不能只用 `qualifies`**：李四资质写着 stove，但健康证过期——只看资质会
        # 把单派给他，他刷卡被拒，单子已经挂在灶上了。
        free = [int(candidate) for candidate in self.matcher.candidates(requirement)
                if int(candidate) not in busy and int(candidate) not in taken]

        if preferred is not None:
            pid = int(preferred)
            if pid in free:
                return pid, None, ""
            if self.matcher.qualifies(pid, requirement):
                note = f"{pid} 资质在册但证件/晨检/排班不符，已改派"
            else:
                note = f"{pid} 无「{requirement}」资质，已改派"
        else:
            pid = None
            note = ""

        if free:
            return free[0], (pid if note else None), note
        return None, pid, (
            f"无「{requirement}」的空闲合格人员（资质/健康证/晨检/排班四查不过）"
        )

    def dispatch_pending(self, *, limit: int | None = None,
                         now: datetime | None = None,
                         staff_plan: dict[int, int] | None = None) -> list[DispatchDecision]:
        """按交期顺序给待派单子项分配空闲工位（接了人员池时同时指派人）。

        `staff_plan`：子项 id → 原定人员（例如小程序指定「张师傅做」）。资质不符
        或本人正忙时**自动改派**给候选集里的人，并在 `DispatchDecision` 里记下
        改派来源——不静默换人，否则师傅会发现「单子莫名变成别人的」。
        """
        moment = now or self.now()
        decisions: list[DispatchDecision] = []
        queue = list(self.queue())
        for index, item in enumerate(queue):
            order = self.orders.order(item.order_id)
            if limit is not None and len([d for d in decisions if d.ok]) >= limit:
                decisions.append(DispatchDecision(
                    item.id, order.order_no, item.dish_name, False,
                    reason="本轮派单已达上限", queue_position=index + 1,
                ))
                continue
            station = self._pick_station(item)
            if station is None:
                busy = [row.code for row in self.stations()
                        if self.station_state(row.id) != STATION_IDLE]
                decisions.append(DispatchDecision(
                    item.id, order.order_no, item.dish_name, False,
                    reason=f"全部工位占用（{('、'.join(busy)) or '无可用工位'}）",
                    queue_position=index + 1,
                ))
                continue
            preferred = None if staff_plan is None else staff_plan.get(item.id)
            staff_id, reassigned, staff_note = self.assign_staff(
                item.id, station.id, preferred=preferred)
            if self.matcher is not None and staff_id is None:
                # 没合格的人就不派：单子挂在灶上而没人能上，只会堵住整条线
                decisions.append(DispatchDecision(
                    item.id, order.order_no, item.dish_name, False,
                    station_id=station.id, reason=staff_note,
                    queue_position=index + 1, reassigned_from=reassigned,
                ))
                continue
            wait_min = (order.due_at - moment).total_seconds() / 60.0
            detail = {"action": "dispatch", "wait_min": round(wait_min, 1)}
            if staff_id is not None:
                detail["staff_id"] = staff_id
            if reassigned is not None:
                detail["reassigned_from"] = reassigned
            self.orders.dispatch_item(
                item.id, station.id, actor="dispatcher", channel="server", now=moment,
                detail=detail,
            )
            self._reserved[station.id] = item.id
            if staff_id is not None:
                self._assigned[item.id] = int(staff_id)
            reason = (f"交期 {order.due_at.strftime('%H:%M')}"
                      f"（{'超时 %.0f' % -wait_min if wait_min < 0 else '剩余 %.0f' % wait_min} 分）")
            if staff_note:
                reason += f"；{staff_note}"
            decisions.append(DispatchDecision(
                item.id, order.order_no, item.dish_name, True, station.id,
                reason=reason, staff_id=staff_id, reassigned_from=reassigned,
            ))
        return decisions

    def dispatch_summary(self, *, now: datetime | None = None) -> dict[str, object]:
        """给 Web / TFT 的一屏派单视图。"""
        return {
            "stations": [
                {
                    "code": station.code,
                    "state": self.station_state(station.id),
                    "flame": self._flame.get(station.id),
                    "staff": (self.occupancy_of(station.id).staff_id
                              if self.occupancy_of(station.id) else None),
                }
                for station in self.repo.stations()
            ],
            "exhaust": self.exhaust_state().describe(),
            "assigned": self.assigned_staff(),
            "queue": [item.describe() for item in self.queue()],
            "pending_flame": [verdict.describe()
                              for verdict in self.pending_flame_checks(now=now)],
        }

    def reset(self) -> None:
        """测试/现场检修：清空全部会话与预约。"""
        self._occupancies.clear()
        self._open_by_station.clear()
        self._reserved.clear()
        self._assigned.clear()
        self._flame.clear()
        self._exhaust.clear()
