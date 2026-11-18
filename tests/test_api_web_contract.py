"""前端契约测试：**锁住「前端说的话后端得认」**。

这三类 bug 在纯前端项目里几乎不可能靠人眼发现，而它们的表现都很「安静」：

1. **调了不存在的接口** → 页面空白 / 一直转圈，控制台只有一条 404；
2. **用了不存在的图标名** → `Icon.vue` fallback 到仪表盘图标，不报错，
   看起来只是「图标不太对」，实际是拼错了；
3. **读了后端没给的字段** → Vue 的 `undefined` 渲染成空字符串，表格里
   整列空白，评审会认为是「没做」而不是「字段名写错」。

所以这里用静态扫描 + 真实 payload 双向核对：改后端字段名而忘了改前端、
或前端写错一个路径，单测就会红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kitchen.api import AppState, AppStateConfig, create_server

WEB = Path(__file__).resolve().parents[1] / "web"
SRC = WEB / "src"

#: `/api/channels/${name}/actions` 这种模板串里的表达式，替换成占位再比对
_TEMPLATE = re.compile(r"\$\{[^}]*\}")
#: 前端里出现的接口路径字面量（含模板串前缀）
_API_LITERAL = re.compile(r"""['"`](/api/[^'"`\s]*)['"`]""")


def _normalize(path: str) -> str:
    """把前端路径与后端路由都归一成「段数相同、参数位统一」的形式。"""
    path = path.split("?")[0].split("#")[0]
    path = _TEMPLATE.sub("X", path)
    parts = [part for part in path.strip("/").split("/") if part]
    return "/" + "/".join("X" if part.startswith("{") or part == "X" else part for part in parts)


def _frontend_calls() -> dict[str, set[str]]:
    """扫出前端源码里的全部 `/api/...` 字面量，返回 `{归一化路径: {原样出处}}`。"""
    found: dict[str, set[str]] = {}
    for file in sorted(SRC.rglob("*")):
        if file.suffix not in {".vue", ".js"}:
            continue
        text = file.read_text(encoding="utf-8")
        for raw in _API_LITERAL.findall(text):
            key = _normalize(raw)
            found.setdefault(key, set()).add(f"{file.relative_to(SRC).as_posix()}: {raw}")
    return found


#: `Router` 只覆盖「一问一答」的 JSON 接口。SSE 要在 HTTP 层一直握着连接写流，
#: 走的是 `ApiServer` 里的专用分支，所以路由表里查不到——这是设计如此，不是漏注册。
NOT_IN_ROUTER = {"/api/stream"}


def _server_paths(server) -> set[str]:
    return {_normalize(route.pattern) for route in server.router.routes} | NOT_IN_ROUTER


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    state = AppState(config=AppStateConfig(var_dir=tmp_path_factory.mktemp("contract")))
    return create_server(state, secret="contract")


@pytest.fixture(scope="module")
def admin(server):
    _token, session = server.login.login("admin", "kitchen2026", source="contract")
    return session


class TestApiPaths:
    """前端写下的每一个接口路径，后端路由表里都得有。"""

    def test_前端源码存在(self):
        """扫描不到文件时这条测试必须红：否则下面几条会「因为没扫到」而全绿。"""
        assert (SRC / "views").is_dir(), f"找不到前端源码目录：{SRC}"
        assert len(list(SRC.rglob("*.vue"))) >= 9

    def test_没有调用不存在的接口(self, server):
        known = _server_paths(server)
        calls = _frontend_calls()
        assert calls, "没扫到任何 `/api/...` 字面量，扫描规则可能坏了"
        unknown = {
            path: where for path, where in calls.items() if path not in known
        }
        assert not unknown, (
            "前端调用了后端不存在的接口：\n"
            + "\n".join(f"  {path}  ← {sorted(where)}" for path, where in unknown.items())
        )

    def test_每个写接口都有前端入口(self, server):
        """反向检查：`POST/PUT` 接口如果前端一处都没调，多半是页面漏做了。"""
        #: 只应由程序（采样线程 / 演示脚本 / 小程序 / TFT 终端）调用的写接口，
        #: 白名单放行。cooking 四条是 A8 TFT 端接口（`src/kitchen/tft/` 与
        #: ESP32 固件调用），Web 管理端不提供工序操作入口是刻意的：
        #: 工序确认必须在灶边的终端上做（要刷卡身份 + 防远程代确认）。
        EXEMPT = {
            "/api/auth/logout", "/api/morning/submit",
            "/api/cooking/{item_id}/weigh", "/api/cooking/{item_id}/confirm",
            "/api/cooking/{item_id}/serve", "/api/cooking/{item_id}/reset",
            # A9 顾客端小程序接口（miniapp/ 调用，Web 管理端刻意无入口：
            # 顾客点餐不进后厨管理界面）
            "/api/customer/session", "/api/customer/orders",
            "/api/customer/orders/items/{item_id}/urge",
            # A11 扩展模块：客诉提交/补证只由小程序 service 页调用，
            # 管理端只做审批（对应 POST 已在 GuestView.vue 里有入口）
            "/api/customer/guest/complaints",
            "/api/customer/guest/complaints/{complaint_id}/supplement",
            # 对话式客服（小程序 chat 页调用，管理端无入口）
            "/api/customer/guest/chat",
        }
        used = set(_frontend_calls())
        missing = [
            route.pattern for route in server.router.routes
            if route.method in {"POST", "PUT"}
            and _normalize(route.pattern) not in used
            and route.pattern not in EXEMPT
        ]
        assert not missing, f"这些写接口前端没有入口：{missing}"


class TestPayloadFields:
    """前端渲染依赖的字段，后端必须给（少一个字段就是整列空白）。"""

    def test_三通道动作清单字段(self, server, admin):
        data = server.router.call("GET", "/api/meta", session=admin)[1]["data"]
        assert data["actions"], "动作清单为空"
        for row in data["actions"]:
            assert {"action", "channel", "label", "kind", "manual_confirm"} <= set(row)
        # 三通道 + 独立 GPIO 都要在：缺 GPIO 的话声光按钮整排消失
        channels = {row["channel"] for row in data["actions"]}
        assert {"CH1", "CH2", "CH3", "GPIO_SIGNAL"} <= channels

    def test_参数页字段(self, server, admin):
        data = server.router.call("GET", "/api/params", session=admin)[1]["data"]
        assert {"source", "version", "degraded", "profile", "rules", "demo_map"} <= set(data)
        assert data["rules"], "阈值表为空，参数页会显示成一张空表"
        for row in data["rules"]:
            assert {"id", "metric", "kind", "lower_bound", "upper_bound",
                    "duration_sec", "window_sec", "level", "enabled"} <= set(row)

    def test_演示映射字段(self, server, admin):
        """A7-09 的「演示值 ↔ 真实值」表：字段少一个这张表就渲染不出来。"""
        data = server.router.call("GET", "/api/params", session=admin)[1]["data"]
        demo = data["demo_map"]
        assert {"time_scale", "scale_note", "rows", "hard_rule"} <= set(demo)
        assert demo["rows"], "演示映射表为空"
        for row in demo["rows"]:
            assert {"item", "real_text", "effective_sec", "effective_text", "note"} <= set(row)

    def test_审计条目字段(self, server, admin):
        # 直调 `login.login()` 是刻意保留的静默通道（单测用），不写审计；
        # 这里走一次路由登录，保证确实有记录可断言
        server.router.call("POST", "/api/auth/login",
                           data={"username": "admin", "password": "kitchen2026"})
        data = server.router.call("GET", "/api/audit", session=admin)[1]["data"]
        assert data["entries"], "审计为空（登录本身就会写一条）"
        for row in data["entries"]:
            assert {"id", "time", "actor", "action", "result"} <= set(row)

    def test_总览字段(self, server, admin):
        """九个页面共用一个 overview，少一个分块就有页面整块空白。"""
        data = server.router.call("GET", "/api/overview", session=admin)[1]["data"]
        assert {"device_id", "server_time", "time_scale", "online", "ticks",
                "profile", "channels", "degrade", "telemetry", "counts",
                "guards"} <= set(data)
        assert {"CH1", "CH2", "CH3"} <= set(data["channels"])
        assert len(data["telemetry"]) >= 12, "12 路遥测少了"
        assert {"queue", "orders_open"} <= set(data["counts"])
        assert "tripped" in data["guards"]


class TestIconNames:
    """图标名拼错不会报错，只会 fallback 成仪表盘图标——所以静态比对一遍。"""

    def test_模板里用到的图标都已定义(self):
        icon_src = (SRC / "components" / "Icon.vue").read_text(encoding="utf-8")
        defined = set(re.findall(r"^  ([a-z_]+):\s*'", icon_src, flags=re.M))
        assert "gauge" in defined and "sliders" in defined, "PATHS 解析失败"

        used: dict[str, set[str]] = {}
        for file in sorted(SRC.rglob("*.vue")):
            text = file.read_text(encoding="utf-8")
            # `(?<!:)` 排掉 `:name="icon"`（动态绑定，值是变量名不是图标名）
            for name in re.findall(r"""<Icon[^>]*(?<!:)\bname="([a-z_]+)\"""", text):
                used.setdefault(name, set()).add(file.name)
            for name in re.findall(r"""\bicon:\s*'([a-z_]+)'""", text):
                used.setdefault(name, set()).add(file.name)
        assert used, "没扫到任何图标引用"

        unknown = {name: where for name, where in used.items() if name not in defined}
        assert not unknown, f"用了未定义的图标：{unknown}"


class TestAdminShell:
    """壳层的最低要求：构建产物与登录页的入口不能被改坏。"""

    def test_index_html挂载点(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        assert '<div id="app">' in html
        assert "/src/main.js" in html

    def test_路由表覆盖八个页面(self):
        """A7 验收口径是「8 个页面全部可用」，路由表少一个就是少一页。"""
        app = (SRC / "App.vue").read_text(encoding="utf-8")
        routes = re.findall(r"\{ key: '(\w+)', path: '#/(\w+)'", app)
        assert len(routes) >= 8, f"路由只有 {len(routes)} 条：{routes}"
        keys = {key for key, _ in routes}
        assert {"kpi", "orders", "stations", "environment",
                "recipes", "compliance", "tickets", "control"} <= keys

    def test_每个路由的视图文件存在(self):
        app = (SRC / "App.vue").read_text(encoding="utf-8")
        for name in re.findall(r"from '\./views/(\w+)\.vue'", app):
            assert (SRC / "views" / f"{name}.vue").is_file(), f"缺少视图 {name}.vue"
