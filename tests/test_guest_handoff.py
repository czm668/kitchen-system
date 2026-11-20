# -*- coding: utf-8 -*-
"""转人工分级接管 + 对客禁语自检 测试。

测的不是「函数返回什么」，而是三条**不能松的底线**：

1. 食品安全/法律风险/顾客要人 → 必须 `human_now`，**模型说不用人也不算**；
2. 「温度 + 要退款」这类组合不能因为分类轻就自动结案（旧逻辑的洞）；
3. 对客文案里不许出现「马上就好 / 已经给您退了 / 热销第一」——
   这些话要么没有数据支撑，要么与 `settled` 恒 0 的代码事实冲突。
"""
from __future__ import annotations

import pytest

from api_support import data_of, login, make_server
from kitchen.guest import guard, handoff
from kitchen.guest.service import ComplaintState

DISH = "D-QJRS"
KEY = "g-handoff-001"


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


def _open(server, key=KEY):
    return data_of(server, "POST", "/api/customer/session",
                   table_id=3, guest_key=key, tags=[])


def _submit(server, session, text, *, key=KEY, wants_refund=False, **kw):
    return data_of(server, "POST", "/api/customer/guest/complaints",
                   customer_token=session["customer_token"], guest_key=key,
                   text=text, wants_refund=wants_refund, **kw)


def _chat(server, session, text, *, key=KEY):
    return data_of(server, "POST", "/api/customer/guest/chat",
                   customer_token=session["customer_token"], guest_key=key,
                   text=text)


# ---------------------------------------------------------------- 分级判定
class TestAssess:
    @pytest.mark.parametrize("text,level,code", [
        ("菜里有头发", "human_now", "food_safety"),
        ("这个汤馊了", "human_now", "food_safety"),
        ("吃完拉肚子，跑了三趟厕所", "human_now", "food_safety"),
        ("吃完身上痒，我过敏了", "human_now", "allergy_reaction"),
        ("你们再不处理我就报警", "human_now", "legal_threat"),
        ("我要打 12315 举报", "human_now", "legal_threat"),
        ("叫你们经理过来", "human_now", "explicit_escalation"),
        ("麻烦转人工", "human_now", "explicit_escalation"),
        ("这道菜我要退款", "ai_plus_human", "money_claim"),
        ("服务员态度很差", "ai_plus_human", "service_attitude"),
        ("等了半个小时还没上", "ai_plus_human", "slow_repeat"),
        ("菜有点凉了", "self_service", "normal"),
    ])
    def test_分级与原因码(self, text, level, code):
        r = handoff.assess(text=text)
        assert (r["level"], r["code"]) == (level, code)
        assert r["code"] in handoff.CODES

    def test_过敏询问不是过敏事故(self):
        """「我花生过敏，这菜能吃吗」是选菜问题，不能派经理到场。"""
        r = handoff.assess(text="我花生过敏，这个菜里有花生吗")
        assert r["level"] == "self_service"
        assert r["code"] == "normal"

    def test_规则层不被模型降级(self):
        """模型说 need_human=false，规则层照样判立即接管，并留下 overridden。"""
        r = handoff.assess(text="菜里有玻璃碴", need_human=False)
        assert r["level"] == "human_now"
        assert r["overridden"] is True
        assert r["model_need_human"] is False

    def test_模型要求人工时不会退回自助(self):
        r = handoff.assess(text="这道菜味道有点不对", need_human=True)
        assert r["level"] == "ai_plus_human"
        assert r["code"] == "model_flag"
        assert r["overridden"] is False

    def test_同类问题反复提升级(self):
        r = handoff.assess(text="菜还是凉的", category="温度", repeat_count=1)
        assert r["level"] == "human_now"
        assert r["code"] == "repeat_complaint"

    def test_非最高级也会给责任人(self):
        r = handoff.assess(text="这道菜我要退款")
        assert r["owner"] == "值班组长"
        assert r["promise_min"] > 0

    def test_每个原因码都有话术与责任人(self):
        for code in handoff.CODES:
            assert handoff._LINES[code] is not None
            assert handoff._OWNERS[code]

    def test_话术不承诺赔退且不含空头时间(self):
        """对客话术只承诺「谁几分钟到场」，不许承诺「退/免/马上」。"""
        for code, line in handoff._LINES.items():
            for bad in ("已退", "免单", "马上", "肯定", "保证"):
                assert bad not in line, f"{code} 话术含越权措辞：{bad}"

    def test_现场承诺不慢于工单跟进(self):
        """级别越高，承诺到场越快——否则分级没有意义。"""
        assert (handoff.assess(text="菜里有头发")["promise_min"]
                < handoff.assess(text="这道菜我要退款")["promise_min"])
        assert (handoff.assess(text="这道菜我要退款")["promise_min"]
                <= handoff.assess(text="这道菜不太好吃", category="菜品质量")["promise_min"])


# ---------------------------------------------------------------- 禁语自检
class TestGuard:
    def test_改写而不是驳回(self):
        text, hits = guard.scrub("好的，马上就好，请稍等")
        assert "马上就好" not in text
        assert "超时会自动升级" in text
        assert hits[0]["term"] == "马上就好"

    def test_替换后句子仍通顺(self):
        """替代句不能带主语，否则「您的菜」+「我帮您盯着」会拼出两个主语。"""
        text, _ = guard.scrub("您的菜马上就到")
        assert text == "您的菜正在出餐，超时会自动升级"

    def test_涉钱禁语改成事实口径(self):
        text, hits = guard.scrub("钱已经给您退了，放心吧")
        assert "已退款" not in text and "已经给您退了" not in text
        assert "还没批下来" in text
        assert hits

    def test_销量禁语(self):
        text, _ = guard.scrub("这是我们的销量第一")
        assert "销量第一" not in text
        assert "不按销量" in text

    def test_scan只读不改(self):
        raw = "马上就好"
        assert guard.scan(raw)
        assert raw == "马上就好"

    def test_自有文案零命中(self):
        """规则层自己的模板必须干净——否则等于边写规矩边破规矩。"""
        assert guard.scan("已收到您的反馈，工单 C1119-001 已受理。"
                          "当前环节：已受理 · 待组长核实证据。"
                          "该环节 SLA 剩余约 30 分钟，超时会自动升级。") == []


# ---------------------------------------------------------------- 提交链路
class TestSubmitHandoff:
    def test_食品安全立即人工接管(self, server):
        s = _open(server)
        r = _submit(server, s, "菜里有一根头发")
        assert r["state"] == ComplaintState.SUBMITTED
        assert r["handoff"]["level"] == "human_now"
        assert r["handoff"]["code"] == "food_safety"
        # 顾客第一眼看到的是「谁几分钟到」，不是工单流程
        assert "值班经理" in r["ai_reply"]
        assert r["handoff"]["customer_line"] in r["ai_reply"]
        # 管理端待办第一条就是人工接管
        assert r["urgent"][0].startswith("食品安全")

    def test_降温加退款不再自动结案(self, server):
        """旧逻辑：分类=温度 → 自动结案；但顾客原话要退款，必须留人处理。"""
        s = _open(server)
        r = _submit(server, s, "菜凉了，我要退款")
        assert r["category"] == "温度"
        assert r["state"] == ComplaintState.SUBMITTED
        assert r["handoff"]["code"] == "money_claim"
        assert r["handoff"]["level"] == "ai_plus_human"

    def test_普通温度仍然自动结案(self, server):
        """别把分级做成「什么都转人工」——那和没有客服一样。"""
        s = _open(server)
        r = _submit(server, s, "菜有点凉了")
        assert r["state"] == ComplaintState.AUTO_CLOSED
        assert r["handoff"]["level"] == "self_service"
        assert r["handoff"]["customer_line"] == ""

    def test_带着未结案工单又提新问题就升级(self, server):
        """分类可能把同一件事判成两类（「凉了」vs「也是凉的」），
        所以升级信号看「名下还有没有悬着的单」，不看分类是否相同。"""
        s = _open(server)
        first = _submit(server, s, "菜凉了，我要退款")
        assert first["state"] == ComplaintState.SUBMITTED
        second = _submit(server, s, "汤也是凉的")
        assert second["handoff"]["code"] == "repeat_complaint"
        assert second["handoff"]["level"] == "human_now"
        assert "未结案" in second["handoff"]["reasons"][0]

    def test_已结案的旧单不背锅(self, server):
        """昨天投诉过不该让今天一开口就被当成惯犯。"""
        s = _open(server)
        first = _submit(server, s, "菜有点凉了")
        assert first["state"] == ComplaintState.AUTO_CLOSED
        second = _submit(server, s, "汤也是凉的")
        assert second["handoff"]["code"] != "repeat_complaint"

    def test_管理端视图带接管级别(self, server):
        s = _open(server)
        submitted = _submit(server, s, "菜里有头发")
        admin = login(server, "admin")
        rows = data_of(server, "GET", "/api/guest/complaints",
                       session=admin)["complaints"]
        row = next(c for c in rows if c["complaint_no"] == submitted["complaint_no"])
        assert row["handoff"]["level"] == "human_now"
        assert row["handoff"]["owner"] == "门店值班经理"
        assert row["handoff"]["reasons"]

    def test_审计与事件都记接管级别(self, server):
        s = _open(server)
        submitted = _submit(server, s, "菜里有头发")
        entry = server.state.audit.filter(action="guest.complaint")[-1]
        after = entry.after
        assert after["handoff"] == "human_now"
        assert after["handoff_code"] == "food_safety"
        rows = [r for r in server.state.events(since=0)
                if r.get("kind") == "complaint" and r.get("op") == "submit"]
        row = next(r for r in rows
                   if r["complaint_no"] == submitted["complaint_no"])
        assert row["handoff_level"] == "human_now"
        assert row["handoff_owner"] == "门店值班经理"

    def test_对话投诉分支带接管信息(self, server):
        s = _open(server)
        r = _chat(server, s, "菜里有头发")
        assert r["intent"] == "complaint"
        assert r["data"]["handoff"]["level"] == "human_now"
        assert "值班经理" in r["reply"]

    def test_出口真的会改写禁语(self, server, monkeypatch):
        """把 compose_reply 换成「模型嘴瓢」版，验证出口护栏接得住。"""
        from kitchen.guest import service as svc
        monkeypatch.setattr(
            svc, "compose_reply",
            lambda *a, **k: "马上就好，已经给您退了，热销第一")
        s = _open(server)
        r = _submit(server, s, "菜里有头发")
        assert "马上就好" not in r["ai_reply"]
        assert "已经给您退了" not in r["ai_reply"]
        assert "热销第一" not in r["ai_reply"]
        admin = login(server, "admin")
        rows = data_of(server, "GET", "/api/guest/complaints",
                       session=admin)["complaints"]
        row = next(c for c in rows if c["complaint_no"] == r["complaint_no"])
        terms = [x["term"] for x in row["handling"]["guard"]]
        assert "马上就好" in terms


class TestFoodSafetyContext:
    """审查 P2-3：材质/口味词不是食安信号，除非挨着「吃进嘴里」的语境。

    旧版把「打包用的是塑料盒吗」判成 human_now，值班经理被喊来处理包装咨询。
    这组用例钉住语境判据，防止有人把裸词加回 `_FOOD_SAFETY`。
    """

    @pytest.mark.parametrize("text", [
        "打包用的是塑料盒吗",
        "有没有酸味的菜",
        "上一单的玻璃瓶可乐没给",
        "这个塑料盒能回收吗",
    ])
    def test_材质与口味咨询不升食安(self, text):
        r = handoff.assess(text=text)
        assert r["code"] != "food_safety", f"{text} 被误判成食安事故"
        assert r["level"] != "human_now"

    @pytest.mark.parametrize("text", [
        "菜里有玻璃",
        "吃出塑料片",
        "这汤有酸味，是不是坏了",
        "喝到沙粒了",
        "头发掉进面里",
    ])
    def test_真食安仍立即接管(self, text):
        r = handoff.assess(text=text)
        assert r["code"] == "food_safety", f"{text} 漏判了食安事故"
        assert r["level"] == "human_now"

    def test_模型说不用人也不能降级(self):
        # 语境命中后，need_human=False 依然压不住确定性下限
        r = handoff.assess(text="菜里有玻璃", need_human=False)
        assert r["level"] == "human_now"
        assert r["overridden"] is True
