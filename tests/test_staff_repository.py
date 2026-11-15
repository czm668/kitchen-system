# -*- coding: utf-8 -*-
"""人员域仓储（A3-01）：唯一键、外键、换卡留痕。

这组测试盯的是「内存实现比 MySQL 宽松」这一整类缺陷：单测全绿、上线 1062。
仓储是唯一能提前拦住它们的地方。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from kitchen.staff import (
    DEFAULT_SEED,
    DuplicateError,
    ForeignKeyError,
    MemoryStaffRepository,
    NotFoundError,
    StaffDomainError,
    normalize_uid,
)
from kitchen.staff.models import MorningAnswers, MorningCheck

DAY = date(2026, 11, 12)


@pytest.fixture()
def repo() -> MemoryStaffRepository:
    # 种子里的 CURDATE() 按本文件的固定 DAY 求值，保持既有断言口径；
    # 不注入就会按「运行当天」装配，DAY=2026-11-12 的排班/健康证断言全落空。
    return MemoryStaffRepository.from_seed(today=DAY)


# ================================================================ 种子导入
class TestSeed:
    def test_三张卡三个人三张证(self, repo):
        snap = repo.snapshot()
        assert snap["staff"] == 3
        assert snap["cards"] == {"total": 3, "active": 3}
        assert snap["certificates"] == 3

    def test_卡号大小写都能查到(self, repo):
        """RC522 固件给的是小写，Web 端录入习惯大写——不归一化就是两张卡。"""
        assert repo.card("A1B2C3D4") is not None
        assert repo.card("a1b2c3d4") is not None
        assert repo.card("0xa1:b2:c3:d4") is not None

    def test_资质JSON被正确解析(self, repo):
        zhang = repo.staff(1)
        assert zhang.qualifications == frozenset({"stove", "cold_dish"})
        assert zhang.has("stove")
        assert not zhang.has("lead")

    def test_工位资质来自kind(self, repo):
        assert repo.station_requirement(1) == "stove"
        assert repo.station_requirement(2) == "stove"
        assert repo.station_requirement(None) == ""

    def test_显式覆盖优先于kind(self, repo):
        repo.station_requirements[2] = "cold_dish"
        assert repo.station_requirement(2) == "cold_dish"

    def test_种子路径可定位(self):
        assert DEFAULT_SEED.is_file()


# ================================================================ 唯一键
class TestUniqueKeys:
    def test_同卡号绑两次报重复(self, repo):
        """对应 uk_cards_uid。裸 dict 会静默覆盖，最后绑的人赢。"""
        with pytest.raises(DuplicateError):
            repo.bind_card("A1B2C3D4", 3)

    def test_员工编号不能重复(self, repo):
        """对应 uk_staff_code。串了编号，考勤与工单都会归到别人名下。"""
        with pytest.raises(DuplicateError):
            repo.upsert_staff(code="E001", name="冒名张三")

    def test_同日重复晨检严格插入报错(self, repo):
        """对应 uk_mc_staff_date。upsert 覆盖，insert 报错——两种语义都要有。"""
        check = MorningCheck(
            staff_id=1, check_date=DAY, answers=MorningAnswers(temp_c=36.5),
        )
        repo.insert_morning_check(check)
        with pytest.raises(DuplicateError):
            repo.insert_morning_check(check)

    def test_同日晨检可覆盖(self, repo):
        """现场就是会重填：体温枪第一次没读出来，第二次补上。"""
        repo.upsert_morning_check(
            MorningCheck(staff_id=1, check_date=DAY, answers=MorningAnswers(temp_c=38.0),
                         result="failed", failures=("体温 38.0℃",))
        )
        repo.upsert_morning_check(
            MorningCheck(staff_id=1, check_date=DAY, answers=MorningAnswers(temp_c=36.6))
        )
        saved = repo.morning_check(1, DAY)
        assert saved.result == "passed"
        assert saved.answers.temp_c == 36.6

    def test_同一班次不能排两次(self, repo):
        with pytest.raises(DuplicateError):
            repo.add_shift(staff_id=1, station_id=1, shift_date=DAY, shift_no=1)


# ================================================================ 外键
class TestForeignKeys:
    def test_卡不能绑到不存在的人(self, repo):
        with pytest.raises(ForeignKeyError):
            repo.bind_card("DEADBEEF", 99)

    def test_健康证不能挂到不存在的人(self, repo):
        with pytest.raises(ForeignKeyError):
            repo.add_certificate(staff_id=99, cert_no="X", valid_to=date(2027, 1, 1))

    def test_排班不能挂到不存在的人(self, repo):
        with pytest.raises(ForeignKeyError):
            repo.add_shift(staff_id=99, station_id=1, shift_date=DAY)

    def test_晨检不能写不存在的人(self, repo):
        with pytest.raises(ForeignKeyError):
            repo.upsert_morning_check(
                MorningCheck(staff_id=99, check_date=DAY, answers=MorningAnswers(temp_c=36.5))
            )


# ================================================================ 换卡
class TestCardRebind:
    def test_换卡后旧卡停用但可查(self, repo):
        """留痕要求：旧行不删除，只置 enabled=0。"""
        old, new = repo.rebind_card("A1B2C3D4", "11223344", 1, now=datetime(2026, 11, 12, 9, 0))
        assert new.card_uid == "11223344" and new.enabled
        assert old.card_uid == "A1B2C3D4" and not old.enabled
        assert repo.card("A1B2C3D4").enabled is False     # 还能查到，只是不能用
        assert repo.active_card_of(1).card_uid == "11223344"

    def test_别人的卡不能直接换给自己(self, repo):
        """误把李四的卡换给张三 → 两人身份串号，这是最危险的一类。"""
        with pytest.raises(StaffDomainError):
            repo.rebind_card("E5F6A7B8", "99887766", 1)

    def test_换不存在的旧卡报找不到(self, repo):
        with pytest.raises(NotFoundError):
            repo.rebind_card("FFFFFFFF", "99887766", 1)

    def test_停用卡可以重新启用(self, repo):
        repo.set_card_enabled("A1B2C3D4", False)
        assert not repo.card("A1B2C3D4").enabled
        repo.set_card_enabled("A1B2C3D4", True)
        assert repo.card("A1B2C3D4").enabled


# ================================================================ 派生查询
class TestDerived:
    def test_枚举按id有序(self, repo):
        assert [staff.id for staff in repo.all_staff()] == [1, 2, 3]

    def test_资质覆盖是AND不是OR(self, repo):
        """要求「灶台+冷菜」时，只有灶台的人不能算覆盖。"""
        zhang = repo.staff(1)          # stove + cold_dish
        li = repo.staff(2)             # 只有 stove
        assert zhang.covers({"stove"})
        assert zhang.covers({"stove", "cold_dish"})
        assert li.covers({"stove"})
        assert not li.covers({"stove", "cold_dish"})

    def test_按日期判证件不看state(self, repo):
        """把李四的 state 改回 valid，日期已过 → 仍然无效。

        这是本域最重要的一条：状态列会漂移，日期不会。
        """
        cert = repo.certificates(2)[0]
        repo.mark_certificate_state(2, cert.id, "valid")
        assert not repo.has_valid_certificate(2, DAY)
        assert repo.has_valid_certificate(1, DAY)

    def test_到期当天仍有效(self, repo):
        # 种子的有效证是「装配日 + 2 年」，按相对日期断言；写死 2027-01-01
        # 会在种子改成 CURDATE() 后把这条测试变成常红。
        expire = date(DAY.year + 2, DAY.month, DAY.day)
        assert repo.has_valid_certificate(1, expire)
        assert not repo.has_valid_certificate(1, expire + timedelta(days=1))

    def test_停用员工不进候选(self, repo):
        repo.upsert_staff(staff_id=1, code="E001", name="张三",
                          qualifications=["stove"], enabled=False)
        assert all(staff.id != 1 for staff in repo.eligible("stove", DAY))

    def test_停用不是删除(self, repo):
        """考勤、工单、晨检都要能回溯到人，所以离职只能停用。"""
        repo.set_staff_enabled(2, False)
        assert repo.staff(2).enabled is False
        assert repo.staff(2).name == "李四"          # 还能查到
        assert repo.card("E5F6A7B8").staff_id == 2   # 卡还挂着
        repo.set_staff_enabled(2, True)
        assert repo.staff(2).enabled is True

    def test_停用不存在的人报找不到(self, repo):
        with pytest.raises(NotFoundError):
            repo.set_staff_enabled(99, False)
