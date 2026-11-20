"""tabBar 线性图标生成器（81×81 PNG）。

为什么自己画而不是找一套图标包：
1. 小程序 tabBar 只认 PNG，不能内联 SVG，引一套第三方图标就要连 license 一起带进毕设，
   来源不明反而多一个被追问的点；
2. 菜品主图是手绘水彩，图标必须是同一种「细线 + 圆端点」语言，才不像两套东西拼的；
3. 线条参数化后，改主色只改一个常量，不用重新找图。

用法：
    python tools/make_tabbar_icons.py            # 生成未选中 + 选中两态
    python tools/make_tabbar_icons.py --check    # 只校验

注意：颜色是 app.wxss 里 --text-2 / --accent 的拷贝。改主题色时这里要同步，
      --check 会比对 page 块的变量值，不一致直接报错，避免图标和 UI 脱色。
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "miniapp" / "assets" / "tabbar"
APP_WXSS = ROOT / "miniapp" / "app.wxss"

SIZE = 81
W = 5                 # 线宽：81px 下约 2.5pt，真机上看是细线但不至于虚
IDLE = "#6b6355"      # --text-2
ACTIVE = "#1f4d3a"    # --accent


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _round_line(d, points, color, width=W):
    """圆头折线：Pillow 的 line 没有 linecap，用两端小圆补出来。"""
    d.line(points, fill=color, width=width, joint="curve")
    r = width / 2 - 0.5
    for x, y in (points[0], points[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def icon_menu(color) -> Image.Image:
    """点菜：一只碗 + 一双斜插的筷子。

    碗的构成：一条比碗身更宽的碗口线（两端各出 4px，像碗沿）+ 完整下半圆弧。
    弧必须从 0 画到 180，端点正好落在碗口线上；少画一段会在两边留缺口，
    再加「碗底」短线就会变成带脚的碗（第一版就是这个毛病，像蘑菇）。
    """
    img, d = _canvas()
    # arc 的端点落在 bbox 垂直中心线上，所以 bbox 中心 y 必须等于碗口线的 y，
    # 否则横线和半圆之间会留一道缝（第一版就画成了悬空的横线 + 下面的半圆）
    d.line([(15, 40), (65, 40)], fill=color, width=W)
    d.arc([18, 12, 62, 68], start=0, end=180, fill=color, width=W)
    # 筷子：两根平行斜线，末端停在碗口上方留一点缝，插进去会看成碗的一部分
    _round_line(d, [(47, 10), (39, 32)], color, width=4)
    _round_line(d, [(57, 14), (49, 34)], color, width=4)
    return img


def icon_orders(color) -> Image.Image:
    """订单：一张单据 + 三条明细线。"""
    img, d = _canvas()
    d.rounded_rectangle([20, 14, 61, 67], radius=8, outline=color, width=W)
    for y in (30, 41, 52):
        _round_line(d, [(30, y), (51, y)], color, width=4)
    return img


def _bezier(p0, p1, p2, n=40):
    """二次贝塞尔采样：Pillow 没有曲线，叶子的尖头只能自己算点。"""
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append((x, y))
    return pts


def icon_trace(color) -> Image.Image:
    """溯源：一片带尖头的叶子 + 一条叶脉。

    不能用椭圆旋转凑：转出来的两头都是圆的，81px 下就是一颗胶囊。
    叶子必须「两头收尖」才认得出，所以左右两条边各用一段贝塞尔，首尾相接成闭合路径。
    """
    img, d = _canvas()
    tip, stem = (40, 13), (40, 67)
    # 控制点故意放到画布外：贝塞尔中点只吃到它的一半权重，
    # 想让叶子最宽处到 42px，控制点必须比目标边界再外扩一段
    left = _bezier(tip, (-2, 40), stem)
    right = _bezier(stem, (82, 40), tip)
    d.line(left + right, fill=color, width=W, joint="curve")
    # 叶脉：从叶尖下方的中轴往下拉，末端靠近叶柄
    _round_line(d, [(40, 26), (40, 54)], color, width=3)
    return img


def icon_profile(color) -> Image.Image:
    """我的：头 + 肩。"""
    img, d = _canvas()
    d.ellipse([31, 15, 50, 34], outline=color, width=W)
    d.arc([17, 40, 64, 82], start=196, end=344, fill=color, width=W)
    return img


ICONS = {
    "menu": icon_menu,
    "orders": icon_orders,
    "trace": icon_trace,
    "profile": icon_profile,
}


def theme_colors() -> tuple[str, str]:
    """从 app.wxss 的 page 块取 --text-2 / --accent，保证图标和主题同源。"""
    text = APP_WXSS.read_text(encoding="utf-8")
    block = re.search(r"page\s*\{(.*?)\n\}", text, re.S)
    scope = block.group(1) if block else text

    def pick(name: str, fallback: str) -> str:
        m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", scope)
        return m.group(1) if m else fallback

    return pick("text-2", IDLE), pick("accent", ACTIVE)


def build() -> int:
    idle, active = theme_colors()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in ICONS.items():
        for suffix, color in (("", idle), ("_on", active)):
            path = OUT / f"tab_{name}{suffix}.png"
            fn(_rgb(color)).save(path, "PNG", optimize=True)
    total = sum(p.stat().st_size for p in OUT.glob("*.png"))
    print(f"  ✓ {len(ICONS) * 2} 张图标 → {OUT.relative_to(ROOT)}"
          f"（合计 {total / 1024:.1f}KB，未选中 {idle} / 选中 {active}）")
    return 0


def check() -> int:
    idle, active = theme_colors()
    problems = []
    if (idle, active) != (IDLE, ACTIVE):
        problems.append(f"图标颜色 {IDLE}/{ACTIVE} 与 app.wxss 的 {idle}/{active} 不一致，先同步常量")
    for name in ICONS:
        for suffix in ("", "_on"):
            if not (OUT / f"tab_{name}{suffix}.png").is_file():
                problems.append(f"缺图标 tab_{name}{suffix}.png，跑一次不带 --check 的生成")
    for p in problems:
        print("  ✗", p)
    print(f"=== tabBar 图标自检：{'通过' if not problems else f'{len(problems)} 项待处理'} ===")
    return 0 if not problems else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="tabBar 线性图标生成器")
    ap.add_argument("--check", action="store_true", help="只校验不生成")
    if ap.parse_args().check:
        return check()
    return build()


if __name__ == "__main__":
    sys.exit(main())
