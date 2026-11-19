# -*- coding: utf-8 -*-
"""phone 标注补齐工具的纯函数与 commit 流程回归。

不测 scan/sheet（需要训练环境的 ultralytics/cv2），只钉死：
1. `dedupe_key` 坐标 4 位小数去重；
2. `parse_approvals` 读裁决表；
3. `merge_approved` 去重与换行;
4. `commit` 在假根上按裁决合并、去重跳过、dry-run 不写盘、test 拒绝。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tools.vision_label_patch import (
    commit,
    dedupe_key,
    merge_approved,
    parse_approvals,
)


# ------------------------------------------------------------- dedupe_key
def test_dedupe_key_rounds_to_4dp():
    a = dedupe_key("x.jpg", 0.123456, 0.5, 0.1, 0.1)
    b = dedupe_key("x.jpg", 0.123461, 0.5, 0.1, 0.1)
    assert a == ("x.jpg", 0.1235, 0.5, 0.1, 0.1)
    assert a == b  # 4 位小数下相同 → 同一条


def test_dedupe_key_distinguishes_images():
    assert dedupe_key("a.jpg", 0.1, 0.1, 0.1, 0.1) != \
        dedupe_key("b.jpg", 0.1, 0.1, 0.1, 0.1)


# ------------------------------------------------------------- parse_approvals
def _write_census(root: Path, split: str, rows: list[dict]) -> Path:
    path = root / "var" / "phone_census" / f"{split}_phone.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["split", "image", "conf",
                                           "xc", "yc", "w", "h"])
        w.writeheader()
        w.writerows(rows)
    return path


def _row(image: str, conf: float, xc: float) -> dict:
    return {"split": "val", "image": image, "conf": conf, "xc": xc,
            "yc": 0.5, "w": 0.1, "h": 0.1}


def test_parse_approvals_reads_verdict(tmp_path):
    path = tmp_path / "approvals.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["split", "image", "conf",
                                           "xc", "yc", "w", "h", "verdict"])
        w.writeheader()
        w.writerow({"split": "val", "image": "a.jpg", "conf": 0.9, "xc": 0.12345,
                    "yc": 0.5, "w": 0.1, "h": 0.1, "verdict": "accept"})
        w.writerow({"split": "val", "image": "b.jpg", "conf": 0.6, "xc": 0.2,
                    "yc": 0.5, "w": 0.1, "h": 0.1, "verdict": "reject"})
    verdicts = parse_approvals(path)
    assert verdicts[dedupe_key("a.jpg", 0.12345, 0.5, 0.1, 0.1)] == "accept"
    assert verdicts[dedupe_key("b.jpg", 0.2, 0.5, 0.1, 0.1)] == "reject"


# ------------------------------------------------------------- merge_approved
def test_merge_appended_and_deduped():
    existing = "0 person 0.5 0.5 0.4 0.8\n"
    out = merge_approved(existing, ["5 0.1 0.2 0.05 0.10", "5 0.1 0.2 0.05 0.10"])
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("0 person") or lines[0].split()[0] == "0"
    assert lines[1] == "5 0.1 0.2 0.05 0.10"
    assert out.endswith("\n")


def test_merge_into_empty():
    out = merge_approved("", ["5 0.1 0.2 0.05 0.1"])
    assert out == "5 0.1 0.2 0.05 0.1\n"


# ------------------------------------------------------------- commit
def _setup_root(tmp_path: Path) -> Path:
    root = tmp_path
    _write_census(root, "val", [
        _row("a.jpg", 0.9, 0.12345),
        _row("b.jpg", 0.8, 0.2),
        _row("c.jpg", 0.7, 0.3),
    ])
    approvals = root / "var" / "phone_census" / "approvals_val.csv"
    with approvals.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["image", "conf", "xc", "yc", "w",
                                           "h", "verdict"])
        w.writeheader()
        w.writerow({"image": "a.jpg", "conf": 0.9, "xc": 0.12345, "yc": 0.5,
                    "w": 0.1, "h": 0.1, "verdict": "accept"})
        w.writerow({"image": "b.jpg", "conf": 0.8, "xc": 0.2, "yc": 0.5,
                    "w": 0.1, "h": 0.1, "verdict": "reject"})
        w.writerow({"image": "c.jpg", "conf": 0.7, "xc": 0.3, "yc": 0.5,
                    "w": 0.1, "h": 0.1, "verdict": "accept"})
    return root


def test_commit_merges_only_accepted(tmp_path):
    root = _setup_root(tmp_path)
    labels = root / "datasets" / "vision" / "labels" / "val"
    labels.mkdir(parents=True)
    (labels / "a.txt").write_text("0 0.5 0.5 0.4 0.8\n", encoding="utf-8")

    approvals = root / "var" / "phone_census" / "approvals_val.csv"
    log = commit("val", approvals, root=root)

    assert log["merged_lines"] == 2
    assert log["merged_images"] == 2
    a_lines = (labels / "a.txt").read_text(encoding="utf-8").splitlines()
    assert a_lines[0].startswith("0 ")  # 原有 person 行保留
    assert a_lines[1] == "5 0.12345 0.5 0.1 0.1"  # accept 行已并入
    assert (labels / "c.txt").exists()              # 无原标注 → 新建
    assert not (labels / "b.txt").exists()          # reject → 不落盘
    assert json.loads((root / "var" / "phone_census").glob("commit_val_*.json")
                      and next((root / "var" / "phone_census")
                               .glob("commit_val_*.json")).read_text(
                                   encoding="utf-8"))["merged_lines"] == 2


def test_commit_dry_run_writes_nothing(tmp_path):
    root = _setup_root(tmp_path)
    labels = root / "datasets" / "vision" / "labels" / "val"
    labels.mkdir(parents=True)
    approvals = root / "var" / "phone_census" / "approvals_val.csv"
    log = commit("val", approvals, root=root, dry_run=True)
    assert log["merged_lines"] == 2 and log["dry_run"]
    assert not (labels / "a.txt").exists()
    assert not list((root / "var" / "phone_census").glob("commit_val_*.json"))


def test_commit_dedupes_second_run(tmp_path):
    root = _setup_root(tmp_path)
    labels = root / "datasets" / "vision" / "labels" / "val"
    labels.mkdir(parents=True)
    (labels / "a.txt").write_text("5 0.12345 0.5 0.1 0.1\n", encoding="utf-8")
    approvals = root / "var" / "phone_census" / "approvals_val.csv"
    log = commit("val", approvals, root=root)
    assert log["merged_lines"] == 1  # a 的候选已存在 → 只有 c 新增
    assert len((labels / "a.txt").read_text(encoding="utf-8").splitlines()) == 1


def test_commit_refuses_test_split(tmp_path):
    root = _setup_root(tmp_path)
    approvals = root / "var" / "phone_census" / "approvals_val.csv"
    with pytest.raises(SystemExit):
        commit("test", approvals, root=root)


def test_commit_refuses_empty_accepts(tmp_path):
    root = tmp_path
    _write_census(root, "val", [_row("a.jpg", 0.9, 0.1)])
    approvals = root / "var" / "phone_census" / "approvals_val.csv"
    with approvals.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["image", "conf", "xc", "yc", "w",
                                           "h", "verdict"])
        w.writeheader()
        w.writerow({"image": "a.jpg", "conf": 0.9, "xc": 0.1, "yc": 0.5,
                    "w": 0.1, "h": 0.1, "verdict": "reject"})
    with pytest.raises(SystemExit):
        commit("val", approvals, root=root)
