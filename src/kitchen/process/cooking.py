# -*- coding: utf-8 -*-
"""#12 配菜称重防错 + #13 SOP 分步确认（A4-03 / A4-04）。

两道关卡在同一个 `CookingSession` 里，因为它们**共用同一份上下文**：这一单的配方、
这一单的步骤、这一单的师傅。拆成两个类就要互相持有对方的引用，或者让调用方
自己记「现在轮到哪一步」。

## #12 的判据与三处易错

目标克重与容差都来自 `dish_recipe_items`，不在代码里写死（改菜谱不该改代码）。

1. **容差取 `max(目标×百分比, min_abs_g)`**。生抽 8 g 的 5% 是 0.4 g，HX711
   单次读数抖动就有 ±0.5~1 g；只按百分比判，小料「怎么放都锁着」，现场最后
   一定是把容差改成 50% 了事——那等于取消防错。绝对下限 `min_abs_g=1.0` 让
   小料按「±1 g」判，与硬件噪声同量级。
2. **锁定只锁「下一步」**，不锁整单。已经称完的项保留结论，师傅补足该项即可
   解锁；全单锁死会逼人取消重下，留下一堆废单。
3. **重新称一次就覆盖上一次结论**。称错了再称是常态（手抖多倒了一点又舀回来），
   做成「累加/取平均」会让师傅永远修不回容差区间。

## #13 的判据

- 必须**按步骤号顺序**确认。SOP 的意义就在顺序：先下锅后切配，菜是废的。
- `ccp=1` 的关键控制点未确认 → **不允许出餐**（不是「提示一下」）。
- `requires_card=1` 的步骤必须带 `staff_id`：写进 `process_events` 的身份，
  是事后追责与日管控台账的唯一凭据，「谁确认的」不能靠界面上的登录态。

## 出餐门禁 `blockers()`

出餐前把所有未满足项**一次列全**，而不是遇到第一个就抛。逐条抛的结果是
「补一样、被拒一次」的循环，师傅在现场会直接打电话叫人来关掉这套系统。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kitchen.domain import CST, DomainError

from .models import (
    ItemState,
    Order,
    OrderItem,
    ProcessEvent,
    RecipeItem,
    SopStep,
)
from .orders import OrderService
from .repository import MemoryProcessRepository

log = logging.getLogger(__name__)

__all__ = [
    "WeighError", "StepLockedError", "NotReadyError",
    "WeighCheck", "StepConfirm", "CookingSession", "CookingBoard",
]

#: 称重的绝对容差下限（g）。见模块文档第 1 条——这不是「宽松」，是对齐硬件噪声。
MIN_ABS_TOLERANCE_G = 1.0


class WeighError(DomainError):
    """称重请求本身不合法（食材不在本单配方里、读数异常）。"""


class StepLockedError(DomainError):
    """配菜称重未通过，下一步被锁。"""


class NotReadyError(DomainError):
    """出餐门禁不通过，`blockers` 里是全部原因。"""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__("不能出餐：" + "；".join(blockers))


@dataclass(frozen=True)
class WeighCheck:
    """一次称重结论。"""

    ingredient: str
    target_g: float
    measured_g: float
    tolerance_pct: float
    low_g: float
    high_g: float
    ok: bool
    detail: str = ""

    @property
    def delta_g(self) -> float:
        return round(self.measured_g - self.target_g, 1)

    @property
    def delta_pct(self) -> float:
        return 0.0 if not self.target_g else round(
            (self.measured_g - self.target_g) / self.target_g * 100.0, 1)

    @property
    def state_text(self) -> str:
        """给 `process_events.state` 用：`ok` / `over` / `under`。"""
        if self.ok:
            return "ok"
        return "over" if self.measured_g > self.target_g else "under"

    def describe(self) -> str:
        mark = "合格" if self.ok else "超差锁定"
        return (f"{self.ingredient} {self.measured_g:.1f}g / 目标 {self.target_g:.0f}g"
                f"（{self.delta_g:+.1f}g，{self.delta_pct:+.1f}%，允许 "
                f"{self.low_g:.1f}~{self.high_g:.1f}g）→ {mark}")


@dataclass(frozen=True)
class StepConfirm:
    """一次步骤确认结论。"""

    step_no: int
    title: str
    ccp: bool
    staff_id: int | None
    ok: bool
    detail: str = ""

    def describe(self) -> str:
        mark = "已确认" if self.ok else "拒绝"
        ccp = "CCP " if self.ccp else ""
        who = f" by {self.staff_id}" if self.staff_id is not None else ""
        return f"步骤 {self.step_no} {ccp}{self.title} {mark}{who}"


class CookingSession:
    """一单菜的配菜核验 + SOP 推进 + 出餐门禁。"""

    def __init__(
        self,
        repo: MemoryProcessRepository,
        orders: OrderService,
        item_id: int,
        *,
        session_uid: str | None = None,
        clock: Callable[[], datetime] | None = None,
        min_abs_g: float = MIN_ABS_TOLERANCE_G,
        recorder: Any = None,
    ) -> None:
        self.repo = repo
        self.orders = orders
        self._clock = clock or (lambda: datetime.now(CST))
        self.min_abs_g = float(min_abs_g)
        #: 业务落库写器（A23）：每次 _emit 后把过程事件追加进 process_events。
        self.recorder = recorder

        order, item = orders.item(item_id)
        self.order: Order = order
        self.item: OrderItem = item
        if item.state in (ItemState.CANCELLED, ItemState.SERVED):
            raise WeighError(f"子项「{item.dish_name}」已 {item.state.value}，无需再做")
        self.session_uid = session_uid or f"S{item.id}-{self._clock():%m%d%H%M}"

        self.recipe_items: tuple[RecipeItem, ...] = repo.recipe_items(item.recipe_id)
        self.steps: tuple[SopStep, ...] = repo.sop_steps(item.recipe_id)
        self._weighs: dict[str, WeighCheck] = {}
        self._confirmed: list[int] = []
        self._events: list[ProcessEvent] = []
        self._next_event = 1

    # ------------------------------------------------------------ 状态
    def now(self) -> datetime:
        return self._clock()

    @property
    def events(self) -> tuple[ProcessEvent, ...]:
        return tuple(self._events)

    @property
    def locked(self) -> bool:
        """有任何一项称重不达标 → 下一步被锁。"""
        return any(not check.ok for check in self._weighs.values())

    def locked_checks(self) -> tuple[WeighCheck, ...]:
        return tuple(check for check in self._weighs.values() if not check.ok)

    @property
    def confirmed_steps(self) -> tuple[int, ...]:
        return tuple(self._confirmed)

    def next_step(self) -> SopStep | None:
        """下一个待确认步骤；全部确认完返回 None。"""
        for step in self.steps:
            if step.step_no not in self._confirmed:
                return step
        return None

    def weighed(self, ingredient: str) -> WeighCheck | None:
        return self._weighs.get(str(ingredient).strip())

    # ------------------------------------------------------------ #12 称重
    def weigh(
        self,
        ingredient: str,
        measured_g: float,
        *,
        staff_id: int | None = None,
    ) -> WeighCheck:
        """核验一味料的实际克重。**不通过时锁下一步**，但保留已有结论。"""
        target = self._find_recipe_item(ingredient)
        value = float(measured_g)
        if value < 0:
            raise WeighError(f"读数为负（{value}g）——HX711 未去皮或传感器未接好")
        low, high = target.bounds(min_abs_g=self.min_abs_g)
        ok = low <= value <= high
        if ok:
            detail = "在容差内"
        elif value > high:
            detail = f"多了 {value - high:.1f}g，请取出一部分后重新称"
        else:
            detail = f"少了 {low - value:.1f}g，请补足后重新称"
        check = WeighCheck(
            ingredient=target.ingredient,
            target_g=target.weight_g,
            measured_g=round(value, 1),
            tolerance_pct=target.tolerance_pct,
            low_g=round(low, 1),
            high_g=round(high, 1),
            ok=ok,
            detail=detail,
        )
        self._weighs[target.ingredient] = check
        self._emit("weigh", check.state_text, {
            "ingredient": check.ingredient,
            "target_g": check.target_g,
            "measured_g": check.measured_g,
            "delta_pct": check.delta_pct,
            "ok": check.ok,
        }, staff_id=staff_id)
        log.info("称重：%s", check.describe())
        return check

    def _find_recipe_item(self, ingredient: str) -> RecipeItem:
        key = str(ingredient).strip()
        for row in self.recipe_items:
            if row.ingredient == key:
                return row
        names = "、".join(row.ingredient for row in self.recipe_items) or "（本单无配方）"
        raise WeighError(f"「{key}」不在「{self.item.dish_name}」的配方里；应称：{names}")

    # ------------------------------------------------------------ #13 SOP
    def confirm(self, step_no: int, staff_id: int | None = None) -> StepConfirm:
        """确认一步。顺序、锁定、身份三道检查都在这里。"""
        step = next((row for row in self.steps if row.step_no == int(step_no)), None)
        if step is None:
            known = "、".join(str(row.step_no) for row in self.steps) or "（无）"
            raise WeighError(f"步骤 {step_no} 不在本单 SOP 里；有效步骤：{known}")
        if step.step_no in self._confirmed:
            # 重复确认是幂等的：TFT 双击、现场回刷都常见，报错会让人以为没生效
            return StepConfirm(step.step_no, step.title, step.ccp, staff_id, True,
                               "该步骤此前已确认")
        if self.locked:
            pending = "、".join(check.ingredient for check in self.locked_checks())
            raise StepLockedError(f"配菜称重未通过（{pending}），补足或更正后才能继续")

        pending_step = self.next_step()
        if pending_step is not None and pending_step.step_no != step.step_no:
            raise WeighError(
                f"下一步应确认步骤 {pending_step.step_no}「{pending_step.title}」，"
                f"不能跳到 {step.step_no}"
            )
        if step.requires_card and staff_id is None:
            raise WeighError(f"步骤 {step.step_no}「{step.title}」必须刷卡确认（需身份留痕）")

        self._confirmed.append(step.step_no)
        result = StepConfirm(step.step_no, step.title, step.ccp, staff_id, True,
                             step.instruction)
        self._emit("sop_step", "confirmed", {
            "step_no": step.step_no,
            "title": step.title,
            "ccp": step.ccp,
            "instruction": step.instruction,
        }, staff_id=staff_id)
        log.info("SOP：%s", result.describe())
        return result

    # ------------------------------------------------------------ 出餐门禁
    def blockers(self) -> tuple[str, ...]:
        """出餐前必须满足的条件，**一次列全**。"""
        reasons: list[str] = []
        missing = [row.ingredient for row in self.recipe_items
                   if row.ingredient not in self._weighs]
        if missing:
            reasons.append("配菜未核验：" + "、".join(missing))
        bad = [check for check in self.locked_checks()]
        if bad:
            reasons.append("配菜超差：" + "、".join(
                f"{check.ingredient}{check.delta_g:+.1f}g" for check in bad))
        open_ccp = [row for row in self.steps
                    if row.ccp and row.step_no not in self._confirmed]
        if open_ccp:
            reasons.append("关键控制点未确认：" + "、".join(
                f"#{row.step_no} {row.title}" for row in open_ccp))
        return tuple(reasons)

    @property
    def ready(self) -> bool:
        return not self.blockers()

    def serve(self, *, staff_id: int | None = None, channel: str = "tft") -> Order:
        """出餐：门禁过了才推状态到 `ready`。

        只推到 `ready` 不直接 `served`，是分工：出餐是厨房的动作，上桌是传菜的
        动作。合成一步，出餐时长统计里就混进了传菜时间，而 #13 的复核要看的
        恰恰是「菜什么时候离火」。
        """
        blockers = self.blockers()
        if blockers:
            raise NotReadyError(blockers)
        order = self.orders.mark_ready(
            self.item.id, actor=str(staff_id or ""), channel=channel, now=self.now(),
            detail={"action": "serve", "session_uid": self.session_uid},
        )
        self._emit("sop_step", "ready", {"blockers": []}, staff_id=staff_id)
        log.info("出餐：%s（%s）", self.item.dish_name, self.order.order_no)
        return order

    # ------------------------------------------------------------ 内部
    def _emit(self, kind: str, state: str, payload: dict, *,
              staff_id: int | None = None) -> ProcessEvent:
        event = ProcessEvent(
            id=self._next_event,
            kind=kind,
            ts=self.now(),
            payload=payload,
            order_item_id=self.item.id,
            station_id=self.item.station_id,
            staff_id=staff_id,
            session_uid=self.session_uid,
            state=state,
        )
        self._next_event += 1
        self._events.append(event)
        if self.recorder is not None:
            try:
                self.recorder.append_process_event(event)
            except Exception:  # pragma: no cover - 写器内已吞错，双保险
                log.warning("工序事件落库异常（%s #%s）", event.kind, event.id,
                            exc_info=True)
        return event

    def summary(self) -> dict[str, object]:
        """给 TFT 的一屏视图。"""
        return {
            "dish": self.item.dish_name,
            "order_no": self.order.order_no,
            "session": self.session_uid,
            "weighs": [check.describe() for check in self._weighs.values()],
            "locked": self.locked,
            "next_step": (self.next_step().describe() if self.next_step() else "全部完成"),
            "confirmed": self.confirmed_steps,
            "blockers": list(self.blockers()),
        }


class CookingBoard:
    """`CookingSession` 的宿主：把「一单菜的工序进度」挂到运行时上。

    ## 为什么需要这一层

    `CookingSession` 自己只持有内存态（称重结论、已确认步骤），**没有人保管它，
    进度就活不过一次请求**——TFT 屏上点「确认步骤 3」，下一次刷新又回到步骤 1，
    现场的结论一定是「这系统坏了」。`AppState` 持有唯一实例，按 `item_id` 存取。

    ## 会话什么时候结束

    出餐（`serve`）或子项取消时**删掉**会话。留着不仅占内存，还会让「已出餐的
    单还能再确认步骤」这种幻影操作混进 `process_events`（表里出现同一 item 的
    两段 session_uid，追责时说不清哪段是真的）。

    ## 出餐后不清 `process_events`

    事件由 `CookingSession._emit` 产生：内存里随会话消亡，但**落库后跨重启可查**
    （A23：写器存在时同步追加进 `process_events` 表）——日管控台账要按时间线
    汇总全部关键操作，内存态只够当次请求用。
    """

    def __init__(
        self,
        repo: MemoryProcessRepository,
        orders: OrderService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.orders = orders
        self._clock = clock
        #: 业务落库写器（A23），由 `api/state.py` 装配后注入并传给会话
        self.recorder: Any = None
        self._sessions: dict[int, CookingSession] = {}

    # ------------------------------------------------------------ 会话
    def session(self, item_id: int) -> CookingSession | None:
        """已有会话或 None。**不自动创建**：创建走 `ensure()`，避免「查一下
        状态」顺手开了个空会话，把 blockers 的「配菜未核验」全列表带出来。"""
        return self._sessions.get(int(item_id))

    def ensure(self, item_id: int, *, session_uid: str | None = None) -> CookingSession:
        """取会话；没有就开。已有会话**不会**被 session_uid 覆盖（重开等于
        把进度清零，必须显式 `reset()`）。"""
        key = int(item_id)
        found = self._sessions.get(key)
        if found is not None:
            return found
        made = CookingSession(
            self.repo, self.orders, key,
            session_uid=session_uid, clock=self._clock, recorder=self.recorder,
        )
        self._sessions[key] = made
        return made

    def reset(self, item_id: int) -> CookingSession:
        """显式重开（进度清零）。称重全错要重来时用。"""
        key = int(item_id)
        self._sessions.pop(key, None)
        made = CookingSession(
            self.repo, self.orders, key, clock=self._clock, recorder=self.recorder,
        )
        self._sessions[key] = made
        return made

    def close(self, item_id: int) -> None:
        """出餐/取消后删会话（见类注释「会话什么时候结束」）。"""
        self._sessions.pop(int(item_id), None)

    # ------------------------------------------------------------ 操作
    def weigh(self, item_id: int, ingredient: str, measured_g: float,
              *, staff_id: int | None = None) -> WeighCheck:
        return self.ensure(item_id).weigh(
            ingredient, measured_g, staff_id=staff_id,
        )

    def confirm(self, item_id: int, step_no: int,
                *, staff_id: int | None = None) -> StepConfirm:
        return self.ensure(item_id).confirm(step_no, staff_id=staff_id)

    def serve(self, item_id: int, *, staff_id: int | None = None,
              channel: str = "tft") -> Order:
        session = self.ensure(item_id)
        order = session.serve(staff_id=staff_id, channel=channel)
        self.close(item_id)
        return order

    def blockers(self, item_id: int) -> tuple[str, ...]:
        session = self.session(item_id)
        if session is None:
            # 没开会话也算一种「没准备好」：配料一克没称、一步没确认
            session = self.ensure(item_id)
        blockers = session.blockers()
        if not self._sessions.get(int(item_id)):
            self.close(item_id)
        return blockers

    # ------------------------------------------------------------ 视图
    def detail(self, item_id: int) -> dict[str, Any]:
        """TFT 工序分步屏的结构化数据（比 `CookingSession.summary` 细）。

        没开会话时也返回完整骨架（steps 全 pending、weighs 全空）——
        TFT 屏先打开、师傅后开称，不能因为「还没开会话」屏就空白。
        """
        key = int(item_id)
        transient: CookingSession | None = None
        session = self._sessions.get(key)
        if session is None:
            transient = CookingSession(
                self.repo, self.orders, key, clock=self._clock,
                recorder=self.recorder,
            )
            session = transient
        order, item = self.orders.item(key)
        confirmed = set(session.confirmed_steps)
        next_step = session.next_step()
        rows = []
        for step in session.steps:
            rows.append({
                "step_no": step.step_no,
                "title": step.title,
                "instruction": step.instruction,
                "heat_level": step.heat_level,
                "duration_sec": step.duration_sec,
                "ccp": step.ccp,
                "requires_card": step.requires_card,
                "state": ("confirmed" if step.step_no in confirmed
                          else "current" if next_step is not None
                          and step.step_no == next_step.step_no
                          else "pending"),
            })
        weighs = []
        for recipe_item in session.recipe_items:
            check = session.weighed(recipe_item.ingredient)
            weighs.append({
                "ingredient": recipe_item.ingredient,
                "target_g": recipe_item.weight_g,
                "tolerance_pct": recipe_item.tolerance_pct,
                "measured_g": None if check is None else check.measured_g,
                "ok": None if check is None else check.ok,
                "detail": "" if check is None else check.describe(),
            })
        return {
            "item_id": key,
            "order_id": order.id,
            "order_no": order.order_no,
            "dish": item.dish_name,
            "item_state": item.state.value,
            "station_id": item.station_id,
            "session": session.session_uid,
            "active": transient is None,
            "locked": session.locked,
            "locked_ingredients": [check.ingredient for check in session.locked_checks()],
            "steps": rows,
            "confirmed_steps": sorted(confirmed),
            "next_step_no": None if next_step is None else next_step.step_no,
            "weighs": weighs,
            "blockers": list(session.blockers()),
            "ready": session.ready,
        }

    def open_items(self) -> tuple[int, ...]:
        """有活动会话的子项（按 id 升序，甘特/队列屏标「做菜中」。"""
        return tuple(sorted(self._sessions))
