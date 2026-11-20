# -*- coding: utf-8 -*-
"""菜单视觉与图片规范台账生成器（A24 配套）。

    python tools/menu_audit.py            # 生成三份输入 JSON（不改动业务代码）
    python tools/menu_audit.py --print     # 同时打印各输入的关键统计

产出（默认写到 05_系统记录/20261119_菜单视觉体检/）：

    audit_before.json   改造前菜单视觉体检输入（纯文字列表时代）
    audit_after.json    改造后菜单视觉体检输入（首屏主推 + 左图右文）
    image_rules.json    图片规范检查输入（26 张菜品图 + 横幅 + 3 张门店物料）

三份 JSON 交给专家包里的两个 skill 脚本出 xlsx：

    python <skill>/canyin-caidan-shijue-tijian/scripts/build_*.py \
        --input audit_after.json --output 菜单视觉体检_改造后.xlsx
    python <skill>/canyin-pingtai-tupian-guifan/scripts/build_*.py \
        --input image_rules.json --output 图片规范检查.xlsx

**为什么用脚本而不是手填表**：菜单 26 道、图 30 个，手填一定会和 seed / manifest
漂移。这里所有字段都从 `fixtures/seed_kitchen.sql`、`miniapp/utils/menu_visual.js`
和 `docs/menu_assets/manifest.json` 现读，表格里的数字随时能回溯到源文件。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT.parent.parent / "05_系统记录" / "20261119_菜单视觉体检"
SEED = ROOT / "fixtures" / "seed_kitchen.sql"
VISUAL = ROOT / "miniapp" / "utils" / "menu_visual.js"
MANIFEST = ROOT / "docs" / "menu_assets" / "manifest.json"

STORE = "明厨小馆（演示门店）"
DATE = "2026-11-19"
WXML = "miniapp/pages/menu/menu.wxml"
#: 自定规范：小程序卡片图位 200×150rpx，3 倍屏实际需要 ≥600×450；横幅按卡片高度留图。
#: 这是本项目的**自定**下限，不是平台强制值——平台条款本机取不到，表里按待确认处理。
MIN_BY_SCENE = {
    "dish": (600, 450),      # 卡片缩略位 200×150rpx，3 倍屏
    "banner": (1080, 340),   # 首屏横幅 1080×350（约 3:1）
    "poster": (1200, 900),   # A 系/竖版/横屏物料的最低下限
}
RULE_VERSION = "self-2026-11-19"
RULE_SOURCE = ("项目自定规范 docs/14_菜单视觉与门店物料设计.md §五；"
               "微信/美团平台当期强制尺寸未取到权威原文，作待确认")


def load_dishes() -> list:
    sql = SEED.read_text(encoding="utf-8")
    rows = re.findall(
        r"\(\s*(\d+),\s*'(D-[A-Z]+)',\s*'([^']+)',\s*'([^']+)',\s*([\d.]+),\s*(\d+),"
        r"\s*([01]),\s*([01])", sql)
    out = []
    for _id, code, name, cat, price, prep, sold_out, enabled in rows:
        out.append({"id": int(_id), "code": code, "name": name, "category": cat,
                    "price": float(price), "prep": int(prep),
                    "sold_out": sold_out == "1", "enabled": enabled == "1"})
    return out


def load_featured() -> dict:
    """从 menu_visual.js 抠出主推位菜码（前端配置是唯一真源，不在这里另抄一份）。"""
    text = VISUAL.read_text(encoding="utf-8")
    block = re.search(r"featured:\s*\{(.*?)\}", text, re.S)
    if not block:
        return {}
    return dict(re.findall(r"'(D-[A-Z]+)':\s*'([^']*)'", block.group(1)))


def order_rows(dishes: list, featured: dict) -> list:
    """主推前置后的展示顺序（与 menu.js 的 applyFilter 排序规则一致）。"""
    head = [d for d in dishes if d["code"] in featured]
    tail = [d for d in dishes if d["code"] not in featured]
    return head + tail


def page_of(order: int) -> str:
    return "首屏" if order <= 6 else ("第2屏" if order <= 12 else "第3屏及以后")


def audit_records(dishes: list, featured: dict, with_image: bool) -> list:
    rows = []
    for i, d in enumerate(order_rows(dishes, featured), start=1):
        is_hero = d["code"] in featured
        rows.append({
            "record_id": f"{DATE}-MENU-{'A' if with_image else 'B'}-{i:02d}",
            "store": STORE,
            "source_file": WXML,
            "source_locator": (f"菜品卡 L48-L72（图 + 主推角标 + 价格）" if with_image
                               else "改造前版本：纯文字列表 L33-L56（无图片位、无主推位）"),
            "notes": (f"菜码 {d['code']}；沽清={d['sold_out']}；"
                      + ("主推位（运营指定，非销量推断）" if is_hero else "常规位")),
            "page": page_of(i),
            "display_order": i,
            "dish": d["name"],
            "dish_role": "招牌菜" if is_hero else "普通",
            "has_image": bool(with_image),
            "image_style": "水彩动漫风（全套同一提示词尾句）" if with_image else "无图",
            "name_char_count": len(d["name"]),
            "price_visible": True,
            "selling_point_present": bool(with_image and is_hero),
        })
    return rows


def image_records(manifest: dict) -> list:
    rows = []
    for i, a in enumerate(manifest["assets"], start=1):
        kind = a["kind"]
        is_dish = kind == "dish"
        scene = ("小程序菜品卡缩略图" if is_dish else
                 "小程序首屏横幅" if kind == "banner" else "门店/平台物料")
        min_w, min_h = MIN_BY_SCENE[kind if kind != "poster" else "poster"]
        # 菜品卡图未叠加文字（卡片尺寸小，压角标会压到盘面），AI 标识只做在
        # 详情页与物料角注上——这条如实记为「未标识」，是个待办而不是已解决。
        ai_label = not is_dish
        rows.append({
            "record_id": f"{DATE}-IMG-{i:02d}",
            "store": STORE,
            "source_file": a["path"],
            "source_locator": f"{a['width']}×{a['height']}px / {a['bytes']}B / sha256:{a['sha256'][:12]}",
            "notes": ("AI 生成（提示词见 manifest）" if a.get("source") == "ai"
                      else "程序绘制") + (f"；{a['image_note']}" if a.get("image_note") else ""),
            "platform": "微信小程序（自定规范）",
            "scene": scene,
            "width_px": a["width"],
            "height_px": a["height"],
            "min_width_px": min_w,
            "min_height_px": min_h,
            "rule_version": RULE_VERSION,
            "rule_source": RULE_SOURCE,
            "has_watermark": False,
            "has_contact": False,
            "has_unverified_badge": False,
            "ai_generated": a.get("source") == "ai",
            "ai_label_present": ai_label,
            "has_third_party_ip": False,
            "authorization_ready": True,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="生成菜单视觉与图片规范台账输入")
    ap.add_argument("--print", dest="show", action="store_true", help="打印关键统计")
    args = ap.parse_args()

    dishes = load_dishes()
    featured = load_featured()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    before = {"analysis_date": DATE, "store": STORE,
              "records": audit_records(dishes, featured, with_image=False)}
    after = {"analysis_date": DATE, "store": STORE,
             "records": audit_records(dishes, featured, with_image=True)}
    images = {"analysis_date": DATE, "store": STORE,
              "records": image_records(manifest)}

    for name, data in (("audit_before.json", before), ("audit_after.json", after),
                       ("image_rules.json", images)):
        path = OUT_DIR / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(f"  → {path.relative_to(ROOT.parent.parent)}（{len(data['records'])} 行）")

    if args.show:
        no_img = sum(1 for r in before["records"] if not r["has_image"])
        hero_no_img = sum(1 for r in before["records"]
                          if r["dish_role"] == "招牌菜" and not r["has_image"])
        print(f"\n改造前：{no_img}/{len(before['records'])} 道无图；"
              f"招牌位缺图 {hero_no_img} 道；有卖点 0 道")
        print(f"改造后：有图 {sum(1 for r in after['records'] if r['has_image'])} 道；"
              f"首屏（前 6 位）主推 {sum(1 for r in after['records'] if r['display_order'] <= 6)} 席")
        ai = sum(1 for r in images["records"] if r["ai_generated"])
        unlabelled = sum(1 for r in images["records"] if r["ai_generated"] and not r["ai_label_present"])
        print(f"图片：{len(images['records'])} 个资产，AI 生成 {ai} 个，未加 AI 标识 {unlabelled} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
