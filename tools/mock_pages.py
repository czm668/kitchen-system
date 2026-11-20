"""购物车 / 订单进度 两页的静态版式预览（750×1334，1rpx = 1px）。

用途：没有微信开发者工具时，也能看到「菜单之外的两页现在长什么样」。
A25 给这两页补了菜品缩略图并统一了暖色令牌，预览图用来核对版式与配色，
**不是真机截图**——字体渲染、安全区、滚动行为都以真机为准。

和 menu_assets.py 的 mock_page 共用字体、菜品数据与图片查找逻辑，避免两套口径。

    python tools/mock_pages.py            # 出两页 + 一张三页并排对照
    python tools/mock_pages.py --check    # 只校验产物在不在
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import menu_assets as A  # noqa: E402

W, H = 750, 1334

#: 与 app.wxss 的 page{} 同源（改主题色时一起改）
BG = (246, 242, 232)
PANEL = (255, 253, 248)
PANEL2 = (241, 235, 221)
TEXT = (36, 31, 24)
TEXT2 = (107, 99, 85)
TEXT3 = (152, 143, 127)
LINE = (230, 223, 208)
ACCENT = (31, 77, 58)
PRICE = (180, 71, 31)
ORANGE = (178, 104, 10)
GREEN = (47, 125, 91)
INFO = (63, 101, 119)

OUT = A.POSTER_DIR


def _rr(d, x, y, w, h, r, fill):
    d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=fill)


def _thumb(img, code, box_w, box_h, xy, radius=10):
    """把菜品图按 cover 裁进缩略位（等价 WXML 的 mode=aspectFill）。"""
    path = A.dish_file(code)
    if not path.is_file():
        return False
    with Image.open(path) as im:
        im = im.convert("RGB")
        scale = max(box_w / im.width, box_h / im.height)
        im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1), Image.LANCZOS)
        left, top = (im.width - box_w) // 2, (im.height - box_h) // 2
        crop = im.crop((left, top, left + box_w, top + box_h))
    mask = Image.new("L", (box_w, box_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, box_w, box_h], radius=radius, fill=255)
    img.paste(crop, xy, mask)
    return True


def _badge(d, x, y, text, fg, bg, size=20):
    f = A._font(size, True)
    tw = d.textlength(text, font=f)
    _rr(d, x, y, tw + 20, size + 12, (size + 12) / 2, bg)
    d.text((x + 10, y + (size + 12) / 2), text, font=f, fill=fg, anchor="lm")
    return tw + 20


def _nav(title):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 88], fill=PANEL)
    d.text((W / 2, 44), title, font=A._font(30, True), fill=TEXT, anchor="mm")
    return img, d


def _tabbar(img, d, active: int) -> None:
    """底部 tabBar。订单页是 tab 页，不画这块就会显得页面「没到底」。"""
    bar_h = 98
    y = H - bar_h
    d.rectangle([0, y, W, H], fill=PANEL)
    d.line([(0, y), (W, y)], fill=LINE, width=1)
    tabs = [("tab_menu", "点菜"), ("tab_orders", "订单"),
            ("tab_trace", "溯源"), ("tab_profile", "我的")]
    step = W / len(tabs)
    for i, (icon, label) in enumerate(tabs):
        cx = step * i + step / 2
        path = A.ASSETS / "tabbar" / f"{icon}{'_on' if i == active else ''}.png"
        if path.is_file():
            with Image.open(path) as ic:
                ic = ic.convert("RGBA").resize((46, 46), Image.LANCZOS)
                img.paste(ic, (int(cx - 23), y + 12), ic)
        d.text((cx, H - 16), label, font=A._font(20, i == active),
               fill=ACCENT if i == active else TEXT2, anchor="ms")


def cart_page(dishes: list) -> Image.Image:
    img, d = _nav("确认订单")
    rows = dishes[:3]

    d.text((24, 122), "已选菜品", font=A._font(30, True), fill=TEXT, anchor="ls")
    d.text((726, 122), "清空", font=A._font(22), fill=TEXT3, anchor="rs")

    top = 146
    row_h = 132
    _rr(d, 24, top, 702, row_h * len(rows) + 8, 18, PANEL)
    for i, dish in enumerate(rows):
        y = top + 4 + i * row_h
        _thumb(img, dish["code"], 96, 96, (44, y + 18))
        d.text((160, y + 34), dish["name"], font=A._font(28, True), fill=TEXT, anchor="ls")
        d.text((160, y + 66), f"{dish['category']} · 参考 {dish['prep']} 分钟",
               font=A._font(22), fill=TEXT2, anchor="ls")
        d.text((160, y + 96), "移除", font=A._font(22), fill=TEXT3, anchor="ls")
        d.text((706, y + 36), f"￥{dish['price'] * (i + 1) / (i + 1):.2f}",
               font=A._font(28, True), fill=PRICE, anchor="rs")
        # 步进器
        cx = 706 - 22
        _rr(d, cx - 26, y + 74, 52, 52, 26, ACCENT)
        d.text((cx, y + 100), "＋", font=A._font(30, True), fill=(255, 253, 248), anchor="mm")
        d.text((cx - 48, y + 100), "1", font=A._font(26, True), fill=TEXT, anchor="mm")
        _rr(d, cx - 96, y + 74, 52, 52, 26, PANEL2)
        d.text((cx - 70, y + 100), "－", font=A._font(30, True), fill=ACCENT, anchor="mm")

    # 用餐信息
    y = top + row_h * len(rows) + 44
    d.text((24, y), "用餐信息", font=A._font(30, True), fill=TEXT, anchor="ls")
    y += 24
    _rr(d, 24, y, 702, 226, 18, PANEL)
    lines = [("桌台", "3 号桌"), ("合计", "￥%.2f" % sum(x["price"] for x in rows)),
             ("参考时长", "%d 分钟" % max(x["prep"] for x in rows))]
    for i, (k, v) in enumerate(lines):
        ly = y + 34 + i * 62
        d.text((48, ly), k, font=A._font(26), fill=TEXT2, anchor="ls")
        d.text((702, ly), v, font=A._font(26, i == 1), fill=PRICE if i == 1 else TEXT, anchor="rs")
        if i < 2:
            d.line([(48, ly + 26), (702, ly + 26)], fill=LINE, width=1)

    # 底部提交栏
    d.rectangle([0, H - 132, W, H], fill=PANEL)
    _rr(d, 24, H - 112, 702, 88, 14, ACCENT)
    d.text((W / 2, H - 68), "确认下单 · ￥%.2f" % sum(x["price"] for x in rows),
           font=A._font(30, True), fill=(255, 253, 248), anchor="mm")
    return img


def orders_page(dishes: list) -> Image.Image:
    img, d = _nav("我的订单")

    # 消息入口
    _rr(d, 24, 108, 702, 100, 18, PANEL)
    _badge(d, 48, 142, "消息", INFO, (232, 236, 238), 20)
    d.text((160, 156), "出餐通知", font=A._font(28, True), fill=TEXT, anchor="ls")
    d.text((702, 156), "›", font=A._font(28), fill=TEXT3, anchor="rs")

    d.text((24, 254), "进行中", font=A._font(30, True), fill=TEXT, anchor="ls")
    d.text((726, 254), "1 单", font=A._font(22), fill=TEXT3, anchor="rs")

    # 订单卡
    top = 276
    card_h = 470
    _rr(d, 24, top, 702, card_h, 18, PANEL)
    d.text((48, top + 44), "O1119-001", font=A._font(30, True), fill=TEXT, anchor="ls")
    _badge(d, 640, top + 26, "3 号桌", INFO, (232, 236, 238), 20)
    d.text((48, top + 78), "20:26 下单 · 预计 20:54 出餐 · 剩 14 分钟",
           font=A._font(22), fill=TEXT2, anchor="ls")

    # 整单进度
    _rr(d, 48, top + 108, 654, 10, 5, PANEL2)
    _rr(d, 48, top + 108, 654 * 0.62, 10, 5, ACCENT)
    d.text((48, top + 140), "整单进度 62% · 共 3 份", font=A._font(22), fill=TEXT3, anchor="ls")

    # 菜品行（带缩略图）：菜名与状态徽标同一行居中，ETA 单独一行，
    # 两行都落在 72px 缩略图的高度内，行高才不会被撑开或压字
    states = [("制作中", ORANGE, (250, 241, 226)), ("排队中", INFO, (232, 236, 238)),
              ("已上桌", GREEN, (233, 242, 236))]
    for i, (dish, (label, fg, bg)) in enumerate(zip(dishes[:3], states)):
        y = top + 176 + i * 96
        d.line([(48, y - 10), (702, y - 10)], fill=LINE, width=1)
        _thumb(img, dish["code"], 72, 72, (48, y + 10), radius=8)
        name = f"{dish['name']} × 1"
        d.text((136, y + 38), name, font=A._font(26, True), fill=TEXT, anchor="lm")
        name_w = d.textlength(name, font=A._font(26, True))
        _badge(d, 136 + name_w + 10, y + 24, label, fg, bg, 16)
        d.text((136, y + 66), "剩 14 分钟" if i == 0 else "—",
               font=A._font(20), fill=TEXT3, anchor="lm")
        if i < 2:
            d.rounded_rectangle([620, y + 24, 702, y + 72], radius=12, outline=ACCENT, width=2)
            d.text((661, y + 48), "催一下", font=A._font(20, True), fill=ACCENT, anchor="mm")

    d.text((24, top + card_h + 44), "已完成的订单", font=A._font(30, True), fill=TEXT, anchor="ls")
    _rr(d, 24, top + card_h + 66, 702, 150, 18, PANEL)
    d.text((48, top + card_h + 110), "O1119-000", font=A._font(28, True), fill=TEXT, anchor="ls")
    _badge(d, 636, top + card_h + 94, "已完成", GREEN, (233, 242, 236), 18)
    d.text((48, top + card_h + 148), "19:40 · 共 2 份 · 宫保鸡丁 ×1、米饭 ×1",
           font=A._font(22), fill=TEXT2, anchor="ls")
    _tabbar(img, d, active=1)
    return img


def contact_sheet(pages: list[tuple[str, Image.Image]]) -> Image.Image:
    pad, gap, scale = 28, 24, 0.62
    tw, th = int(W * scale), int(H * scale)
    width = pad * 2 + len(pages) * tw + (len(pages) - 1) * gap
    height = pad * 2 + th + 40
    sheet = Image.new("RGB", (width, height), (252, 250, 244))
    d = ImageDraw.Draw(sheet)
    for i, (label, page) in enumerate(pages):
        x = pad + i * (tw + gap)
        sheet.paste(page.resize((tw, th), Image.LANCZOS), (x, pad))
        d.text((x, pad + th + 14), label, font=A._font(22, True), fill=TEXT2, anchor="ls")
    return sheet


def build() -> int:
    dishes = A.load_dishes()
    if not dishes:
        print("✗ 读不到菜品数据，先跑 tools/menu_assets.py")
        return 2
    cart, orders = cart_page(dishes), orders_page(dishes)
    cart.save(OUT / "cart_page_preview.png", "PNG", optimize=True)
    orders.save(OUT / "orders_page_preview.png", "PNG", optimize=True)

    pages = [("点菜（A24 首屏）", Image.open(OUT / "menu_page_preview.png")),
             ("确认订单（A25 补图）", cart), ("订单进度（A25 补图）", orders)]
    contact_sheet(pages).save(OUT / "ui_pages_preview.png", "PNG", optimize=True)
    print(f"  ✓ 三页预览 → {(OUT / 'ui_pages_preview.png').relative_to(A.ROOT)}")
    return 0


def check() -> int:
    missing = [n for n in ("menu_page_preview.png", "cart_page_preview.png",
                           "orders_page_preview.png", "ui_pages_preview.png")
               if not (OUT / n).is_file()]
    for n in missing:
        print("  ✗ 缺", n)
    print(f"=== 页面预览自检：{'通过' if not missing else f'{len(missing)} 项待生成'} ===")
    return 0 if not missing else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="购物车/订单页静态版式预览")
    ap.add_argument("--check", action="store_true")
    if ap.parse_args().check:
        return check()
    return build()


if __name__ == "__main__":
    sys.exit(main())
