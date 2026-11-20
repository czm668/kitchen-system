# -*- coding: utf-8 -*-
"""菜单视觉资产生成器（A24）：菜品示意图 + 门店物料。

    python tools/menu_assets.py            # 生成全部资产（幂等，重跑覆盖）
    python tools/menu_assets.py --check    # 只校验：尺寸 / 哈希 / 映射是否齐全

**为什么是「示意图」而不是实拍**：本工程没有菜品实拍授权，也没有真实门店。
生成图只承担两件事——(1) 菜单首屏的视觉层级；(2) 图片规格/风格一致性的可检验样本。
所以每张图都：不含文字、不含价格、不含水印、不含第三方 IP，且在
`miniapp/assets/manifest.json` 里如实标注 `生成方式=程序绘制矢量示意`。
价格一律由前端按 `fixtures/seed_kitchen.sql` 渲染，图价分离，避免「图上一个价、
单里另一个价」这种答辩上最难解释的问题。

输出：

    miniapp/assets/dishes/<code>.png          800×600   菜品示意（24 张）
    miniapp/assets/poster/poster_vertical.png 1200×1600 门店竖版物料
    miniapp/assets/poster/poster_wide.png     1600×900  横屏物料
    miniapp/assets/poster/poster_square.png   1200×1200 平台主图
    miniapp/assets/manifest.json              资产清单（sha256 / 尺寸 / 来源）
    miniapp/utils/dish_images.js              code → 图路径（前端 require）

绘制用 4 倍超采样（画在 3200×2400 再 LANCZOS 降采样），边缘才不会有锯齿；
纯 Pillow 实现，不引入 cairo / 浏览器内核，答辩机器上零额外依赖。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "miniapp" / "assets"
DISH_DIR = ASSETS / "dishes"          # 小程序包内（受 2MB 主包预算约束）
POSTER_DIR = ROOT / "docs" / "menu_assets"   # 门店物料不进包，随设计文档归档
MANIFEST = POSTER_DIR / "manifest.json"
MAPPING_JS = ROOT / "miniapp" / "utils" / "dish_images.js"
RAW_DIR = POSTER_DIR / "raw"          # AI 生成原图（1024×768，带平台角水印）
WM_CROP = (42, 10, 982, 715)          # 裁掉右下角水印，得 940×705（4:3）
CARD_W, CARD_H = 600, 450             # 卡片图输出尺寸（3 倍屏下 200×150rpx 仍有余量）

#: 640×480 而不是 800×600：菜品图在卡片里最大显示 200×150rpx（3 倍屏 ≈ 300×225px），
#: 640 宽已经够；再大只是让 24 张图把主包撑过 2MB 上限。
DISH_W, DISH_H = 640, 480
SS = 4  # 超采样倍数

#: 背景与器皿的三个基准色，全套餐品图共用——「同页图片风格一致」靠的就是这套常量，
#: 而不是靠美工手感。改这里等于改全套，改完跑 --check 会重新对哈希。
BG_TOP = (251, 248, 242)
BG_BOTTOM = (240, 233, 221)
PLATE = (255, 255, 255)
PLATE_RIM = (235, 229, 218)
BOWL = (250, 246, 239)


# --------------------------------------------------------------- 菜品配方表
# 一道菜 = 器皿 + 主料块（形状/颜色/数量/尺寸区间）+ 点缀。
# 颜色取真食材的近似色，不用高饱和商业色；同一品类共用一套明度，避免某张图跳出来。
def _dish(code, family, plate_tint, food, garnish=(), sheen=None):
    return {"code": code, "family": family, "plate": plate_tint,
            "food": food, "garnish": list(garnish), "sheen": sheen}


# food 项：(形状, 颜色, 数量, 最小尺寸, 最大尺寸)
DISH_SPECS = {
    # 热菜 —— 盘装、酱色系
    "D-QJRS": _dish("D-QJRS", "plate", PLATE, [
        ("strip", "#8B5A3C", 16, 9, 13), ("strip", "#4E8C3A", 12, 8, 12),
    ], garnish=["#C9D8A8"], sheen=(160, 96, 48, 26)),
    "D-GBJD": _dish("D-GBJD", "plate", PLATE, [
        ("cube", "#C8813E", 14, 30, 40), ("ball", "#D9B382", 12, 13, 17),
        ("strip", "#B23A2B", 6, 8, 11),
    ], garnish=["#8FBF6A"], sheen=(180, 92, 40, 24)),
    "D-QGD": _dish("D-QGD", "plate", PLATE, [
        ("leaf", "#4C8B3F", 16, 26, 40), ("leaf", "#6BAA52", 10, 20, 30),
    ], garnish=["#F2EEDF"], sheen=(90, 140, 70, 20)),
    "D-SLTD": _dish("D-SLTD", "plate", PLATE, [
        ("strip", "#E7C86A", 26, 7, 10), ("strip", "#B23A2B", 7, 6, 9),
    ], garnish=["#8FBF6A"], sheen=(226, 196, 110, 22)),
    "D-HSJR": _dish("D-HSJR", "plate", PLATE, [
        ("cube", "#7A3B22", 12, 38, 50), ("cube", "#94502C", 8, 30, 40),
    ], garnish=["#4C8B3F"], sheen=(140, 60, 30, 60)),
    "D-SYR": _dish("D-SYR", "bowl", BOWL, [
        ("slice", "#F0E6D8", 12, 34, 48), ("strip", "#B23A2B", 8, 7, 10),
    ], garnish=["#7FA05A"], sheen=(190, 60, 40, 70)),
    "D-KSJD": _dish("D-KSJD", "plate", PLATE, [
        ("slice", "#8B4A2B", 14, 40, 54), ("strip", "#5A9E4B", 8, 9, 13),
        ("cube", "#5B2C16", 4, 26, 34),
    ], garnish=["#E6D9BE"], sheen=(150, 80, 40, 40)),
    "D-MJRS": _dish("D-MJRS", "plate", PLATE, [
        ("blob", "#F2C74B", 10, 24, 34), ("strip", "#8B5A3C", 10, 8, 11),
        ("blob", "#3B3B3B", 6, 18, 26), ("strip", "#6BAA52", 6, 7, 10),
    ], garnish=["#C9D8A8"], sheen=(200, 170, 90, 22)),
    # 凉菜 —— 盘装、冷色系、块面更规整
    "D-LJHD": _dish("D-LJHD", "plate", PLATE, [
        ("strip", "#2F5D3A", 30, 6, 9), ("strip", "#4E7A4A", 14, 5, 8),
    ], garnish=["#E8E2D0"], sheen=(60, 110, 70, 18)),
    "D-LJHG": _dish("D-LJHG", "plate", PLATE, [
        ("slice", "#6FBF5A", 14, 32, 44), ("ball", "#F2EEDF", 6, 12, 16),
    ], garnish=["#4C8B3F"], sheen=(110, 180, 100, 20)),
    "D-ZR": _dish("D-ZR", "plate", PLATE, [
        ("slice", "#6B3226", 14, 40, 56), ("slice", "#8A4634", 8, 32, 44),
    ], garnish=["#C9D8A8"], sheen=(120, 60, 40, 40)),
    "D-PD": _dish("D-PD", "plate", PLATE, [
        ("cube", "#F7F4E8", 10, 40, 54), ("cube", "#3A3F3B", 8, 30, 40),
    ], garnish=["#4C8B3F"], sheen=(200, 200, 180, 18)),
    # 素菜 —— 盘装、绿色为主
    "D-DSH": _dish("D-DSH", "plate", PLATE, [
        ("blob", "#5B3B6B", 8, 30, 42), ("cube", "#E7C86A", 10, 26, 36),
        ("strip", "#4E8C3A", 10, 8, 12),
    ], garnish=["#8FBF6A"], sheen=(150, 120, 90, 24)),
    "D-SC": _dish("D-SC", "plate", PLATE, [
        ("leaf", "#A9CF6E", 18, 30, 46), ("strip", "#B23A2B", 6, 7, 10),
    ], garnish=["#F2EEDF"], sheen=(150, 190, 110, 20)),
    # 汤羹 —— 碗装、液面 + 浮料
    "D-XLDT": _dish("D-XLDT", "bowl", BOWL, [
        ("blob", "#C9522F", 8, 26, 36), ("blob", "#F4D06A", 10, 18, 26),
    ], garnish=["#7FA05A"], sheen=(210, 100, 70, 90)),
    "D-ZCST": _dish("D-ZCST", "bowl", BOWL, [
        ("blob", "#2E3B33", 9, 22, 32), ("blob", "#F4D06A", 10, 18, 26),
    ], garnish=["#E8E2D0"], sheen=(120, 110, 80, 80)),
    "D-YMJG": _dish("D-YMJG", "bowl", BOWL, [
        ("cube", "#F0C040", 8, 34, 44), ("cube", "#C9A07A", 8, 30, 40),
    ], garnish=["#7FA05A"], sheen=(230, 210, 160, 90)),
    # 主食 —— 碗装米饭 / 盘装面食
    "D-MIF": _dish("D-MIF", "bowl", BOWL, [
        ("ball", "#F6F2E4", 42, 19, 27), ("ball", "#E3DAC4", 20, 14, 20),
    ], garnish=[], sheen=(255, 255, 255, 60)),
    "D-JIAOZ": _dish("D-JIAOZ", "plate", PLATE, [
        ("dumpling", "#F2EBDD", 9, 46, 60),
    ], garnish=["#C9D8A8"], sheen=(240, 230, 210, 30)),
    "D-CHAOM": _dish("D-CHAOM", "plate", PLATE, [
        ("strip", "#E3B85C", 26, 8, 11), ("strip", "#6BAA52", 8, 7, 10),
        ("strip", "#8B5A3C", 6, 7, 10),
    ], garnish=["#F2EEDF"], sheen=(220, 180, 110, 24)),
    # 甜品
    "D-HSJG": _dish("D-HSJG", "bowl", BOWL, [
        ("ball", "#7A2E2E", 14, 20, 28), ("ball", "#8E3A38", 10, 16, 22),
    ], garnish=["#E8C25A"], sheen=(150, 70, 70, 90)),
    "D-BN": _dish("D-BN", "plate", PLATE, [
        ("slice", "#E8C25A", 10, 40, 54), ("strip", "#F0D98A", 12, 5, 8),
    ], garnish=["#F7EFC8"], sheen=(240, 200, 110, 40)),
    # 饮品 —— 杯装（俯视：杯口 + 液面 + 冰块 + 吸管）
    "D-NCZ": _dish("D-NCZ", "glass", (238, 244, 246), [], sheen=(245, 154, 43, 235)),
    "D-YCH": _dish("D-YCH", "glass", (238, 244, 246), [], sheen=(120, 52, 74, 235)),
}

#: 每条提示词都写进 manifest：图是从哪句话来的，答辩被问「这图哪来的」时能当场翻。
#: 统一尾句保证风格一致：水彩手绘 / 吉卜力食物质感 / 米白桌 / 俯视 45° / 无字无水印无人手。
_STYLE = ("吉卜力动画食物质感，柔和暖光，俯视45度角，米白色桌面背景，构图居中。"
          "没有文字，没有水印，没有人手。")
PROMPTS = {
    "D-QJRS": "水彩手绘动漫风格中餐菜品插画：青椒肉丝 — 白瓷盘里是菱形块状的青椒片（宽而短的青椒块，不是绿色长豆角）与细瘦的猪肉丝一起爆炒，酱色包浆，油亮，热气袅袅。" + _STYLE,
    "D-GBJD": "水彩手绘动漫风格中餐菜品插画：宫保鸡丁 — 白瓷盘里盛着鸡丁、花生米与干辣椒，红亮酱汁，撒白芝麻，热气袅袅。" + _STYLE,
    "D-QGD": "水彩手绘动漫风格中餐菜品插画：清炒时蔬 — 白瓷盘里盛着碧绿的清炒青菜，蒜末点缀，油亮清香，热气袅袅。" + _STYLE,
    "D-SLTD": "水彩手绘动漫风格中餐菜品插画：酸辣土豆丝 — 白瓷盘里盛着金黄透亮的土豆丝，红干椒丝点缀，色彩明快，热气袅袅。" + _STYLE,
    "D-HSJR": "水彩手绘动漫风格中餐菜品插画：红烧肉 — 深色砂锅/白瓷盘里码着方正的带皮五花肉块，红褐色浓汁油亮反光，点缀葱花。" + _STYLE,
    "D-SYR": "水彩手绘动漫风格中餐菜品插画：水煮鱼 — 白色深盆里是红油滚烫的鱼片，白嫩鱼片与干红椒、花椒浮在红汤上，热气腾升。" + _STYLE,
    "D-KSJD": "水彩手绘动漫风格中餐菜品插画：烤鸭 — 白瓷盘里码着切片的酥皮烤鸭，皮色油亮棕红，旁边配几碟甜面酱与黄瓜条、薄饼。" + _STYLE,
    "D-MJRS": "水彩手绘动漫风格中餐菜品插画：木须肉 — 白瓷盘里有金黄炒蛋块、肉丝、黑木耳与黄瓜片，色彩丰富，热气袅袅。" + _STYLE,
    "D-LJHD": "水彩手绘动漫风格中餐菜品插画：凉拌海带丝 — 白瓷小盘里盛着墨绿海带丝，蒜末与红椒丝点缀，清爽油亮。" + _STYLE,
    "D-LJHG": "水彩手绘动漫风格中餐菜品插画：凉拌黄瓜 — 白瓷小盘里是拍碎的黄瓜块，蒜末与少许红椒，水润青翠，酱汁清亮。" + _STYLE,
    "D-ZR": "水彩手绘动漫风格中餐菜品插画：酱牛肉 — 白瓷盘里码着切片的酱色牛肉，纹理清晰油亮，蒜泥蘸料小碟，几根香菜。" + _STYLE,
    "D-PD": "水彩手绘动漫风格中餐菜品插画：皮蛋豆腐 — 白瓷盘里是雪白的内酯豆腐块与墨色皮蛋块，淋生抽、撒葱花与香菜。" + _STYLE,
    "D-DSH": "水彩手绘动漫风格中餐菜品插画：地三鲜 — 白瓷盘里是油亮的茄子块、土豆块与青椒块，酱汁浓郁，热气袅袅。" + _STYLE,
    "D-SC": "水彩手绘动漫风格中餐菜品插画：手撕包菜 — 白瓷盘里是手掰的包菜叶，边缘微焦，干红椒与蒜片点缀，油光发亮。" + _STYLE,
    "D-XLDT": "水彩手绘动漫风格中餐菜品插画：西红柿蛋汤 — 白色陶瓷汤碗，橙红色番茄汤里漂着金黄蛋花，热气升腾。" + _STYLE,
    "D-ZCST": "水彩手绘动漫风格中餐菜品插画：紫菜蛋花汤 — 白瓷汤碗里是清汤、紫菜与嫩黄蛋花，淋几滴香油，热气升腾。" + _STYLE,
    "D-YMJG": "水彩手绘动漫风格中餐菜品插画：玉米排骨汤 — 白瓷汤碗里是奶白汤、金黄的玉米段与排骨块，汤面漂几粒枸杞，热气升腾。" + _STYLE,
    "D-MIF": "水彩手绘动漫风格中餐菜品插画：米饭 — 白瓷小碗里盛着冒尖的松软白米饭，热气袅袅，旁边木匙。" + _STYLE,
    "D-JIAOZ": "水彩手绘动漫风格中餐菜品插画：饺子 — 白瓷盘里摆着一圈水饺，皮白饱满带着褶子，旁边一小碟醋，热气袅袅。" + _STYLE,
    "D-CHAOM": "水彩手绘动漫风格中餐菜品插画：炒面 — 白瓷盘里是油亮的炒面条，配豆芽、韭菜与肉丝，酱色均匀，热气袅袅。" + _STYLE,
    "D-HSJG": "水彩手绘动漫风格中餐菜品插画：红豆沙 — 白瓷小碗里是浓稠紫红的红豆沙，撒几粒金黄桂花。" + _STYLE,
    "D-BN": "水彩手绘动漫风格中餐甜品插画：拔丝香蕉 — 白瓷盘里堆着切成短圆柱的炸香蕉段，外衣金黄酥脆，切开处能看到淡黄色的香蕉果肉，盘上拉出许多细长透明的糖丝，糖浆亮晶晶。" + _STYLE,
    # 下面两条是「以图定菜」补进来的品：提示词当初按原菜写，成品更像这两道菜，
    # 于是把图配给真正像的那道菜，原菜另行重绘——图片清单里保留原始提示词可回查。
    "D-CJ": "水彩手绘动漫风格中餐小吃插画：炸春卷 — 白瓷盘里摆着五根细长圆柱形的炸春卷，外皮金黄酥脆带气泡酥皮，两端露出笋丝与韭黄馅，旁边一小碟红亮甜辣酱，没有糖丝。" + _STYLE,
    "D-DJRS": "水彩手绘动漫风格中餐菜品插画：青椒肉丝 — 白瓷盘里盛着刚炒好的青椒丝与肉丝，酱汁油亮，热气袅袅。" + _STYLE,
    "D-NCZ": "水彩手绘动漫风格中餐菜品插画：鲜榨橙汁 — 透明玻璃杯里是橙黄的鲜榨橙汁，杯沿挂一片橙子，冰块与吸管，杯壁凝着水珠。" + _STYLE,
    "D-YCH": "水彩手绘动漫风格中餐菜品插画：酸梅汤 — 透明玻璃杯里是深紫红色的冰镇酸梅汤，浮冰块与一两颗乌梅，杯壁凝着水珠。" + _STYLE,
}


#: 以图定菜的判定记录：图是照 A 的提示词生成的，但成品更像 B，于是配给 B。
#: 这类换配必须留痕，不然「图与菜不符」就是审核和答辩最容易挑的一处。
IMAGE_NOTES = {
    "D-DJRS": "原图按「青椒肉丝」提示词生成，青椒呈长条状更像豆角；按图配菜给豆角炒肉丝，青椒肉丝已重绘",
}


# ------------------------------------------------------------------ 绘制基元
def _px(v: float) -> int:
    return int(round(v * SS))


def background(img: Image.Image) -> None:
    """竖向柔和渐变 + 中心提亮，模拟桌面自然光，避免纯色死板。"""
    d = ImageDraw.Draw(img)
    w, h = img.size
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=c)


def _layer(size) -> Image.Image:
    """透明图层。凡是要半透明的元素（投影/高光/油光）都先画在图层上再 alpha_composite，
    直接往 RGBA 底图上填带 alpha 的颜色会把 alpha 写进像素本身，
    转 RGB 时不留神就变成一块死黑的圈。"""
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _shadow(base: Image.Image, cx, cy, rx, ry, alpha=54) -> Image.Image:
    """器皿投影：偏移向下 + 高斯模糊，落在底图下面，看起来才是「放在桌上」。"""
    layer = _layer(base.size)
    d = ImageDraw.Draw(layer)
    d.ellipse([_px(cx - rx * .98), _px(cy - ry * .92 + 20),
               _px(cx + rx * .98), _px(cy + ry * .92 + 20)],
              fill=(60, 48, 34, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(SS * 7))
    return Image.alpha_composite(base, layer)


def _strip(d, x, y, size, color, rng):
    """条状料（肉丝/土豆丝/海带丝）：粗线 + 两端圆头，比矩形更像切丝。"""
    ang = rng.uniform(0, math.pi)
    half = size * rng.uniform(1.6, 2.6)
    x1 = x - math.cos(ang) * half
    y1 = y - math.sin(ang) * half
    x2 = x + math.cos(ang) * half
    y2 = y + math.sin(ang) * half
    w = max(2, size)
    d.line([(_px(x1), _px(y1)), (_px(x2), _px(y2))], fill=color, width=_px(w))
    for px, py in ((x1, y1), (x2, y2)):
        d.ellipse([_px(px - w / 2), _px(py - w / 2),
                   _px(px + w / 2), _px(py + w / 2)], fill=color)


def _cube(d, x, y, size, color, rng):
    w = size * rng.uniform(.85, 1.15)
    h = w * rng.uniform(.62, .92)
    x0, y0 = x - w / 2, y - h / 2
    d.rounded_rectangle([_px(x0), _px(y0), _px(x0 + w), _px(y0 + h)],
                        radius=_px(min(w, h) * .26), fill=color,
                        outline=_shift(color, -24) if isinstance(color, str) else None,
                        width=_px(1.6))


def _ball(d, x, y, size, color, rng):
    r = size / 2
    d.ellipse([_px(x - r), _px(y - r), _px(x + r), _px(y + r)], fill=color)


def _leaf(d, x, y, size, color, rng):
    ang = rng.uniform(0, math.pi)
    a, b = size / 2, size / 2.9
    pts = []
    for i in range(16):
        t = 2 * math.pi * i / 16
        ex, ey = a * math.cos(t), b * math.sin(t)
        pts.append((_px(x + ex * math.cos(ang) - ey * math.sin(ang)),
                    _px(y + ex * math.sin(ang) + ey * math.cos(ang))))
    d.polygon(pts, fill=color)


def _slice(d, x, y, size, color, rng):
    """片状料（肉片/鱼片/香蕉段）：扁椭圆 + 一道内环，像切片厚度。"""
    ang = rng.uniform(-0.6, 0.6)
    a, b = size / 2, size / 2.6
    for k, c in ((1.0, color), (.66, _shift(color, 12))):
        pts = []
        for i in range(20):
            t = 2 * math.pi * i / 20
            ex, ey = a * k * math.cos(t), b * k * math.sin(t)
            pts.append((_px(x + ex * math.cos(ang) - ey * math.sin(ang)),
                        _px(y + ex * math.sin(ang) + ey * math.cos(ang))))
        d.polygon(pts, fill=c)


def _blob(d, x, y, size, color, rng):
    """不规则块（蛋花/木耳/茄子）：7 边形半径抖动，别太规则也别太碎。"""
    pts = []
    n = 7
    for i in range(n):
        t = 2 * math.pi * i / n
        r = size / 2 * rng.uniform(.72, 1.14)
        pts.append((_px(x + math.cos(t) * r), _px(y + math.sin(t) * r)))
    d.polygon(pts, fill=color)


def _dumpling(d, x, y, size, color, rng):
    """饺子：圆角梯形 + 顶部褶线，俯视辨识度高。"""
    ang = rng.uniform(-0.5, 0.5)
    w, h = size, size * .62
    x0, y0 = x - w / 2, y - h / 2
    d.rounded_rectangle([_px(x0), _px(y0), _px(x0 + w), _px(y0 + h)],
                        radius=_px(h * .55), fill=color)
    for k in range(3):
        cx = x0 + w * (.3 + k * .2)
        d.arc([_px(cx - w * .08), _px(y0 + h * .18),
               _px(cx + w * .08), _px(y0 + h * .52)],
              start=200, end=340, fill=_shift(color, -18), width=_px(2.4))


def _shift(color: str, delta: int) -> tuple:
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return (max(0, min(255, r + delta)), max(0, min(255, g + delta)),
            max(0, min(255, b + delta)))


def _place(rng, count, cx, cy, rx, ry, smin, smax, gap=0.82):
    """在椭圆内布点，带最小间距拒绝采样——料块不叠成一坨，也不留大空洞。"""
    pts, guard = [], 0
    limit = max(count * 400, 400)
    while len(pts) < count and guard < limit:
        guard += 1
        a = rng.uniform(0, 2 * math.pi)
        r = math.sqrt(rng.random())
        x, y = cx + math.cos(a) * rx * r, cy + math.sin(a) * ry * r
        s = rng.uniform(smin, smax)
        if all((x - px) ** 2 + (y - py) ** 2 > (gap * (s + ps) / 2) ** 2
               for px, py, ps in pts):
            pts.append((x, y, s))
    return pts


def _place(rng, count, cx, cy, rx, ry, smin, smax, gap=0.82, bias=1.0):
    """在椭圆内布点，带最小间距拒绝采样——料块不叠成一坨，也不留大空洞。

    bias < 1 时点向中心收拢（r 开根号再取幂），这样一盘菜是「中间堆高、
    边缘露盘」的正常摆盘，而不是均匀撒在盘底像示意图。"""
    pts, guard = [], 0
    limit = max(count * 400, 400)
    while len(pts) < count and guard < limit:
        guard += 1
        a = rng.uniform(0, 2 * math.pi)
        r = (rng.random() ** bias) ** .5
        x, y = cx + math.cos(a) * rx * r, cy + math.sin(a) * ry * r
        s = rng.uniform(smin, smax)
        if all((x - px) ** 2 + (y - py) ** 2 > (gap * (s + ps) / 2) ** 2
               for px, py, ps in pts):
            pts.append((x, y, s))
    return pts


#: 料块整体放大倍数。生成图会被塞进 200×150rpx 的卡片缩略位，料太小就成了
#: 一盘彩色碎屑；放大后缩略图里仍能看出「这是什么菜」。
SIZE_GAIN = 1.55
COUNT_GAIN = 1.7


def draw_dish(spec: dict, seed: int) -> Image.Image:
    """画一张 800×600。随机数只由 code 派生，所以同一道菜每次生成完全一致。"""
    rng = random.Random(seed)
    size = (DISH_W * SS, DISH_H * SS)
    img = Image.new("RGBA", size, (255, 255, 255, 255))
    background(img)

    cx, cy = DISH_W / 2, DISH_H / 2 + 4
    family = spec["family"]
    rx, ry = (306, 220) if family != "glass" else (176, 176)
    img = _shadow(img, cx, cy, rx, ry, 60 if family != "glass" else 46)
    d = ImageDraw.Draw(img)

    if family == "glass":
        # 俯视杯口：玻璃圈 + 液面 + 冰块 + 吸管
        d.ellipse([_px(cx - rx), _px(cy - ry), _px(cx + rx), _px(cy + ry)],
                  fill=(247, 251, 252), outline=(209, 220, 225), width=_px(6))
        d.ellipse([_px(cx - rx * .88), _px(cy - ry * .88),
                   _px(cx + rx * .88), _px(cy + ry * .88)],
                  fill=(255, 255, 255), outline=(232, 240, 242), width=_px(3))
        lr = rx * .76
        fill = spec["sheen"]
        d.ellipse([_px(cx - lr), _px(cy - lr * .97),
                   _px(cx + lr), _px(cy + lr * .97)],
                  fill=(fill[0], fill[1], fill[2]))
        # 液面高光 + 冰块 + 吸管都画在半透明图层上
        hl = _layer(size)
        dh = ImageDraw.Draw(hl)
        dh.ellipse([_px(cx - lr * .8), _px(cy - lr * .82),
                    _px(cx - lr * .05), _px(cy - lr * .18)],
                   fill=(255, 255, 255, 56))
        for i in range(3):
            a = -2.35 + i * .6
            ix, iy = cx + math.cos(a) * rx * .5, cy + math.sin(a) * rx * .5
            s = 62 - i * 7
            dh.rounded_rectangle([_px(ix - s / 2), _px(iy - s / 2),
                                  _px(ix + s / 2), _px(iy + s / 2)],
                                 radius=_px(11), fill=(255, 255, 255, 108))
        img = Image.alpha_composite(img, hl)
        d = ImageDraw.Draw(img)
        d.line([(_px(cx + rx * .34), _px(cy - rx * .92)),
                (_px(cx + rx * .74), _px(cy + rx * .3))],
               fill=(231, 235, 238), width=_px(15))
        d.line([(_px(cx + rx * .34), _px(cy - rx * .92)),
                (_px(cx + rx * .74), _px(cy + rx * .3))],
               fill=(252, 253, 254), width=_px(7))
        return img.convert("RGB").resize((DISH_W, DISH_H), Image.LANCZOS)

    if family == "plate":
        d.ellipse([_px(cx - rx), _px(cy - ry), _px(cx + rx), _px(cy + ry)],
                  fill=PLATE)
        d.ellipse([_px(cx - rx * .95), _px(cy - ry * .95),
                   _px(cx + rx * .95), _px(cy + ry * .95)],
                  outline=PLATE_RIM, width=_px(3))
        d.ellipse([_px(cx - rx * .78), _px(cy - ry * .78),
                   _px(cx + rx * .78), _px(cy + ry * .78)],
                  outline=(247, 243, 236), width=_px(2))
        fx, fy = rx * .76, ry * .74
    else:
        # 碗：碗沿 + 内壁 + 液面，液面颜色就是汤色
        d.ellipse([_px(cx - rx), _px(cy - ry), _px(cx + rx), _px(cy + ry)],
                  fill=BOWL)
        d.ellipse([_px(cx - rx * .93), _px(cy - ry * .93),
                   _px(cx + rx * .93), _px(cy + ry * .93)],
                  outline=(228, 220, 206), width=_px(7))
        fx, fy = rx * .68, ry * .64
        if spec["sheen"]:
            sc = spec["sheen"]
            d.ellipse([_px(cx - fx * 1.02), _px(cy - fy * 1.02),
                       _px(cx + fx * 1.02), _px(cy + fy * 1.02)],
                      fill=(sc[0], sc[1], sc[2]))

    # 盘底「料堆」：一层比主料略深的柔和椭圆，先给体积感，再往上面摆料
    if spec["food"]:
        base_color = _shift(spec["food"][0][1], -26)
        mound = _layer(size)
        dm = ImageDraw.Draw(mound)
        dm.ellipse([_px(cx - fx * .86), _px(cy - fy * .84),
                    _px(cx + fx * .86), _px(cy + fy * .84)],
                   fill=base_color + (58,))
        img = Image.alpha_composite(img, mound.filter(ImageFilter.GaussianBlur(SS * 9)))
        d = ImageDraw.Draw(img)

    # 主料：中间堆高、边缘少量外溢
    for idx, (shape, color, count, smin, smax) in enumerate(spec["food"]):
        n = int(round(count * COUNT_GAIN))
        pts = _place(rng, n, cx, cy, fx, fy, smin * SIZE_GAIN, smax * SIZE_GAIN,
                     gap=.86 if shape in ("cube", "dumpling", "slice") else .72,
                     bias=.86)
        drawer = {"strip": _strip, "cube": _cube, "ball": _ball, "leaf": _leaf,
                  "slice": _slice, "blob": _blob, "dumpling": _dumpling}[shape]
        for x, y, s in pts:
            drawer(d, x, y, s, color, rng)

    # 点缀（葱花/蒜末/枸杞）：压在料上面，少量
    for color in spec["garnish"]:
        pts = _place(rng, 16, cx, cy, fx * .9, fy * .88, 11, 17, gap=.9, bias=.8)
        for x, y, s in pts:
            _ball(d, x, y, s, color, rng)

    # 油光：只在中上部压一小片低透明度高光，别做成「一滩酱」
    if spec["sheen"] and family == "plate":
        sc = spec["sheen"]
        gloss = _layer(size)
        dg = ImageDraw.Draw(gloss)
        dg.ellipse([_px(cx - fx * .58), _px(cy - fy * .54),
                    _px(cx + fx * .12), _px(cy - fy * .02)],
                   fill=(255, 255, 255, 30))
        for x, y, s in _place(rng, 8, cx, cy, fx * .7, fy * .66, 9, 15, gap=1.2):
            dg.ellipse([_px(x - s / 2), _px(y - s / 2),
                        _px(x + s / 2), _px(y + s / 2)],
                       fill=(255, 255, 255, 46))
        img = Image.alpha_composite(img, gloss)
    return img.convert("RGB").resize((DISH_W, DISH_H), Image.LANCZOS)


# ------------------------------------------------------------------ 门店物料
# 物料用「基元列表 → 双渲染」：同一份布局既出可编辑 SVG（改字改色），
# 也出 PNG（贴门口/发朋友圈）。价格全部来自 seed 数据，脚本里不写死金额。
def poster_primitives(w: int, h: int, kind: str, data: dict) -> list:
    """返回绘制基元：('rect'|'ellipse'|'text'|'image'|'line', ...)。

    绘制顺序即图层顺序：底板 → 图片 → 文字。图片位先铺白底卡片再贴图，
    反过来会把图盖掉（第一版就这么翻的，横屏物料上只剩三个白框）。
    """
    p = []
    bg = "#faf6ee"
    p.append(("rect", 0, 0, w, h, bg, 0))
    cards = data["grid"]  # [(path, 名称)]，最多 4 条
    if kind == "vertical":
        p.append(("rect", 0, 0, w, 286, "#1f4d3a", 0))
        p.append(("text", 84, 124, data["brand"], 56, "#f6f1e4", "bold"))
        p.append(("text", 86, 200, "扫码点餐 · 后厨现做", 32, "#cfe0d2", "400"))
        # 2×2 四宫格：四道菜各占一格，等比不裁切
        for i, (path, _name) in enumerate(cards[:4]):
            col, row = i % 2, i // 2
            x = 84 + col * (496 + 40)
            y = 330 + row * (372 + 26)
            p.append(("rect", x, y, 496, 372, "#ffffff", 14))
            p.append(("image", path, x, y, 496, 372))
        p.append(("text", 84, 1150, data["combo_title"], 58, "#1f2937", "bold"))
        p.append(("text", 84, 1212, data["combo_desc"], 28, "#5b6472", "400"))
        p.append(("text", 84, 1310, data["price_line"], 50, "#b4471f", "bold"))
        p.append(("text", 84, 1352, data["price_note"], 22, "#8a919e", "400"))
        y = 1412
        for line in data["features"]:
            p.append(("ellipse", 96, y - 12, 8, 8, "#2f7d5b"))
            p.append(("text", 122, y, line, 28, "#33404f", "400"))
            y += 46
        p.append(("rect", 0, h - 90, w, 90, "#e9e2d2", 0))
        p.append(("text", 84, h - 36, data["footer"], 22, "#6b7280", "400"))
    elif kind == "wide":
        p.append(("rect", 0, 0, 660, h, "#1f4d3a", 0))
        p.append(("text", 72, 150, data["brand"], 50, "#f6f1e4", "bold"))
        p.append(("text", 72, 218, data["combo_title"], 36, "#d7e6d8", "400"))
        p.append(("text", 72, 318, data["price_line"], 58, "#f3c86a", "bold"))
        p.append(("text", 72, 366, data["price_note"], 22, "#a8c2ac", "400"))
        y = 470
        for line in data["features"]:
            p.append(("ellipse", 84, y - 11, 7, 7, "#f3c86a"))
            p.append(("text", 108, y, line, 26, "#e4eee4", "400"))
            y += 48
        p.append(("text", 72, h - 56, data["footer"], 20, "#93b09a", "400"))
        for i, (path, _name) in enumerate(cards[:3]):
            x = 706 + i * 292
            p.append(("rect", x, 180, 272, 204, "#ffffff", 12))
            p.append(("image", path, x, 180, 272, 204))
        p.append(("text", 706, 428, "四道菜组合 · 菜品图为示意非实拍",
                  22, "#8a919e", "400"))
        # 小程序码预留位：没有真码就留白框并写明，不画一个假的二维码充数
        p.append(("rect", 706, 476, 196, 196, "#ffffff", 16))
        p.append(("rect", 726, 496, 156, 156, "#f2efe6", 10))
        p.append(("text", 734, 582, "小程序码位", 22, "#9aa1ab", "400"))
        p.append(("text", 926, 540, "扫码点餐", 30, "#1f4d3a", "bold"))
        p.append(("text", 926, 580, "后厨进度实时可见", 22, "#5b6472", "400"))
        p.append(("text", 926, 614, "出餐可溯源（演示）", 22, "#5b6472", "400"))
    elif kind == "banner":
        # 小程序首屏横幅：左文右图，7:3 左右，卡片高 300rpx 时正好不裁切
        p.append(("text", 56, 104, data["brand"], 40, "#1f3b2e", "bold"))
        p.append(("text", 56, 152, "今日现炒 · 扫码点餐，后厨进度实时可见", 24, "#5b6472", "400"))
        p.append(("rect", 56, 190, 236, 64, "#1f4d3a", 32))
        p.append(("text", 104, 231, "扫码点餐", 26, "#f6f1e4", "bold"))
        for i, (path, _name) in enumerate(cards[:3]):
            x = 600 + i * 172
            y = 46 + (i % 2) * 48
            p.append(("rect", x, y, 160, 120, "#ffffff", 14))
            p.append(("image", path, x, y, 160, 120))
        p.append(("text", 56, 320, "菜品图为 AI 生成示意，非实拍", 18, "#9aa1ab", "400"))
    else:  # square 平台主图
        p.append(("rect", 0, 0, w, 132, "#1f4d3a", 0))
        p.append(("text", 72, 88, data["brand"], 44, "#f6f1e4", "bold"))
        for i, (path, _name) in enumerate(cards[:2]):
            x = 72 + i * (528 + 24)
            p.append(("rect", x, 172, 528, 396, "#ffffff", 12))
            p.append(("image", path, x, 172, 528, 396))
        p.append(("text", 72, 640, data["combo_title"], 50, "#1f2937", "bold"))
        p.append(("text", 72, 700, data["combo_desc"], 26, "#5b6472", "400"))
        p.append(("text", 72, 812, data["price_line"], 52, "#b4471f", "bold"))
        p.append(("text", 72, 852, data["price_note"], 22, "#8a919e", "400"))
        y = 930
        for line in data["features"][:2]:
            p.append(("ellipse", 84, y - 11, 7, 7, "#2f7d5b"))
            p.append(("text", 108, y, line, 26, "#33404f", "400"))
            y += 48
        p.append(("rect", 0, h - 84, w, 84, "#e9e2d2", 0))
        p.append(("text", 72, h - 32, data["footer"], 20, "#6b7280", "400"))
    return p


def _font(size: int, bold: bool = False):
    candidates = ["C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
                  "C:/Windows/Fonts/simhei.ttf", "/System/Library/Fonts/PingFang.ttc"]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_png(prims: list, w: int, h: int, out: Path) -> None:
    img = Image.new("RGB", (w, h), (250, 246, 238))
    d = ImageDraw.Draw(img, "RGBA")
    for item in prims:
        kind = item[0]
        if kind == "rect":
            _, x, y, rw, rh, color, rx = item
            d.rounded_rectangle([x, y, x + rw, y + rh], radius=rx, fill=color)
        elif kind == "ellipse":
            _, cx, cy, rx, ry, color = item
            d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=color)
        elif kind == "line":
            _, x1, y1, x2, y2, color, width = item
            d.line([x1, y1, x2, y2], fill=color, width=width)
        elif kind == "text":
            _, x, y, value, size, color, weight = item
            d.text((x, y), value, font=_font(size, weight == "bold"),
                   fill=color, anchor="ls")
        elif kind == "image":
            _, path, x, y, rw, rh = item
            im = Image.open(path).convert("RGB")
            # 等比裁切填满（cover），不拉伸变形
            scale = max(rw / im.width, rh / im.height)
            im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1),
                           Image.LANCZOS)
            left = (im.width - rw) // 2
            top = (im.height - rh) // 2
            img.paste(im.crop((left, top, left + rw, top + rh)), (x, y))
    img.save(out, "PNG", optimize=True)


def render_svg(prims: list, w: int, h: int, out: Path) -> None:
    """可编辑 SVG：文字是 <text> 不是路径，改文案不用回脚本重画。"""
    import html
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
             f'viewBox="0 0 {w} {h}">']
    for item in prims:
        kind = item[0]
        if kind == "rect":
            _, x, y, rw, rh, color, rx = item
            lines.append(f'<rect x="{x}" y="{y}" width="{rw}" height="{rh}" '
                         f'rx="{rx}" fill="{color}"/>')
        elif kind == "ellipse":
            _, cx, cy, rx, ry, color = item
            lines.append(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" '
                         f'fill="{color}"/>')
        elif kind == "line":
            _, x1, y1, x2, y2, color, width = item
            lines.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                         f'stroke="{color}" stroke-width="{width}"/>')
        elif kind == "text":
            _, x, y, value, size, color, weight = item
            lines.append(
                f'<text x="{x}" y="{y}" font-family="PingFang SC, Microsoft YaHei, '
                f'sans-serif" font-size="{size}" font-weight="{weight}" '
                f'fill="{color}">{html.escape(str(value))}</text>')
        elif kind == "image":
            _, path, x, y, rw, rh = item
            lines.append(f'<image href="../dishes/{Path(path).name}" x="{x}" '
                         f'y="{y}" width="{rw}" height="{rh}" '
                         f'preserveAspectRatio="xMidYMid slice"/>')
    lines.append("</svg>")
    out.write_text("\n".join(lines), encoding="utf-8")


def load_featured_js() -> dict:
    """从 miniapp/utils/menu_visual.js 读主推位（前端配置是唯一真源）。"""
    import re as _re
    text = (ROOT / "miniapp" / "utils" / "menu_visual.js").read_text(encoding="utf-8")
    block = _re.search(r"featured:\s*\{(.*?)\}", text, _re.S)
    return dict(_re.findall(r"'(D-[A-Z]+)':\s*'([^']*)'", block.group(1))) if block else {}


# ------------------------------------------------------------------ 首屏预览
def mock_page(dishes: list, featured: dict) -> Path:
    """按 WXML 的实际层级画一张 750×1334 的静态预览（不是截图，是布局核对图）。

    用途：没有微信开发者工具时也能看「首屏长什么样」——尺寸、留白、图卡比例按
    menu.wxml / menu.wxss 的 rpx 值换算（750rpx = 750px，1:1），
    但字体渲染与真机仍有差异，只作版式核对，不作为真机验收。
    """
    W, H = 750, 1334
    img = Image.new("RGB", (W, H), (246, 242, 232))
    d = ImageDraw.Draw(img)

    def rr(x, y, w, h, r, fill):
        d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=fill)

    # 横幅 1080×350 → 702×228
    with Image.open(ASSETS / "banner" / "menu_banner.jpg") as b:
        img.paste(b.convert("RGB").resize((702, 228), Image.LANCZOS), (24, 16))

    # 搜索框
    rr(24, 260, 702, 72, 14, (241, 235, 221))
    d.text((48, 296), "搜菜名 / 分类 / 编码", font=_font(26), fill=(152, 143, 127), anchor="ls")

    # 分类 chip
    chips = ["全部", "热菜", "凉菜", "素菜", "汤羹", "主食", "甜品", "饮品"]
    x = 24
    for i, c in enumerate(chips):
        w = 24 + len(c) * 26
        active = i == 0
        rr(x, 352, w, 56, 28, (31, 77, 58) if active else (255, 253, 248))
        d.text((x + w / 2, 380), c, font=_font(26, active),
               fill=(255, 255, 255) if active else (107, 99, 85), anchor="mm")
        x += w + 14

    # 段标题
    d.text((24, 462), "今日菜单", font=_font(30, True), fill=(36, 31, 24), anchor="ls")
    d.text((726, 462), "隐藏沽清 · 共 %d 道" % len(dishes), font=_font(22),
           fill=(152, 143, 127), anchor="rs")

    # 主推前置
    head = [x for x in dishes if x["code"] in featured][:2]
    tail = [x for x in dishes if x["code"] not in featured][:1]
    rows = head + tail

    y = 486
    for dish in rows:
        rr(24, y, 702, 190, 18, (255, 253, 248))
        # 缩略图 200×150
        path = dish_file(dish["code"])
        with Image.open(path) as im:
            im = im.convert("RGB")
            scale = max(200 / im.width, 150 / im.height)
            im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1), Image.LANCZOS)
            left, top = (im.width - 200) // 2, (im.height - 150) // 2
            img.paste(im.crop((left, top, left + 200, top + 150)), (44, y + 20))
        # 主推角标
        if dish["code"] in featured:
            rr(52, y + 28, 64, 30, 6, (178, 104, 10))
            d.text((84, y + 44), "主推", font=_font(20, True), fill=(255, 255, 255), anchor="mm")

        tx = 264
        d.text((tx, y + 56), dish["name"], font=_font(30, True), fill=(36, 31, 24), anchor="ls")
        d.text((706, y + 58), "￥%.2f" % dish["price"], font=_font(32, True),
               fill=(180, 71, 31), anchor="rs")
        d.text((tx, y + 92), "%s · 参考 %d 分钟" % (dish["category"], dish["prep"]),
               font=_font(22), fill=(152, 143, 127), anchor="ls")
        note = featured.get(dish["code"])
        if note:
            # 真机是 align-self:flex-start 自适应宽度；预览图必须按字宽实测，
            # 否则长文案会拉一条到价格底下的长条，看起来像版式错了
            w = d.textlength(note, font=_font(22)) + 26
            rr(tx, y + 106, w, 36, 8, (250, 241, 226))
            d.text((tx + 12, y + 125), note, font=_font(22), fill=(178, 104, 10), anchor="ls")
        # 步进器
        rr(646, y + 140, 56, 52, 26, (31, 77, 58))
        d.text((674, y + 167), "＋", font=_font(32, True), fill=(255, 255, 255), anchor="mm")
        y += 206

    # 底部 tabBar：预览图不画这块就会让人以为「菜单页到底就结束了」，
    # 实际它在首屏末尾一直占位，版式核对必须连它一起看
    bar_h = 98
    bar_y = H - bar_h
    d.rectangle([0, bar_y, W, H], fill=(255, 253, 248))
    d.line([(0, bar_y), (W, bar_y)], fill=(230, 223, 208), width=1)
    tabs = [("tab_menu", "点菜", True), ("tab_orders", "订单", False),
            ("tab_trace", "溯源", False), ("tab_profile", "我的", False)]
    step = W / len(tabs)
    for i, (icon, label, on) in enumerate(tabs):
        cx = step * i + step / 2
        path = ASSETS / "tabbar" / f"{icon}{'_on' if on else ''}.png"
        if path.is_file():
            with Image.open(path) as ic:
                ic = ic.convert("RGBA").resize((46, 46), Image.LANCZOS)
                img.paste(ic, (int(cx - 23), bar_y + 12), ic)
        d.text((cx, H - 16), label, font=_font(20, on),
               fill=(31, 77, 58) if on else (107, 99, 85), anchor="ms")

    d.text((24, bar_y - 64), "静态版式预览（按 menu.wxml/wxss 的 rpx 换算，非真机截图）",
           font=_font(20), fill=(152, 143, 127), anchor="ls")
    out = POSTER_DIR / "menu_page_preview.png"
    img.save(out, "PNG", optimize=True)
    return out


# ------------------------------------------------------------------ 物料文案
def load_dishes() -> list:
    """从 seed SQL 读菜单事实（名称/品类/价格/沽清），不让物料脚本另造一份数据。"""
    import re
    sql = (ROOT / "fixtures" / "seed_kitchen.sql").read_text(encoding="utf-8")
    rows = re.findall(
        r"\(\s*(\d+),\s*'(D-[A-Z]+)',\s*'([^']+)',\s*'([^']+)',\s*([\d.]+),\s*(\d+),"
        r"\s*([01]),\s*([01])", sql)
    out = []
    for _id, code, name, cat, price, prep, sold_out, enabled in rows:
        out.append({"code": code, "name": name, "category": cat,
                    "price": float(price), "prep": int(prep),
                    "sold_out": sold_out == "1", "enabled": enabled == "1"})
    return out


def combo(dishes: list) -> dict:
    """组合按「一荤一素+主食×2」从可售菜品里取，金额是菜单价算术合计。

    不编「原价」「立减」——没有用户确认的折扣就不出现在物料上，
    这是物料制作里最容易翻车的一处（图上写 6 折，后厨按原价收）。
    """
    sellable = [d for d in dishes if d["enabled"] and not d["sold_out"]]
    by_cat = lambda c: [d for d in sellable if d["category"] == c]  # noqa: E731
    picks = []
    for cat in ("热菜", "素菜", "汤羹"):
        pool = by_cat(cat)
        if pool:
            picks.append(pool[0])
    rice = by_cat("主食")
    total = sum(d["price"] for d in picks)
    if rice:
        total += rice[0]["price"] * 2  # 两人份，主食算两份
    return {"picks": picks, "rice": rice[0] if rice else None,
            "total": round(total, 2),
            "max_prep": max((d["prep"] for d in picks), default=0)}


def dish_file(code: str) -> Path:
    """菜品图实际扩展名随来源变（AI 原图存 .jpg，绘制兜底存 .png），统一在这里取。"""
    for ext in (".jpg", ".png"):
        path = DISH_DIR / f"{code}{ext}"
        if path.is_file():
            return path
    return DISH_DIR / f"{code}.jpg"


def poster_data(dishes: list, combo_info: dict) -> dict:
    picks = combo_info["picks"]
    rice = combo_info["rice"]
    parts = [d["name"] for d in picks] + ([f"{rice['name']}×2"] if rice else [])
    chosen = list(picks) + ([rice] if rice else [])
    grid = [(str(dish_file(d["code"])), d["name"]) for d in chosen]
    # 四宫格不够四张时用同店其它可售菜补位（不写名字，只做视觉平衡）
    if len(grid) < 4:
        codes = {d["code"] for d in chosen}
        for d in dishes:
            if len(grid) >= 4:
                break
            if d["code"] not in codes and not d["sold_out"]:
                grid.append((str(dish_file(d["code"])), d["name"]))
    return {
        "brand": "明厨小馆 · 演示门店",
        "combo_title": "两人份家常组合",
        "combo_desc": "  ·  ".join(parts),
        "price_line": f"￥{combo_info['total']:.2f}",
        "price_note": "按菜单单价合计，未含折扣；以当次菜单为准",
        "features": ["扫码点餐，不用等服务员",
                     f"后厨进度实时可见，参考 {combo_info['max_prep']} 分钟",
                     "出餐留样与溯源可查（演示数据）"],
        "grid": grid,
        "footer": "课程演示物料 · 菜品图为 AI 生成示意，非实拍；门店名与价格为演示数据",
    }


# ------------------------------------------------------------------ 主流程
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_raw(name: str) -> Path | None:
    """原图按文件名里的菜名匹配（生成器把提示词写进了文件名，菜名是唯一稳定片段）。"""
    if not RAW_DIR.is_dir():
        return None
    hits = sorted(p for p in RAW_DIR.glob("*.png") if name in p.name)
    return hits[0] if hits else None


def import_raw(dishes: list) -> list:
    """把 AI 生成原图裁成卡片图：去掉右下角平台水印 → 中心裁 4:3 → 640×480 → JPEG。

    平台水印固定落在 1024×768 的右下角约 (920,715) 之后，裁切框按这个位置留 4:3
    比例（940×705），既清掉水印又不切到盘子边缘。裁切参数集中在这里，
    换生成器/换尺寸时只改 WM_CROP 一处。
    """
    entries = []
    for d in dishes:
        src = _find_raw(d["name"])
        if src is None:
            print(f"  ! {d['code']} {d['name']} 没找到原图，跳过（前端回退无图卡片）")
            continue
        with Image.open(src) as im:
            img = im.convert("RGB").crop(WM_CROP).resize((CARD_W, CARD_H), Image.LANCZOS)
        out = DISH_DIR / f"{d['code']}.jpg"
        img.save(out, "JPEG", quality=72, optimize=True, progressive=True)
        entries.append({
            "kind": "dish", "code": d["code"], "name": d["name"],
            "category": d["category"], "path": f"miniapp/assets/dishes/{out.name}",
            "width": CARD_W, "height": CARD_H, "bytes": out.stat().st_size,
            "sha256": sha256(out), "source": "ai",
            "prompt": PROMPTS.get(d["code"], ""),
            **({"image_note": IMAGE_NOTES[d["code"]]} if d["code"] in IMAGE_NOTES else {}),
            "raw": str(src.relative_to(ROOT)).replace("\\", "/"),
            "raw_sha256": sha256(src),
            "raw_note": "已裁去生成平台右下角水印，未做其它修图；菜品份量按提示词示意",
        })
    return entries


def build(mode: str = "raw") -> int:
    DISH_DIR.mkdir(parents=True, exist_ok=True)
    POSTER_DIR.mkdir(parents=True, exist_ok=True)

    dishes = load_dishes()
    if not dishes:
        print("✗ 没能从 fixtures/seed_kitchen.sql 解析出菜品，检查 SQL 格式")
        return 2

    # 重跑时先清掉旧的菜品图（png / jpg 两种格式都可能残留），避免小程序里新旧混用
    for old in list(DISH_DIR.glob("D-*.png")) + list(DISH_DIR.glob("D-*.jpg")):
        old.unlink()

    if mode == "raw":
        entries = import_raw(dishes)
        if not entries:
            print("✗ docs/menu_assets/raw/ 下没有可用原图；"
                  "先按 PROMPTS 生成，或用 --procedural 走离线绘制")
            return 3
    else:
        entries = []
        for d in dishes:
            spec = DISH_SPECS.get(d["code"])
            if spec is None:
                print(f"  ! {d['code']} {d['name']} 没有配色配方，跳过（前端会回退到无图卡片）")
                continue
            out = DISH_DIR / f"{d['code']}.png"
            img = draw_dish(spec, seed=int(d["code"].split("-")[1].encode().hex(), 16))
            img.quantize(colors=128, method=Image.MEDIANCUT).save(out, "PNG", optimize=True)
            entries.append({"kind": "dish", "code": d["code"], "name": d["name"],
                            "category": d["category"],
                            "path": f"miniapp/assets/dishes/{out.name}",
                            "width": DISH_W, "height": DISH_H,
                            "bytes": out.stat().st_size, "sha256": sha256(out),
                            "source": "procedural"})

    info = combo(dishes)
    data = poster_data(dishes, info)
    banners = ASSETS / "banner"

    # 小程序首屏横幅：包内放压缩 JPG（PNG 太大），可编辑 SVG 随物料归档
    bw, bh = 1080, 350
    bprims = poster_primitives(bw, bh, "banner", data)
    bsvg = POSTER_DIR / "menu_banner.svg"
    render_png(bprims, bw, bh, POSTER_DIR / "menu_banner.png")
    render_svg(bprims, bw, bh, bsvg)
    banners.mkdir(parents=True, exist_ok=True)
    bjpg = banners / "menu_banner.jpg"
    with Image.open(POSTER_DIR / "menu_banner.png") as im:
        im.convert("RGB").save(bjpg, "JPEG", quality=80, optimize=True, progressive=True)
    entries.append({"kind": "banner", "code": "menu_banner", "name": "小程序首屏横幅",
                    "category": "门店物料", "path": f"miniapp/assets/banner/{bjpg.name}",
                    "svg": "docs/menu_assets/menu_banner.svg",
                    "width": bw, "height": bh, "bytes": bjpg.stat().st_size,
                    "sha256": sha256(bjpg)})

    posters = [("poster_vertical", 1200, 1600, "vertical"),
               ("poster_wide", 1600, 900, "wide"),
               ("poster_square", 1200, 1200, "square")]
    for stem, w, h, kind in posters:
        prims = poster_primitives(w, h, kind, data)
        png = POSTER_DIR / f"{stem}.png"
        svg = POSTER_DIR / f"{stem}.svg"
        render_png(prims, w, h, png)
        render_svg(prims, w, h, svg)
        entries.append({"kind": "poster", "code": stem, "name": stem,
                        "category": "门店物料", "path": f"docs/menu_assets/{png.name}",
                        "svg": f"docs/menu_assets/{svg.name}",
                        "width": w, "height": h, "bytes": png.stat().st_size,
                        "sha256": sha256(png)})

    sources = sorted({e.get("source", "procedural") for e in entries if e["kind"] == "dish"})
    manifest = {
        "generated_by": "tools/menu_assets.py",
        "dish_source": sources,
        "note": ("菜品图为" + ("AI 生成的水彩动漫风示意" if "ai" in sources else "程序绘制矢量示意")
                 + "，非实拍；已裁去生成平台角水印，不含文字与第三方素材；"
                 "价格由前端按 seed 数据渲染，图价分离；门店名与组合价为演示数据。"
                 "平台发布时 AI 生成图需按当期规则加 AI 标识，本项目在详情页与物料角注「示意」。"),
        "style": {"palette": "水彩 + 米白桌 + 暖光，全局同一套提示词尾句",
                  "crop": "原图 1024×768 → 裁水印 → 940×705 → 640×480",
                  "prompt_suffix": _STYLE},
        "assets": entries,
        "combo": {"dishes": [d["name"] for d in info["picks"]],
                  "total": info["total"],
                  "price_source": "fixtures/seed_kitchen.sql 菜单单价算术合计（无折扣）"},
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    mapping = {e["code"]: "/" + e["path"].split("miniapp/", 1)[1]
               for e in entries if e["kind"] == "dish"}
    body = ",\n".join(f'  "{code}": "{path}"' for code, path in sorted(mapping.items()))
    MAPPING_JS.write_text(
        "// 自动生成，勿手改：python tools/menu_assets.py\n"
        "// 菜品示意图形路径映射（code → 包内路径）。新增菜品先补 DISH_SPECS 再重跑生成器；\n"
        "// 没映射到的 code 前端走无图回退，不会白屏。\n"
        "module.exports = {\n" + body + "\n};\n", encoding="utf-8")

    total_kb = sum(e["bytes"] for e in entries) / 1024
    print(f"✓ 生成 {len(entries)} 个资产（菜品 {len(mapping)} + 物料 {len(posters)}），"
          f"合计 {total_kb:.0f} KB")
    print(f"  manifest → {MANIFEST.relative_to(ROOT)}")
    print(f"  映射     → {MAPPING_JS.relative_to(ROOT)}")
    print(f"  组合价   → ￥{info['total']:.2f}（{' + '.join(d['name'] for d in info['picks'])}）")
    return 0


def check() -> int:
    """校验：清单存在、文件齐全、尺寸对、哈希对、前端映射与清单一致。"""
    fails = 0
    if not MANIFEST.exists():
        print("✗ manifest.json 不存在，先跑一次生成")
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for e in manifest["assets"]:
        path = ROOT / e["path"]
        if not path.is_file():
            print(f"  ✗ 缺文件 {e['path']}")
            fails += 1
            continue
        with Image.open(path) as im:
            if im.size != (e["width"], e["height"]):
                print(f"  ✗ 尺寸不符 {e['path']} {im.size} != {(e['width'], e['height'])}")
                fails += 1
        if sha256(path) != e["sha256"]:
            print(f"  ✗ 哈希不符 {e['path']}（图被改过但没重跑生成器）")
            fails += 1
    js = MAPPING_JS.read_text(encoding="utf-8") if MAPPING_JS.exists() else ""
    codes = {e["code"] for e in manifest["assets"] if e["kind"] == "dish"}
    missing = [c for c in codes if f'"{c}":' not in js]
    if missing:
        print(f"  ✗ 前端映射缺 {len(missing)} 项：{','.join(sorted(missing)[:5])}")
        fails += 1
    print(f"\n=== menu_assets --check：{'PASS' if not fails else f'{fails} 项失败'} ===")
    return 0 if not fails else 1


def contact_sheet() -> Path:
    """把 24 张拼成一张 6×4 联表，方便一眼看风格是否统一（不参与验收）。"""
    files = sorted(list(DISH_DIR.glob("D-*.jpg")) + list(DISH_DIR.glob("D-*.png")))
    if not files:
        raise SystemExit("没有菜品图可拼")
    cols, rows, tw, th = 4, 6, 320, 240
    sheet = Image.new("RGB", (cols * tw, rows * th), (245, 241, 233))
    for i, f in enumerate(files[:cols * rows]):
        im = Image.open(f).resize((tw - 8, th - 8), Image.LANCZOS)
        sheet.paste(im, ((i % cols) * tw + 4, (i // cols) * th + 4))
    out = ROOT / "var" / "menu_contact_sheet.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "PNG", optimize=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="菜品示意图与门店物料生成器")
    ap.add_argument("--check", action="store_true", help="只校验不生成")
    ap.add_argument("--sheet", action="store_true", help="额外拼一张联表供目视检查")
    ap.add_argument("--mock", action="store_true", help="额外画一张首屏静态版式预览")
    ap.add_argument("--procedural", action="store_true",
                    help="离线兜底：不用 AI 原图，用 Pillow 画矢量示意（风格朴素，仅应急）")
    args = ap.parse_args()
    if args.check:
        return check()
    code = build("procedural" if args.procedural else "raw")
    if code == 0 and args.mock:
        dishes = load_dishes()
        print(f"  首屏预览 → {mock_page(dishes, load_featured_js()).relative_to(ROOT)}")
    if code == 0 and args.sheet:
        print(f"  联表     → {contact_sheet().relative_to(ROOT)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
