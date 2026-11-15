#!/usr/bin/env python
"""A7 管理端 Web 验收自检：**八个页面各自的数据链路是否真的通**。

答辩前跑一遍比「打开浏览器逐个点」更可靠，因为页面打开时是空表格还是
「接口 500」，肉眼看起来都只是「没数据」。

它做四件事：

1. 构建产物（`web/dist`）在不在，`index.html` 引用的每个资源是否真的存在；
2. 八个页面用到的每个只读接口，逐个直调并检查必需字段；
3. 每个写接口确认已被登记（不实际执行，避免改坏演示数据）；
4. 打印一张「页面 → 接口 → 状态」的表，出问题的地方退出码非 0。

用法::

    python tools/web_check.py            # 全量检查
    python tools/web_check.py --serve    # 检查完顺手起一个本地服务（Ctrl+C 退出）
    python tools/web_check.py --json     # 只输出 JSON，便于 CI 消费
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kitchen.api import AppState, AppStateConfig, ApiServer, create_server  # noqa: E402

WEB = ROOT / "web"
DIST = WEB / "dist"

#: 页面 → 它必须打得通的只读接口 + 必须存在的字段。
#: 少一个字段页面就会「整块空白」，而不是报错，所以这里逐个点名。
PAGES: tuple[tuple[str, str, tuple[str, tuple[str, ...]]], ...] = (
    ("总览与 KPI", "kpi", ("/api/overview", ("channels", "degrade", "telemetry", "counts")),
                   ("/api/kpi", ("cook_duration",))),
    ("订单与调度", "orders", ("/api/orders", ("orders", "total")),
                    ("/api/dishes", ("dishes",))),
    ("工位甘特", "stations", ("/api/stations", ("stations",)),
                  ("/api/stations/gantt", ("segments", "handovers"))),
    ("环境与设备", "environment", ("/api/telemetry", ("series",)),
                     ("/api/interlock", ("channels",))),
    ("配方与工艺", "recipes", ("/api/recipes", ("recipes",))),
    ("批次与资质", "compliance", ("/api/batches", ("batches",)),
                      ("/api/staff", ("staff",)),
                      ("/api/staff/certificates", ("certificates",)),
                      ("/api/morning", ("staff",))),
    ("工单中心", "tickets", ("/api/tickets", ("tickets",))),
    ("远程控制与参数", "control", ("/api/params", ("rules", "demo_map")),
                       ("/api/meta", ("actions", "metrics")),
                       ("/api/audit", ("entries",))),
)

#: 写接口清单：只检查「路由在不在」，不真的执行（会把演示数据改坏）。
WRITE_ROUTES = (
    ("POST", "/api/orders"),
    ("POST", "/api/orders/items/{item_id}/dispatch"),
    ("POST", "/api/orders/items/{item_id}/urge"),
    ("POST", "/api/orders/items/{item_id}/state"),
    ("POST", "/api/dishes/{dish_id}/soldout"),
    ("POST", "/api/stations/occupy"),
    ("POST", "/api/stations/release"),
    ("POST", "/api/admission/scan"),
    ("POST", "/api/morning/submit"),
    ("POST", "/api/morning/review"),
    ("POST", "/api/tickets/{ticket_id}/ack"),
    ("POST", "/api/tickets/{ticket_id}/close"),
    ("POST", "/api/recipes/{dish_id}/versions"),
    ("PUT", "/api/params"),
    ("POST", "/api/params/time-scale"),
    ("POST", "/api/channels/{channel}/actions"),
    ("POST", "/api/channels/{channel}/review"),
    ("POST", "/api/network"),
    ("POST", "/api/admin/flush"),
    ("POST", "/api/demo/orders"),
    ("POST", "/api/demo/inject"),
)


class Checker:
    def __init__(self, verbose: bool = True) -> None:
        self.rows: list[dict] = []
        self.ok = 0
        self.bad = 0
        self.verbose = verbose

    def add(self, scope: str, name: str, passed: bool, detail: str = "") -> None:
        self.rows.append({"scope": scope, "check": name,
                          "passed": bool(passed), "detail": detail})
        if passed:
            self.ok += 1
            if self.verbose:
                print(f"  [OK] {name} {detail}")
        else:
            self.bad += 1
            if self.verbose:
                print(f"  [!!] {name} {detail}")


def check_dist(check: Checker) -> None:
    print("\n① 构建产物")
    index = DIST / "index.html"
    check.add("dist", "dist/index.html 存在", index.is_file(),
              "" if index.is_file() else "先跑 `cd web && npm run build`")
    if not index.is_file():
        return
    html = index.read_text(encoding="utf-8")
    check.add("dist", "挂载点 #app", '<div id="app">' in html)
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
    check.add("dist", "资源引用", bool(assets), f"{len(assets)} 个")
    missing = [path for path in assets if not (DIST / path.lstrip("/")).is_file()]
    check.add("dist", "资源文件齐全", not missing, str(missing) if missing else "")
    total = sum((DIST / path.lstrip("/")).stat().st_size
                for path in assets if (DIST / path.lstrip("/")).is_file())
    print(f"       构建产物合计 {total / 1024:.0f} KB（未压缩）")


def check_views(check: Checker) -> None:
    print("\n② 页面文件")
    app = (WEB / "src" / "App.vue").read_text(encoding="utf-8")
    routes = re.findall(r"\{ key: '(\w+)', path: '#/(\w+)'", app)
    check.add("views", "路由条数", len(routes) >= 8, f"{len(routes)} 条")
    imported = re.findall(r"from '\./views/(\w+)\.vue'", app)
    missing = [name for name in imported
               if not (WEB / "src" / "views" / f"{name}.vue").is_file()]
    check.add("views", "视图文件齐全", not missing,
              f"{len(imported)} 个" if not missing else str(missing))


def check_apis(check: Checker, server) -> None:
    print("\n③ 页面数据链路")
    token, session = server.login.login("admin", "kitchen2026", source="web-check")
    check.add("api", "登录拿令牌", bool(token))

    for page, _key, *endpoints in PAGES:
        for path, fields in endpoints:
            status, payload = server.router.call("GET", path, session=session)
            if status != 200:
                check.add(page, path, False, f"HTTP {status} {payload.get('detail', '')}")
                continue
            data = payload.get("data") or {}
            absent = [field for field in fields if field not in data]
            nonzero = ""
            for field in fields:
                value = data.get(field)
                if isinstance(value, (list, tuple, dict)) and len(value) == 0:
                    nonzero = f"（{field} 为空）"
            check.add(page, path, not absent,
                      f"缺字段 {absent}" if absent else f"OK{nonzero}")


def check_write_routes(check: Checker, server) -> None:
    print("\n④ 写接口登记")
    registered = {(route.method, route.pattern) for route in server.router.routes}
    missing = [f"{method} {path}" for method, path in WRITE_ROUTES
               if (method, path) not in registered]
    check.add("write", "写接口全部登记", not missing,
              f"{len(WRITE_ROUTES)} 个" if not missing else f"缺 {missing}")


def check_health(check: Checker, server) -> None:
    print("\n⑤ 设备与会话")
    try:
        server.state.tick()
        check.add("runtime", "采样一帧", True)
    except Exception as exc:  # noqa: BLE001
        check.add("runtime", "采样一帧", False, str(exc))
    overview = server.state.overview()
    check.add("runtime", "12 路遥测", len(overview.get("telemetry") or {}) >= 12,
              f"{len(overview.get('telemetry') or {})} 路")
    check.add("runtime", "三通道快照", {"CH1", "CH2", "CH3"} <= set(overview.get("channels") or {}))
    check.add("runtime", "降级报告", "degrade" in overview)


def main() -> int:
    parser = argparse.ArgumentParser(description="A7 管理端 Web 验收自检")
    parser.add_argument("--serve", action="store_true", help="检查完起本地服务（默认 8848）")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    verbose = not args.quiet and not args.json
    check = Checker(verbose=verbose)

    state = AppState(config=AppStateConfig(var_dir=ROOT / ".local" / "web-check"))
    # 先造演示订单：**空表也能通过「字段齐全」的检查**，但「订单页是空的」
    # 和「订单页打不开」在答辩现场看起来一样糟。所以这里和 `server.py` 的
    # 启动路径保持一致，看到的是有数据的样子。
    try:
        state.demo.seed_orders()
    except Exception as exc:  # noqa: BLE001 - 种子失败不该让自检整体失败
        if verbose:
            print(f"  [!!] 演示订单种子失败（不影响检查）：{exc}")
    server = create_server(state, secret="web-check")

    if verbose:
        print("=" * 62)
        print("A7 管理端 Web 验收自检")
        print("=" * 62)

    check_dist(check)
    check_views(check)
    check_apis(check, server)
    check_write_routes(check, server)
    check_health(check, server)

    if args.json:
        print(json.dumps({"ok": check.ok, "failed": check.bad, "rows": check.rows},
                         ensure_ascii=False, indent=2))
    elif verbose:
        print("\n" + "=" * 62)
        print(f"通过 {check.ok} 项，失败 {check.bad} 项")
        if check.bad:
            print("失败明细：")
            for row in check.rows:
                if not row["passed"]:
                    print(f"  - [{row['scope']}] {row['check']} {row['detail']}")
        print("=" * 62)

    if args.serve and not check.bad:
        api = ApiServer(state, port=args.port, secret="web-check",
                        static_dir=DIST if DIST.is_dir() else None)
        api.start()
        print(f"\n服务已启动：http://127.0.0.1:{api.port}/  （Ctrl+C 退出）")
        print("演示账号：admin / kitchen2026（组长 lead01、厨师 chef01、只读 guest）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            api.stop()
        finally:
            state.stop()

    return 0 if check.bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
