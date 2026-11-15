# -*- coding: utf-8 -*-
"""顾客小程序「网络不可用」一键体检（只读，不改任何配置）。

    python tools/miniapp_netcheck.py           # 只体检
    python tools/miniapp_netcheck.py --fix     # 8848 无监听时自动拉起后端

按「端口 → 回环 HTTP → 局域网 HTTP → 环境干扰」四层定位，最后给一句结论
和一条可直接复制的修复命令。90% 的现场问题是**后端服务压根没起**：
开发者工具往 8848 发 SYN 没人接，页面只会笼统显示「网络不可用」。

两个容易误判的点：

* 系统设了 `HTTP_PROXY` 时，`curl http://127.0.0.1:8848/...` 会走代理拿回
  **502**，看起来像服务挂了，其实服务可能好好的。这里一律用
  `ProxyHandler({})` 绕开代理直连。
* 端口「能连上」和「返回 200」是两回事：连上但返回 404/500 说明服务在，
  只是路径或后端状态有问题，不该去查防火墙。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from miniapp_check import _lan_ip  # noqa: E402

PORT = 8848
PROBE_PATH = "/api/customer/menu"


def _config_base_url() -> str:
    text = (ROOT / "miniapp" / "config.js").read_text(encoding="utf-8")
    match = re.search(r"const\s+BASE_URL\s*=\s*['\"]([^'\"]+)['\"]", text)
    return match.group(1) if match else "（读不到）"


def _section(title: str) -> None:
    print(f"\n[{title}]")


def _check_port(host: str = "127.0.0.1") -> bool:
    """TCP 三次握手能否完成。只判断「有没有人在听」，不判断 HTTP 对不对。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, PORT)) == 0


def _http(url: str) -> tuple[bool, str]:
    """绕过代理直连，返回 (是否 200, 描述)。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as resp:
            return resp.status == 200, f"HTTP {resp.status} {resp.headers.get('Content-Type', '')}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}（服务在，但不是预期响应）"
    except Exception as exc:  # URLError / timeout / ConnectionRefused
        return False, f"{type(exc).__name__}: {exc}"


def auto_start_server() -> bool:
    """`--fix`：8848 无监听时，新开一个**最小化控制台**把后端拉起来。

    之所以开新控制台而不是本进程内 serve：体检命令跑完就退出，服务必须
    挂在一个**不随本命令结束**的进程里；最小化窗口让「窗口关了=服务停」
    这件事依然肉眼可见（任务栏里能看到 Kitchen API 图标）。
    返回是否就绪（端口有监听）。
    """
    if _check_port():
        return True
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 6  # SW_MINIMIZE
    # CREATE_BREAKAWAY_FROM_JOB：某些执行环境（CI runner、嵌入终端）用 Job
    # Object 收割整棵进程树，不带这个标志的话，本命令一退出新拉起的服务就
    # 被连带杀掉——实测 PID 每次都换新。若环境不允许 breakaway 会抛
    # OSError，降级为普通启动（至少交互式 cmd / 双击场景不受影响）。
    flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_BREAKAWAY_FROM_JOB
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "miniapp_check.py"), "--serve", "--lan"],
            cwd=str(ROOT),
            creationflags=flags,
            startupinfo=startupinfo,
        )
    except OSError:
        subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "miniapp_check.py"), "--serve", "--lan"],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startupinfo,
        )
    for _ in range(15):  # 最多等 ~6 秒
        time.sleep(0.4)
        if _check_port():
            return True
    return False


def main() -> int:
    fix = "--fix" in sys.argv
    print("顾客小程序网络体检" + ("（--fix：服务没起会自动拉起）" if fix else "（只读）"))
    base = _config_base_url()
    lan = _lan_ip()

    _section("0 当前配置")
    cfg_text = (ROOT / "miniapp" / "project.config.json").read_text(encoding="utf-8")
    appid_match = re.search(r'"appid"\s*:\s*"([^"]+)"', cfg_text)
    appid = appid_match.group(1) if appid_match else "（读不到）"
    print(f"  miniapp/config.js  BASE_URL = {base}")
    print(f"  miniapp/project.config.json  appid = {appid}")
    print(f"  本机局域网 IP = {lan}")
    if "127.0.0.1" in base or "localhost" in base:
        print("  ⓘ 当前是【模拟器】配置；手机真机预览必须改成 http://" + lan + ":8848")

    _section("1 端口 8848 有没有服务在听")
    listening = _check_port()
    if not listening:
        print("  ❌ 无监听（后端服务未启动 —— 最常见根因）")
        if fix:
            print("  → --fix：自动拉起后端（新开最小化窗口，关窗口=停服务）...")
            if auto_start_server():
                listening = True
                print("  ✅ 已拉起，端口恢复监听")
            else:
                print("  ❌ 拉起后仍未监听，看最小化窗口里的报错")
    else:
        print("  ✅ 有监听")

    _section("2 回环 HTTP（开发者工具模拟器走这条）")
    ok_loop, msg_loop = _http(f"http://127.0.0.1:{PORT}{PROBE_PATH}")
    print(f"  {'✅' if ok_loop else '❌'} {msg_loop}")

    _section("3 局域网 HTTP（手机真机预览走这条）")
    ok_lan, msg_lan = _http(f"http://{lan}:{PORT}{PROBE_PATH}")
    print(f"  {'✅' if ok_lan else '❌'} {msg_lan}")

    _section("4 环境干扰")
    proxies = {k: v for k, v in os.environ.items()
               if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
    if proxies:
        for key, value in proxies.items():
            print(f"  ⓘ {key}={value}")
        print("  ⓘ curl 默认走代理会拿回 502 假象；验证请用 curl --noproxy '*' ...")
    else:
        print("  ✅ 未设置 HTTP(S)_PROXY")
    print("  ⓘ 手机端若超时，另查 Windows 防火墙是否放行 TCP 8848 入站")

    _section("结论")
    if ok_loop and ok_lan:
        print("  ✅ 后端就绪：模拟器与手机都不该再报「网络不可用」。")
        print("     仍报错 → 查开发者工具「不校验合法域名」/ 手机端「调试模式」/ BASE_URL 拼写。")
        return 0
    if not listening:
        print("  ❌ 根因：8848 没有服务在监听，后端没起（或窗口被关了）。")
        print("     修复：双击 tools/start_miniapp_server.bat 并**保持窗口开着**；")
        print("     或执行  python tools/miniapp_netcheck.py --fix   （自动拉起）")
        return 1
    if ok_loop and not ok_lan:
        print("  ❌ 回环通、局域网不通：服务只绑了 127.0.0.1，或防火墙拦了入站。")
        print("     修复：改用 --serve --lan 绑 0.0.0.0；放行 TCP 8848 入站。")
        return 1
    print(f"  ❌ 端口在监听但请求不正常（回环 {msg_loop} / 局域网 {msg_lan}）。")
    print("     修复：确认 8848 上跑的是本工程 API，而不是别的进程占了端口。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
