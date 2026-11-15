# -*- coding: utf-8 -*-
"""帧 → 像素的机械翻译（文本渲染版）。

## 为什么渲染成 60 列 × 22 行的文本帧

2.8" 320x240、16px 中文字模 → 每行约 20 个汉字；帧模型 + 这张表就是
「LVGL 布局的镜像」：LVGL 版按同样的行列摆 label，两版内容必须逐行一致
（设计文档 08 的映射表就是这个约定）。测试锁的是**帧模型**的行数与字段，
不锁 ASCII 画法。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = ["render_frame", "render_frames"]

_WIDTH = 60
_HEIGHT = 22

_STEP_MARK = {"confirmed": "[√]", "current": "[▶]", "pending": "[ ]"}
_STATE_MARK = {"ok": "[√]", "bad": "[×]", "untouched": "[ ]"}


def _cut(text: str, width: int = _WIDTH) -> str:
    """按显示宽度截断（汉字算 2 列）。"""
    shown = 0
    out = []
    for ch in str(text):
        wide = 1 if ord(ch) < 128 else 2
        if shown + wide > width:
            break
        out.append(ch)
        shown += wide
    return "".join(out)


def _center(text: str) -> str:
    wide = sum(2 if ord(ch) >= 128 else 1 for ch in str(text))
    pad = max(0, (_WIDTH - wide) // 2)
    return " " * pad + _cut(text)


def render_frame(screen: Mapping[str, Any]) -> str:
    """渲染一屏（含状态栏与离线红框）。"""
    lines: list[str] = []

    status = screen.get("status") or {}
    if not status.get("online", True):
        lines.append("█" * _WIDTH)
        lines.append(_center("⚠ 离线模式 " +
                             str(status.get("hint") or "")))
        lines.append("█" * _WIDTH)
    else:
        head = (f"{status.get('clock', '')}  {status.get('device', '')}  "
                f"在线  待同步 {status.get('pending_actions', 0)}")
        lines.append(_cut(head))
        lines.append("─" * _WIDTH)

    stype = screen.get("type")
    if stype == "queue":
        lines.extend(_render_queue(screen))
    elif stype == "steps":
        lines.extend(_render_steps(screen))
    elif stype == "card":
        lines.extend(_render_card(screen))

    while len(lines) < _HEIGHT:
        lines.append("")
    lines = lines[:_HEIGHT]
    return "\n".join(lines)


def render_frames(screens: list[Mapping[str, Any]]) -> list[str]:
    return [render_frame(row) for row in screens]


# --------------------------------------------------------------------- 各屏
def _render_queue(screen: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    rows = screen.get("rows") or []
    if not rows:
        lines.append(_center(screen.get("empty_text") or "暂无任务"))
        return lines
    lines.append(_cut("待配 / 在做"))
    lines.append("─" * _WIDTH)
    for row in rows:
        mark = f"[{row.get('mark')}]" if row.get("mark") else "   "
        # 扫码单在单号后挂桌号短标签（如 `3桌`）；员工手工单没有桌台时
        # tag 为空，这一行与改动前逐字一致——既有快照断言不会假红。
        tag = f" {row.get('table_short')}" if row.get("table_short") else ""
        line = (f"{mark} {row.get('order_no', '')}{tag} "
                f"{row.get('dish', '')} x{row.get('qty', 1)}"
                f"  工位{row.get('station') or '-'}")
        lines.append(_cut(line))
    if screen.get("truncated"):
        lines.append(_center(f"（还有 {screen['truncated']} 条未显示）"))
    return lines


def _render_steps(screen: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    title = f"{screen.get('dish', '')}  {screen.get('order_no', '')}"
    lines.append(_cut(title))
    if not screen.get("session_active"):
        lines.append(_cut("（工序会话未开启，开始称重后自动建立）"))
    lines.append("─" * _WIDTH)
    lines.append(_cut("配料称重"))
    for row in screen.get("weighs") or []:
        measured = row.get("measured_g")
        target = row.get("target_g")
        text = f"  {_STATE_MARK.get(row.get('state'), '[ ]')} " \
               f"{row.get('ingredient')} " \
               f"{'' if measured is None else f'{measured}g'}/目标{target}g"
        lines.append(_cut(text))
    lines.append("─" * _WIDTH)
    if screen.get("locked"):
        lines.append(_cut("⚠ 称重超差锁定：" +
                          "、".join(screen.get("locked_ingredients") or ())))
    lines.append(_cut("工序步骤"))
    for step in screen.get("steps") or ():
        mark = _STEP_MARK.get(step.get("state"), "[ ]")
        flags = f"({step.get('flags')})" if step.get("flags") else ""
        lines.append(_cut(f"  {mark} {step.get('step_no')}. "
                          f"{step.get('title')} {flags}"))
    if screen.get("ready"):
        lines.append(_center("✔ 门禁通过，可出餐"))
    return lines


def _render_card(screen: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    tone = screen.get("tone")
    if tone == "green":
        lines.append(_center("█ 身份确认，可以开工 █"))
    elif tone == "yellow":
        lines.append(_center("█ 离线放行（待复核） █"))
    else:
        lines.append(_center("██ 拒绝上岗 ██"))
    name = screen.get("name")
    if name:
        lines.append(_center(name))
    for line in screen.get("lines") or ():
        lines.append(_cut("  " + line))
    return lines
