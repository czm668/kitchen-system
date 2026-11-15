# -*- coding: utf-8 -*-
"""大屏演示启动器。

为什么存在：便携 Python 是 `._pth` 隔离模式（isolated=1），`-m kitchen.api.server`
既不注入 cwd 也不吃 PYTHONPATH——必须显式把 `src/` 塞进 sys.path 才能找到
kitchen 包。这个脚本就是那一行 sys.path + 转调 server.main()，其余参数与
`python -m kitchen.api.server` 完全一致。

用法：
    python tools/serve_dashboard.py --db var/dashboard.db --schedule
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _p in (str(_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kitchen.api.server import main  # noqa: E402

if __name__ == "__main__":
    main()
