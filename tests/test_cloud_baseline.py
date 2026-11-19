# -*- coding: utf-8 -*-
"""R6 验收基线守卫（审查报告缺陷 #13）。

钉死三件事：
1. 外部超阈值但本机健康 → 判开发机出网抖动，不算服务端回归（不误报）；
2. 两端都慢 → 真回归，必须失败；
3. 无基线 → 退化为旧口径，但明细里明确「无法区分抖动来源」。

外加预采样本 JSON 的读取。这些都是纯函数，不需要云端。
"""
from __future__ import annotations

import json

from tools.miniapp_cloud_check import (
    SELF_HOST_HEALTHY_MS,
    classify_poll_latency,
    load_self_host_samples,
)


def test_external_jitter_with_healthy_self_host_is_not_a_regression():
    # 开发机出网 3.57s，服务器本机 1.2ms —— 审查报告里的真实数字
    ok, cause, detail = classify_poll_latency(
        [40, 60, 3570, 55, 48], [1.1, 1.2, 1.0, 1.4, 0.9])
    assert ok is True
    assert cause == "network_jitter"
    assert "非服务端回归" in detail
    assert "p100=3570ms" in detail
    assert "本机 p50=1ms p100=1ms" in detail


def test_both_sides_slow_is_a_real_regression():
    ok, cause, detail = classify_poll_latency(
        [1200, 1500, 1800], [900, 1100, 1300])
    assert ok is False
    assert cause == "server_slow"
    assert "真服务端回归" in detail


def test_external_fast_passes_with_or_without_baseline():
    ok, cause, _ = classify_poll_latency([20, 40, 60], None)
    assert (ok, cause) == (True, "no_baseline")
    ok, cause, _ = classify_poll_latency([20, 40, 60], [1, 2, 3])
    assert (ok, cause) == (True, "ok")


def test_no_baseline_external_slow_fails_but_says_why():
    ok, cause, detail = classify_poll_latency([300, 400, 2500], None)
    assert ok is False
    assert cause == "no_baseline"
    assert "无法区分" in detail


def test_self_host_threshold_is_the_documented_200ms():
    # 200ms 是"服务端健康"的红线，外部延迟超 1s 时才用它做归因
    assert SELF_HOST_HEALTHY_MS == 200.0
    ok, cause, _ = classify_poll_latency([1500], [199])
    assert (ok, cause) == (True, "network_jitter")
    ok, cause, _ = classify_poll_latency([1500], [201])
    assert (ok, cause) == (False, "server_slow")


def test_load_self_host_samples_array_and_object(tmp_path):
    arr = tmp_path / "a.json"
    arr.write_text(json.dumps([1.0, 2.5, 3]), encoding="utf-8")
    assert load_self_host_samples(str(arr)) == [1.0, 2.5, 3.0]

    obj = tmp_path / "b.json"
    obj.write_text(json.dumps({"samples_ms": [4, 5], "host": "127.0.0.1"}),
                   encoding="utf-8")
    assert load_self_host_samples(str(obj)) == [4.0, 5.0]


def test_load_self_host_samples_rejects_bad_shape(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    try:
        load_self_host_samples(str(bad))
    except ValueError:
        pass
    else:
        raise AssertionError("基线形状不对时应抛 ValueError")
