# -*- coding: utf-8 -*-
"""A14：论文图表生成器的解析层回归（纯函数，不加载模型、不画图）。

数据源（runs/ var/ BOM）都真实存在时才跑；换机器缺数据时跳过，
避免把「素材库未同步」误报成代码回归。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# paper_figures 依赖 matplotlib（论文图可选依赖）：本机未装时不让它把
# 整套测试的收集阶段拖崩，模块级跳过。
pytest.importorskip("matplotlib", reason="matplotlib 未安装（论文图可选依赖），跳过论文图回归")

from tools import paper_figures as pf  # noqa: E402

ROOT = pf.ROOT
HAS_DATA = all(p.exists() for p in (pf.RESULTS_CSV, pf.TRAIN_LOG, pf.PR_PNG,
                                    pf.BOM_XLSX, pf.EXPERIMENT_MD))
pytestmark = pytest.mark.skipif(not HAS_DATA, reason="训练/实验/BOM 素材不在本机，跳过解析层回归")


def test_训练曲线读到完整_80_epochs():
    rows = pf.parse_results()
    assert len(rows) == 80
    assert rows[-1]["epoch"] == 80
    assert 0.92 < rows[-1]["metrics/mAP50(B)"] < 0.94
    assert 0.63 < rows[-1]["metrics/mAP50-95(B)"] < 0.66


def test_分项AP_只取最终验证表且缺类不补零():
    log = pf.parse_train_log()
    cls = log["classes"]
    assert {"all", "person", "hat", "mask", "cigarette"} <= set(cls)
    assert cls["all"]["mAP50"] == 0.927
    assert cls["person"]["mAP50_95"] == 0.59
    assert "uniform" not in cls and "phone" not in cls
    assert log["speed_ms"]["inference"] == 0.6


def test_BOM_按类别汇总且如实保留合计差异():
    bom = pf.parse_bom()
    assert "传感" in bom and bom["传感"] == 92.0
    assert abs(sum(bom.values()) - 255.7) < 1e-6  # 与 BOM「合计 257.7」差 2 元，图中已标注待核对


def test_对比实验解析出四组延迟():
    exp = pf.parse_experiment()
    values = dict(exp["scenarios"])
    assert values["明火离人"] == 12.0
    assert values["冷链超限"] == 3.0
    assert values["燃气泄漏"] == 0.0 and values["排烟超温"] == 0.0
    assert exp["false_alarm_per_hour"] == 0.0


def test_check_模式只读不写(tmp_path: Path):
    assert pf.main(["--check", "--out", str(tmp_path)]) == 0
    assert not list(tmp_path.iterdir())


def test_未跑bench时延迟是参考值而非伪造分布(tmp_path: Path):
    lat = pf.load_latency(tmp_path, pf.parse_train_log())
    assert lat["mode"] == "reference"
    assert "samples_ms" not in lat
    assert "不是分布" in lat["note"]


def test_实测样本清单可复现():
    path = pf.DEFAULT_OUT / "latency_samples.json"
    if not path.exists():
        pytest.skip("尚未执行 --bench")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mode"] == "measured"
    assert len(data["samples_ms"]) == data["count"] >= 1
    assert all(v > 0 for v in data["samples_ms"])
