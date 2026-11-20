# -*- coding: utf-8 -*-
"""A9 顾客端小程序验收自检（对齐 `tools/web_check.py` 的口径）。

    python tools/miniapp_check.py                 # 跑全部检查
    python tools/miniapp_check.py --serve         # 顺便把 API 起在 127.0.0.1:8848
    python tools/miniapp_check.py --serve --lan   # 绑 0.0.0.0，打印手机可连的局域网地址

四组检查：

1. **工程完整性**：app 三件套、7 个页面（wxml/wxss/js/json）、config、
   request 封装、project.config——缺一个微信开发者工具都打不开；外加 AppID
   格式/非占位断言与 urlCheck 开关（占位号 touristappid 能编译但不能真机预览）。
   1b. **页面结构**：四件套齐全、页面 json 合法、WXML 里绑定的事件方法在 js 中
   真实存在、@import 的样式文件存在（小程序没有编译期，拼错方法名只有点下去才炸）。
2. **契约（正向）**：小程序源码里出现过的每个 `/api/...` 字面量，
   后端路由表里必须存在。小程序没有编译期，拼错的接口名只有运行时才炸，
   这条检查把「拼错」提前到提交前。
3. **契约（反向）**：顾客通道调用的接口**必须全部在 /api/customer/ 前缀下**。
   小程序里出现员工接口（/api/orders、/api/staff…）说明有人绕过了顾客通道。
4. **全链路冒烟**：起一个真 AppState + Router，跑「开桌→菜单→下单→
   催菜→限频 429→通知→溯源→PNG 标签字节」，任何一环失败即退出码非 0。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))  # kitchen.storage 复用 tools/migrate.py 需要工程根可导入

#: 8 个页面：4 个 tab（点菜/订单/溯源/我的）+ 确认订单、订单详情、
#: 消息中心、智能客服与客诉（A11 扩展模块）
PAGES = [
    "pages/menu/menu", "pages/cart/cart", "pages/orders/orders",
    "pages/order-detail/order-detail", "pages/trace/trace",
    "pages/notifications/notifications", "pages/chat/chat",
    "pages/service/service", "pages/profile/profile",
]

#: 微信开发者工具打开工程所需的最小文件集（公共文件 + 页面四件套）
COMMON_FILES = [
    "app.js", "app.json", "app.wxss", "config.js", "project.config.json",
    "sitemap.json",
    "utils/api.js", "utils/store.js", "utils/format.js",
    "templates/common.wxml", "templates/common.wxss",
]
REQUIRED_FILES = COMMON_FILES + [
    page + ext for page in PAGES
    for ext in (".js", ".wxml", ".wxss", ".json")
]

#: 启动自检用的探针：匿名 GET、无副作用、能证明路由表和菜单数据都活着
PROBE_PATH = "/api/customer/menu"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        print(f"  {'✅' if ok else '❌'} {name}" + (f"：{detail}" if detail else ""))
        return bool(ok)

    @property
    def failures(self) -> int:
        return sum(1 for ok, _, _ in self.rows if not ok)

    def summary(self) -> int:
        total = len(self.rows)
        print(f"\n=== miniapp_check：{total - self.failures}/{total} 项通过"
              f"（失败 {self.failures}）===")
        return 0 if self.failures == 0 else 1


# --------------------------------------------------------------------- 1 工程
def check_files(report: Report) -> None:
    print("\n[1] 工程完整性")
    base = ROOT / "miniapp"
    for rel in REQUIRED_FILES:
        report.check((base / rel).is_file(), f"文件 {rel}")
    # app.json 注册的页面与目录对齐
    import json

    app = json.loads((base / "app.json").read_text(encoding="utf-8"))
    for page in app.get("pages", []):
        report.check((base / page).with_suffix(".js").is_file()
                     or (base / (page + ".js")).is_file(),
                     f"app.json 页面 {page} 有对应 js")
    # tabBar 页面必须在 pages 里
    for tab in app.get("tabBar", {}).get("list", []):
        report.check(tab["pagePath"] in app.get("pages", []),
                     f"tabBar 页面 {tab['pagePath']} 已注册")

    # AppID：占位号 touristappid 只能在模拟器里编译，不能真机预览、不能上传。
    # 答辩包里混进占位号，开发者工具照样打开、手机扫码却是空的，所以做成硬断言。
    cfg = json.loads((base / "project.config.json").read_text(encoding="utf-8"))
    appid = str(cfg.get("appid", ""))
    report.check(bool(re.fullmatch(r"wx[0-9a-f]{16}", appid)),
                 "AppID 格式 wx+16hex", appid or "（空）")
    report.check(appid not in ("touristappid", "", "wxxxxxxxxxxxxxxxxx"),
                 "AppID 非占位号", "touristappid 不能真机预览/上传")
    report.check(cfg.get("setting", {}).get("urlCheck") is False,
                 "urlCheck=false（模拟器可连本地 http）",
                 "正式发布前仍需在小程序后台配 request 合法域名")


# ------------------------------------------------------------- 1b 页面结构
def check_pages(report: Report) -> None:
    """静态查页面：四件套、json 合法、事件方法存在、@import 存在。

    微信小程序没有编译期：WXML 里写 `bindtap="goCart"` 而 js 里没定义，
    编译照样通过，只有用户点下去才在控制台报错。这里把它提前到提交前。
    """
    import json

    print("\n[1b] 页面结构与事件绑定")
    base = ROOT / "miniapp"
    app = json.loads((base / "app.json").read_text(encoding="utf-8"))
    event_re = re.compile(
        r'\b(?:bind|catch)(?:tap|input|confirm|change|error|longpress)'
        r'\s*=\s*"([A-Za-z_$][\w$]*)"')
    for page in app.get("pages", []):
        stem = base / page
        exts = (".js", ".wxml", ".wxss", ".json")
        absent = [ext for ext in exts
                  if not Path(str(stem) + ext).is_file()]
        report.check(not absent, f"页面 {page} 四件套", ",".join(absent))
        if absent:
            continue
        try:
            json.loads(Path(str(stem) + ".json").read_text(encoding="utf-8"))
            report.check(True, f"页面 {page}.json 合法")
        except Exception as exc:  # noqa: BLE001 - 自检脚本要把任何解析错都报出来
            report.check(False, f"页面 {page}.json 合法", str(exc))
        js_text = Path(str(stem) + ".js").read_text(encoding="utf-8")
        wxml_text = Path(str(stem) + ".wxml").read_text(encoding="utf-8")
        events = set(event_re.findall(wxml_text))
        # 公共模板里的 retry / emptyAction 由引用它的页面提供
        if "templates/common.wxml" in wxml_text:
            events.add("retry")
            if 'is="state-empty"' in wxml_text:
                events.add("emptyAction")
        missing = [name for name in sorted(events)
                   if not re.search(r'\b' + re.escape(name) +
                                    r'\s*:\s*(?:function|\()', js_text)]
        report.check(not missing, f"页面 {page} 事件方法齐备",
                     ";".join(missing) if missing else f"{len(events)} 个")
        wxss_text = Path(str(stem) + ".wxss").read_text(encoding="utf-8")
        for inc in re.findall(r'@import\s+["\']([^"\']+)["\']', wxss_text):
            report.check((stem.parent / inc).is_file(),
                         f"页面 {page} @import {inc}")


# --------------------------------------------------------------------- 2/3 契约
#: 拼接链：`'/api/a/' + id + '/b'` 的后续 token（字符串字面量或标识符）
_CONCAT_RE = re.compile(
    r"\s*\+\s*(?:(?P<lit>['\"][^'\"]*['\"])|(?P<var>[A-Za-z_$][\w$.]*))")

#: 字面量起点（必须 /api/ 开头才算接口调用）
_LITERAL_RE = re.compile(r"['\"](/api/[^'\"]*)['\"]")


def _scan_api_literals() -> dict[str, list[str]]:
    """扫小程序全部 js/wxml 里的 `/api/...` 调用 → {归一化路径: [文件]}。

    拼接表达式归一：`'/api/customer/orders/items/' + itemId + '/urge'`
    → `/api/customer/orders/items/{param}/urge`（变量一律占 `{param}`）。
    小程序没有编译期，拼错接口名只有运行时才炸——这里就是把它提前。
    """
    found: dict[str, list[str]] = {}
    for path in (ROOT / "miniapp").rglob("*"):
        if path.suffix not in (".js", ".wxml"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _LITERAL_RE.finditer(text):
            api = match.group(1)
            # 向后吞拼接链（变量 → {param}，字符串直接续上）
            i = match.end()
            while True:
                piece = _CONCAT_RE.match(text, i)
                if piece is None:
                    break
                if piece.group("var") is not None:
                    api += "{param}"
                else:
                    api += piece.group("lit")[1:-1]
                i = piece.end()
            api = api.split("?")[0]
            found.setdefault(api, []).append(path.name)
    return found


def _route_signatures() -> set[tuple[str, str]]:
    # tests/ 不是包（无 __init__.py），目录已进 sys.path，直接按模块导入
    from api_support import make_state

    state = make_state(Path(".local/tmp/miniapp-check"))
    from kitchen.api import Router

    router = Router(state)
    out: set[tuple[str, str]] = set()
    for route in router.routes:
        out.add((route.method, route.pattern))
    # server.py 的二进制端点不走 Router（上传/取图/溯源 PNG），但仍属于
    # 顾客可调用的合法接口，纳入签名以免契约检查误报。
    out.update({
        ("POST", "/api/customer/guest/evidence-upload"),
        ("GET", "/api/customer/guest/evidence-image"),
        ("GET", "/api/customer/trace/label.png"),
    })
    return out


def check_contract(report: Report) -> None:
    print("\n[2] 前后端契约（小程序 → 后端路由）")
    literals = _scan_api_literals()
    report.check(bool(literals), "扫到至少一个接口调用", f"{len(literals)} 个")
    signatures = _route_signatures()

    def matches(method: str, path: str) -> bool:
        segs = [s for s in path.strip("/").split("/") if s]
        for m, pattern in signatures:
            if m != method:
                continue
            parts = [p for p in pattern.strip("/").split("/") if p]
            if len(parts) != len(segs):
                continue
            if all(re.fullmatch(r"\{[a-zA-Z_]+\}", t) or t == a
                   for t, a in zip(parts, segs)):
                return True
        return False

    for api in sorted(literals):
        methods = ("GET",) if api.endswith(("menu", "orders", "trace",
                                            "notifications")) else ("POST",)
        # 顾客通道全部走 request('GET'|'POST', path)——两种都试
        ok = matches("GET", api) or matches("POST", api) or matches("PUT", api)
        report.check(ok, f"接口存在 {api}", ", ".join(sorted(set(literals[api]))))

    print("\n[3] 顾客通道边界（不允许出现员工接口）")
    leaked = [api for api in literals
              if not api.startswith("/api/customer/")]
    report.check(not leaked, "无越界接口",
                 "; ".join(leaked) if leaked else "全部在 /api/customer/ 下")


# --------------------------------------------------------------------- 4 视觉
#: A9–A23 的冷色板。A25 统一改暖后，任何一处残留都会让某个页面看起来还是旧 UI，
#: 所以这里按色值卡死，而不是靠答辩前肉眼过一遍、过完又忘。
LEGACY_COLORS = ("#0071e3", "#0a84ff", "#f5f5f7", "#f2f2f7", "#8e8e93", "#e5e5ea")
TOKEN_NAMES = ("--bg", "--panel", "--panel-2", "--text", "--text-2", "--text-3",
               "--line", "--accent", "--price", "--info", "--green", "--orange", "--red")


def check_visual(report: Report) -> None:
    print("\n[4] 视觉一致性（设计令牌 / tabBar 图标 / 图与菜对应）")
    miniapp = ROOT / "miniapp"
    app_wxss = (miniapp / "app.wxss").read_text(encoding="utf-8")
    block = re.search(r"page\s*\{(.*?)\n\}", app_wxss, re.S)
    scope = block.group(1) if block else ""

    # 1) 令牌集中定义在 page 块，且必须是裸值。
    #    --x: var(--x, #hex) 是自引用，变量会整体失效——批量换色最容易踩这个坑。
    missing = [t for t in TOKEN_NAMES if not re.search(rf"{re.escape(t)}:\s*#", scope)]
    report.check(not missing, "设计令牌集中在 app.wxss 的 page 块",
                 f"缺 {', '.join(missing)}" if missing else f"{len(TOKEN_NAMES)} 个")
    loops = re.findall(r"(--[\w-]+):\s*var\(\1", app_wxss)
    report.check(not loops, "令牌定义无自引用", "; ".join(loops) if loops else "ok")

    # 2) 旧冷色板不许残留在任何 wxss 里
    hits = []
    for path in miniapp.rglob("*.wxss"):
        text = path.read_text(encoding="utf-8").lower()
        for color in LEGACY_COLORS:
            if color in text:
                hits.append(f"{path.relative_to(miniapp)}:{color}")
    report.check(not hits, "无旧冷色板残留",
                 f"{len(hits)} 处：{'; '.join(hits[:4])}" if hits else "全部走变量")

    # 3) tabBar 图标齐全且在包内；选中色与主题主色同源
    cfg = json.loads((miniapp / "app.json").read_text(encoding="utf-8"))
    tabs = cfg.get("tabBar", {}).get("list", [])
    declared = all(t.get("iconPath") and t.get("selectedIconPath") for t in tabs)
    on_disk = all((miniapp / t[k]).is_file() for t in tabs
                  for k in ("iconPath", "selectedIconPath") if t.get(k))
    report.check(declared and on_disk, "tabBar 四个 tab 都有图标且在包内",
                 f"{len(tabs)} 个 tab")
    accent = re.search(r"--accent:\s*(#[0-9a-fA-F]{6})", scope)
    selected = str(cfg.get("tabBar", {}).get("selectedColor", ""))
    report.check(bool(accent) and accent.group(1).lower() == selected.lower(),
                 "tabBar 选中色与 --accent 同源", selected)

    # 4) 图与菜一一对应：新增菜品忘了跑生成器，卡片会静默退化成色块，不会报错
    mapping = (miniapp / "utils" / "dish_images.js").read_text(encoding="utf-8")
    # 生成的映射用双引号（JSON 风格），这里两种引号都吃
    mapped = set(re.findall(r"[\"'](D-[A-Z]+)[\"']", mapping))
    seed = (ROOT / "fixtures" / "seed_kitchen.sql").read_text(encoding="utf-8")
    seeded = set(re.findall(r"'(D-[A-Z]+)'", seed))
    lack, extra = sorted(seeded - mapped), sorted(mapped - seeded)
    detail = f"缺 {', '.join(lack)}" if lack else f"{len(mapped)} 道菜全部有图"
    if extra:
        detail += f"；多余 {', '.join(extra)}（seed 已删的菜，可直接删图省包体）"
    report.check(not lack, "菜单每道菜都有主图", detail)

    # 5) 溯源标签图不能把 HTTP 地址直接绑给 <image>。
    #    基础库 3.18.0 起组件层拒 http（关掉域名检查也没用），必须落本地临时文件。
    trace_wxml = (miniapp / "pages" / "trace" / "trace.wxml").read_text(encoding="utf-8")
    bound = re.findall(r"<image[^>]*src=\"([^\"]+)\"", trace_wxml)
    direct = [s for s in bound if "labelUrl" in s]
    trace_js = (miniapp / "pages" / "trace" / "trace.js").read_text(encoding="utf-8")
    landed = "writeFile" in trace_js and "responseType: 'arraybuffer'" in trace_js
    report.check(not direct and landed, "溯源标签图走本地临时文件而非远程 HTTP",
                 "仍直绑 labelUrl" if direct else
                 ("arraybuffer→writeFile" if landed else "缺少落盘逻辑"))


# --------------------------------------------------------------------- 5 冒烟
def check_smoke(report: Report) -> bool:
    print("\n[5] 顾客全链路冒烟（Router 直调 + 真 HTTP PNG）")
    import tempfile
    import urllib.request

    workdir = ROOT / ".local/tmp/miniapp-check"
    workdir.mkdir(parents=True, exist_ok=True)
    from api_support import make_state
    from kitchen.api import create_server

    state = make_state(workdir / "var")
    server = create_server(state, port=0, secret="check")

    def call(method, path, session=None, **data):
        return server.router.call(method, path, data=data, session=session)

    token = call("POST", "/api/customer/session")[1]["data"]["customer_token"]
    report.check(bool(token), "① 开桌发令牌", token[:8] + "…")

    st, p = call("GET", "/api/customer/menu")
    ok = st == 200 and p["data"]["total"] > 0
    report.check(ok, "② 菜单可看", f"{p.get('data', {}).get('total')} 道菜")

    st, p = call("POST", "/api/customer/orders", customer_token=token,
                 table_id=3, items=[{"code": "D-QJRS", "qty": 1}])
    report.check(st == 200, "③ 下单成功", p["data"]["order_no"])

    st, p = call("GET", "/api/customer/orders", customer_token=token)
    item = p["data"]["orders"][0]["items"][0]
    report.check(st == 200 and "remain_minutes" in item, "④ 进度含 ETA",
                 f"剩 {item['remain_minutes']} 分钟")

    st, p = call("POST", f"/api/customer/orders/items/{item['id']}/urge",
                 customer_token=token)
    report.check(st == 200 and p["data"]["urge_count"] == 1, "⑤ 催菜计数")

    st, p = call("POST", f"/api/customer/orders/items/{item['id']}/urge",
                 customer_token=token)
    report.check(st == 429, "⑥ 二次催菜被限频 429", p.get("detail", "")[:30])

    st, p = call("GET", "/api/customer/notifications", customer_token=token)
    report.check(st == 200 and p["data"]["total"] >= 1, "⑦ 通知拉取",
                 f"{p['data']['total']} 条")

    st, p = call("GET", "/api/customer/trace", code="TC-DEMO-0001")
    ok = st == 200 and p["data"]["supplier"] == "鲜丰肉联厂"
    report.check(ok, "⑧ 溯源查询", p["data"].get("dish", ""))

    # PNG：真 HTTP（server 层的专门分支只有走 HTTP 才覆盖得到）
    server.start()
    try:
        url = f"{server.url}/api/customer/trace/label.png?code=TC-DEMO-0001"
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read()
            ctype = resp.headers["Content-Type"]
    finally:
        server.stop()
    report.check(raw[:4] == b"\x89PNG" and ctype == "image/png",
                 "⑨ PNG 标签字节流", f"{len(raw)} bytes")

    # 汇报口径：顾客单对员工可见（三端同一状态机）
    from api_support import login as staff_login

    chef = staff_login(server, "chef01")
    st, p = call("GET", "/api/orders", session=chef)
    ok = st == 200 and any(row["source"] == "miniapp"
                           for row in p["data"]["orders"])
    report.check(ok, "⑩ 顾客单对员工可见（同一状态机）")
    return True


def _lan_ip() -> str:
    """取本机局域网出口 IP。

    UDP connect 不真发包，只是让操作系统按路由表选一张出口网卡；比解析
    `gethostbyname(gethostname())` 靠谱（后者常返回 127.0.0.1 或虚拟网卡地址，
    装了 VMware / Docker / 手机热点时尤其容易选错）。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _sync_base_url(base_url: str) -> str:
    """把本次服务实际监听地址写回 ``miniapp/config.js`` 的 BASE_URL。

    现场反复踩过的坑：config.js 里手写的局域网 IP 在换 WiFi / DHCP 重分配
    后就过期了，表现是请求超时或打到旧地址，而页面上只看到「网络不可用」。
    服务是唯一知道自己绑在哪个地址的地方，所以由它在启动自检通过后同步，
    不再要求人肉 `ipconfig` 改代码。只在值真的变化时才写盘，避免每次启动
    都触发开发者工具的热重载。

    返回一句可直接打印的中文说明（未改动 / 已改动 / 跳过原因）。
    """
    path = ROOT / "miniapp" / "config.js"
    if not path.is_file():
        return "config.js 不存在，跳过"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"(const\s+BASE_URL\s*=\s*)['\"][^'\"]+['\"](\s*;)")
    match = pattern.search(text)
    if not match:
        return "未找到 BASE_URL 声明，跳过"
    current = re.search(r"['\"]([^'\"]+)['\"]", match.group(0)).group(1)
    if current == base_url:
        return f"已是 {base_url}，无需改动"
    replacement = match.group(1) + "'" + base_url + "'" + match.group(2)
    path.write_text(pattern.sub(lambda _: replacement, text, count=1),
                    encoding="utf-8")
    return (f"{current} → {base_url}（已写回 config.js，"
            "请在微信开发者工具重新编译）")


def main() -> int:
    report = Report()
    print("A9 顾客端小程序验收自检")
    check_files(report)
    check_pages(report)
    check_contract(report)
    check_visual(report)
    check_smoke(report)
    code = report.summary()

    if "--serve" in sys.argv and code == 0:
        # 默认只绑回环：本机模拟器够用，也不会把演示服务暴露到局域网。
        # --lan 才绑 0.0.0.0——手机真机预览必须走这个，否则手机连不上。
        lan = "--lan" in sys.argv
        # A1：--db 开启运行时库落库（遥测/告警写 SQLite，重启后历史可查，
        # /api/admin/db/stats 查看落库情况）。默认关闭=纯内存，行为不变。
        use_db = "--db" in sys.argv
        # A3：--schedule 开启定时任务托管（留样到期扫描 + 证件每日扫描）。
        # 演示口径扫描间隔压到 10s，答辩时事件流里能看到任务触发记录。
        use_sched = "--schedule" in sys.argv
        host = "0.0.0.0" if lan else "127.0.0.1"
        ip = _lan_ip() if lan else "127.0.0.1"
        base_url = f"http://{ip}:8848"
        print(f"\n--serve：API 起在 {base_url}"
              + ("（0.0.0.0，手机可连）" if lan else "（仅本机）"))
        if use_db:
            print(f"  运行时库（A1）：{workdir / 'runtime.db'}"
                  "——遥测/告警落库，重启不丢；GET /api/admin/db/stats 查看落库情况")
        if use_sched:
            print("  定时任务（A3）：留样到期扫描 10s/次 + 证件扫描每日 06:00；"
                  "GET /api/admin/scheduler 查看运行统计")
        if lan:
            print("  1) BASE_URL 会在服务就绪后自动同步到 miniapp/config.js"
                  "（换 WiFi 后不用再手改 IP）")
            print("  2) 手机与电脑同一 WiFi；基础库 2.4.0+ 的开发版可直接请求"
                  "同网段 http IP，若仍报域名错再打开手机端「调试模式」")
            print("  3) Windows 防火墙放行 TCP 8848 入站（不然手机一直超时）")
        from api_support import make_state
        from kitchen.api import create_server

        # 端口预检：本机 8848 已有人听就别再起第二个。Windows 上双开同端口
        # 的表现是「请求随机打到旧进程」，比直接报错难查得多。
        import socket as _socket

        with _socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", 8848)) == 0:
                print("  ⚠️ 8848 已有服务在监听，本次不重复启动。")
                print("     要确认它是不是本工程 API：python tools/miniapp_netcheck.py")
                # 换 WiFi 后 IP 会变，但旧服务还占着端口：这里仍要把新 IP
                # 写回 config.js，否则小程序会一直打向过期的地址。
                print(f"  🔄 BASE_URL 自动同步：{_sync_base_url(base_url)}")
                return 1

        workdir = ROOT / ".local/tmp/miniapp-serve"
        workdir.mkdir(parents=True, exist_ok=True)
        state = make_state(
            workdir / "var",
            db_path=(workdir / "runtime.db") if use_db else None,
            scheduler_enabled=use_sched,
            retention_sweep_sec=10.0 if use_sched else 30.0,
        )
        try:
            server = create_server(state, host=host, port=8848, secret="demo")
            server.start()
        except OSError as exc:
            print(f"  ❌ 8848 绑定失败：{exc}（端口被别的进程占了？）")
            return 1

        # 启动后自检：光打印「已启动」不算数，真发一次 HTTP 才算数。
        # ProxyHandler({}) 绕开系统代理，否则 HTTP_PROXY 会把请求拐去别处拿 502。
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        targets = [("回环 127.0.0.1", f"http://127.0.0.1:8848{PROBE_PATH}")]
        if lan:
            targets.append((f"局域网 {ip}", f"http://{ip}:8848{PROBE_PATH}"))
        all_ok = True
        for label, url in targets:
            try:
                with opener.open(url, timeout=3) as resp:
                    got, ok = resp.status, resp.status == 200
            except Exception as exc:
                got, ok = f"{type(exc).__name__}: {exc}", False
            all_ok = all_ok and ok
            print(f"  {'✅' if ok else '❌'} 启动自检 {label}: {got}")
        if not all_ok:
            print("  ❌ 服务已起但请求不通，跑 python tools/miniapp_netcheck.py 定位")
            server.stop()
            return 1

        # 端口通、HTTP 自检也通了，才把地址写回 config.js：避免自检失败
        # 却把小程序指向一个不可用的地址。
        print(f"  🔄 BASE_URL 自动同步：{_sync_base_url(base_url)}")

        print(f"\n  ✅ 后端已就绪：{base_url}")
        print("     保持此窗口开着，再去微信开发者工具点「编译」。（Ctrl+C 停止）",
              flush=True)
        try:
            import time

            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
