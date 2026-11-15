# -*- coding: utf-8 -*-
"""A8 后厨 TFT 终端（模拟版）。

覆盖四条主线：
1. **播报**：事件流 → 语音编号（新单响、超差响、合格不响、去抖）；
2. **离线队列**：断网仍能看队列（缓存快照）+ 刷卡/工序动作入队落盘；
3. **恢复同步**：`ack` 回放，业务拒绝（4xx）不重试进 denied，网络失败留队列；
4. **弱校验方向**：离线刷卡「未知卡/证无效」直接红拒——宁可误拒不放行。
"""
from __future__ import annotations

import time

import pytest

from api_support import call, data_of, login, make_server
from kitchen.tft import (
    DirectTransport,
    TftTerminal,
    build_status,
    render_frame,
)
from kitchen.tft.audio import DfPlayerSim, announce_events, VOICE_TABLE


# =========================================================== 播报
class TestVoice:
    def test_编号表_只增不改(self):
        # 固件烧录后 mp3 与编号绑定；编号被改等于让真机播错话
        nos = sorted(track.no for track in VOICE_TABLE.values())
        assert nos == list(range(1, 13))
        assert len({track.name for track in VOICE_TABLE.values()}) == len(nos)

    def test_新单与报警会播(self):
        player = DfPlayerSim()
        fired = announce_events([
            {"kind": "order", "op": "created"},
            {"kind": "rule", "event": "over", "level": "warn"},
        ], player)
        assert fired == ["new_order", "alarm"]

    def test_称重合格不播_超差播(self):
        player = DfPlayerSim()
        fired = announce_events([
            {"kind": "cooking", "op": "weigh", "ok": True},
            {"kind": "cooking", "op": "weigh", "ok": False},
        ], player)
        assert fired == ["weigh_bad"]

    def test_同一编号三秒内去抖(self):
        player = DfPlayerSim(replay_gap=3.0)
        assert player.play("alarm", now=100.0) is True
        assert player.play("alarm", now=101.0) is False   # 报警抖动重发
        assert player.play("alarm", now=104.0) is True

    def test_步骤确认与演示注入不播(self):
        player = DfPlayerSim()
        fired = announce_events([
            {"kind": "cooking", "op": "sop"},
            {"kind": "inject", "op": "telemetry"},
        ], player)
        assert fired == []


# =========================================================== 终端在线链路
@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def chef(server):
    return login(server, "chef01")


@pytest.fixture()
def lead(server):
    return login(server, "lead01")


@pytest.fixture()
def terminal(server, tmp_path):
    term = TftTerminal(
        DirectTransport(server),
        device_id="tft-node-01",
        queue_dir=tmp_path / "tft",
    )
    term.bind_session(login(server, "chef01"))
    term.poll()   # 拉白名单 + 订单快照
    return term


def _make_item(server, session, code="D-QJRS") -> int:
    order = data_of(server, "POST", "/api/orders", session=session,
                    items=[{"code": code, "qty": 1}], source="miniapp")["order"]
    item_id = order["items"][0]["id"]
    data_of(server, "POST", f"/api/orders/items/{item_id}/dispatch",
            session=session, station_id=1)
    data_of(server, "POST", f"/api/orders/items/{item_id}/state",
            session=session, state="cooking")
    return item_id


def _admit(server, lead, card="A1B2C3D4"):
    """提交健康晨检问卷，让张三当天准入可用（无症状 = 无需复核直接放行）。"""
    data_of(server, "POST", "/api/morning/submit", session=lead,
            card_uid=card, answers={"temp_c": 36.2, "diarrhea": False,
                                    "pharyngitis": False, "hand_wound": False})


class TestTerminalOnline:
    def test_新单事件驱动播报(self, server, chef, terminal):
        _make_item(server, chef)
        result = terminal.poll()
        assert result["synced"] is True
        assert "new_order" in terminal.player.spoken()

    def test_在线刷卡走服务端强校验(self, server, chef, lead, terminal):
        _admit(server, lead)
        result = terminal.scan("A1B2C3D4", station_id=1)
        assert result["ok"] is True
        assert "张三" in result["detail"]
        assert result.get("offline_local") is not True

    def test_证过期在线被拒(self, terminal):
        result = terminal.scan("E5F6A7B8", station_id=1)
        assert result["ok"] is False

    def test_工序屏拉到当前步(self, server, chef, terminal):
        item_id = _make_item(server, chef)
        screen = terminal.screen_steps(item_id)
        assert screen["dish"] == "青椒肉丝"
        assert screen["next_step_no"] == 1
        states = [step["state"] for step in screen["steps"]]
        assert states[0] == "current"


# =========================================================== 离线（A8-05）
class TestOffline:
    def test_离线队列屏仍可看缓存快照(self, server, chef, terminal):
        _make_item(server, chef)
        terminal.poll()
        terminal.set_link(False)
        screen = terminal.screen_queue()
        assert screen["status"]["online"] is False
        assert screen["status"]["banner"] == "offline"
        assert screen["rows"]          # 缓存里的待配任务
        text = render_frame(screen)
        assert "离线模式" in text

    def test_离线刷卡白名单放行入队(self, server, chef, lead, terminal):
        _admit(server, lead)
        terminal.poll()               # 刷新白名单缓存
        terminal.set_link(False)
        result = terminal.scan("A1B2C3D4", station_id=1)
        assert result["ok"] is True
        assert result["offline_local"] is True
        assert len(terminal.queue) == 1
        text = render_frame(terminal.screen_card(result))
        assert "离线放行" in text

    def test_离线未知卡红拒不入队(self, terminal):
        terminal.set_link(False)
        result = terminal.scan("DEADBEEF", station_id=1)
        assert result["ok"] is False
        assert len(terminal.queue) == 0

    def test_离线证过期红拒不入队(self, terminal):
        terminal.set_link(False)
        result = terminal.scan("E5F6A7B8", station_id=1)
        assert result["ok"] is False
        assert "证" in result["detail"]
        assert len(terminal.queue) == 0

    def test_离线工序确认入队(self, server, chef, terminal):
        item_id = _make_item(server, chef)
        terminal.set_link(False)
        result = terminal.confirm(item_id, step_no=1, staff_id=1)
        assert result["queued"] is True
        assert len(terminal.queue) == 1


# =========================================================== 恢复同步
class TestSync:
    def test_恢复后回放_动作生效(self, server, chef, lead, terminal):
        _admit(server, lead)
        terminal.poll()
        terminal.set_link(False)
        terminal.scan("A1B2C3D4", station_id=1)
        assert len(terminal.queue) == 1
        terminal.set_link(True)      # 触发 sync
        assert len(terminal.queue) == 0
        assert terminal.denied == []
        # 服务端收到了回放的刷卡（离线那次不发审计，回放时才落到服务端）
        actions = [row.action for row in server.state.audit.entries()]
        assert actions.count("admission.scan") == 1

    def test_业务拒绝进denied不重试(self, server, chef, terminal):
        item_id = _make_item(server, chef)
        terminal.set_link(False)
        # 离线入队一条「跳步确认」（服务端必拒：顺序错误 400）
        terminal.confirm(item_id, step_no=3, staff_id=1)
        terminal.set_link(True)
        assert len(terminal.queue) == 0
        assert len(terminal.denied) == 1
        assert terminal.denied[0]["status"] in (400, 409)
        assert "card_denied" in terminal.player.spoken()

    def test_队列动作断电不丢(self, server, tmp_path):
        """入队即落盘（jsonl）：换一个终端实例（模拟重启）队列还在。"""
        term = TftTerminal(DirectTransport(server),
                           queue_dir=tmp_path / "tft")
        term.bind_session(login(server, "chef01"))
        term.set_link(False)
        term.confirm(1, step_no=1, staff_id=1)
        reborn = TftTerminal(DirectTransport(server),
                             queue_dir=tmp_path / "tft")
        assert len(reborn.queue) == 1

    def test_恢复播报与离线播报(self, terminal):
        terminal.set_link(False)
        assert "offline" in terminal.player.spoken()
        terminal.set_link(True)
        assert "replay" in terminal.player.spoken()


# =========================================================== 帧模型
class TestScreens:
    def test_状态栏离线提示待同步数(self):
        status = build_status(clock_text="12:00:00", device_id="tft-node-01",
                               online=False, offline_sec=42.5,
                               queue_pending=3, pending_actions=2)
        assert status["banner"] == "offline"
        assert status["offline_sec"] == 42.5
        assert "待同步动作 2 条" in status["hint"]

    def test_队列超行截断(self, terminal):
        from kitchen.tft.screens import build_queue_screen
        rows = [{"item_id": i, "dish": f"菜{i}", "order_no": f"O{i}",
                 "qty": 1, "urge": 0, "station_id": 1} for i in range(15)]
        screen = build_queue_screen(pending=rows, cooking=[],
                                    status=terminal.status_bar())
        assert len(screen["rows"]) == 10
        assert screen["truncated"] == 5

    def test_帧高度定长22行(self, terminal):
        # 22 行帧 = 21 个换行（尾部空行 join 后不带结尾 \n，不能用 splitlines 数）
        text = render_frame(terminal.screen_queue())
        assert text.count("\n") == 21
