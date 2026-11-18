# -*- coding: utf-8 -*-
"""云端小程序链路验收：按 wx.request 的口径打真实 HTTP 接口。

`tools/miniapp_check.py` 是**本机**自检：起一个内存里的 AppState + Router，
证明代码本身没坏；但它绕过了网络、Docker、MySQL 和云端反向代理。这个脚本
补另一头：从外网按小程序真机的方式发请求，证明**部署在云上的那套**对顾客
端是通的。

口径与 `miniapp/utils/api.js` 完全一致（改前端时同步改这里）：

- 顾客令牌叫 `customer_token`；GET 放 query string，POST 放 JSON body
  （wx.request 的 data 在 GET 时会被拼进 URL，别按「一律 body」写）。
- 成功判据是 `2xx` + `{ok: true, data: {...}}`，脚本取的是 `data`。
- 顾客通道**免登录**，401 只可能来自令牌本身（缺失/失效），不是员工鉴权。

用法：

    python tools/miniapp_cloud_check.py                 # 只读体检，地址取 miniapp/config.js
    python tools/miniapp_cloud_check.py --base http://101.37.82.186
    python tools/miniapp_cloud_check.py --write         # 加跑下单/催菜/客服对话
    python tools/miniapp_cloud_check.py --write --table 5

默认只读：不动任何业务数据，随时可跑（答辩前跑一遍最省事）。加 `--write`
会在云端**真下一单**（打印 order_no 与清理 SQL），用来验证 MySQL 自增、
沽清校验、限频 429 和 LLM 是否真的活着。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_JS = ROOT / "miniapp" / "config.js"

#: 溯源页空态默认码（miniapp/pages/trace/trace.js::demoCode）
DEMO_TRACE_CODE = "TC-DEMO-0001"

#: 客服对话的可能意图（后端 guest/llm.py），只做「是不是这几个之一」的粗校验
CHAT_INTENTS = {"urge", "order_query", "menu_query", "chitchat", "complaint"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))
        return bool(ok)

    @property
    def failures(self) -> int:
        return sum(1 for ok, _, _ in self.rows if not ok)

    def summary(self) -> int:
        total = len(self.rows)
        print(f"\n=== miniapp_cloud_check: {total - self.failures}/{total} passed "
              f"({self.failures} failed) ===")
        return 0 if self.failures == 0 else 1


def config_base_url() -> str:
    """从 config.js 里抠出编译进包的 BASE_URL，用来和 --base 对账。"""
    try:
        text = CONFIG_JS.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"const\s+BASE_URL\s*=\s*'([^']+)'", text)
    return m.group(1).rstrip("/") if m else ""


class Client:
    def __init__(self, base: str, timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _url(self, path: str, query: dict | None) -> str:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def call(self, method: str, path: str, payload: dict | None = None,
             token: str | None = None, timeout: float | None = None):
        """返回 (status, body_obj, elapsed_ms)。网络失败抛 RuntimeError。"""
        payload = dict(payload or {})
        if token:
            payload["customer_token"] = token
        if method == "GET":
            url = self._url(path, payload or None)
            req = urllib.request.Request(url, method="GET")
        else:
            url = self._url(path, None)
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method=method,
                headers={"Content-Type": "application/json"})
        req.add_header("User-Agent", "kitchen-miniapp-cloud-check/1.0")
        req.add_header("Accept", "application/json")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as err:          # 4xx/5xx 也要读 body
            raw = err.read()
            status = err.code
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise RuntimeError(f"连不上 {url}: {err}") from err
        elapsed = (time.perf_counter() - started) * 1000.0
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"_raw": raw[:200]}
        return status, body, elapsed

    def data(self, method: str, path: str, payload: dict | None = None,
             token: str | None = None):
        status, body, elapsed = self.call(method, path, payload, token)
        ok = status == 200 and isinstance(body, dict) and body.get("ok") is True
        return status, (body.get("data") if ok else body), elapsed

    def raw(self, path: str, query: dict | None = None):
        url = self._url(path, query)
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()


# ------------------------------------------------------------------ 只读体检
def check_readonly(c: Client, rep: Report, table_id: int) -> tuple[str, str, dict]:
    """返回 (token, guest_key, menu_data)，给 --write 阶段复用。"""
    guest_key = f"cloudcheck-{int(time.time())}"

    print("\n[1] 匿名探活（页面打开第一件事）")
    status, body, ms = c.call("GET", "/api/customer/menu")
    menu = body.get("data") if isinstance(body, dict) else None
    ok = status == 200 and bool(menu and menu.get("dishes"))
    rep.check(ok, "GET /api/customer/menu 匿名可达",
              f"{status} {ms:.0f}ms {len((menu or {}).get('dishes') or [])} 道菜")
    if not ok:
        return "", guest_key, {}
    rep.check(ms < 1500, "菜单响应 < 1.5s（冷启动可放宽）", f"{ms:.0f}ms")
    dishes = menu["dishes"]
    rep.check(all(k in dishes[0] for k in ("code", "name", "price", "sold_out")),
              "菜单字段齐全（code/name/price/sold_out）",
              ",".join(sorted(dishes[0].keys())[:6]))

    print("\n[2] 开桌（POST /api/customer/session）")
    status, data, ms = c.data("POST", "/api/customer/session",
                              {"table_id": table_id, "guest_key": guest_key,
                               "tags": ["少辣"]})
    token = (data or {}).get("customer_token", "") if status == 200 else ""
    ref = (data or {}).get("customer_ref", "") if status == 200 else ""
    rep.check(status == 200 and bool(token), "开桌拿到 customer_token",
              f"{status} token={len(token)} 位")
    rep.check(bool(re.fullmatch(r"T\d{2}-[A-Z0-9]{4}", ref or "")),
              "customer_ref 是「桌号-随机」格式", ref or "(空)")
    rep.check(ms < 2000, "开桌耗时 < 2s", f"{ms:.0f}ms")

    if not token:
        return "", guest_key, menu

    print("\n[3] 令牌边界（顾客通道不认员工鉴权，只认令牌）")
    status, _, _ = c.call("GET", "/api/customer/orders")
    rep.check(status == 401, "不带令牌取「我的单」→ 401", f"{status}")
    status, _, _ = c.call("GET", "/api/customer/orders", {"customer_token": "0" * 32})
    rep.check(status == 401, "伪造令牌 → 401（不是 500/放行）", f"{status}")

    print("\n[4] 带令牌的四个读接口（页面轮询都打这几个）")
    status, data, ms = c.data("GET", "/api/customer/menu", {}, token)
    rep.check(status == 200 and (data or {}).get("total") == menu.get("total"),
              "带令牌取菜单与匿名一致", f"{status} total={(data or {}).get('total')}")

    status, data, ms = c.data("GET", "/api/customer/orders", {}, token)
    orders = (data or {}).get("orders") or []
    rep.check(status == 200 and isinstance(orders, list),
              "订单列表返回数组", f"{status} {len(orders)} 单 {ms:.0f}ms")
    if orders:
        keys = {"id", "order_no", "state", "table_id", "items", "due_at"}
        rep.check(keys <= set(orders[0]), "订单字段齐全（进度条需要的都在）",
                  ",".join(sorted(orders[0].keys()))[:80])
        if orders[0].get("items"):
            ikeys = {"id", "dish_name", "qty", "state", "urge_count", "eta_state"}
            rep.check(ikeys <= set(orders[0]["items"][0]), "菜品条目字段齐全", "")

    status, data, ms = c.data("GET", "/api/customer/notifications", {}, token)
    rep.check(status == 200 and isinstance(data, dict),
              "通知接口可用（消息中心 4s 轮询）", f"{status} {ms:.0f}ms")

    print("\n[5] A11 扩展：画像 / 工单 / AI 客服")
    status, data, _ = c.data("GET", "/api/customer/guest/profile",
                             {"guest_key": guest_key}, token)
    rep.check(status == 200 and "portrait" in (data or {}),
              "顾客画像可用", f"{status}")

    status, data, _ = c.data("GET", "/api/customer/guest/complaints",
                             {"guest_key": guest_key}, token)
    rep.check(status == 200 and isinstance((data or {}).get("complaints"), list),
              "我的工单列表可用", f"{status}")

    print("\n[6] 溯源（免令牌，匿名可查）")
    status, data, ms = c.data("GET", "/api/customer/trace", {"code": DEMO_TRACE_CODE})
    trace_ok = status == 200 and (data or {}).get("code") == DEMO_TRACE_CODE
    rep.check(trace_ok, f"溯源码 {DEMO_TRACE_CODE} 可查", f"{status}")
    status, ctype, blob = c.raw("/api/customer/trace/label.png",
                                {"code": DEMO_TRACE_CODE})
    rep.check(status == 200 and blob[:8] == b"\x89PNG\r\n\x1a\n",
              "溯源二维码 PNG 可下载", f"{status} {ctype} {len(blob)}B")

    print("\n[7] 轮询抖动（订单页 2s 一次，慢请求会叠罗汉）")
    lat = []
    for _ in range(5):
        _, _, ms = c.call("GET", "/api/customer/orders", token=token)
        lat.append(ms)
        time.sleep(0.2)
    p95 = max(lat)
    rep.check(p95 < 1000, "订单轮询 p100 < 1s（不会越轮越积压）",
              f"p50={statistics.median(lat):.0f}ms p100={p95:.0f}ms")
    return token, guest_key, menu


# ------------------------------------------------------------------ 写路径
def check_write(c: Client, rep: Report, token: str, guest_key: str,
                menu: dict, table_id: int) -> str:
    """真下单 + 真催菜 + 真对话。返回 order_no（失败返回空串）。"""
    dishes = [d for d in (menu.get("dishes") or []) if not d.get("sold_out")][:2]
    if len(dishes) < 2:
        rep.check(False, "写路径跳过：可用菜品不足 2 道", "")
        return ""
    items = [{"code": d["code"], "qty": 1} for d in dishes]

    print("\n[8] 真下单（cart 页的唯一写操作）")
    status, data, ms = c.data("POST", "/api/customer/orders",
                              {"items": items, "table_id": table_id}, token)
    order_no = (data or {}).get("order_no", "") if status == 200 else ""
    rep.check(status == 200 and bool(order_no), "下单成功",
              f"{status} {order_no} {ms:.0f}ms")
    status, data, _ = c.data("GET", "/api/customer/orders", {}, token)
    mine = [o for o in ((data or {}).get("orders") or []) if o.get("order_no") == order_no]
    rep.check(bool(mine), "新单立刻出现在「我的订单」里", f"{len(mine)} 条")
    item_ids = [it["id"] for it in (mine[0]["items"] if mine else [])]
    rep.check(len(item_ids) == 2, "两个菜品条目都落库", f"item_ids={item_ids}")

    print("\n[9] 催菜与限频（前端 30s 冷却对应后端 429）")
    if item_ids:
        status, data, _ = c.data("POST", f"/api/customer/orders/items/{item_ids[0]}/urge",
                                 {}, token)
        rep.check(status == 200 and (data or {}).get("urge_count") == 1,
                  "第一次催菜成功", f"{status} count={(data or {}).get('urge_count')}")
        status, data, _ = c.data("POST", f"/api/customer/orders/items/{item_ids[0]}/urge",
                                 {}, token)
        rep.check(status == 429, "30s 内二次催菜被限频", f"{status}")

    print("\n[10] AI 客服对话（要云端真的配了 LLM key）")
    status, data, ms = c.data("POST", "/api/customer/guest/chat",
                              {"guest_key": guest_key, "text": "我的菜还要多久",
                               "order_id": (mine[0]["id"] if mine else None)}, token)
    reply = (data or {}).get("reply", "") if status == 200 else ""
    rep.check(status == 200 and bool(reply), "客服返回了回复",
              f"{status} {ms:.0f}ms intent={(data or {}).get('intent')}")
    rep.check((data or {}).get("intent") in CHAT_INTENTS,
              "意图命中白名单", str((data or {}).get("intent")))
    if (data or {}).get("mode"):
        rep.check(True, "回复来源", str(data.get("mode")))
    return order_no


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="云端小程序链路验收")
    ap.add_argument("--base", default="", help="服务地址，默认取 miniapp/config.js 的 BASE_URL")
    ap.add_argument("--table", type=int, default=3, help="开桌桌号，默认 3")
    ap.add_argument("--write", action="store_true", help="加跑下单/催菜/客服对话（会留真实订单）")
    ap.add_argument("--timeout", type=float, default=15.0, help="单请求超时秒数")
    args = ap.parse_args(argv)

    cfg = config_base_url()
    base = (args.base or cfg).rstrip("/")
    if not base:
        print("找不到服务地址：config.js 里没有 BASE_URL，也没有给 --base")
        return 2
    if cfg and args.base and base != cfg:
        print(f"注意：代码包里的 BASE_URL={cfg}，本次验的是 {base}（真机会打前者）")

    print(f"目标 {base}   桌号 {args.table}   模式 {'读写' if args.write else '只读'}")
    c = Client(base, timeout=args.timeout)
    rep = Report()
    try:
        token, guest_key, menu = check_readonly(c, rep, args.table)
    except RuntimeError as err:
        print(f"\n[FAIL] {err}")
        print("排查顺序：云端服务是否在跑 → 安全组/防火墙是否放行 80 → "
              "代码包 BASE_URL 是否还是旧 IP")
        return 1

    order_no = ""
    if args.write and token and menu:
        try:
            order_no = check_write(c, rep, token, guest_key, menu, args.table)
        except RuntimeError as err:
            print(f"\n[FAIL] 写路径中断：{err}")

    code = rep.summary()
    if order_no:
        print(f"\n本次写路径生成了真实订单 {order_no}。要还原成出厂演示态，"
              f"在服务器上执行（把 <容器名> 换成 docker ps 里的 mysql 容器）：")
        print(f"  docker exec -i <容器名> mysql -uroot -p\"$MYSQL_ROOT_PASSWORD\" kitchen -e \""
              f"DELETE oi FROM order_items oi JOIN orders o ON o.id=oi.order_id "
              f"WHERE o.order_no='{order_no}'; DELETE FROM orders WHERE order_no='{order_no}';\"")
    return code


if __name__ == "__main__":
    sys.exit(main())
