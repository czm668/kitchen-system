# -*- coding: utf-8 -*-
"""过程域资料仓储（A4-01）：唯一键、外键、配方版本、沽清与溯源。

与 `test_staff_repository.py` 同一目标：把「内存比 MySQL 宽松」这类缺陷拦在单测里。
"""
from __future__ import annotations

import pytest

from kitchen.domain import DomainError, DuplicateError, ForeignKeyError, NotFoundError
from kitchen.process import MemoryProcessRepository


@pytest.fixture()
def repo() -> MemoryProcessRepository:
    return MemoryProcessRepository.from_seed()


# ================================================================ 种子
class TestSeed:
    def test_七类资料都有(self, repo):
        snap = repo.snapshot()
        assert snap == {
            "dishes": 4, "stations": 2, "recipes": 2, "recipe_items": 6,
            "sop_steps": 8, "suppliers": 2, "batches": 1, "trace_codes": 1,
        }

    def test_菜品编码大小写都查得到(self, repo):
        """小程序传小写、种子与 Web 端是大写——不归一化就成了两种菜。"""
        assert repo.dish_by_code("D-QJRS").id == 1
        assert repo.dish_by_code("d-qjrs").id == 1

    def test_沽清不影响停售标记(self, repo):
        dish = repo.set_sold_out(3, True)
        assert dish.sold_out is True
        assert dish.enabled is True, "沽清不能顺手停售，否则退款流程查不到菜品"

    def test_配方取启用版本里最高的(self, repo):
        repo.add_recipe(recipe_id=9, dish_id=1, version=2, name="青椒肉丝 v2")
        assert repo.recipe_for(1).version == 2
        repo.add_recipe(recipe_id=10, dish_id=1, version=3, name="草稿", enabled=False)
        assert repo.recipe_for(1).version == 2, "停用版本不参与选择"

    def test_没有配方的菜返回None(self, repo):
        assert repo.recipe_for(3) is None  # 清炒时蔬 seed 里没有配方
        assert repo.recipe_items(None) == ()
        assert repo.sop_steps(None) == ()

    def test_预计交期由SOP时长算出(self, repo):
        """青椒肉丝 6 步合计 635 秒 ≈ 11 分钟，加 2 分钟装盘 → 13 分钟。"""
        assert repo.estimate_minutes(1) == 13

    def test_没配方的菜取保守值(self, repo):
        """按 5 分钟算会让它永远插队首——「先做哪单」变成「哪道菜没配 SOP」。"""
        assert repo.estimate_minutes(3) == 15
        assert repo.estimate_minutes(3, default_min=20) == 20

    def test_小料按绝对容差判(self, repo):
        """生抽 8g 的 5% 只有 0.4g，HX711 抖一下就超出；必须抬到 ±1g。"""
        items = {row.ingredient: row for row in repo.recipe_items(1)}
        assert items["生抽"].bounds() == (7.0, 9.0)
        assert items["猪里脊"].bounds() == (142.5, 157.5)


# ================================================================ 唯一键 / 外键
class TestIntegrity:
    def test_菜品编码重复(self, repo):
        with pytest.raises(DuplicateError):
            repo.add_dish(dish_id=99, code="D-QJRS", name="重复")

    def test_工位编码重复(self, repo):
        with pytest.raises(DuplicateError):
            repo.add_station(station_id=99, code="ST-A")

    def test_配方版本重复(self, repo):
        with pytest.raises(DuplicateError):
            repo.add_recipe(recipe_id=99, dish_id=1, version=1)

    def test_SOP步骤号重复(self, repo):
        with pytest.raises(DuplicateError):
            repo.add_sop_step(step_id=99, recipe_id=1, step_no=1, title="重复")

    def test_配方挂到不存在的菜(self, repo):
        with pytest.raises(NotFoundError):
            repo.add_recipe(recipe_id=99, dish_id=404)

    def test_配方项挂到不存在的配方(self, repo):
        with pytest.raises(ForeignKeyError):
            repo.add_recipe_item(item_id=99, recipe_id=404, ingredient="盐", weight_g=3)

    def test_目标克重必须为正(self, repo):
        """克重 0 会让容差区间退化成一点，称重关卡永远过不去。"""
        with pytest.raises(DomainError):
            repo.add_recipe_item(item_id=99, recipe_id=1, ingredient="盐", weight_g=0)

    def test_溯源码重复(self, repo):
        with pytest.raises(DuplicateError):
            repo.add_trace_code("TC-DEMO-0001", {"batch_id": 1})

    def test_溯源码指向不存在的批次(self, repo):
        with pytest.raises(ForeignKeyError):
            repo.add_trace_code("TRACE-9999", {"batch_id": 404})


# ================================================================ 溯源视图
class TestTrace:
    def test_扫码能查到批次与供应商(self, repo):
        view = repo.trace_view("tc-demo-0001")
        assert view["batch_no"]
        assert view["supplier"]
        assert view["produced_at"] is not None

    def test_查不到就明确报错(self, repo):
        with pytest.raises(NotFoundError):
            repo.trace_view("TRACE-UNKNOWN")
