# -*- coding: utf-8 -*-
"""端到端：界面保存 → journal 落盘 → 「重启」→ 数据还在。

用两次 AppState 构造模拟服务重启（同一 var_dir），
走真实路由而不是直接调仓储，保证动作层的 journal 写入也被覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_support import call, data_of, force_session, make_state  # noqa: E402

from kitchen.api import AppState, AppStateConfig  # noqa: E402


def test_保存的菜与配方_跨重启不丢(tmp_path):
    var = tmp_path / "var"
    # ---------------- 第一次「运行」：界面保存
    state1 = AppState(config=AppStateConfig(var_dir=var), online=True)
    server1 = _server_of(state1)
    session = force_session("admin", "manager")

    data_of(server1, "POST", "/api/recipes/dishes", session=session,
            code="D-E2E", name="端到端菜", category="热菜", price=25.0)
    data_of(server1, "POST", "/api/recipes/3/versions", session=session,
            name="清炒时蔬 v1",
            items=[{"ingredient": "时蔬", "weight_g": 180.0}],
            steps=[{"step_no": 1, "title": "清炒", "instruction": "大火快炒",
                    "heat_level": "high", "duration_sec": 120, "ccp": True}])

    # ---------------- 「重启」：同一 var_dir 重建
    state2 = AppState(config=AppStateConfig(var_dir=var), online=True)
    server2 = _server_of(state2)
    lead = force_session("admin", "manager")

    recipes = data_of(server2, "GET", "/api/recipes", session=lead)["recipes"]
    target = next(row for row in recipes if row["dish_code"] == "D-E2E")
    assert target["dish"] == "端到端菜"
    veg = next(row for row in recipes if row["dish_id"] == 3)
    assert veg["recipe"] is not None
    assert veg["recipe"]["items"][0]["ingredient"] == "时蔬"
    assert veg["recipe"]["steps"][0]["ccp"] is True


def test_journal归档失败_保存仍生效且note如实提示(tmp_path, monkeypatch):
    """写盘失败不回滚内存、不炸接口——如实告诉用户「重启会丢」。"""
    state = make_state(tmp_path)
    server = _server_of(state)
    session = force_session("admin", "manager")

    # 把 journal 路径弄成不可写的目录：append 会失败
    state._journal_path = tmp_path / "no_such_dir" / "sub" / "journal.jsonl"
    (tmp_path / "no_such_dir").write_text("占位成文件，mkdir 必败\n")

    status, payload = call(server, "POST", "/api/recipes/dishes", session=session,
                           code="D-NOTE", name="提示语菜", price=3.0)
    assert status == 200 and payload["ok"] is True
    assert "重启后会丢失" in payload["data"]["note"]
    # 内存里仍然生效（三端立即可见）
    assert state.repo.dish_by_code("D-NOTE").name == "提示语菜"


def _server_of(state):
    from kitchen.api import create_server
    return create_server(state, port=0, secret="test-secret")
