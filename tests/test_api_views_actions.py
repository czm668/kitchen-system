# -*- coding: utf-8 -*-
"""读接口与写接口。

写接口的用例基本都按「**改状态 → 查审计 → 查副作用**」三步走：只断言返回值
是不够的，因为最严重的 bug 恰恰是「返回 200 但没真的改」（动作名写错、通道
串路、审计漏记）。
"""
from __future__ import annotations

import pytest

from api_support import call, data_of, login, make_server, make_state


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def admin(server):
    return login(server, "admin")


@pytest.fixture()
def lead(server):
    return login(server, "lead01")


# =========================================================== 读接口
class TestViews:
    def test_overview一次拿全(self, server, admin):
        data = data_of(server, "GET", "/api/overview", session=admin)
        for key in ("device_id", "channels", "degrade", "telemetry", "counts",
                    "guards", "event_seq", "profile"):
            assert key in data
        assert set(data["channels"]) >= {"CH1", "CH2", "CH3"}
        assert len(data["telemetry"]) == 12

    def test_遥测曲线定长(self, server, admin):
        state = server.state
        for i in range(30):
            state.tick()
        data = data_of(server, "GET", "/api/telemetry?points=5", session=admin)
        assert len(data["series"]["t"]) == 5
        # 时间戳必须单调，否则前端折线会来回折
        stamps = [row["ts"] for row in data["series"]["t"]]
        assert stamps == sorted(stamps)

    def test_遥测可只取指定指标(self, server, admin):
        server.state.tick()
        data = data_of(server, "GET", "/api/telemetry?metrics=gas_level,hood_temp",
                       session=admin)
        assert set(data["series"]) == {"gas_level", "hood_temp"}

    def test_未采样指标标记stale(self, server, admin):
        data = data_of(server, "GET", "/api/telemetry", session=admin)
        assert data["meta"]["t"]["stale"] is True
        server.state.tick()
        data = data_of(server, "GET", "/api/telemetry", session=admin)
        assert data["meta"]["t"]["stale"] is False
        assert data["meta"]["t"]["unit"] == "°C"

    def test_工位视图带点火结论(self, server, admin):
        data = data_of(server, "GET", "/api/stations", session=admin)
        assert data["total"] == 2
        station = data["stations"][0]
        assert station["state"] in ("idle", "reserved", "busy")
        assert "flame" in station and "text" in station["flame"]

    def test_空列表带note而不是报错(self, server, admin):
        data = data_of(server, "GET", "/api/orders", session=admin)
        assert data["total"] == 0
        assert data["note"], "空列表必须给一句说明，前端不该显示红色报错"

    def test_配方带CCP与重量区间(self, server, admin):
        data = data_of(server, "GET", "/api/recipes", session=admin)
        recipes = [row for row in data["recipes"] if row["recipe"]]
        assert recipes, "种子里必须有配方"
        recipe = recipes[0]["recipe"]
        assert recipe["items"] and recipe["steps"]
        item = recipe["items"][0]
        assert item["min_g"] <= item["weight_g"] <= item["max_g"]
        assert isinstance(recipe["ccp_count"], int)

    def test_健康证台账带到期预警(self, server, admin):
        data = data_of(server, "GET", "/api/staff/certificates", session=admin)
        assert data["total"] >= 1
        row = data["certificates"][0]
        assert {"staff_id", "days_left", "valid_today", "warn"} <= set(row)
        # 按剩余天数升序：最先过期的排最前，运维从上往下处理
        days = [item["days_left"] for item in data["certificates"]
                if item["days_left"] is not None]
        assert days == sorted(days)

    def test_晨检列出未提交的人(self, server, admin):
        data = data_of(server, "GET", "/api/morning", session=admin)
        assert data["total"] >= 1
        assert all(row["state"] in ("missing", "locked", "admitted", "pending")
                   for row in data["staff"])

    def test_人员视图带在岗与占用(self, server, admin):
        data = data_of(server, "GET", "/api/staff", session=admin)
        person = data["staff"][0]
        assert person["name"]
        assert isinstance(person["qualifications"], list)
        assert isinstance(person["assignments"], list)

    def test_批次与溯源码(self, server, admin):
        data = data_of(server, "GET", "/api/batches", session=admin)
        assert data["total"] >= 1
        assert len(data["trace_codes"]) >= 1  # 完整行，供管理端编辑
        assert data["trace_codes_total"] >= 1
        assert isinstance(data["suppliers"], list) and data["suppliers"]

    def test_KPI无样本时不下发0(self, server, admin):
        """**「0」和「没有样本」必须分开**：显示「出餐 0 分钟」是能被当场问住的。"""
        data = data_of(server, "GET", "/api/kpi", session=admin)
        cook = data["cook_duration"]
        assert cook["samples"] == 0
        assert cook["value"] is None
        assert cook["note"] == "暂无样本"
        assert data["disinfection"]["samples"] == 0
        assert data["disinfection"]["value"] is None

    def test_参数页带演示换算(self, server, admin):
        data = data_of(server, "GET", "/api/params", session=admin)
        assert data["total"] > 0
        demo = data["demo_map"]
        assert demo["time_scale"] == 1.0
        assert len(demo["rows"]) >= 5
        # 每条都要有「真实值」和「生效值」，这是 A7-09 的验收要求
        for row in demo["rows"]:
            assert {"item", "real_text", "effective_text", "note"} <= set(row)

    def test_互锁视图三通道(self, server, admin):
        data = data_of(server, "GET", "/api/interlock", session=admin)
        assert [row["channel"] for row in data["channels"]] == ["CH1", "CH2", "CH3"]
        assert data["restore_requires"]["ch3_double_review"] == ["web", "tft"]


# =========================================================== 订单链路
class TestOrderFlow:
    def test_下单到出餐全链路(self, server, admin):
        order = data_of(server, "POST", "/api/orders", session=admin,
                        items=[{"code": "D-QJRS", "qty": 1}])["order"]
        assert order["state"] == "created"
        item_id = order["items"][0]["id"]

        data_of(server, "POST", f"/api/orders/items/{item_id}/dispatch",
                session=admin, station_id=1)
        data_of(server, "POST", "/api/stations/occupy", session=admin,
                station_id=1, staff_id=1, item_id=item_id)
        data_of(server, "POST", f"/api/orders/items/{item_id}/state",
                session=admin, state="ready")
        data_of(server, "POST", "/api/stations/release", session=admin,
                station_id=1)
        data_of(server, "POST", f"/api/orders/items/{item_id}/state",
                session=admin, state="served")

        rows = data_of(server, "GET", "/api/orders", session=admin)
        state = rows["orders"][0]["items"][0]["state"]
        assert state == "served"

        # 出餐时长现在应该有样本了
        kpi = data_of(server, "GET", "/api/kpi", session=admin)
        assert kpi["cook_duration"]["samples"] >= 1
        assert kpi["cook_duration"]["value"] is not None

    def test_退不了的迁移被拒(self, server, admin):
        order = data_of(server, "POST", "/api/orders", session=admin,
                        items=[{"code": "D-QJRS", "qty": 1}])["order"]
        item_id = order["items"][0]["id"]
        status, payload = call(server, "POST", f"/api/orders/items/{item_id}/state",
                               session=admin, state="served")
        assert status == 409, "pending 不能直接 served"
        assert payload["error"] == "InvalidTransition"

    def test_沽清菜下不了单(self, server, admin):
        status, payload = call(server, "POST", "/api/orders", session=admin,
                               items=[{"code": "D-QGD", "qty": 1}])
        assert status == 409
        assert payload["error"] == "SoldOutError"

    def test_同一工位不能被两个人占(self, server, admin):
        data_of(server, "POST", "/api/stations/occupy", session=admin,
                station_id=1, staff_id=1)
        status, payload = call(server, "POST", "/api/stations/occupy",
                               session=admin, station_id=1, staff_id=2)
        assert status == 400
        assert "StationBusyError" in payload["error"]

    def test_催菜会记录次数(self, server, admin):
        order = data_of(server, "POST", "/api/orders", session=admin,
                        items=[{"code": "D-QJRS", "qty": 1}])["order"]
        item_id = order["items"][0]["id"]
        data = data_of(server, "POST", f"/api/orders/items/{item_id}/urge",
                       session=admin)
        assert data["urge_count"] == 1
        assert server.state.audit.filter(action="order.urge")

    def test_下单幂等(self, server, admin):
        first = data_of(server, "POST", "/api/orders", session=admin,
                        items=[{"code": "D-QJRS", "qty": 1}],
                        client_event_id="evt-1")["order"]
        second = data_of(server, "POST", "/api/orders", session=admin,
                         items=[{"code": "D-QJRS", "qty": 1}],
                         client_event_id="evt-1")["order"]
        assert first["id"] == second["id"]

    def test_沽清开关(self, server, admin):
        data = data_of(server, "POST", "/api/dishes/1/soldout", session=admin,
                       sold_out=True)
        assert data["dish"]["sold_out"] is True
        entries = server.state.audit.filter(action="dish.soldout")
        assert entries[-1].before == {"sold_out": False}
        assert entries[-1].after == {"sold_out": True}

    def test_甘特含占用段与时长(self, server, admin):
        data_of(server, "POST", "/api/stations/occupy", session=admin,
                station_id=1, staff_id=1)
        data = data_of(server, "GET", "/api/stations/gantt", session=admin)
        assert data["total"] == 1
        segment = data["segments"][0]
        assert segment["station_id"] == 1
        assert segment["end_epoch"] is None, "还没释放的段没有结束时间"
        assert segment["elapsed_sec"] >= 0


# =========================================================== 通道与联锁
class TestChannelActions:
    def test_触发立刻登记(self, server, admin):
        data = data_of(server, "POST", "/api/channels/CH1/actions", session=admin,
                       action="gas_valve_close")
        assert data["channel"] == "CH1"
        assert data["state"] in ("latched", "manual_reset_required")

    def test_恢复必须人工确认(self, server, admin):
        data_of(server, "POST", "/api/channels/CH1/actions", session=admin,
                action="gas_valve_close")
        status, payload = call(server, "POST", "/api/channels/CH1/actions",
                               session=admin, action="gas_valve_open")
        assert status == 409
        assert payload["error"] == "InterlockBlocked"

    def test_有触发源时确认了也不给恢复(self, server, admin):
        """**这条最容易被写错**：只校验 `manual_confirmed` 不校验触发源，
        等于「点一下确认就把泄漏中的燃气阀打开」。"""
        data_of(server, "POST", "/api/channels/CH2/actions", session=admin,
                action="fan_on")
        status, payload = call(server, "POST", "/api/channels/CH2/actions",
                               session=admin, action="fan_off",
                               manual_confirmed=True)
        assert status == 409
        assert "仍有未恢复触发源" in payload["detail"]

    def test_CH3需要双通道复核(self, server, admin, lead):
        data_of(server, "POST", "/api/channels/CH3/actions", session=admin,
                action="power_cut")
        # 触发源先注销（视觉违规消失 / 人工确认现场已处置）——这是复位的**前置**
        # 条件；不注销直接复位，任何通道都只会报「仍有未恢复触发源」
        server.state.controller.note_recovered("CH3", "web:admin")
        # 只有 web 一票 → 还是不给恢复
        data_of(server, "POST", "/api/channels/CH3/review", session=lead,
                channel="CH3")
        status, payload = call(server, "POST", "/api/channels/CH3/actions",
                               session=admin, action="power_restore",
                               manual_confirmed=True)
        assert status == 409
        assert "双通道" in payload["detail"]

    def test_声光不受互锁抑制(self, server, admin):
        data_of(server, "POST", "/api/channels/CH3/actions", session=admin,
                action="power_cut")
        data = data_of(server, "POST", "/api/channels/GPIO_SIGNAL/actions",
                       session=admin, action="alarm_light_on")
        assert data["channel"] == "GPIO_SIGNAL"

    def test_未知动作被拒(self, server, admin):
        status, payload = call(server, "POST", "/api/channels/CH1/actions",
                               session=admin, action="valve_close")
        assert status == 400
        assert payload["error"] == "UnknownActionError"

    def test_失败的复位也进审计(self, server, admin):
        data_of(server, "POST", "/api/channels/CH1/actions", session=admin,
                action="gas_valve_close")
        call(server, "POST", "/api/channels/CH1/actions", session=admin,
             action="gas_valve_open", manual_confirmed=True)
        entries = server.state.audit.filter(action="channel.action")
        assert [entry.result for entry in entries] == ["ok", "blocked"]


# =========================================================== 参数与兜底
class TestParams:
    def test_改阈值留下改前改后(self, server, admin):
        before = data_of(server, "GET", "/api/params", session=admin)
        rule = next(row for row in before["rules"] if row["upper_bound"] is not None)
        data = data_of(server, "PUT", "/api/params", session=admin,
                       rules=[{"id": rule["id"], "upper_bound": 123.0}])
        key = f"{rule['id']}.upper_bound"
        assert data["changed"][key] == [rule["upper_bound"], 123.0]
        entry = server.state.audit.filter(action="param.save")[-1]
        assert entry.before[key] == rule["upper_bound"]
        assert entry.after[key] == 123.0

    def test_没变化时不写审计(self, server, admin):
        before = data_of(server, "GET", "/api/params", session=admin)
        rule = next(row for row in before["rules"] if row["upper_bound"] is not None)
        data = data_of(server, "PUT", "/api/params", session=admin,
                       rules=[{"id": rule["id"], "upper_bound": rule["upper_bound"]}])
        assert data["changed"] == {}
        assert not server.state.audit.filter(action="param.save")

    def test_未知阈值id被拒(self, server, admin):
        status, payload = call(server, "PUT", "/api/params", session=admin,
                               rules=[{"id": "no-such-rule", "level": 2}])
        assert status == 400
        assert "未知阈值" in payload["detail"]

    def test_加速因子改变换算表(self, server, admin):
        data = data_of(server, "POST", "/api/params/time-scale", session=admin,
                       time_scale=60)
        assert data["time_scale"] == 60
        row = next(item for item in data["rows"] if "#3 长烧" in item["item"])
        assert row["real_sec"] == 3600.0
        assert row["effective_sec"] == pytest.approx(60.0)
        # 展示文案按「人话」给：3600 秒 = 1.0 小时，压缩后 = 1 分钟
        assert row["real_text"] == "1 小时"
        assert row["effective_text"] == "1 分钟"

    def test_加速不改阈值只改时间轴(self, server, admin):
        before = data_of(server, "GET", "/api/params", session=admin)
        data_of(server, "POST", "/api/params/time-scale", session=admin,
                time_scale=60)
        after = data_of(server, "GET", "/api/params", session=admin)
        assert before["rules"] == after["rules"], "演示加速绝不能改阈值"
        assert "没有任何一条因为演示被改小" in after["demo_map"]["hard_rule"]

    def test_非法加速因子被拒(self, server, admin):
        assert call(server, "POST", "/api/params/time-scale", session=admin,
                    time_scale=0)[0] == 400

    def test_断网后降级报告说是离线(self, server, admin):
        data = data_of(server, "POST", "/api/network", session=admin, online=False)
        assert data["online"] is False
        report = data_of(server, "GET", "/api/degrade", session=admin)
        assert report["severity"] == "offline"
        assert "本地保护仍生效" in report["headline"]

    def test_断网期间仍然保护(self, server, admin):
        """验收项：拔网 30 秒，三条保护仍然生效。这里用一帧超限气体验证。"""
        data_of(server, "POST", "/api/network", session=admin, online=False)
        state = server.state
        for _ in range(2):
            state.tick({"t": 24.0, "h": 50.0, "gas_level": 0.9, "hood_temp": 60.0,
                        "sterilize_temp": 40.0, "fridge_temp": 4.0,
                        "freezer_temp": -18.0, "sample_temp": 3.0,
                        "sct_current": 0.5, "weight_g": 100.0,
                        "flame_a": 0.0, "flame_b": 0.0})
        overview = data_of(server, "GET", "/api/overview", session=admin)
        assert "guard:gas_surge" in overview["guards"]["tripped"]
        assert overview["counts"]["queue"] >= 1, "离线期间数据必须进本地队列"

    def test_恢复联网自动补传(self, server, admin):
        data_of(server, "POST", "/api/network", session=admin, online=False)
        state = server.state
        state.tick({"gas_level": 0.06, "t": 24.0, "h": 50.0, "hood_temp": 60.0,
                    "sterilize_temp": 40.0, "fridge_temp": 4.0,
                    "freezer_temp": -18.0, "sample_temp": 3.0,
                    "sct_current": 0.5, "weight_g": 100.0,
                    "flame_a": 0.0, "flame_b": 0.0})
        data = data_of(server, "POST", "/api/network", session=admin, online=True)
        assert data["replay"] is not None
        assert data["queued"] == 0

    def test_补传两次不重复入库(self, server, admin):
        """同一批数据补传两遍，服务端行数不变（唯一键 `(device_id, seq)`）。"""
        data_of(server, "POST", "/api/network", session=admin, online=False)
        server.state.tick({"gas_level": 0.06})
        server.state.tick({"gas_level": 0.07})
        # 离线时 `flush()` 是空操作（不碰网络），所以要先恢复联网
        first = data_of(server, "POST", "/api/network", session=admin, online=True)
        assert first["replay"]["accepted"] >= 2
        assert first["queued"] == 0
        snapshot = data_of(server, "POST", "/api/admin/flush", session=admin)
        assert snapshot["queued"] == 0
        # 再补传一次：队列已空，服务端行数不能涨（唯一键去重）
        second = data_of(server, "POST", "/api/admin/flush", session=admin)
        assert second["store_rows"] == snapshot["store_rows"]


# =========================================================== 演示与审计
class TestDemoAndAudit:
    def test_造演示订单跳过沽清(self, server, admin):
        data = data_of(server, "POST", "/api/demo/orders", session=admin)
        assert data["created"] >= 1
        assert isinstance(data["skipped"], list)

    def test_信号注入产出告警(self, server, admin):
        data = data_of(server, "POST", "/api/demo/inject", session=admin,
                       metrics={"gas_level": 0.95, "t": 24.0, "h": 50.0,
                                "hood_temp": 60.0, "sterilize_temp": 40.0,
                                "fridge_temp": 4.0, "freezer_temp": -18.0,
                                "sample_temp": 3.0, "sct_current": 0.5,
                                "weight_g": 100.0, "flame_a": 0.0,
                                "flame_b": 0.0})
        assert data["injected"]["gas_level"] == 0.95
        # 注入也要留痕，否则事后分不清「真报警」还是「演出来的」
        assert server.state.audit.filter(action="demo.inject")

    def test_刷卡被拒也记录(self, server, admin):
        data = data_of(server, "POST", "/api/admission/scan", session=admin,
                       card_uid="不存在的卡")
        assert data["ok"] is False
        entry = server.state.audit.filter(action="admission.scan")[-1]
        assert entry.result == "denied"

    def test_审计条目可筛选(self, server, admin):
        data_of(server, "POST", "/api/demo/orders", session=admin)
        data = data_of(server, "GET", "/api/audit?action=demo.seed_orders",
                       session=admin)
        assert data["returned"] >= 1
        assert all(row["action"] == "demo.seed_orders" for row in data["entries"])

    def test_事件流有增量序号(self, server, admin):
        state = server.state
        state.tick()
        first = state.event_seq
        state.tick()
        rows = state.events(since=first - 1)
        assert rows
        assert all(row["seq"] > first - 1 for row in rows)
        assert rows[-1]["seq"] == state.event_seq
