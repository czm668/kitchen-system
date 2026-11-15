# -*- coding: utf-8 -*-
"""重新生成 `fixtures/kitchen_config_default.json`（端侧内置默认配置）。

## 为什么要有这个脚本

那份 JSON 是**生成物**，不是手写配置：它的阈值行必须和 `seed_kitchen.sql`
逐字一致，动作语义必须和 `kitchen_rules.json` 一致。手改 JSON 的后果是
「端侧按旧阈值判、平台按新阈值显示」，现场排查一次的成本远超写这个脚本。

`tests/test_hood_profile.py::test_内置默认与种子阈值行逐条一致` 是这道护栏的
自动化版本——它读 seed SQL 逐行比对 JSON。所以本脚本的正确性由测试兜底：
改完 seed 跑一下脚本，再跑测试，两边都对才算改完。

## 用法

```
python tools/build_config.py            # 从 seed + rules 重建
python tools/build_config.py --check    # 只比对，不一致就退出码 1（CI 用）
```
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kitchen.rules.schedule import builtin_schedule   # noqa: E402
from kitchen.sqlseed import (                          # noqa: E402
    cell_value, cells, insert_body, split_rows, strip_comments,
)

SEED = ROOT / "fixtures" / "seed_kitchen.sql"
RULES = ROOT / "fixtures" / "kitchen_rules.json"
TARGET = ROOT / "fixtures" / "kitchen_config_default.json"

#: env_thresholds 的列顺序，与 seed 里的 INSERT 一一对应
#: （`updated_at` 只存在于 SQL，不进端侧配置——端侧没有「行更新时间」这个概念，
#: 整个配置包只有一个 `updated_at`）
THRESHOLD_COLUMNS = (
    "id", "point_id", "kind", "profile", "lower_bound", "upper_bound",
    "duration_sec", "window_sec", "aggregate", "level", "enabled", "updated_at",
)

#: 边界列一律 float（`8.0` / `-30.0`），时长列一律取整（`120` / `600`）——
#: 和最初那份生成物保持一致，避免每次重建都产生一堆 `600` ↔ `600.0` 的无谓 diff
NUMERIC = {"lower_bound", "upper_bound"}
SECONDS = {"duration_sec", "window_sec"}
INTEGER = {"id", "point_id", "level", "enabled"}


# 解析逻辑已抽到 `kitchen.sqlseed`（人员域 A3 也要读同一份种子）。
# 这里保留同名的薄包装，只为 `build_payload()` 少改几行——两处各自实现一份
# 解析器才是真正危险的事：种子里多一个引号内的逗号，只有一处被修好。
_strip_comments = strip_comments
_insert_body = insert_body
_split_rows = split_rows
_cells = cells
_value = cell_value


def _coerce(column: str, cell: str):
    raw = _value(cell)
    if raw is None:
        return None
    if column in INTEGER:
        return int(float(raw))
    if column in NUMERIC:
        return float(raw)
    if column in SECONDS:
        value = float(raw)
        return int(value) if value.is_integer() else value
    return str(raw)


def build_payload() -> dict:
    sql_text = SEED.read_text(encoding="utf-8")

    # env_points → id → metric
    point_metric: dict[int, str] = {}
    for row in _split_rows(_insert_body(sql_text, "env_points")):
        cells = _cells(row)
        point_metric[int(float(_value(cells[0])))] = str(_value(cells[3]))

    rows: list[dict] = []
    for row in _split_rows(_insert_body(sql_text, "env_thresholds")):
        cells = _cells(row)
        if len(cells) != len(THRESHOLD_COLUMNS):
            raise SystemExit(
                f"阈值行列数不对（{len(cells)} != {len(THRESHOLD_COLUMNS)}）：{row[:80]}"
            )
        item = {name: _coerce(name, cell) for name, cell in zip(THRESHOLD_COLUMNS, cells)}
        point_id = item["point_id"]
        if point_id not in point_metric:
            raise SystemExit(f"阈值行 id={item['id']} 的 point_id={point_id} 在 env_points 里不存在")
        # metric 由 env_points join 出来，写进 JSON 是为了让行自解释——
        # 端侧只有一份 JSON，没有第二张表可以 join。
        item["metric"] = point_metric[point_id]
        rows.append({
            "id": item["id"],
            "point_id": item["point_id"],
            "metric": item["metric"],
            **{key: item[key] for key in THRESHOLD_COLUMNS
               if key not in ("id", "point_id", "updated_at")},
        })

    rules = json.loads(RULES.read_text(encoding="utf-8"))
    previous_updated = 1762905600
    if TARGET.is_file():
        try:
            previous_updated = int(json.loads(TARGET.read_text(encoding="utf-8"))
                                   .get("updated_at") or previous_updated)
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "schema": 1,
        "dataset": "demo",
        "version": 1,
        "updated_at": previous_updated,
        "schedule": builtin_schedule().to_dict(),
        "scenario_rules": rules.get("rules") or {},
        "threshold_rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="重建端侧内置默认配置")
    parser.add_argument("--check", action="store_true", help="只比对不写文件")
    args = parser.parse_args()

    payload = build_payload()
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current.strip() != text.strip():
            print(f"[不一致] {TARGET} 与 seed SQL / kitchen_rules.json 不同步")
            print("请执行：python tools/build_config.py")
            return 1
        print(f"[一致] {TARGET.name}：{len(payload['threshold_rows'])} 条阈值行")
        return 0

    TARGET.write_text(text, encoding="utf-8")
    ids = sorted(int(row["id"]) for row in payload["threshold_rows"])
    print(f"已写入 {TARGET.relative_to(ROOT)}")
    print(f"  阈值行 {len(ids)} 条（id {ids[0]}~{ids[-1]}，"
          f"{'连续' if ids == list(range(ids[0], ids[-1] + 1)) else '有断号'}）")
    print(f"  动作语义 {len(payload['scenario_rules'])} 组")
    print(f"  档位表 {payload['schedule']['source']}，"
          f"{len(payload['schedule']['slots'])} 个时段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
