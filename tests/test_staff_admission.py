# -*- coding: utf-8 -*-
"""#8 晨检 / #9 健康证 / #10 资质：准入判定与每日扫描。

这组测试盯的是「门禁看着很严、其实挡不住」的形态：

- 拒绝原因顺序错 → 人被引导去补一件不相干的事，来回跑；
- 证件按 `state` 判 → 扫描任务漏跑一次就放人进去；
- 复核只要刷卡 → 组长一个人就能把不合格的人放进去（还留了痕）；
- 候选集与准入判定分叉 → 把证过期的人派了单，刷卡时才被拒。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from kitchen.staff import (
    AdmissionController,
    CertificateSweeper,
    DenyReason,
    DuplicateError,
    MemoryStaffRepository,
)
from kitchen.staff.models import CST, MorningAnswers

DAY = date(2026, 11, 12)
AT_9AM = datetime(2026, 11, 12, 9, 0, tzinfo=CST)

ZHANG_CARD = "A1B2C3D4"   # 张三：证有效、有排班（工位1）、资质 stove+cold_dish
LI_CARD = "E5F6A7B8"      # 李四：证已过期、无排班
LEAD_CARD = "C9D0E1F2"    # 王组长：证有效、有排班（工位2）、有 lead 资质


@pytest.fixture()
def repo() -> MemoryStaffRepository:
    # 注入固定 DAY：种子相对日期按本文件时钟求值，跨到别的日期跑也不失效。
    return MemoryStaffRepository.from_seed(today=DAY)


@pytest.fixture()
def ctrl(repo) -> AdmissionController:
    return AdmissionController(repo)


def pass_morning(ctrl, card: str = ZHANG_CARD, at: datetime = AT_9AM):
    return ctrl.submit_morning(card, MorningAnswers(temp_c=36.5), now=at)


# ================================================================ 五道门
class TestGates:
    def test_未登记卡直接拒绝(self, ctrl):
        result = ctrl.scan("FFFFFFFF", station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.UNKNOWN_CARD,)
        assert result.masked is True

    def test_停用卡与未登记卡区分(self, repo, ctrl):
        """现场「卡坏了」和「换过卡」的处理方式完全不同，提示不能一样。"""
        repo.set_card_enabled(ZHANG_CARD, False)
        result = ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.CARD_DISABLED,)
        assert result.staff.name == "张三"        # 人查出来了，只是卡不能用

    def test_健康证过期优先于未晨检(self, ctrl):
        """顺序错了会让人白填一遍问卷——而且填完还是进不去。"""
        result = ctrl.scan(LI_CARD, station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.CERT_EXPIRED,)
        assert DenyReason.MORNING_MISSING not in result.reasons
        assert result.masked is True

    def test_证件问题不给填问卷(self, ctrl, repo):
        """填了也没用的事，不要让人做。"""
        outcome = ctrl.submit_morning(LI_CARD, MorningAnswers(temp_c=36.5), now=AT_9AM)
        assert outcome.ok is False
        assert repo.morning_check(2, DAY) is None

    def test_未晨检提示就地填问卷(self, ctrl):
        result = ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.MORNING_MISSING,)
        assert result.needs_morning_check is True
        assert result.masked is False        # 不是硬拦截：当场就能解决

    def test_无排班被拒(self, repo, ctrl):
        repo.add_certificate(staff_id=2, cert_no="HC-NEW", valid_to=date(2027, 6, 30))
        pass_morning(ctrl, LI_CARD)
        result = ctrl.scan(LI_CARD, station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.NOT_ON_SHIFT,)

    def test_排班被拦截要说出原因(self, repo, ctrl):
        shift = repo.shifts(1, DAY)[0]
        repo.block_shift(shift.id, reason="健康证 HC-2026-0001 已过期")
        pass_morning(ctrl)
        result = ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.SHIFT_BLOCKED,)
        assert any("已过期" in detail for detail in result.details)

    def test_资质不符(self, repo, ctrl):
        repo.station_requirements[1] = "sample"     # 没人持有该资质
        pass_morning(ctrl)
        result = ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.QUALIFICATION_MISS,)
        assert "sample" in result.details[-1]

    def test_全绿时通过(self, ctrl):
        pass_morning(ctrl)
        result = ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM)
        assert result.ok and not result.reasons
        assert result.staff.name == "张三"
        assert result.station_requirement == "stove"

    def test_换岗到无排班的工位仍放行(self, repo, ctrl):
        """现场临时换岗很常见。只要当天有班、人不违规，就让他上。"""
        pass_morning(ctrl)
        result = ctrl.scan(ZHANG_CARD, station_id=2, now=AT_9AM)
        assert result.ok
        assert any("本次在 2" in detail for detail in result.details)


# ================================================================ 晨检
class TestMorning:
    def test_体温超限不合格并锁定(self, ctrl, repo):
        outcome = ctrl.submit_morning(ZHANG_CARD, MorningAnswers(temp_c=38.0), now=AT_9AM)
        assert outcome.ok is False
        assert outcome.locked is True
        assert repo.morning_check(1, DAY).result == "failed"
        result = ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM)
        assert result.reasons == (DenyReason.MORNING_FAILED,)

    def test_三项症状任一即不合格(self, ctrl):
        for answers in (
            MorningAnswers(temp_c=36.5, diarrhea=True),
            MorningAnswers(temp_c=36.5, pharyngitis=True),
            MorningAnswers(temp_c=36.5, hand_wound=True),
        ):
            outcome = ctrl.submit_morning(ZHANG_CARD, answers, now=AT_9AM)
            assert outcome.ok is False
            assert outcome.locked

    def test_体温未填算不合格(self, ctrl):
        """门禁侧的安全偏好与环境侧相反：环境缺数据宁可不动，门禁缺数据一律不放。"""
        outcome = ctrl.submit_morning(ZHANG_CARD, MorningAnswers(), now=AT_9AM)
        assert outcome.ok is False
        assert any("体温" in item for item in outcome.check.failures)

    def test_37点3度是边界(self, ctrl):
        exact = ctrl.submit_morning(ZHANG_CARD, MorningAnswers(temp_c=37.3), now=AT_9AM)
        assert exact.ok is False
        below = ctrl.submit_morning(ZHANG_CARD, MorningAnswers(temp_c=37.2), now=AT_9AM)
        assert below.ok is True

    def test_同日重填以最后一次为准(self, ctrl):
        ctrl.submit_morning(ZHANG_CARD, MorningAnswers(temp_c=38.0), now=AT_9AM)
        ctrl.submit_morning(ZHANG_CARD, MorningAnswers(temp_c=36.4), now=AT_9AM)
        assert ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM).ok


# ================================================================ 复核
class TestReview:
    def _fail(self, ctrl, card: str = ZHANG_CARD, staff_id: int = 1):
        ctrl.submit_morning(card, MorningAnswers(temp_c=38.2), now=AT_9AM)
        return staff_id

    def test_双通道复核放行(self, ctrl):
        self._fail(ctrl)
        outcome = ctrl.review(1, reviewer_card_uid=LEAD_CARD, web_confirmed=True, now=AT_9AM)
        assert outcome.ok is True
        assert outcome.check.result == "excused"
        assert outcome.check.channel == "tft+web"
        assert outcome.check.confirmed_by          # 留痕必须有人名
        assert ctrl.scan(ZHANG_CARD, station_id=1, now=AT_9AM).ok

    def test_只有刷卡没有Web确认被拒(self, ctrl, repo):
        """单通道复核 = 组长一个人说了算，留痕看起来还很规范。"""
        self._fail(ctrl)
        outcome = ctrl.review(1, reviewer_card_uid=LEAD_CARD, web_confirmed=False, now=AT_9AM)
        assert outcome.ok is False
        assert "二次确认" in outcome.reason
        assert repo.morning_check(1, DAY).locked is True     # 没被偷偷放行

    def test_不能复核自己(self, ctrl, repo):
        ctrl.submit_morning(LEAD_CARD, MorningAnswers(temp_c=38.2), now=AT_9AM)
        outcome = ctrl.review(3, reviewer_card_uid=LEAD_CARD, web_confirmed=True, now=AT_9AM)
        assert outcome.ok is False
        assert "自己" in outcome.reason
        assert repo.morning_check(3, DAY).locked is True

    def test_无组长资质不能复核(self, ctrl):
        self._fail(ctrl)
        outcome = ctrl.review(1, reviewer_card_uid=ZHANG_CARD, web_confirmed=True, now=AT_9AM)
        assert outcome.ok is False
        assert "组长资质" in outcome.reason

    def test_复核人自己证过期不能复核(self, repo, ctrl):
        """自己的证都过期了还去放别人进来，复核本身该被打回。"""
        self._fail(ctrl)
        cert = repo.certificates(3)[0]
        repo.add_certificate(staff_id=3, cert_no="HC-OLD", valid_to=date(2026, 1, 1))
        repo.mark_certificate_state(3, cert.id, "revoked")
        outcome = ctrl.review(1, reviewer_card_uid=LEAD_CARD, web_confirmed=True, now=AT_9AM)
        assert outcome.ok is False
        assert "健康证" in outcome.reason

    def test_没锁定的晨检不用复核(self, ctrl):
        pass_morning(ctrl)
        outcome = ctrl.review(1, reviewer_card_uid=LEAD_CARD, web_confirmed=True, now=AT_9AM)
        assert outcome.ok is False
        assert "无需复核" in outcome.reason

    def test_没有晨检记录不能凭空复核(self, ctrl):
        outcome = ctrl.review(1, reviewer_card_uid=LEAD_CARD, web_confirmed=True, now=AT_9AM)
        assert outcome.ok is False
        assert "没有晨检记录" in outcome.reason


# ================================================================ 资质候选（#10）
class TestCandidates:
    def test_候选必须过了晨检(self, repo, ctrl):
        """把没过晨检的人放进候选，派单模块会派出去，再在刷卡时被拒。"""
        assert ctrl.candidates("stove", now=AT_9AM) == ()
        pass_morning(ctrl)
        names = [staff.name for staff in ctrl.candidates("stove", now=AT_9AM)]
        assert names == ["张三"]

    def test_证过期的人不进候选(self, repo, ctrl):
        repo.add_certificate(staff_id=2, cert_no="DUMMY", valid_to=date(2020, 1, 1))
        repo.add_shift(staff_id=2, station_id=1, shift_date=DAY, shift_no=2)
        pass_morning(ctrl, LI_CARD)
        assert all(staff.id != 2 for staff in ctrl.candidates("stove", now=AT_9AM))

    def test_资质不符的人不进候选(self, repo, ctrl):
        pass_morning(ctrl)
        assert ctrl.candidates("sample", now=AT_9AM) == ()
        assert ctrl.qualifies(1, "stove") is True
        assert ctrl.qualifies(1, "sample") is False


# ================================================================ 每日扫描（#9）
class TestSweeper:
    def test_过期证件被置状态并拦截排班(self, repo):
        shift = repo.add_shift(staff_id=2, station_id=1, shift_date=DAY, shift_no=1)
        sweep = CertificateSweeper(repo).run(DAY)
        assert [staff.name for staff, _cert in sweep.expired] == ["李四"]
        assert shift.id in sweep.blocked_shifts
        assert repo.shifts(2, DAY)[0].state == "blocked"
        assert "过期" in repo.shifts(2, DAY)[0].assignment["blocked_reason"]

    def test_状态列被同步为expired(self, repo):
        """判定不看 state，但 state 要跟日期一致，否则报表自相矛盾。"""
        CertificateSweeper(repo).run(DAY)
        assert repo.certificates(2)[0].state == "expired"

    def test_临期提醒(self, repo):
        """到期当天才提醒等于逼人无证上岗——办证要时间。"""
        # 王组长的证 = 装配日 + 1 年；取到期前 15 天，落在 30 天提醒窗内。
        # 张三的证是 +2 年，不该被一起报出来（证件告警最怕狼来了）。
        expire = date(DAY.year + 1, DAY.month, DAY.day)
        sweep = CertificateSweeper(repo, warn_days=30).run(
            expire - timedelta(days=15)
        )
        assert [staff.name for staff, _cert, _days in sweep.expiring_soon] == ["王组长"]

    def test_只报告真正过期的(self, repo):
        """张三/王组长的证都有效，不该被顺手报出来——证件告警最怕狼来了。"""
        sweep = CertificateSweeper(repo).run(DAY)
        assert [staff.name for staff, _cert in sweep.expired] == ["李四"]
        assert not sweep.expiring_soon

    def test_描述能直接贴到通知里(self, repo):
        text = CertificateSweeper(repo).run(DAY).describe()
        assert "李四" in text and "过期" in text
