# -*- coding: utf-8 -*-
"""重建二维码基准夹具（`tests/fixtures/qr_golden.json`）。

**这个脚本不是运行时依赖，也不在 CI 里跑。** 它只在「怀疑自研编码器有问题」或
「要升级版本范围」时手工执行一次，用 `segno`（第三方参考实现）当外部真值：

```
pip install --target <隔离目录> segno        # 不要装进工程运行环境
python tools/qr_bench.py --oracle <隔离目录>
```

工程本身**不依赖 segno**：`process/qr.py` 是纯标准库实现，单测只比对夹具里的
哈希。之所以保留这个脚本，是因为「基准怎么来的」必须可复现——不然半年后没人
知道 `qr_golden.json` 里那堆 sha256 是谁生成的。

## 为什么只比「满容量」用例

`segno.encoder.write_padding_bits` 在 `length % 8 == 0` 时会多补 8 个 0 位
（ISO/IEC 18004 要求此时**不**补），于是非满容量内容两者填充码字错位、矩阵必然
不同——但都能正常扫。拿它当基准会把一个第三方偏差锁进自己的测试，所以基准只取
「内容恰好填满数据容量」的用例（此时双方都不需要终止符与填充码字）。
非满容量内容的正确性靠 `tests/test_qr.py` 的**自解码往返**兜（RS 余式必须全 0）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from kitchen.process import qr  # noqa: E402

BASE = "https://k.example.com/t/TC-DEMO-0001?dish=1&batch=B-DEMO-001"
OUT = _ROOT / "tests" / "fixtures" / "qr_golden.json"

REGRESSION = [
    ("https://k.example.com/t/TC-DEMO-0001", "真实溯源短链"),
    ("TC-DEMO-0001", "只放溯源码本体"),
    ("http://192.168.1.20:8000/trace?code=TC-DEMO-0001", "局域网部署"),
    ("青椒肉丝 B-DEMO-001 鲜丰肉联厂", "中文标签（UTF-8 多字节）"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="重建二维码基准夹具")
    parser.add_argument("--oracle", default="", help="segno 所在目录（pip --target 装到哪）")
    parser.add_argument("--out", default=str(OUT), help="输出路径")
    args = parser.parse_args()

    if args.oracle and args.oracle not in sys.path:
        sys.path.insert(0, args.oracle)
    try:
        import segno
    except ImportError:
        print("缺少 segno：先 pip install --target <隔离目录> segno，再用 --oracle 指过去。\n"
              "（故意不做成运行时依赖，见模块文档）", file=sys.stderr)
        return 2

    exact = []
    for version, cap in sorted(qr.BYTE_CAPACITY_M.items()):
        payload = (BASE * 4)[:cap]
        if len(payload.encode()) != cap:
            print(f"v{version} 内容长度 {len(payload.encode())} != 容量 {cap}", file=sys.stderr)
            return 1
        masks = []
        for mask in range(8):
            mine = qr.encode(payload, mask=mask, version=version)
            ref = segno.make(payload, error="m", mode="byte", mask=mask,
                             version=version, boost_error=False, micro=False)
            want = ["".join(str(int(v)) for v in row) for row in ref.matrix]
            if want != mine.rows_text():
                print(f"[不一致] v{version} mask={mask}", file=sys.stderr)
                return 1
            masks.append(mine.hex_digest())
        exact.append({"version": version, "payload": payload, "masks": masks})
        print(f"v{version:2} cap={cap:3} OK")

    regression = []
    for text, note in REGRESSION:
        code = qr.encode(text)
        regression.append({
            "text": text, "version": code.version, "mask": code.mask,
            "size": code.size, "sha256": code.hex_digest(), "note": note,
        })
        print(f"回归 {text[:28]!r} v{code.version} mask={code.mask}")

    payload = {
        "note": "满容量用例的矩阵哈希由 segno 逐掩码比对确认（外部真值）；回归用例锁本实现行为。"
                "重建方式见 tools/qr_bench.py。",
        "exact_capacity": exact,
        "regression": regression,
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
