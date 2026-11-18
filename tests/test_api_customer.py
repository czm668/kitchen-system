# -*- coding: utf-8 -*-
"""A9 顾客通道：开桌/菜单/下单/进度/催菜限频/归属隔离/溯源/通知。

顾客通道的测试重点不是「下单能不能成功」（那是 OrderService 的事），而是
**顾客视角的边界**：只能看自己的单、催菜限频、溯源公开但只读、员工令牌
进不来这条通道的身份混淆。
"""
from __future__ import annotations

import pytest

from api_support import call, data_of, login, make_server, make_state  # noqa: F401


@pytest.fixture()
def server(tmp_path):
    return make_server(tmp_path)


@pytest.fixture()
def chef(server):
    return login(server, "chef01")


# ----------------------------------------------------------------- 开桌
class TestSession:
    def test_开桌发令牌且可重复开(self, server):
        first = data_of(server, "POST", "/api/customer/session")
        second = data_of(server, "POST", "/api/customer/session")
        assert first["customer_token"] != second["customer_token"]
        assert len(first["customer_token"]) >= 16

    def test_换var目录令牌失效(self, tmp_path):
        """令牌摘要持久化在各自 var_dir：换一个目录（相当于换一套数据）就查不到。
        同目录重启后令牌仍有效——见 TestSessionPersistence。
        注意**不调 stop()**：`make_server` 不起 serve 线程，`shutdown()` 会
        永久阻塞（http.server 的经典坑）。"""
        first = make_server(tmp_path)
        token = data_of(first, "POST", "/api/customer/session")["customer_token"]

        second = make_server(tmp_path / "again")
        status, payload = call(second, "GET", "/api/customer/orders",
                               customer_token=token)
        assert status == 401

    def test_无令牌查单401(self, server):
        status, payload = call(server, "GET", "/api/customer/orders")
        assert status == 401
        assert "重新开桌" in payload.get("detail", "")


class TestSessionPersistence:
    """顾客会话跨重启持久化：token 与订单归属不丢（否则重启后顾客看不到自己的单）。"""

    def test_同目录重启后令牌仍有效且会话档案恢复(self, tmp_path):
        """重启后令牌不再 401，脱敏编号/桌号/guest_key 随会话恢复。

        边界：裸 order_id 归属**刻意不回放**——订单本体纯内存、id 每进程
        从 seed 重分配，回放旧 id 会撞号造成越权催菜。订单能否显示取决于
        订单系统自身的持久化，是另一个设计边界；这里保证的是会话层不 401。
        """
        first = make_server(tmp_path)
        session = data_of(first, "POST", "/api/customer/session",
                          table_id=3, guest_key="gk-persist")
        token = session["customer_token"]
        menu = data_of(first, "GET", "/api/customer/menu")
        code = menu["dishes"][0]["code"]
        data_of(first, "POST", "/api/customer/orders",
                customer_token=token, items=[{"code": code, "qty": 1}])

        # 同一 var_dir 重建 = 模拟服务重启
        second = make_server(tmp_path)
        status, payload = call(second, "GET", "/api/customer/orders",
                               customer_token=token)
        assert status == 200, payload          # 关键：不再 401
        after = payload["data"]
        # 脱敏编号/桌号/guest_key 随会话恢复
        assert after["customer_ref"] == session["customer_ref"]
        assert after["table_id"] == 3
        assert second.state.customer.profile_of(token)["guest_key"] == "gk-persist"
        # 旧 order_id 不回放：新进程归属簿为空，绝不撞号
        assert second.state.customer.order_ids_of(token) == set()
        assert second.state.customer.journal_stats["applied"] >= 1

    def test_重启后新订单归属不串到旧会话(self, tmp_path):
        """回归：回放的旧 order_id 不得让新会话的催菜被误判为不属于自己。"""
        first = make_server(tmp_path)
        data_of(first, "POST", "/api/customer/session", guest_key="gk-old")
        menu = data_of(first, "GET", "/api/customer/menu")
        code = menu["dishes"][0]["code"]
        # 旧会话下过一单（order_id=1 进了 journal）
        s = data_of(first, "POST", "/api/customer/session", guest_key="gk-old2")
        data_of(first, "POST", "/api/customer/orders",
                customer_token=s["customer_token"],
                items=[{"code": code, "qty": 1}])

        second = make_server(tmp_path)
        # 新进程里开新桌、下新单（id 从 seed 重新分配，大概率也是 1）
        s2 = data_of(second, "POST", "/api/customer/session", guest_key="gk-new")
        data_of(second, "POST", "/api/customer/orders",
                customer_token=s2["customer_token"],
                items=[{"code": code, "qty": 2}])
        st, p = call(second, "GET", "/api/customer/orders",
                     customer_token=s2["customer_token"])
        item = p["data"]["orders"][0]["items"][0]
        # 催自己的新单应成功，而不是被回放的旧归属撞成 404
        st, p = call(second, "POST",
                     f"/api/customer/orders/items/{item['id']}/urge",
                     customer_token=s2["customer_token"])
        assert st == 200, p

    def test_重启后guest_key双因子校验仍生效(self, tmp_path):
        first = make_server(tmp_path)
        session = data_of(first, "POST", "/api/customer/session",
                          guest_key="gk-right")
        token = session["customer_token"]
        second = make_server(tmp_path)
        # 令牌对、guest_key 错 → 仍拒绝（持久化没削弱安全边界）
        status, _ = call(second, "GET", "/api/customer/guest/profile",
                         customer_token=token, guest_key="gk-wrong")
        assert status == 401
        status, payload = call(second, "GET", "/api/customer/guest/profile",
                               customer_token=token, guest_key="gk-right")
        assert status == 200, payload

    def test_journal不落明文令牌(self, tmp_path):
        first = make_server(tmp_path)
        session = data_of(first, "POST", "/api/customer/session",
                          guest_key="gk-noleak")
        token = session["customer_token"]
        menu = data_of(first, "GET", "/api/customer/menu")
        data_of(first, "POST", "/api/customer/orders", customer_token=token,
                items=[{"code": menu["dishes"][0]["code"], "qty": 1}])
        journal = (tmp_path / "var" / "customer_journal.jsonl")
        text = journal.read_text(encoding="utf-8")
        assert token not in text, "journal 不得出现明文令牌"
        assert "token_sha256" in text


# ----------------------------------------------------------------- 菜单
class TestMenu:
    def test_菜单不含停售菜与内部字段(self, server):
        data = data_of(server, "GET", "/api/customer/menu")
        assert data["total"] > 0
        for row in data["dishes"]:
            # 顾客不需要、也不该看到后厨的配方/工位编排
            assert "recipe_id" not in row
            assert "station_kind" not in row
            assert {"id", "code", "name", "category",
                    "price", "sold_out", "estimate_minutes"} <= set(row)

    def test_菜单匿名可看(self, server):
        status, _ = call(server, "GET", "/api/customer/menu")
        assert status == 200

    def test_全品类菜单分类齐全(self, server):
        """全品类种子：7 大品类都有菜，前端据此自动生成分类导航。"""
        data = data_of(server, "GET", "/api/customer/menu")
        cats = {row["category"] for row in data["dishes"]}
        assert {"热菜", "凉菜", "素菜", "汤羹", "主食", "甜品", "饮品"} <= cats
        assert data["total"] >= 24

    def test_过期批次菜标记下架并说明原因(self, server):
        """酸辣土豆丝(D-SLTD)唯一批次 B-DEMO-004 已过期 → 菜单标沽清+原因。"""
        rows = {r["code"]: r for r in data_of(server, "GET", "/api/customer/menu")["dishes"]}
        sltd = rows["D-SLTD"]
        assert sltd["sold_out"] is True
        assert "保质期" in sltd["unavailable_reason"]
        # 有新鲜批次的菜照常卖
        assert rows["D-QJRS"]["sold_out"] is False
        # 有新鲜批次的全品类菜照常卖（不因溯源信息被误判下架）
        assert rows["D-HSJR"]["sold_out"] is False


# ----------------------------------------------------------------- 下单
class TestPlace:
    def test_顾客下单归属自己(self, server):
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        order = data_of(server, "POST", "/api/customer/orders",
                        customer_token=token, table_id=3,
                        items=[{"code": "D-QJRS", "qty": 1}])
        assert order["order_no"].startswith("O")
        mine = data_of(server, "GET", "/api/customer/orders",
                       customer_token=token)
        assert mine["total"] == 1
        assert mine["orders"][0]["order_no"] == order["order_no"]
        # 进度字段齐全：三档 eta_state 直接给前端画条
        item = mine["orders"][0]["items"][0]
        assert item["eta_state"] in ("cooking", "soon", "done")
        assert isinstance(item["remain_minutes"], int)

    def test_两桌互相看不见对方的单(self, server):
        """归属隔离是这条通道的安全底线：B 桌查不到 A 桌的任何字段。"""
        token_a = data_of(server, "POST", "/api/customer/session")["customer_token"]
        token_b = data_of(server, "POST", "/api/customer/session")["customer_token"]
        data_of(server, "POST", "/api/customer/orders", customer_token=token_a,
                items=[{"code": "D-QJRS", "qty": 1}])
        mine_b = data_of(server, "GET", "/api/customer/orders",
                         customer_token=token_b)
        assert mine_b["total"] == 0

    def test_空items拒400(self, server):
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        status, payload = call(server, "POST", "/api/customer/orders",
                               customer_token=token, items=[])
        assert status == 400

    def test_沽清整单拒绝并列出菜名(self, server, chef):
        """复用 OrderService 的沽清口径：整单 409 + 全部沽清项。"""
        lead = login(server, "lead01")
        data_of(server, "POST", "/api/dishes/1/soldout", session=lead,
                sold_out=True)
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        status, payload = call(server, "POST", "/api/customer/orders",
                               customer_token=token,
                               items=[{"code": "D-QJRS", "qty": 1}])
        assert status == 409
        assert "青椒肉丝" in payload.get("detail", "")

    def test_过期批次菜下单被拦截(self, server):
        """菜单隐藏还不够：直接调下单接口也拦，避免绕过菜单买到过期食材。"""
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        status, payload = call(server, "POST", "/api/customer/orders",
                               customer_token=token,
                               items=[{"code": "D-SLTD", "qty": 1}])
        assert status == 409
        assert "酸辣土豆丝" in payload.get("detail", "")
        # 确认没有污染归属簿：这单没成，查单应为空
        mine = data_of(server, "GET", "/api/customer/orders", customer_token=token)
        assert mine["total"] == 0

    def test_顾客单对员工可见_同一状态机(self, server, chef):
        """顾客下的单出现在员工 /api/orders 里（来源 miniapp）——
        三端共用一条状态机，不是顾客单单独一套。"""
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        order = data_of(server, "POST", "/api/customer/orders",
                        customer_token=token,
                        items=[{"code": "D-QJRS", "qty": 1}])
        staff = data_of(server, "GET", "/api/orders", session=chef)
        assert any(row["order_no"] == order["order_no"] for row in staff["orders"])


# ----------------------------------------------------------------- 催菜
class TestUrge:
    @pytest.fixture()
    def placed(self, server):
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        order = data_of(server, "POST", "/api/customer/orders",
                        customer_token=token,
                        items=[{"code": "D-QJRS", "qty": 1}])
        mine = data_of(server, "GET", "/api/customer/orders",
                       customer_token=token)
        item_id = mine["orders"][0]["items"][0]["id"]
        return token, item_id, order

    def test_催菜成功计数_加(self, server, placed):
        token, item_id, _ = placed
        data = data_of(server, "POST",
                       f"/api/customer/orders/items/{item_id}/urge",
                       customer_token=token)
        assert data["urge_count"] == 1

    def test_30秒内二次催菜429(self, server, placed):
        token, item_id, _ = placed
        data_of(server, "POST", f"/api/customer/orders/items/{item_id}/urge",
                customer_token=token)
        status, payload = call(server, "POST",
                               f"/api/customer/orders/items/{item_id}/urge",
                               customer_token=token)
        assert status == 429
        assert "30" in payload.get("detail", "")

    def test_催别人的单404_不泄漏存在性(self, server, placed):
        """A 桌的单，B 桌拿 item_id 直接催：404（而不是 403——403 等于
        告诉陌生人「这单存在但不是你的」）。"""
        token, item_id, _ = placed
        other = data_of(server, "POST", "/api/customer/session")["customer_token"]
        status, _ = call(server, "POST",
                         f"/api/customer/orders/items/{item_id}/urge",
                         customer_token=other)
        assert status == 404

    def test_顾客催菜进审计与事件流(self, server, placed):
        token, item_id, _ = placed
        data_of(server, "POST", f"/api/customer/orders/items/{item_id}/urge",
                customer_token=token)
        rows = server.state.audit.entries()
        assert any(row.action == "customer.order" for row in rows)
        events = server.state.events()
        assert any(row["kind"] == "order" and row.get("urge") == 1
                   for row in events)


# ----------------------------------------------------------------- 通知
class TestNotifications:
    def test_下单催菜产生通知且只给自己(self, server):
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        other = data_of(server, "POST", "/api/customer/session")["customer_token"]
        data_of(server, "POST", "/api/customer/orders", customer_token=token,
                items=[{"code": "D-QJRS", "qty": 1}])
        mine = data_of(server, "GET", "/api/customer/notifications",
                       customer_token=token)
        assert mine["total"] >= 1
        assert any("受理" in n["message"] for n in mine["notifications"])
        # 通知也做归属过滤
        theirs = data_of(server, "GET", "/api/customer/notifications",
                         customer_token=other)
        assert theirs["total"] == 0

    def test_since增量拉取(self, server):
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        data_of(server, "POST", "/api/customer/orders", customer_token=token,
                items=[{"code": "D-QJRS", "qty": 1}])
        first = data_of(server, "GET", "/api/customer/notifications",
                        customer_token=token)
        last_seq = first["notifications"][-1]["seq"]
        again = data_of(server, "GET", "/api/customer/notifications",
                        customer_token=token, since=last_seq)
        assert again["total"] == 0

    def test_环境报警不进顾客通知(self, server):
        """顾客只关心自己的单；燃气报警/遥测事件一概不给（避免骚扰，
        也避免把后厨内部状态暴露给顾客端）。"""
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        server.state.emit("alarm", {"metric": "gas_level", "value": 0.9})
        data = data_of(server, "GET", "/api/customer/notifications",
                       customer_token=token)
        assert all(n["kind"] != "alarm" for n in data["notifications"])


# ----------------------------------------------------------------- 溯源
class TestTrace:
    def test_公开溯源查询含批次供应商与检测(self, server):
        code = "TC-DEMO-0001"   # 种子固定码
        data = data_of(server, "GET", "/api/customer/trace", code=code)
        assert data["code"] == code
        assert data["dish"] == "青椒肉丝"
        assert data["supplier"] == "鲜丰肉联厂"
        assert data["inspection"]
        assert data["qr_url"].endswith(f"code={code}")

    def test_不存在的码404(self, server):
        status, _ = call(server, "GET", "/api/customer/trace",
                         code="TC-NOPE-9999")
        assert status == 404

    def test_空码400(self, server):
        status, _ = call(server, "GET", "/api/customer/trace", code="")
        assert status == 400

    def test_PNG标签字节流(self, tmp_path):
        """server 层专门分支：label.png 返回 PNG 字节（匿名可扫）。
        这条要真 HTTP：`make_server` 不起 serve 线程，这里 start/stop 配对。"""
        import urllib.request

        server = make_server(tmp_path)
        server.start()
        try:
            url = (f"{server.url}/api/customer/trace/label.png"
                   f"?code=TC-DEMO-0001")
            with urllib.request.urlopen(url, timeout=5) as resp:
                raw = resp.read()
                assert resp.headers["Content-Type"] == "image/png"
        finally:
            server.stop()
        assert raw[:4] == b"\x89PNG"
        assert len(raw) > 200

    def test_缺code的PNG端点报错也走JSON(self, tmp_path):
        import json
        import urllib.error
        import urllib.request

        server = make_server(tmp_path)
        server.start()
        try:
            url = f"{server.url}/api/customer/trace/label.png"
            try:
                urllib.request.urlopen(url, timeout=5)
                raise AssertionError("应当 4xx")
            except urllib.error.HTTPError as exc:
                payload = json.loads(exc.read().decode("utf-8"))
                assert exc.code == 400
                assert payload["ok"] is False
        finally:
            server.stop()


# ----------------------------------------------------------------- 边界
class TestBoundary:
    def test_顾客令牌不能进员工接口(self, server):
        """customer_token 不是登录令牌：拿它访问 /api/orders 只会被当
        匿名请求拒掉（401），不存在「顾客令牌碰巧通过」的路径。"""
        token = data_of(server, "POST", "/api/customer/session")["customer_token"]
        status, _ = call(server, "GET", "/api/orders", token=token)
        assert status == 401

    def test_顾客看不到工位人员阈值(self, server):
        for path in ("/api/stations", "/api/staff", "/api/params"):
            status, _ = call(server, "GET", path)
            assert status == 401, path
