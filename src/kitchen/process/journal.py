# -*- coding: utf-8 -*-
"""配置期资料的落盘日志（A4 配套）：菜品/配方的「界面保存」跨重启不丢。

## 为什么需要它

七类配置期资料（菜品/配方/SOP/工位…）的架构口径是「内存只读视图 + seed
唯一持久层」（见 `repository.py` 模块头）。但管理端配置页上线后，「界面
保存」成了第二个写入口，而它只改内存——服务一重启，用户在页面上建的
菜品和配方就消失了。答辩演示时这正是最容易翻车的场景：上午建好的配方，
下午重启没了。

## 方案：操作日志（journal），不是把内存整表回写

每次配置写操作成功后，把**语义化 payload**（不存派生的 id）追加到
`var/config_journal.jsonl`；启动时 seed 装载完毕后按序重放。选追加日志
而不是整表快照，有三个理由：

1. **幂等安全**：重放时所有 id（dish_id/recipe_id/version/item/step id）
   都按「当前内存最大值 +1」现算，跟保存时的逻辑一致——seed 后来新增了
   同 id 资料也不会撞车，撞唯一键的行整条跳过（等价 INSERT IGNORE）。
2. **可审计**：一行一次操作，跟审计日志同构，出问题能定位到哪次保存。
3. **零迁移**：不引入新表、不动 seed 的权威地位（seed 仍是出厂基线，
   journal 只承载「出厂之后的改动」）。

## 失败口径

写 journal 失败**不回滚**内存（三端已经看到数据了，回滚制造更大的不一致），
由调用方把「归档失败」如实带进返回值，让界面提示用户重启会丢。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kitchen.domain import DuplicateError, DomainError, NotFoundError

__all__ = [
    "append_journal", "replay_journal", "journal_recipe_payload",
    "journal_supplier_payload", "journal_batch_payload", "journal_trace_payload",
    "journal_ingredient_batch_payload",
]


def append_journal(path: Path | None, record: dict[str, Any]) -> bool:
    """追加一条配置操作记录。**尽力而为**：写失败返回 False，不抛。"""
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def journal_recipe_payload(
    dish_id: int, name: str, items: list[dict[str, Any]], steps: list[dict[str, Any]]
) -> dict[str, Any]:
    """配方版本日志的语义化 payload：只存内容，不存派生 id。"""
    return {
        "op": "recipe",
        "dish_id": int(dish_id),
        "name": str(name),
        "items": [
            {"ingredient": str(row.get("ingredient") or ""),
             "weight_g": float(row.get("weight_g") or 0),
             "tolerance_pct": float(row.get("tolerance_pct") or 5.0),
             "sort_no": int(row.get("sort_no") or offset + 1)}
            for offset, row in enumerate(items)
        ],
        "steps": [
            {"step_no": int(row.get("step_no") or offset + 1),
             "title": str(row.get("title") or f"步骤 {offset + 1}"),
             "instruction": str(row.get("instruction") or ""),
             "heat_level": str(row.get("heat_level") or ""),
             "duration_sec": int(row.get("duration_sec") or 0),
             "ccp": bool(row.get("ccp")),
             "requires_card": bool(row.get("requires_card"))}
            for offset, row in enumerate(steps or [])
        ],
    }


def _apply_dish(repo, payload: dict[str, Any]) -> None:
    """重放「新建菜品」。id 现算（seed 可能已增长），编码撞唯一键则跳过。"""
    new_id = max((row.id for row in repo.dishes()), default=0) + 1
    repo.add_dish(
        dish_id=new_id,
        code=str(payload.get("code") or "").strip(),
        name=str(payload.get("name") or ""),
        category=str(payload.get("category") or ""),
        price=float(payload.get("price") or 0.0),
        prep_minutes=max(0, int(payload.get("prep_minutes") or 0)),
    )


def _apply_recipe(repo, payload: dict[str, Any]) -> None:
    """重放「保存配方版本」。id/版本号全部现算，逻辑同保存动作。"""
    dish_id = int(payload["dish_id"])
    dish = repo.dish(dish_id)  # 菜品不存在 → NotFoundError → 整行跳过
    versions = [row for row in repo.all_recipes() if row.dish_id == dish_id]
    next_version = max((row.version for row in versions), default=0) + 1
    next_recipe_id = max((row.id for row in repo.all_recipes()), default=0) + 1
    next_item_id = 1 + max(
        (row.id for recipe in repo.all_recipes()
         for row in repo.recipe_items(recipe.id)), default=0)
    next_step_id = 1 + max(
        (row.id for recipe in repo.all_recipes()
         for row in repo.sop_steps(recipe.id)), default=0)

    items = payload.get("items") or []
    if not items:
        raise DomainError("重放的配方没有配料，跳过（同保存口径）")

    recipe = repo.add_recipe(
        recipe_id=next_recipe_id, dish_id=dish_id, version=next_version,
        name=str(payload.get("name") or dish.name),
    )
    for offset, row in enumerate(items):
        repo.add_recipe_item(
            item_id=next_item_id + offset, recipe_id=recipe.id,
            ingredient=str(row.get("ingredient") or ""),
            weight_g=float(row.get("weight_g") or 0),
            tolerance_pct=float(row.get("tolerance_pct") or 5.0),
            sort_no=int(row.get("sort_no") or offset + 1),
        )
    for offset, row in enumerate(payload.get("steps") or []):
        repo.add_sop_step(
            step_id=next_step_id + offset, recipe_id=recipe.id,
            step_no=int(row.get("step_no") or offset + 1),
            title=str(row.get("title") or f"步骤 {offset + 1}"),
            instruction=str(row.get("instruction") or ""),
            heat_level=str(row.get("heat_level") or ""),
            duration_sec=int(row.get("duration_sec") or 0),
            ccp=bool(row.get("ccp")),
            requires_card=bool(row.get("requires_card")),
        )


def _apply_supplier(repo, payload: dict[str, Any]) -> None:
    """重放供应商录入/编辑。id 用日志里的值（编辑要保持原 id，upsert 幂等）。"""
    sid = int(payload["id"])
    repo.put_supplier(sid, {
        "name": str(payload.get("name") or ""),
        "license_no": str(payload.get("license_no") or "") or None,
        "contact": str(payload.get("contact") or ""),
        "qualifications": list(payload.get("qualifications") or []),
        "state": str(payload.get("state") or "active"),
    })


def _apply_batch(repo, payload: dict[str, Any]) -> None:
    """重放批次录入/编辑。时间存字符串，put_batch 负责归一化成带时区 datetime。"""
    bid = int(payload["id"])
    repo.put_batch(bid, {
        "batch_no": str(payload.get("batch_no") or ""),
        "dish_id": int(payload["dish_id"]),
        "produced_at": payload.get("produced_at"),
        "expire_at": payload.get("expire_at"),
        "state": str(payload.get("state") or "produced"),
    })


def _apply_ingredient_batch(repo, payload: dict[str, Any]) -> None:
    """重放原料批次录入/编辑。id 用日志值，时间字符串由 put 负责归一化。"""
    bid = int(payload["id"])
    repo.put_ingredient_batch(bid, {
        "batch_no": str(payload.get("batch_no") or ""),
        "ingredient_name": str(payload.get("ingredient_name") or ""),
        "supplier_id": int(payload.get("supplier_id") or 0),
        "received_at": payload.get("received_at"),
        "expire_at": payload.get("expire_at"),
        "inspection": dict(payload.get("inspection") or {}),
        "state": str(payload.get("state") or "received"),
    })


def _apply_trace(repo, payload: dict[str, Any]) -> None:
    """重放溯源码录入/编辑。用 code 作业务键 upsert，重放两遍也幂等。"""
    code = str(payload["code"])
    repo.put_trace_code(code, {
        "id": int(payload.get("id") or 0),
        "batch_id": payload.get("batch_id"),
        "supplier_id": payload.get("supplier_id"),
        "payload": dict(payload.get("payload") or {}),
        "created_at": payload.get("created_at"),
    })


def journal_supplier_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "supplier", "id": int(row["id"]),
        "name": str(row.get("name") or ""),
        "license_no": str(row.get("license_no") or ""),
        "contact": str(row.get("contact") or ""),
        "qualifications": list(row.get("qualifications") or []),
        "state": str(row.get("state") or "active"),
    }


def journal_batch_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "batch", "id": int(row["id"]),
        "batch_no": str(row.get("batch_no") or ""),
        "dish_id": int(row.get("dish_id") or 0),
        "produced_at": str(row.get("produced_at") or ""),
        "expire_at": str(row.get("expire_at") or ""),
        "state": str(row.get("state") or "produced"),
    }


def journal_ingredient_batch_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "ingredient_batch", "id": int(row["id"]),
        "batch_no": str(row.get("batch_no") or ""),
        "ingredient_name": str(row.get("ingredient_name") or ""),
        "supplier_id": int(row.get("supplier_id") or 0),
        "received_at": str(row.get("received_at") or ""),
        "expire_at": str(row.get("expire_at") or ""),
        "inspection": dict(row.get("inspection") or {}),
        "state": str(row.get("state") or "received"),
    }


def journal_trace_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "trace", "code": str(row.get("code") or ""),
        "id": int(row.get("id") or 0),
        "batch_id": row.get("batch_id"),
        "supplier_id": row.get("supplier_id"),
        "payload": dict(row.get("payload") or {}),
        "created_at": str(row.get("created_at") or ""),
    }


def replay_journal(path: Path | None, repo) -> tuple[int, int]:
    """按序重放配置日志，返回 (成功条数, 跳过条数)。

    单行失败只跳过那一行（计数），绝不中断后续重放——一行脏数据
    不能让整个启动挂掉。
    """
    if path is None or not path.exists():
        return (0, 0)
    applied = skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            op = record.get("op")
            if op == "dish":
                _apply_dish(repo, record)
            elif op == "recipe":
                _apply_recipe(repo, record)
            elif op == "supplier":
                _apply_supplier(repo, record)
            elif op == "batch":
                _apply_batch(repo, record)
            elif op == "ingredient_batch":
                _apply_ingredient_batch(repo, record)
            elif op == "trace":
                _apply_trace(repo, record)
            else:
                skipped += 1
                continue
            applied += 1
        except (ValueError, KeyError, TypeError, OSError,
                DuplicateError, NotFoundError, DomainError):
            skipped += 1
    return (applied, skipped)
