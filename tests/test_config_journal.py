# -*- coding: utf-8 -*-
"""配置日志（journal）跨重启持久化：菜品/配方「界面保存」不丢。

口径：
- 写入是语义化 payload（不存派生 id），重放时 id/版本号按当前内存现算；
- 撞唯一键 / 脏行只跳过该行，不中断后续重放（等价 INSERT IGNORE）；
- var_dir=None（纯内存，单测默认）时日志自动失效，返回 False。
"""
from __future__ import annotations

import json

import pytest

from kitchen.process.journal import (
    append_journal,
    journal_recipe_payload,
    replay_journal,
)
from kitchen.process.repository import MemoryProcessRepository


@pytest.fixture()
def repo():
    return MemoryProcessRepository.from_seed()


def test_追加与重放_新建菜品(repo, tmp_path):
    path = tmp_path / "config_journal.jsonl"
    assert append_journal(path, {
        "op": "dish", "code": "D-NEW1", "name": "新菜一号",
        "category": "热菜", "price": 22.0,
    })
    assert path.exists()

    applied, skipped = replay_journal(path, repo)
    assert (applied, skipped) == (1, 0)
    dish = repo.dish_by_code("D-NEW1")
    assert dish.name == "新菜一号"
    assert dish.price == 22.0


def test_重放id现算_不与seed撞车(repo, tmp_path):
    """seed 已占 id 1-4，重放的新菜必须拿 max+1=5，而不是沿用保存时的 id。"""
    path = tmp_path / "config_journal.jsonl"
    append_journal(path, {
        "op": "dish", "code": "D-NEW2", "name": "新菜二号",
        "category": "凉菜", "price": 12.0,
    })
    replay_journal(path, repo)
    assert repo.dish_by_code("D-NEW2").id == 5


def test_配方版本重放_完整复刻(repo, tmp_path):
    path = tmp_path / "config_journal.jsonl"
    payload = journal_recipe_payload(
        dish_id=3,  # 清炒时蔬，seed 里无配方
        name="清炒时蔬 v1",
        items=[{"ingredient": "时蔬", "weight_g": 200.0, "tolerance_pct": 5.0}],
        steps=[{"step_no": 1, "title": "清炒", "instruction": "大火快炒",
                "heat_level": "high", "duration_sec": 120,
                "ccp": True, "requires_card": True}],
    )
    append_journal(path, payload)

    applied, _ = replay_journal(path, repo)
    assert applied == 1
    recipe = repo.recipe_for(3)
    assert recipe is not None and recipe.version == 1
    items = list(repo.recipe_items(recipe.id))
    steps = list(repo.sop_steps(recipe.id))
    assert items[0].ingredient == "时蔬" and items[0].bounds() == (190.0, 210.0)
    assert steps[0].ccp and steps[0].requires_card
    assert repo.estimate_minutes(3) != 15  # 有了 SOP，不再取保守值


def test_重复重放_撞唯一键整行跳过(repo, tmp_path):
    """重放两遍同一行：第二遍撞编码唯一键，跳过不炸。"""
    path = tmp_path / "config_journal.jsonl"
    append_journal(path, {
        "op": "dish", "code": "D-DUP", "name": "重复菜", "price": 9.0,
    })
    replay_journal(path, repo)
    applied, skipped = replay_journal(path, repo)
    assert (applied, skipped) == (0, 1)
    assert repo.dish_by_code("D-DUP").name == "重复菜"  # 只有第一遍生效


def test_脏行不中断后续重放(repo, tmp_path):
    path = tmp_path / "config_journal.jsonl"
    path.write_text(
        "{不是json}\n"
        + json.dumps({"op": "unknown_op", "x": 1}, ensure_ascii=False) + "\n"
        + json.dumps({"op": "dish", "code": "D-AFTER", "name": "脏行后的菜",
                      "price": 5.0}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    applied, skipped = replay_journal(path, repo)
    assert (applied, skipped) == (1, 2)
    assert repo.dish_by_code("D-AFTER").name == "脏行后的菜"


def test_配方指向不存在的菜_整行跳过(repo, tmp_path):
    path = tmp_path / "config_journal.jsonl"
    append_journal(path, {"op": "recipe", "dish_id": 999, "name": "孤儿配方",
                          "items": [{"ingredient": "x", "weight_g": 10.0}]})
    applied, skipped = replay_journal(path, repo)
    assert (applied, skipped) == (0, 1)


def test_纯内存模式_日志自动失效(repo):
    """var_dir=None 时 append 返回 False，调用方据此提示「重启会丢」。"""
    assert append_journal(None, {"op": "dish"}) is False
    assert replay_journal(None, repo) == (0, 0)


def test_文件不存在_重放零操作(repo, tmp_path):
    assert replay_journal(tmp_path / "不存在.jsonl", repo) == (0, 0)
