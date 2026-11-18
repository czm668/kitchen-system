# -*- coding: utf-8 -*-
"""过程执行域的资料仓储（A4-01）：菜品 / 配方 / SOP / 工位 / 供应商 / 批次 / 溯源码。

与 `staff.repository` 同一套做法：把种子里的静态资料读成不可变对象，**主动复刻
唯一键与外键**，让「内存里过了 = 库里也会过」。

## 为什么这一层只读

`orders` / `order_items` / `order_events` 是**运行期**数据，读写逻辑在
`process/orders.py`；这里的七张表是**配置期**资料（菜品、配方、SOP 步骤、工位、
批次），改它们的入口只有「管理端配置页」和种子。把两类混在一个仓储里，单测就
必须为「改一条 SOP」造一堆订单，成本全落在无关的地方。

## 配方版本的口径

`dish_recipes` 的唯一键是 `(dish_id, version)`，同一个菜可以有多个版本。
`recipe_for()` 取**当前启用版本的最高 version**——做单时把 `recipe_id` 快照进
`order_items`（模型里那个字段），这样「改配方」只影响新单，在做的单不受影响。
不让在做的单跟着变，是因为称重判据与 SOP 步骤一旦中途换版，已经确认过的步骤号
就对不上了，出餐校验会失败在半夜。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kitchen.domain import (
    CST,
    DomainError,
    DuplicateError,
    ForeignKeyError,
    NotFoundError,
    as_bool,
    parse_datetime,
)
from kitchen.sqlseed import read_table

from .models import Dish, Recipe, RecipeItem, SopStep, WorkStation, estimate_minutes

__all__ = ["ProcessDomainError", "MemoryProcessRepository", "DEFAULT_SEED"]

log = logging.getLogger("kitchen.process.repository")

#: 工程根的种子文件（`src/kitchen/process/repository.py` → parents[3] = 工程根）
DEFAULT_SEED = Path(__file__).resolve().parents[3] / "fixtures" / "seed_kitchen.sql"

#: 与 staff 域共用同一族异常，旧名留作别名
ProcessDomainError = DomainError


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(float(str(value)))


def _float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(str(value))


def _json_object(value: Any) -> dict[str, Any]:
    """`JSON` 列读出来可能是字符串——统一转成 dict，别让调用方各自 `json.loads`。

    漏转一处的后果很隐蔽：`payload["检测"]` 抛 `TypeError: string indices must be
    integers`，而错误发生在**顾客扫码**这条链路上，开发环境里根本没人点。
    """
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


class MemoryProcessRepository:
    """静态资料的只读视图（可加载后小改，用于测试与配置页）。"""

    def __init__(self) -> None:
        self._dishes: dict[int, Dish] = {}
        self._dish_by_code: dict[str, int] = {}
        self._stations: dict[int, WorkStation] = {}
        self._station_by_code: dict[str, int] = {}
        self._recipes: dict[int, Recipe] = {}
        self._recipe_items: dict[int, list[RecipeItem]] = {}
        self._sop: dict[int, list[SopStep]] = {}
        self._suppliers: dict[int, dict[str, Any]] = {}
        self._batches: dict[int, dict[str, Any]] = {}
        #: 原料批次：ingredient_name ↔ RecipeItem.ingredient 同名关联。
        self._ingredient_batches: dict[int, dict[str, Any]] = {}
        self._trace: dict[str, dict[str, Any]] = {}
        #: 业务落库写器（A23）。由 `api/state.py` 装配后注入；None = 纯内存。
        #: 种子装载发生在装配之前，天然不触发——基线由 `sync_baseline()` 一次性物化。
        self.recorder: Any = None

    # ------------------------------------------------------------ 装载
    @classmethod
    def from_seed(
        cls, path: str | Path | None = None, *, today: date | None = None
    ) -> "MemoryProcessRepository":
        """`today` 是种子里 `CURDATE()` 链的求值基准（不传则按运行当天）。

        批次的 produced_at/expire_at 现在锚定「当天 11:30 / +2 天 11:30」，
        单测注入 FakeClock 的日期就能验证「演示当天装配」而不靠碰巧。
        """
        seed = Path(path) if path is not None else DEFAULT_SEED
        anchor = today or date.today()
        repo = cls()
        for row in read_table(seed, "dishes", today=anchor):
            repo.add_dish(
                dish_id=_int(row["id"]), code=str(row["code"]), name=str(row["name"]),
                category=str(row.get("category") or ""),
                price=_float(row.get("price")),
                sold_out=as_bool(row.get("sold_out"), False),
                enabled=as_bool(row.get("enabled"), True),
                prep_minutes=_int(row.get("prep_minutes")),
            )
        for row in read_table(seed, "work_stations", today=anchor):
            repo.add_station(
                station_id=_int(row["id"]), code=str(row["code"]),
                name=str(row.get("name") or ""), kind=str(row.get("kind") or "stove"),
                device_id=str(row.get("device_id") or ""),
                capacity=_int(row.get("capacity"), 1),
            )
        for row in read_table(seed, "dish_recipes", today=anchor):
            repo.add_recipe(
                recipe_id=_int(row["id"]), dish_id=_int(row["dish_id"]),
                version=_int(row.get("version"), 1), name=str(row.get("name") or ""),
                enabled=as_bool(row.get("enabled"), True),
            )
        for row in read_table(seed, "dish_recipe_items", today=anchor):
            repo.add_recipe_item(
                item_id=_int(row["id"]), recipe_id=_int(row["recipe_id"]),
                ingredient=str(row["ingredient_name"]),
                weight_g=_float(row["weight_g"]),
                tolerance_pct=_float(row.get("tolerance_pct"), 5.0),
                sort_no=_int(row.get("sort_no")),
            )
        for row in read_table(seed, "sop_steps", today=anchor):
            repo.add_sop_step(
                step_id=_int(row["id"]), recipe_id=_int(row["recipe_id"]),
                step_no=_int(row["step_no"]), title=str(row["title"]),
                instruction=str(row.get("instruction") or ""),
                heat_level=str(row.get("heat_level") or ""),
                duration_sec=_int(row.get("duration_sec")),
                ccp=as_bool(row.get("ccp"), False),
                requires_card=as_bool(row.get("requires_card"), False),
            )
        for row in read_table(seed, "suppliers", today=anchor):
            repo.add_supplier(_int(row["id"]), dict(row))
        for row in read_table(seed, "dish_batches", today=anchor):
            repo.add_batch(_int(row["id"]), dict(row))
        for row in read_table(seed, "ingredient_batches", today=anchor):
            repo.add_ingredient_batch(_int(row["id"]), dict(row))
        for row in read_table(seed, "trace_codes", today=anchor):
            repo.add_trace_code(str(row["code"]), dict(row))
        return repo

    # ------------------------------------------------------------ 菜品
    def add_dish(self, *, dish_id: int, code: str, name: str, category: str = "",
                 price: float = 0.0, sold_out: bool = False, enabled: bool = True,
                 prep_minutes: int = 0) -> Dish:
        if dish_id in self._dishes:
            raise DuplicateError(f"dish_id {dish_id} 已存在（uk_dishes_code/PK）")
        key = code.strip().upper()
        if key in self._dish_by_code:
            raise DuplicateError(f"菜品编码 {code} 重复（uk_dishes_code）")
        dish = Dish(dish_id, code.strip(), name, category, price, sold_out, enabled,
                    prep_minutes)
        self._dishes[dish_id] = dish
        self._dish_by_code[key] = dish_id
        self._persist("dishes", "insert_dish_row", dish)
        return dish

    def dish(self, dish_id: int) -> Dish:
        found = self._dishes.get(int(dish_id))
        if found is None:
            raise NotFoundError(f"菜品 {dish_id} 不存在")
        return found

    def dish_by_code(self, code: str) -> Dish:
        found = self._dish_by_code.get(str(code).strip().upper())
        if found is None:
            raise NotFoundError(f"菜品编码 {code} 不存在")
        return self._dishes[found]

    def dishes(self) -> tuple[Dish, ...]:
        return tuple(self._dishes[key] for key in sorted(self._dishes))

    def set_sold_out(self, dish_id: int, sold_out: bool = True) -> Dish:
        """沽清是**可撤销**的独立状态，不写进 `enabled`。

        用 `enabled=0` 表达沽清，会连带把这道菜从「历史订单可查、可退款」里
        隐掉，退款流程查不到菜品名称。
        """
        dish = self.dish(dish_id)
        updated = Dish(dish.id, dish.code, dish.name, dish.category, dish.price,
                       bool(sold_out), dish.enabled, dish.prep_minutes)
        self._dishes[dish.id] = updated
        # 沽清是对既有行的字段更新：INSERT IGNORE 不会生效，走定向 UPDATE
        self._persist("dishes", lambda r: self.recorder.update_dish_sold_out(
            r.id, r.sold_out), updated)
        return updated

    # ------------------------------------------------------------ 工位
    def add_station(self, *, station_id: int, code: str, name: str = "",
                    kind: str = "stove", device_id: str = "", capacity: int = 1) -> WorkStation:
        if station_id in self._stations:
            raise DuplicateError(f"station_id {station_id} 已存在")
        key = code.strip().upper()
        if key in self._station_by_code:
            raise DuplicateError(f"工位编码 {code} 重复（uk_stations_code）")
        station = WorkStation(station_id, code.strip(), name or code, kind.strip(),
                              device_id, "idle", capacity)
        self._stations[station_id] = station
        self._station_by_code[key] = station_id
        self._persist("work_stations", "insert_station_row", station)
        return station

    def station(self, station_id: int) -> WorkStation:
        found = self._stations.get(int(station_id))
        if found is None:
            raise NotFoundError(f"工位 {station_id} 不存在")
        return found

    def stations(self, kind: str | None = None) -> tuple[WorkStation, ...]:
        items = [self._stations[key] for key in sorted(self._stations)]
        if kind:
            items = [item for item in items if item.kind == kind]
        return tuple(items)

    def with_station_state(self, station_id: int, state: str) -> WorkStation:
        station = self.station(station_id)
        updated = WorkStation(station.id, station.code, station.name, station.kind,
                              station.device_id, state, station.capacity)
        self._stations[station.id] = updated
        return updated

    # ------------------------------------------------------------ 配方 / SOP
    def add_recipe(self, *, recipe_id: int, dish_id: int, version: int = 1,
                   name: str = "", enabled: bool = True) -> Recipe:
        if recipe_id in self._recipes:
            raise DuplicateError(f"recipe_id {recipe_id} 已存在")
        self.dish(dish_id)                         # 外键 fk_recipes_dish
        for other in self._recipes.values():
            if other.dish_id == dish_id and other.version == version:
                raise DuplicateError(
                    f"菜品 {dish_id} 的 v{version} 配方已存在（uk_recipes_dish_version）"
                )
        recipe = Recipe(recipe_id, dish_id, version, name, enabled)
        self._recipes[recipe_id] = recipe
        self._persist("dish_recipes", "insert_recipe_row", recipe)
        return recipe

    def add_recipe_item(self, *, item_id: int, recipe_id: int, ingredient: str,
                        weight_g: float, tolerance_pct: float = 5.0,
                        sort_no: int = 0) -> RecipeItem:
        if recipe_id not in self._recipes:
            raise ForeignKeyError(f"配方 {recipe_id} 不存在（fk_recipe_items_recipe）")
        for existing in self._recipe_items.get(recipe_id, []):
            if existing.id == item_id:
                raise DuplicateError(f"recipe_item id {item_id} 重复")
        if weight_g <= 0:
            # 目标克重为 0 或负数，容差区间退化成一点，「配菜称重」变成不可通过的关卡
            raise DomainError(f"目标克重必须 > 0，实际 {weight_g}")
        item = RecipeItem(item_id, recipe_id, ingredient.strip(), float(weight_g),
                          float(tolerance_pct), int(sort_no))
        self._recipe_items.setdefault(recipe_id, []).append(item)
        self._recipe_items[recipe_id].sort(key=lambda row: (row.sort_no, row.id))
        self._persist("dish_recipe_items", "insert_recipe_item_row", item)
        return item

    def add_sop_step(self, *, step_id: int, recipe_id: int, step_no: int, title: str,
                     instruction: str = "", heat_level: str = "", duration_sec: int = 0,
                     ccp: bool = False, requires_card: bool = False) -> SopStep:
        if recipe_id not in self._recipes:
            raise ForeignKeyError(f"配方 {recipe_id} 不存在（fk_sop_recipe）")
        for existing in self._sop.get(recipe_id, []):
            if existing.step_no == step_no:
                raise DuplicateError(
                    f"配方 {recipe_id} 的第 {step_no} 步已存在（uk_sop_recipe_step）"
                )
        step = SopStep(step_id, recipe_id, int(step_no), title.strip(), instruction,
                       heat_level, int(duration_sec), bool(ccp), bool(requires_card))
        self._sop.setdefault(recipe_id, []).append(step)
        self._sop[recipe_id].sort(key=lambda row: row.step_no)
        self._persist("sop_steps", "insert_sop_row", step)
        return step

    def all_recipes(self) -> tuple[Recipe, ...]:
        """全部配方版本（按菜品 + 版本排序）。

        `recipe_for()` 只给「当前启用的最高版本」，管理端要展示版本历史、并且
        新建版本时要避开已用过的版本号，就必须能看到全部版本。
        """
        return tuple(sorted(self._recipes.values(), key=lambda r: (r.dish_id, r.version)))

    def recipe_for(self, dish_id: int) -> Recipe | None:
        """取该菜**当前启用**的最高版本配方；都没有返回 None。"""
        candidates = [item for item in self._recipes.values()
                      if item.dish_id == dish_id and item.enabled]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.version)

    def recipe_items(self, recipe_id: int | None) -> tuple[RecipeItem, ...]:
        if recipe_id is None:
            return ()
        return tuple(self._recipe_items.get(int(recipe_id), ()))

    def sop_steps(self, recipe_id: int | None) -> tuple[SopStep, ...]:
        if recipe_id is None:
            return ()
        return tuple(self._sop.get(int(recipe_id), ()))

    def estimate_minutes(self, dish_id: int, *, default_min: int = 15) -> int:
        """预计出餐分钟数，优先级：人工难度标定 > SOP 步骤时长 > 保守默认值。

        `prep_minutes` 是按烹饪难度标定的参考时长（凉菜几分钟、炖菜几十分钟），
        由种子/管理端维护，菜单展示、购物车汇总、下单交期都读它，因此不同菜
        的参考时长能真实反映难度，而不是一刀切 15 分钟。

        没有标定值时退回 SOP 工艺时长（青椒肉丝等）；连 SOP 都没有才取保守
        默认值——按 5 分钟算会让没配方的菜**永远插在队首**，于是「先做哪单」
        变成「哪道菜没配 SOP 就先做」。取保守值让不确定的菜不占便宜。
        """
        dish = self._dishes.get(int(dish_id))
        if dish is not None and dish.prep_minutes > 0:
            return int(dish.prep_minutes)
        recipe = self.recipe_for(dish_id)
        if recipe is None:
            return max(1, int(default_min))
        return estimate_minutes(list(self.sop_steps(recipe.id)))

    # ------------------------------------------------------------ 供应链
    def add_supplier(self, supplier_id: int, row: dict[str, Any]) -> None:
        if supplier_id in self._suppliers:
            raise DuplicateError(f"supplier_id {supplier_id} 已存在")
        self._suppliers[supplier_id] = dict(row)
        self._persist("suppliers", lambda r: self.recorder.insert_supplier_row(
            supplier_id, r), self._suppliers[supplier_id])

    def put_supplier(self, supplier_id: int, row: dict[str, Any]) -> None:
        """新增或更新供应商（管理端录入入口）。id 复用既有行即更新。"""
        self._suppliers[int(supplier_id)] = dict(row)
        self._persist("suppliers", lambda r: r.upsert_supplier_row(
            int(supplier_id), self._suppliers[int(supplier_id)]),
            self._suppliers[int(supplier_id)])

    def add_batch(self, batch_id: int, row: dict[str, Any]) -> None:
        if batch_id in self._batches:
            raise DuplicateError(f"batch_id {batch_id} 已存在")
        self.dish(_int(row.get("dish_id")))        # 外键 fk_batches_dish
        # 种子里的时间是字符串；统一在这里转成带时区 datetime，避免「有的地方 str
        # 有的地方 datetime」，日期比较就会在某个分支上抛 TypeError
        normalized = dict(row)
        for key in ("produced_at", "expire_at"):
            normalized[key] = parse_datetime(row.get(key))
        self._batches[batch_id] = normalized
        self._persist("dish_batches", lambda r: self.recorder.insert_batch_row(
            batch_id, r), normalized)

    def put_batch(self, batch_id: int, row: dict[str, Any]) -> None:
        """新增或更新批次。时间归一化与外键校验同 add_batch，避免 str/datetime 混用。"""
        self.dish(_int(row.get("dish_id")))        # 外键 fk_batches_dish
        normalized = dict(row)
        for key in ("produced_at", "expire_at"):
            normalized[key] = parse_datetime(row.get(key))
        self._batches[int(batch_id)] = normalized
        self._persist("dish_batches", lambda r: r.upsert_batch_row(
            int(batch_id), self._batches[int(batch_id)]), normalized)

    # ------------------------------------------------------------ 原料批次
    def add_ingredient_batch(self, batch_id: int, row: dict[str, Any]) -> None:
        if batch_id in self._ingredient_batches:
            raise DuplicateError(f"ingredient_batch_id {batch_id} 已存在")
        self.supplier(_int(row.get("supplier_id")))  # 外键：原料必有其供应商
        normalized = self._normalize_ingredient_batch(row)
        self._ingredient_batches[batch_id] = normalized
        self._persist("ingredient_batches",
                      lambda r: self.recorder.insert_ingredient_batch_row(batch_id, r),
                      normalized)

    def put_ingredient_batch(self, batch_id: int, row: dict[str, Any]) -> None:
        """新增或更新原料批次（管理端录入/编辑入口）。"""
        self.supplier(_int(row.get("supplier_id")))
        normalized = self._normalize_ingredient_batch(row)
        self._ingredient_batches[int(batch_id)] = normalized
        self._persist("ingredient_batches",
                      lambda r: r.upsert_ingredient_batch_row(
                          int(batch_id), self._ingredient_batches[int(batch_id)]),
                      normalized)

    @staticmethod
    def _normalize_ingredient_batch(row: dict[str, Any]) -> dict[str, Any]:
        """时间归一到带时区 datetime、检疫 JSON 归一到 dict——与成品批次同口径。

        漏掉的后果是扫码时 `expire_at < datetime` 抛 TypeError，而这条链路
        开发环境根本没人点，直到顾客扫码才炸。
        """
        normalized = dict(row)
        for key in ("received_at", "expire_at"):
            normalized[key] = parse_datetime(row.get(key))
        normalized["inspection"] = _json_object(row.get("inspection"))
        return normalized

    def ingredient_batch_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._ingredient_batches))

    def ingredient_batch(self, batch_id: int) -> dict[str, Any]:
        found = self._ingredient_batches.get(int(batch_id))
        if found is None:
            raise NotFoundError(f"原料批次 {batch_id} 不存在")
        return dict(found)

    def ingredient_batches_of(self, ingredient_name: str) -> tuple[dict[str, Any], ...]:
        """某原料的全部到货批次（先按未过期、再按到货时间倒序）。

        配方项只存原料名（`猪里脊`），同名原料可能到货多批；取未过期且最新
        的一批作为「这盘菜用的原料」，过期批次仍返回但排在后面，供追溯展示。
        """
        key = str(ingredient_name).strip()
        rows = [dict(row) for row in self._ingredient_batches.values()
                if str(row.get("ingredient_name") or "").strip() == key]
        now = datetime.now(CST)
        rows.sort(key=lambda r: (
            bool(r.get("expire_at")) and r["expire_at"] < now,
            -(r.get("received_at").timestamp() if isinstance(r.get("received_at"), datetime) else 0),
        ))
        return tuple(rows)

    def batch_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._batches))

    def expired_batch_of(self, dish_id: int, now: datetime) -> dict[str, Any] | None:
        """菜品是否"全部关联批次都过期"——用于售卖门禁。

        规则（与 #14 应留样、#15 出库门禁同口径，取最严）：
        - 该菜没有任何批次 → 返回 None（没有批次信息，不构成下架理由，照常卖）。
         新扩充的菜品大多没有批次，不能因此全下架。
        - 有批次但存在任一未过期 → 返回 None（还有新鲜批次可用，照常卖）。
        - 有批次且全部过期 → 返回最早过期的那条批次（含 batch_no/expire_at），
          菜单据此标"已下架"，下单据此拦截。

        返回 datetime 供上层格式化，不在仓储层拼中文文案（分层）。
        """
        rows = [self._batches[bid] for bid in self._batches
                if _int(self._batches[bid].get("dish_id")) == int(dish_id)]
        if not rows:
            return None
        # 统一到同一时区口径再比：批次是 aware，而调用方（测试/脚本）可能传 naive
        # 的 now。直接 `<` 会抛「can't compare offset-naive and offset-aware」，
        # 让售卖门禁在那一刻整个失效。naive 按本地 CST 解释。
        ref = parse_datetime(now)
        fresh = [r for r in rows
                 if not (isinstance(r.get("expire_at"), datetime)
                         and r["expire_at"] < ref)]
        if fresh:
            return None
        # 全部过期：给出最早过期的一条作为代表原因
        expired = [r for r in rows if isinstance(r.get("expire_at"), datetime)]
        if not expired:
            return None
        expired.sort(key=lambda r: r["expire_at"])
        return dict(expired[0])

    def add_trace_code(self, code: str, row: dict[str, Any]) -> None:
        key = code.strip().upper()
        if key in self._trace:
            raise DuplicateError(f"溯源码 {code} 重复（uk_trace_code）")
        batch_id = row.get("batch_id")
        if batch_id is not None and _int(batch_id) not in self._batches:
            raise ForeignKeyError(f"批次 {batch_id} 不存在（fk_trace_batch）")
        normalized = dict(row)
        normalized["payload"] = _json_object(row.get("payload"))
        self._trace[key] = normalized
        self._persist("trace_codes", lambda r: self.recorder.insert_trace_row(
            key, r), normalized)

    def put_trace_code(self, code: str, row: dict[str, Any]) -> None:
        """新增或更新溯源码（管理端录入入口）。外键校验同 add_trace_code。"""
        key = str(code).strip().upper()
        if not key:
            raise ValueError("溯源码不能为空")
        batch_id = row.get("batch_id")
        if batch_id not in (None, ""):
            if _int(batch_id) not in self._batches:
                raise ForeignKeyError(f"批次 {batch_id} 不存在（fk_trace_batch）")
        supplier_id = row.get("supplier_id")
        if supplier_id not in (None, ""):
            if _int(supplier_id) not in self._suppliers:
                raise ForeignKeyError(f"供应商 {supplier_id} 不存在（fk_trace_supplier）")
        normalized = dict(row)
        normalized["payload"] = _json_object(row.get("payload"))
        normalized["created_at"] = parse_datetime(row.get("created_at")) or self._now()
        self._trace[key] = normalized
        self._persist("trace_codes", lambda r: r.upsert_trace_row(key, normalized),
                      normalized)

    def _now(self) -> datetime:
        """录入溯源码未填时间时的落库基准（带时区，与种子时间同口径）。"""
        return parse_datetime(datetime.now().isoformat())

    def batch(self, batch_id: int) -> dict[str, Any]:
        found = self._batches.get(int(batch_id))
        if found is None:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        return dict(found)

    def supplier(self, supplier_id: int) -> dict[str, Any]:
        found = self._suppliers.get(int(supplier_id))
        if found is None:
            raise NotFoundError(f"供应商 {supplier_id} 不存在")
        return dict(found)

    def suppliers(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(self._suppliers[key]) for key in sorted(self._suppliers))

    def trace_rows(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        """全部溯源码（code, row）——批次反查溯源码时用，只有个位数行。"""
        return tuple((code, dict(self._trace[code])) for code in sorted(self._trace))

    def trace_code(self, code: str) -> dict[str, Any]:
        found = self._trace.get(str(code).strip().upper())
        if found is None:
            raise NotFoundError(f"溯源码 {code} 不存在")
        return dict(found)

    def trace_view(self, code: str) -> dict[str, Any]:
        """扫码溯源：#14/顾客端要的「批次 + 供应商 + 生产时间 + 检测记录」一屏。"""
        row = self.trace_code(code)
        batch = self._batches.get(_int(row["batch_id"])) if row.get("batch_id") else None
        supplier = self._suppliers.get(_int(row["supplier_id"])) if row.get("supplier_id") else None
        return {
            "code": code.strip().upper(),
            "batch_no": (batch or {}).get("batch_no"),
            "produced_at": (batch or {}).get("produced_at"),
            "expire_at": (batch or {}).get("expire_at"),
            "supplier": (supplier or {}).get("name"),
            "license_no": (supplier or {}).get("license_no"),
            "qualifications": (supplier or {}).get("qualifications"),
        }

    # ------------------------------------------------------------ 视图
    def snapshot(self) -> dict[str, int]:
        return {
            "dishes": len(self._dishes),
            "stations": len(self._stations),
            "recipes": len(self._recipes),
            "recipe_items": sum(len(rows) for rows in self._recipe_items.values()),
            "sop_steps": sum(len(rows) for rows in self._sop.values()),
            "suppliers": len(self._suppliers),
            "batches": len(self._batches),
            "ingredient_batches": len(self._ingredient_batches),
            "trace_codes": len(self._trace),
        }

    def iter_sop(self) -> Iterable[SopStep]:
        for rows in self._sop.values():
            yield from rows

    # ------------------------------------------------------------ 内部
    def _persist(self, what: str, method: Any, entity: Any) -> None:
        """落库旁路（A23）：写器未装配或写失败都不影响内存主路径。

        `method` 传**方法名字符串**（在确认 `recorder` 非 None 后才 getattr），
        或已捕获外部参数的 lambda——绑定方法不能提前求值，否则写器未装配时
        AttributeError 会在业务主路径上炸出来。
        失败已在写器内计数，这里只防意外异常外溢。"""
        if self.recorder is None:
            return
        try:
            fn = getattr(self.recorder, method) if isinstance(method, str) else method
            fn(entity)
        except Exception:  # pragma: no cover - 双保险
            log.warning("配置资料落库失败（%s）", what, exc_info=True)
