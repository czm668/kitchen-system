# -*- coding: utf-8 -*-
"""#16 违规处置闭环（A5-04）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from vision_support import P1, P2, ROIS, bare, frame, full_kit

from kitchen.domain import CST
from kitchen.vision.disposal import (
    DisposalError,
    PowerBusyError,
    ReviewerNotAllowed,
    StationPowerBank,
    ViolationDisposal,
    voice_text,
)
from kitchen.vision.models import ViolationKind
from kitchen.vision.postprocess import analyze
from kitchen.vision.violations import Violation, ViolationTracker

T0 = datetime(2026, 11, 12, 11, 30, tzinfo=CST)

LEAD = {"王组长"}
STAFF = {"张三", "李四"}


def violation(kind: str = ViolationKind.SMOKING, station_id: int = 1) -> Violation:
    box = P1 if station_id == 1 else P2
    state = analyze(bare(box), frame(1, ts=T0), ROIS).persons[0]
    return Violation(kind=kind, station_id=station_id, since=T0,
                     last=T0 + timedelta(seconds=2), frames=4, person=state)


def build(*, capacity: int = 2, approvers=None, on_duty=None,
          min_cut_sec: float = 0.0, tracker=None, voice=None, notify=None,
          evidence=None):
    bank = StationPowerBank(capacity=capacity)
    disposal = ViolationDisposal(
        bank, tracker=tracker, evidence=evidence,
        approvers=approvers, on_duty=on_duty,
        min_cut_sec=min_cut_sec, voice=voice, notify=notify,
        clock=lambda: T0,
    )
    return bank, disposal


class TestDispose:
    def test_行为类切电并开单(self):
        bank, disposal = build()
        result = disposal.dispose(violation(ViolationKind.SMOKING), now=T0)
        assert result.power_cut is True
        assert bank.is_cut(1) is True
        assert result.ticket.power_cut is True
        assert result.ticket.state == "open"
        assert "power_cut" in result.actions

    def test_着装类不切电(self):
        bank, disposal = build()
        result = disposal.dispose(violation(ViolationKind.NO_HAT), now=T0)
        assert result.power_cut is False
        assert bank.is_cut(1) is False
        assert result.actions == ("voice_play",)
        assert "着装类" in result.reason

    def test_语音文案带工位与具体缺项(self):
        said: list[str] = []
        _bank, disposal = build(voice=said.append)
        disposal.dispose(violation(ViolationKind.NO_MASK, station_id=2), now=T0)
        assert said == ["ST2 请戴好口罩"]

    def test_voice_text_未知类型有兜底(self):
        assert "ST3" in voice_text("something_new", 3)

    def test_重复切电不再request(self):
        """重复切电会清空 reset_reviewers，把组长的确认抹掉。"""
        bank, disposal = build()
        disposal.dispose(violation(ViolationKind.SMOKING), now=T0)
        bank.confirm(1, "web", "王组长")
        assert bank.reviewers(1) == {"web"}, "复核记录存的是通道，不是人名"
        result = disposal.dispose(violation(ViolationKind.SMOKING), now=T0)
        assert "已断电，跳过" in result.actions[0]
        assert bank.reviewers(1) == {"web"}, "复核记录不能被重复切电冲掉"

    def test_继电器路数不足时不切电(self):
        _bank, disposal = build(capacity=1)
        disposal.dispose(violation(ViolationKind.SMOKING, 1), now=T0)
        result = disposal.dispose(violation(ViolationKind.SMOKING, 2), now=T0)
        assert result.power_cut is False
        assert any("继电器只有 1 路" in b for b in result.blocked)

    def test_同一工位复用一张单(self):
        _bank, disposal = build()
        first = disposal.dispose(violation(ViolationKind.SMOKING), now=T0)
        second = disposal.dispose(violation(ViolationKind.NO_HAT),
                                  now=T0 + timedelta(seconds=200))
        assert first.ticket.id == second.ticket.id
        assert second.ticket.repeated is True

    def test_不同工位各一张单(self):
        _bank, disposal = build()
        a = disposal.dispose(violation(ViolationKind.SMOKING, 1), now=T0)
        b = disposal.dispose(violation(ViolationKind.SMOKING, 2), now=T0)
        assert a.ticket.id != b.ticket.id
        assert {t.station_id for t in disposal.tickets()} == {1, 2}

    def test_通知回调收到工单(self):
        pushes: list[tuple[str, dict]] = []
        _bank, disposal = build(notify=lambda topic, payload: pushes.append((topic, payload)))
        disposal.dispose(violation(), now=T0)
        assert pushes[0][0] == "violation_disposal"
        assert pushes[0][1]["type"] == "vision_violation"

    def test_语音异常不影响开单(self):
        def boom(_text):
            raise RuntimeError("喇叭没接")

        _bank, disposal = build(voice=boom)
        result = disposal.dispose(violation(), now=T0)
        assert result.ticket.state == "open"
        assert any("语音失败" in b for b in result.blocked)


class TestReview:
    def test_单通道确认不恢复(self):
        bank, disposal = build(approvers=lambda n: n in LEAD)
        disposal.dispose(violation(), now=T0)
        result = disposal.confirm(1, "tft", "王组长", now=T0 + timedelta(seconds=20))
        assert result.complete is False
        assert result.restored is False
        assert bank.is_cut(1) is True

    def test_双通道确认恢复并关单(self):
        bank, disposal = build(approvers=lambda n: n in LEAD)
        disposal.dispose(violation(), now=T0)
        moment = T0 + timedelta(seconds=20)
        disposal.confirm(1, "tft", "王组长", now=moment)
        result = disposal.confirm(1, "web", "王组长", now=moment)
        assert result.complete is True
        assert result.restored is True
        assert bank.is_cut(1) is False
        assert result.ticket.state == "closed"

    def test_非组长拒绝(self):
        _bank, disposal = build(approvers=lambda n: n in LEAD)
        disposal.dispose(violation(), now=T0)
        with pytest.raises(ReviewerNotAllowed, match="组长资质"):
            disposal.confirm(1, "tft", "张三", now=T0 + timedelta(seconds=20))

    def test_本人不能复核自己(self):
        class Occupant:
            name = "张三"

        _bank, disposal = build(on_duty=lambda sid: Occupant())
        disposal.dispose(violation(), now=T0)
        with pytest.raises(ReviewerNotAllowed, match="不能复核自己"):
            disposal.confirm(1, "tft", "张三", now=T0 + timedelta(seconds=20))

    def test_未知通道拒绝(self):
        _bank, disposal = build()
        disposal.dispose(violation(), now=T0)
        with pytest.raises(DisposalError, match="未知复核通道"):
            disposal.confirm(1, "sms", "王组长")

    def test_空复核人拒绝(self):
        _bank, disposal = build()
        disposal.dispose(violation(), now=T0)
        with pytest.raises(ReviewerNotAllowed, match="不能为空"):
            disposal.confirm(1, "web", "   ")

    def test_断电太快不允许恢复(self):
        bank, disposal = build(min_cut_sec=10.0)
        disposal.dispose(violation(), now=T0)
        moment = T0 + timedelta(seconds=3)
        disposal.confirm(1, "tft", "王组长", now=moment)
        result = disposal.confirm(1, "web", "王组长", now=moment)
        assert result.complete is True
        assert result.restored is False
        assert "距断电仅" in result.blocked
        assert bank.is_cut(1) is True

    def test_等够时间后重试成功(self):
        bank, disposal = build(min_cut_sec=10.0)
        disposal.dispose(violation(), now=T0)
        disposal.confirm(1, "tft", "王组长", now=T0 + timedelta(seconds=3))
        disposal.confirm(1, "web", "王组长", now=T0 + timedelta(seconds=3))
        result = disposal.confirm(1, "web", "王组长", now=T0 + timedelta(seconds=30))
        assert result.restored is True
        assert bank.is_cut(1) is False

    def test_着装类双通道确认即关单(self):
        bank, disposal = build()
        disposal.dispose(violation(ViolationKind.NO_HAT), now=T0)
        disposal.confirm(1, "tft", "王组长", now=T0)
        result = disposal.confirm(1, "web", "王组长", now=T0)
        assert result.restored is True
        assert result.ticket.state == "closed"
        assert bank.is_cut(1) is False

    def test_关单同时清visual状态(self):
        tracker = ViolationTracker()
        for index in range(4):
            tracker.observe(analyze(bare(P1), frame(index + 1,
                              ts=T0 + timedelta(seconds=index * 0.8)), ROIS))
        assert tracker.snapshot()["tracks"] == 3
        _bank, disposal = build(tracker=tracker)
        disposal.dispose(violation(), now=T0)
        disposal.confirm(1, "tft", "王组长", now=T0 + timedelta(seconds=20))
        disposal.confirm(1, "web", "王组长", now=T0 + timedelta(seconds=20))
        assert tracker.snapshot()["tracks"] == 0, "复核后不能还挂着活跃违规"

    def test_复核记录写进工单(self):
        _bank, disposal = build()
        disposal.dispose(violation(), now=T0)
        moment = T0 + timedelta(seconds=20)
        disposal.confirm(1, "tft", "王组长", note="已戴好帽子", now=moment)
        ticket = disposal.open_ticket(1)
        assert ticket.reviewers == {"tft": "王组长"}
        assert "已戴好帽子" in ticket.note


class TestPowerBank:
    def test_容量与占用(self):
        bank = StationPowerBank(capacity=2)
        bank.cut(1, source="vision_violation", now=T0)
        assert bank.used == 1
        bank.cut(2, source="vision_violation", now=T0)
        assert bank.used == 2
        with pytest.raises(PowerBusyError):
            bank.cut(3, source="vision_violation", now=T0)

    def test_重复切返回already(self):
        bank = StationPowerBank()
        assert bank.cut(1, source="x", now=T0) == "cut"
        assert bank.cut(1, source="x", now=T0) == "already"

    def test_容量非法拒绝(self):
        with pytest.raises(DisposalError):
            StationPowerBank(capacity=0)

    def test_未切电时查询安全(self):
        bank = StationPowerBank()
        assert bank.is_cut(9) is False
        assert bank.cut_at(9) is None
        assert bank.reviewers(9) == set()

    def test_降级同步(self):
        bank = StationPowerBank()
        bank.cut(1, source="x", now=T0)
        bank.set_degraded(True)
        with pytest.raises(Exception):
            bank.restore(1)

    def test_snapshot结构(self):
        bank = StationPowerBank()
        bank.cut(1, source="vision_violation", now=T0)
        snap = bank.snapshot()
        assert snap["capacity"] == 2
        assert snap["stations"]["1"]["cut"] is True


class TestSnapshot:
    def test_汇总(self):
        _bank, disposal = build()
        disposal.dispose(violation(ViolationKind.SMOKING), now=T0)
        disposal.dispose(violation(ViolationKind.NO_HAT, 2), now=T0)
        snap = disposal.snapshot()
        assert len(snap["open_tickets"]) == 2
        assert snap["closed"] == 0
        assert snap["power_bank"]["used"] == 1


class TestDescription:
    def test_工单描述(self):
        _bank, disposal = build()
        result = disposal.dispose(violation(), now=T0)
        text = result.ticket.describe()
        assert "ST1" in text and "工位吸烟" in text and "已断电" in text

    def test_处置描述含动作与说明(self):
        _bank, disposal = build()
        text = disposal.dispose(violation(), now=T0).describe()
        assert "power_cut" in text and "语音" not in text.split("｜")[0]
