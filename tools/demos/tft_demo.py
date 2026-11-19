# -*- coding: utf-8 -*-
"""A8 后厨 TFT 终端全链路演示：模拟终端跑一遍「一天的后厨」。

## 演示什么（九段）

1. **终端上线**：登录 → 拉快照与白名单 → 语音 001；
2. **新单**：管理端造单 → 终端轮询到事件 → 响铃 002 → 队列屏；
3. **首刷拦截**（#8）：张三刷卡 → 「今日未晨检」→ 屏给问卷提示；
4. **晨检后准入**：提交问卷 → 再刷 → 绿屏 + 语音 005；
5. **工序分步**（#12/#13）：称重超差 → 播报 007 + 锁定红字；重称合格解锁；
   按序确认 6 步（CCP/需刷卡步骤带标记）；
6. **断网**（A8-05）：红框队列屏（缓存快照仍可看）+ 语音 009；
   李四证过期 → 红拒（弱校验宁可误拒）；张三 → 黄屏离线放行 + 动作落盘；
7. **恢复同步**：语音 010 → 回放离线动作（服务端强校验）；
8. **报警**（A8-04）：服务端注入燃气 → 轮询 → 语音 004；
9. **出餐**（#13 门禁）：全部合格 → serve → 语音 008。

## 用法

```
python tools/demos/tft_demo.py
python tools/demos/tft_demo.py --quiet      # 只看结论不画屏
```

## 与真机的关系

本 demo 的每一步 = 固件主循环的一个状态（`03_设计文档/08_后厨TFT终端设计.md`
的映射表）。板子到货后固件按同一份接口契约实现，演示脚本即验收脚本。
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from kitchen.api import create_server                          # noqa: E402
from kitchen.tft import DirectTransport, TftTerminal, render_frame  # noqa: E402
from api_support import make_state                              # noqa: E402

TFT_ACCOUNT = ("chef01", "kitchen2026")


def line(kind: str, text: str) -> str:
    marks = {"step": "▶", "ok": "  ✓", "deny": "  ✗", "info": "  ·",
             "warn": "  !", "voice": "  ♪"}
    return f"{marks.get(kind, '  ')} {text}"


def main() -> int:
    parser = argparse.ArgumentParser(description="后厨 TFT 终端全链路演示（A8）")
    parser.add_argument("--quiet", action="store_true", help="只输出结论不画屏")
    args = parser.parse_args()
    verbose = not args.quiet
    logging.getLogger("kitchen").setLevel(logging.ERROR)

    # 演示落盘全部收进 .local/tmp（本机 TEMP 是 8.3 短名不可写，见 README 环境坑）
    workdir = Path(".local/tmp/tft-demo")
    workdir.mkdir(parents=True, exist_ok=True)
    state = make_state(workdir / "var")
    server = create_server(state, port=0, secret="tft-demo-secret")

    out: list[str] = []
    out.append("后厨 TFT 终端演示 · 模拟终端与 43 条接口走同一条链路")
    out.append("=" * 78)

    # ---------------------------------------------------------- ① 上线
    out.append(line("step", "① 09:00 终端上电，登录并拉快照/白名单"))
    terminal = TftTerminal(DirectTransport(server),
                           device_id="tft-node-01",
                           queue_dir=workdir / "tft")
    # 清掉上次演示残留：队列 journal 是持久化的（掉电不丢靠它），
    # 但 demo 语义是「从头演一遍」，残留会让「离线动作 N 条」对不上数
    terminal.queue.reset()
    terminal.player.play("boot")
    terminal.login(*TFT_ACCOUNT)
    terminal.poll()
    out.append(line("ok", f"登录 {TFT_ACCOUNT[0]}（operator），白名单缓存 "
                          f"{len(terminal.admission_cache)} 张卡"))
    out.append(line("voice", "语音 001「设备自检完成」"))

    # ---------------------------------------------------------- ② 新单
    out.append(line("step", "② 09:05 顾客下单两道热菜 → 终端轮询到事件"))
    # 管理端代下一单（miniapp 来源标记），用登录会话走正规接口：
    _admin_session = server.login.login("admin", "kitchen2026", source="demo")[1]
    _status, _payload = server.router.call(
        "POST", "/api/orders",
        data={"items": [{"code": "D-QJRS", "qty": 1}],
              "source": "miniapp", "channel": "miniapp"},
        session=_admin_session,
    )
    assert _status == 200, _payload
    terminal.poll()
    last = terminal.player.log[-1]
    out.append(line("voice", f"语音 {last.no:03d}「{last.text}」"))
    if verbose:
        out.append("")
        out.append("── 队列屏（在线）" + "─" * 58)
        out.append(render_frame(terminal.screen_queue()))
        out.append("")

    # 取第一个子项做工序演示
    orders = terminal.snapshot.get("orders") or []
    items = [item for order in orders for item in order.get("items", ())]
    item_id = items[0]["id"] if items else 0

    # ---------------------------------------------------------- ③ 首刷拦截
    out.append(line("step", "③ 09:10 张三刷卡 → 未晨检被拦（#8 先问卷后准入）"))
    result = terminal.scan("A1B2C3D4", station_id=1)
    out.append(line("deny", result["detail"]))
    if verbose:
        out.append("")
        out.append("── 刷卡屏（拦截）" + "─" * 58)
        out.append(render_frame(terminal.screen_card(result)))
        out.append("")

    # ---------------------------------------------------------- ④ 晨检后准入
    out.append(line("step", "④ 09:11 TFT 屏答晨检问卷（36.2℃ 无症状）→ 再刷"))
    _m = server.router.call(
        "POST", "/api/morning/submit",
        data={"card_uid": "A1B2C3D4",
              "answers": {"temp_c": 36.2, "diarrhea": False,
                          "pharyngitis": False, "hand_wound": False}},
        session=terminal.session,
    )
    assert _m[0] == 200, _m
    result = terminal.scan("A1B2C3D4", station_id=1)
    out.append(line("ok", result["detail"]))
    out.append(line("voice", "语音 005「身份确认」"))
    if verbose:
        out.append("")
        out.append("── 刷卡屏（放行）" + "─" * 58)
        out.append(render_frame(terminal.screen_card(result)))
        out.append("")

    # ---------------------------------------------------------- ⑤ 工序分步
    out.append(line("step", "⑤ 09:12 工序分步：派单进 cooking 后开始称重"))
    server.router.call("POST", f"/api/orders/items/{item_id}/dispatch",
                       data={"station_id": 1}, session=terminal.session)
    server.router.call("POST", f"/api/orders/items/{item_id}/state",
                       data={"state": "cooking"}, session=terminal.session)
    screen = terminal.screen_steps(item_id)
    out.append(line("info", f"{screen['dish']} · {len(screen['steps'])} 步 · "
                            f"{len(screen['weighs'])} 味料"))
    # 第一称超差
    result = terminal.weigh(item_id, "猪里脊", 180.0, staff_id=1)
    out.append(line("warn", result["check"]["detail"]))
    terminal.player.play("weigh_bad")
    out.append(line("voice", "语音 007「配料超差，请重新称重」"))
    if verbose:
        out.append("")
        out.append("── 工序屏（称重超差锁定）" + "─" * 50)
        out.append(render_frame(terminal.screen_steps(item_id)))
        out.append("")
    # 重称合格 + 其余三味
    for ingredient, grams in (("猪里脊", 150.0), ("青椒", 100.0),
                              ("食用油", 20.0), ("生抽", 8.0)):
        terminal.weigh(item_id, ingredient, grams, staff_id=1)
    out.append(line("ok", "四味料全部合格，锁定解除"))
    # 按序确认 6 步（1/4/6 是 CCP 且需刷卡）
    for step_no in range(1, 7):
        terminal.confirm(item_id, step_no,
                         staff_id=1 if step_no in (1, 4, 6) else None)
    screen = terminal.screen_steps(item_id)
    out.append(line("ok", f"6 步全部确认（含 3 个 CCP），门禁通过："
                          f"ready={screen['ready']}"))
    if verbose:
        out.append("")
        out.append("── 工序屏（全部完成）" + "─" * 54)
        out.append(render_frame(screen))
        out.append("")

    # ---------------------------------------------------------- ⑥ 断网
    out.append(line("step", "⑥ 09:20 WiFi 断开 → 红框离线屏，缓存队列仍可看"))
    terminal.set_link(False)
    out.append(line("voice", "语音 009「网络断开，转入离线模式」"))
    if verbose:
        out.append("")
        out.append("── 队列屏（离线红框）" + "─" * 56)
        out.append(render_frame(terminal.screen_queue()))
        out.append("")
    # 李四（证过期）离线刷卡 → 弱校验红拒
    result = terminal.scan("E5F6A7B8", station_id=1)
    out.append(line("deny", result["detail"] + "（弱校验宁可误拒不放行）"))
    # 张三离线刷卡 → 黄屏放行 + 动作落盘
    result = terminal.scan("A1B2C3D4", station_id=1)
    out.append(line("warn", result["detail"]))
    out.append(line("info", f"离线动作队列：{len(terminal.queue)} 条（jsonl 落盘，断电不丢）"))
    if verbose:
        out.append("")
        out.append("── 刷卡屏（离线放行，黄屏）" + "─" * 50)
        out.append(render_frame(terminal.screen_card(result)))
        out.append("")

    # ---------------------------------------------------------- ⑦ 恢复同步
    out.append(line("step", "⑦ 09:25 网络恢复 → 回放离线动作（服务端强校验）"))
    terminal.set_link(True)     # 触发 sync
    out.append(line("voice", "语音 010「网络恢复，数据已同步」"))
    out.append(line("ok", f"回放完成：队列剩 {len(terminal.queue)} 条，"
                          f"被拒 {len(terminal.denied)} 条"))

    # ---------------------------------------------------------- ⑧ 报警
    out.append(line("step", "⑧ 09:30 服务端注入燃气浓度 0.9 → 终端播报"))
    server.router.call("POST", "/api/demo/inject",
                       data={"metrics": {"gas_level": 0.9}},
                       session=_admin_session)
    terminal.poll()
    out.append(line("warn", "燃气浓度超限 → 关阀动作已由保护层下发"))
    alarms = [log for log in terminal.player.log if log.no == 4]
    if alarms:
        out.append(line("voice", f"语音 004「{alarms[-1].text}」"))
    else:
        out.append(line("warn", "（报警在去抖窗口内被抑制，未重复播报）"))

    # ---------------------------------------------------------- ⑨ 出餐
    out.append(line("step", "⑨ 09:35 出餐：门禁（#12+#13）通过 → ready"))
    result = terminal.serve(item_id, staff_id=1)
    ok = "order" in result
    out.append(line("ok", f"出餐 {'成功' if ok else '被拒'}：{result}"))
    if ok:
        out.append(line("voice", "语音 008「出餐确认」"))

    # ---------------------------------------------------------- 收尾
    out.append("")
    out.append("=" * 78)
    out.append(f"播报记录（{len(terminal.player.log)} 条）：")
    out.append("  " + " → ".join(
        f"{log.no:03d}{log.text}" for log in terminal.player.log))
    out.append(f"离线队列残留：{len(terminal.queue)} 条；"
               f"同步被拒：{len(terminal.denied)} 条")
    out.append("验收口径：真机与手机终端至少一条能完整演示——本演示为模拟终端链路，"
               "Web 终端（旧手机浏览器）已由 A7 覆盖。")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
