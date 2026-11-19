# -*- coding: utf-8 -*-
"""视觉模型指标单一数据源与报告工具的守卫。

钉死两件事：
1. JSON 与 var/train.log 的真实值对齐（防止有人手抄一个更好看的 AP）；
2. check() 能抓住各种自洽性破坏（顺序错、编造 AP、边界声明不符）。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

from vision_model_report import check, load_metrics, report  # noqa: E402


@pytest.fixture(scope="module")
def metrics():
    return load_metrics()


class TestAgainstTrainLog:
    """指标必须与 var/train.log 的 best.pt 验证结果逐字对齐。"""

    def test_文件存在且可解析(self, metrics):
        assert metrics["overall"]["map50_95"] == 0.643
        assert metrics["overall"]["map50"] == 0.927

    def test_整体指标对齐日志(self, metrics):
        log = (_ROOT / "var" / "train.log").read_text(encoding="utf-8", errors="replace")
        # 日志 L460 的最终行：all 3484 9893 0.904 0.877 0.927 0.643
        assert "3484       9893      0.904      0.877      0.927      0.643" in log
        # nt_per_class / ap_class_index 是审计锚点
        assert "nt_per_class: array([1326, 8147,  331,    0,   89,    0])" in log
        assert "ap_class_index: array([0, 1, 2, 4])" in log

    def test_日志内容锚点仍在(self, metrics):
        """溯源用内容锚点不用行号（日志混用 \\r/\\n，行号会漂）；锚点必须在日志里。"""
        log = (_ROOT / "var" / "train.log").read_text(
            encoding="utf-8", errors="replace")
        prov = metrics["provenance"]
        for key in ("anchor_overall", "anchor_per_class", "anchor_arrays"):
            assert prov[key] in log, f"日志里找不到 {key}，指标来源可能已变"

    def test_真实数据源自洽(self, metrics):
        assert check(metrics) == []


class TestCheckGuard:
    """check() 必须对破坏自洽的篡改报警——这是防手抄漂移的核心。"""

    def test_类别顺序改了会报错(self, metrics):
        bad = copy.deepcopy(metrics)
        bad["per_class"][0]["name"], bad["per_class"][1]["name"] = (
            bad["per_class"][1]["name"], bad["per_class"][0]["name"])
        assert any("类别顺序不符" in x for x in check(bad))

    def test_给无验证样本的类编造AP会报错(self, metrics):
        bad = copy.deepcopy(metrics)
        bad["per_class"][3]["map50"] = 0.9  # uniform 本应是 null
        assert any("无验证样本却填了" in x for x in check(bad))

    def test_ap_class_index与validated不一致会报错(self, metrics):
        bad = copy.deepcopy(metrics)
        bad["ap_class_index"] = [0, 1, 2, 3, 4]  # 多塞了 uniform(3)
        assert any("ap_class_index" in x for x in check(bad))

    def test_边界声明的validated数不符会报错(self, metrics):
        bad = copy.deepcopy(metrics)
        bad["boundary"]["validated_classes"] = 6
        assert any("validated_classes" in x for x in check(bad))

    def test_nt_per_class与instances不符会报错(self, metrics):
        bad = copy.deepcopy(metrics)
        bad["nt_per_class"][0] = 9999
        assert any("nt_per_class" in x for x in check(bad))

    def test_overall不等于各类均值会报错(self, metrics):
        bad = copy.deepcopy(metrics)
        bad["overall"]["map50_95"] = 0.99  # 破坏 overall = validated 各类均值
        assert any("overall map50_95" in x and "均值" in x for x in check(bad))

    def test_篡改单个类别AP会破坏均值等式(self, metrics):
        """强约束：改任意一类 AP 都会让 overall 均值对不上，无法只改一处蒙混。"""
        bad = copy.deepcopy(metrics)
        bad["per_class"][0]["map50_95"] = 0.70  # person 从 0.590 改 0.70
        assert any("均值" in x for x in check(bad))

    def test_per_class为空直接报错(self, metrics):
        bad = copy.deepcopy(metrics)
        bad["per_class"] = []
        assert check(bad)


class TestReport:
    def test_报告含诚实口径(self, metrics):
        md = report(metrics)
        assert "uniform, phone 两类无独立验证样本" in md
        assert "不等于厨房现场实测" in md
        assert "0.643" in md

    def test_报告不声称六类全过(self, metrics):
        md = report(metrics)
        assert "不得声称 6 类全部通过验证" in md
