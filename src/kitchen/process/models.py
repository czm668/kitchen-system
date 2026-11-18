# -*- coding: utf-8 -*-
"""过程执行域模型（P4 / A4）：#4 工位占用、#12 称重、#13 SOP、#14 留样、#15 消毒。

对应 `migrations/014_kitchen_core.sql` 的这批表：`dishes` / `work_stations` /
`orders` / `order_items` / `order_events` / `dish_recipes` / `dish_recipe_items` /
`sop_steps` / `dish_batches` / `process_events` / `station_occupancy` /
`retention_samples` / `disinfection_records`。

## 一句话口径：`orders.state` 是算出来的，不是改出来的

订单状态不靠人手动「改成 cooking」，而是**每次子项状态变化后按子项重算**
（`rollup_order`）。理由：手改状态必然会漏改——小程序退了一道菜、TFT 又确认了
另一道，两个入口各改各的，最终 `orders.state` 和 `order_items` 自相矛盾，而
报表与甘特都读前者。算出来的状态永远和明细一致，`replay()` 能把它从
`order_events` 完整复原（这条是可测的，不是设计意图）。

## 三个状态的边界

| 状态 | 含义 | 谁推动 |
|---|---|---|
| `pending` | 已下单，未派工位 | `dispatch` |
| `dispatched` | 已占住工位，等人上灶刷卡 | 厨师刷卡 |
| `cooking` | 已点火开做 | 出餐确认 |
| `ready` | 已装盘，等传菜 | 传菜确认 |
| `served` | 已上桌 | — |

`dispatched` 与 `cooking` 必须分开：前者是**预约**（工位已被这张单占住，别人
拿不到），后者是**实际开火**。合在一起，就无法回答「派了单但一直没人上灶」——
而这正是 #4 要抓的第一个问题。

## 时间

全流程用**带时区**的 `datetime`（CST）。库里是墙上时间，读进来立刻补偏移量，
否则「aware 减 naive」会在某个分支上抛 `TypeError`。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from kitchen.domain import CST, InvalidTransition

__all__ = [
    "OrderState", "ItemState", "ITEM_TRANSITIONS", "can_item_move", "rollup_order",
    "Dish", "RecipeItem", "Recipe", "SopStep", "WorkStation",
    "OrderItem", "Order", "OrderEvent", "StationOccupancy", "ProcessEvent",
    "STATION_IDLE", "STATION_RESERVED", "STATION_BUSY",
    "READY_TEMP_C", "estimate_minutes", "PRIORITY_URGE_MIN",
]

#: 催菜一次把交期提前多久。用「提前交期」而不是「加优先级字段」，
#: 是为了让派单只有一条排序规则（按交期），不用为催菜写第二个分支。
PRIORITY_URGE_MIN = 3

#: 出锅前的中心温度要求（#13 第 6 步「确认中心温度 ≥75℃ 后出锅」）
READY_TEMP_C = 75.0


class OrderState(str, Enum):
    """订单级状态（`orders.state`）。"""

    CREATED = "created"
    QUEUED = "queued"
    COOKING = "cooking"
    READY = "ready"
    SERVED = "served"
    CANCELLED = "cancelled"


class ItemState(str, Enum):
    """子项状态（`order_items.state`）。"""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    COOKING = "cooking"
    READY = "ready"
    SERVED = "served"
    CANCELLED = "cancelled"


#: 子项允许的迁移。只列正向与退菜，不做「任意改」——
#: `ready → cooking` 这种回退看似方便（出餐发现没熟），实际会让甘特图上同一道菜
#: 的时间倒流，统计出餐时长得到负数。真出问题走「退菜重下」留两条记录。
ITEM_TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.PENDING: frozenset({ItemState.DISPATCHED, ItemState.CANCELLED}),
    ItemState.DISPATCHED: frozenset({ItemState.COOKING, ItemState.PENDING, ItemState.CANCELLED}),
    ItemState.COOKING: frozenset({ItemState.READY, ItemState.CANCELLED}),
    ItemState.READY: frozenset({ItemState.SERVED}),
    ItemState.SERVED: frozenset(),
    ItemState.CANCELLED: frozenset(),
}


def can_item_move(current: ItemState, target: ItemState) -> bool:
    return target in ITEM_TRANSITIONS.get(current, frozenset())


def rollup_order(states: list[ItemState] | tuple[ItemState, ...]) -> OrderState:
    """子项状态集合 → 订单状态。

    判定顺序即优先级，写成一条阶梯而不是嵌套 if：

    1. 全退 → `cancelled`（哪怕之前已经出过菜，全退就是全退）
    2. 全上桌 → `served`
    3. 剩下的都是 `ready/served` → `ready`（还有菜没端走，不能算服务完成）
    4. 任何一项动过火（`cooking/ready/served`）→ `cooking`
    5. 任何一项占住工位 → `queued`
    6. 其余 → `created`

    第 3 条是最容易写错的一处：只要有 `ready` 就报「已就绪」，
    那么五道菜出一道时报 ready、其余四道还没派单，页面显示「已完成」。
    """
    items = list(states)
    if not items:
        return OrderState.CREATED
    if all(state is ItemState.CANCELLED for state in items):
        return OrderState.CANCELLED
    if all(state is ItemState.SERVED for state in items):
        return OrderState.SERVED
    if all(state in (ItemState.READY, ItemState.SERVED) for state in items):
        return OrderState.READY
    if any(state in (ItemState.COOKING, ItemState.READY, ItemState.SERVED) for state in items):
        return OrderState.COOKING
    if any(state is ItemState.DISPATCHED for state in items):
        return OrderState.QUEUED
    return OrderState.CREATED


#: 工位状态。`reserved` = 已派单但厨师还没刷卡上灶。
STATION_IDLE = "idle"
STATION_RESERVED = "reserved"
STATION_BUSY = "busy"


# ---------------------------------------------------------------- 静态资料
@dataclass(frozen=True)
class Dish:
    id: int
    code: str
    name: str
    category: str = ""
    price: float = 0.0
    sold_out: bool = False
    enabled: bool = True
    #: 人工按烹饪难度标定的参考制作时长（分钟）。>0 时作为权威值优先于 SOP，
    #: 让菜单/购物车展示和下单交期按难度区分（凉菜几分钟、炖菜几十分钟）。
    #: 0 表示未标定，回退到 SOP 步骤时长、再回退到保守默认值。
    prep_minutes: int = 0

    @property
    def station_kind(self) -> str:
        """菜品类别 → 需要的工位类型。

        不单独建一张「菜品-工位」映射表：菜系只有四种，而工位 `kind` 已经在
        `work_stations` 里了。真要按菜品细分时再加表，别提前建。
        """
        return {"凉菜": "cold_dish", "冷菜": "cold_dish", "素菜": "stove",
                "热菜": "stove", "主食": "stove"}.get(self.category, "stove")


@dataclass(frozen=True)
class IngredientBatch:
    """原料批次（`ingredient_batches`）：溯源真正要回答的「这批肉/菜哪来的」。

    与 `dish_batches`（成品出餐批次，供留样/出库门禁）分开：菜品按配方用到
    若干原料，每个原料按 `ingredient_name` 落到自己的到货批次与检疫报告。
    `ingredient_name` 刻意与 `RecipeItem.ingredient` 同名——配方清单就是骨架。
    """

    id: int
    batch_no: str
    ingredient_name: str
    supplier_id: int
    received_at: datetime
    expire_at: datetime
    inspection: dict[str, object]
    state: str = "received"

    @property
    def expired(self) -> bool:
        return self.expire_at < datetime.now(CST)


@dataclass(frozen=True)
class RecipeItem:
    """#12 称重的判定基准（`dish_recipe_items`）。"""

    id: int
    recipe_id: int
    ingredient: str
    weight_g: float
    tolerance_pct: float = 5.0
    sort_no: int = 0

    def bounds(self, *, min_abs_g: float = 1.0) -> tuple[float, float]:
        """允许区间 `(下界, 上界)`。

        **绝对下限的必要性**：生抽只有 8 g，5% = 0.4 g，而 HX711 单次读数抖动
        就有 ±0.5~1 g（10 kg 量程、普通片式传感器）。只按百分比判，这味料
        「怎么放都锁着」，现场最后一定是把容差改成 50% 了事——那等于没有防错。
        所以容差取 `max(目标×百分比, min_abs_g)`，小料按绝对误差判。
        """
        tolerance = max(abs(self.weight_g) * self.tolerance_pct / 100.0, min_abs_g)
        return (self.weight_g - tolerance, self.weight_g + tolerance)


@dataclass(frozen=True)
class Recipe:
    id: int
    dish_id: int
    version: int = 1
    name: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class SopStep:
    """#13 的工序步骤（`sop_steps`）。"""

    id: int
    recipe_id: int
    step_no: int
    title: str
    instruction: str = ""
    heat_level: str = ""
    duration_sec: int = 0
    ccp: bool = False
    requires_card: bool = False

    def describe(self) -> str:
        mark = "CCP" if self.ccp else "   "
        card = " 需刷卡" if self.requires_card else ""
        return f"[{self.step_no}] {mark} {self.title}（{self.heat_level or '—'}，" \
               f"{self.duration_sec}s）{card}"


@dataclass(frozen=True)
class WorkStation:
    """工位（`work_stations`）。A/B 两个虚拟工位共用一个采集节点。"""

    id: int
    code: str
    name: str = ""
    kind: str = "stove"
    device_id: str = ""
    state: str = STATION_IDLE
    capacity: int = 1


def estimate_minutes(steps: list[SopStep], *, overhead_min: int = 2, floor_min: int = 5) -> int:
    """SOP 步骤时长 → 预计出餐分钟数（`orders.promised_at` 的来源）。

    用 SOP 时长而不是写死「热菜 15 分钟」：预计交期与实际工艺脱节时，
    「按交期最近派单」这条约束就退化成随机派单。步骤总时长之外加 2 分钟装盘
    传菜，并不追求准确——要的是**同一批单之间的相对顺序**正确。
    """
    total = sum(max(0, int(step.duration_sec)) for step in steps) / 60.0
    return max(floor_min, int(round(total)) + overhead_min)


# ---------------------------------------------------------------- 运行时实体
@dataclass
class OrderItem:
    id: int
    order_id: int
    dish_id: int
    dish_name: str = ""
    qty: int = 1
    state: ItemState = ItemState.PENDING
    urge_count: int = 0
    station_id: int | None = None
    note: str = ""
    #: 该单适用的配方（快照）。菜品换配方版本时，在做的单不受影响。
    recipe_id: int | None = None
    #: 下单瞬间的单价快照。客诉补偿金额必须按它算，不能按当前 dishes.price——
    #: 菜品改价后按现价退款会算错钱，且事后无法复现当时该退多少。
    unit_price: float = 0.0

    @property
    def amount(self) -> float:
        """子项金额（快照价 × 份数）。退菜/取消的不计入可补偿基数。"""
        if self.state is ItemState.CANCELLED:
            return 0.0
        return round(self.unit_price * max(0, int(self.qty)), 2)

    @property
    def finished(self) -> bool:
        return self.state in (ItemState.SERVED, ItemState.CANCELLED)

    def describe(self) -> str:
        station = f"@ST{self.station_id}" if self.station_id else ""
        urge = f" 催×{self.urge_count}" if self.urge_count else ""
        return f"#{self.id} {self.dish_name or self.dish_id}×{self.qty} " \
               f"[{self.state.value}]{station}{urge}"


@dataclass
class Order:
    id: int
    order_no: str
    table_id: int | None = None
    source: str = "web"
    state: OrderState = OrderState.CREATED
    version: int = 0
    promised_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    items: list[OrderItem] = field(default_factory=list)
    # 顾客会话的脱敏编号（如 T03-A1B2）。**不是账号、不是 openid**：
    # 只在本次服务进程内有效，用来让后厨/管理端区分「哪一桌的哪一次开桌」。
    # 追加在末尾而不是插在 table_id 后面：Order 在测试里有位置参数构造，
    # 中间插字段会把 source/state 全部错位。员工下的单恒为 None。
    customer_ref: str | None = None
    #: 设备级匿名档案 ID（顾客画像/习惯聚合用）。与 customer_ref 分工：
    #: ref 是「本次开桌会话」（后厨对单），guest_key 是「跨开桌的本机档案」。
    #: 生产接微信登录后由 openid 绑定替换，结构不用改。
    guest_key: str | None = None

    @property
    def amount(self) -> float:
        """整单可补偿基数：按快照价求和。"""
        return round(sum(item.amount for item in self.items), 2)

    @property
    def due_at(self) -> datetime:
        """派单排序用的「有效交期」：催菜一次提前 3 分钟。

        `promised_at` 缺失（老数据/手工建单）时退到 `created_at`，再退到
        「很久以前」——派单不能让一条缺交期的单永远排在队尾。
        """
        anchor = self.promised_at or self.created_at or datetime(1970, 1, 1, tzinfo=CST)
        urges = sum(item.urge_count for item in self.items) if self.items else 0
        return anchor - timedelta(minutes=PRIORITY_URGE_MIN * urges)

    def describe(self) -> str:
        return f"{self.order_no}[{self.state.value}] " + \
               "、".join(item.describe() for item in self.items)


@dataclass(frozen=True)
class OrderEvent:
    """`order_events` 一行。**唯一事实源**，`Order.state` 是它的物化结果。"""

    order_id: int
    from_state: str
    to_state: str
    ts: datetime
    actor: str = ""
    channel: str = "tft"
    client_event_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def item_id(self) -> int | None:
        raw = self.detail.get("item_id")
        return int(raw) if raw is not None else None

    def describe(self) -> str:
        who = f" by {self.actor}" if self.actor else ""
        item = self.detail.get("item_id")
        scope = f"item#{item} " if item is not None else ""
        return (f"{self.ts.strftime('%H:%M:%S')} {scope}{self.from_state}"
                f"→{self.to_state}（{self.channel}{who}）")


@dataclass
class StationOccupancy:
    """`station_occupancy` 一行：一次「上灶 → 出餐」的占用会话。"""

    id: int
    station_id: int
    staff_id: int
    started_at: datetime
    order_item_id: int | None = None
    device_id: str = ""
    state: str = "open"
    flame_verified: bool = False
    ended_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.state == "open"

    def elapsed_sec(self, now: datetime) -> float:
        return ((self.ended_at or now) - self.started_at).total_seconds()

    def close(self, now: datetime, state: str = "closed") -> "StationOccupancy":
        return replace(self, state=state, ended_at=now)

    def describe(self) -> str:
        flag = "已点火" if self.flame_verified else "**未点火**"
        return f"ST{self.station_id} 由 {self.staff_id} 占用（{flag}，" \
               f"{self.state}）"


@dataclass(frozen=True)
class ProcessEvent:
    """`process_events` 一行：#12 称重 / #13 SOP / #14 留样 / #15 消毒共用。

    `kind` 取值固定为 `weigh` / `sop_step` / `retention` / `disinfection`。
    不按功能各建一张表：这四类的形状完全一样（谁、在哪个单/工位、什么结论、
    什么时候），分表的唯一收益是查询少一个 `where kind=`，代价是合规导出
    （日管控台账要按时间线汇总全部关键操作）要 union 四张表。
    """

    id: int
    kind: str
    ts: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    order_item_id: int | None = None
    station_id: int | None = None
    staff_id: int | None = None
    session_uid: str | None = None
    state: str = ""

    def describe(self) -> str:
        who = f" by {self.staff_id}" if self.staff_id is not None else ""
        return f"{self.ts.strftime('%H:%M:%S')} [{self.kind}] {self.state}{who}"


def require_transition(current: ItemState, target: ItemState, *, what: str = "子项") -> None:
    """状态机守卫。违例抛 `InvalidTransition`，不做静默修正。

    静默修正的后果：TFT 连点两次「出餐」，第二次被吞掉，操作员以为没生效又点
    第三次——现场看到的是「点了没反应」，最后靠重启解决。
    """
    if not can_item_move(current, target):
        raise InvalidTransition(
            f"{what}状态不允许 {current.value} → {target.value}"
            f"（允许：{sorted(state.value for state in ITEM_TRANSITIONS[current]) or '无'}）"
        )
