# -*- coding: utf-8 -*-
"""离线自检 CLI 入口：`python -m tools.sim_cli --seconds 40 [--json]`。

业务逻辑全部在 `src/kitchen/sim.py`；本文件只负责把 `src/` 挂进 sys.path，
使命令在未安装包、未设 PYTHONPATH 的情况下也能直接跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kitchen.sim import main  # noqa: E402


if __name__ == "__main__":
    main()
