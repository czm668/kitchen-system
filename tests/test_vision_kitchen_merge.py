# -*- coding: utf-8 -*-
"""厨房场景补集工具的纯函数与 commit 门禁回归。

不测 ingest/scan/sheet（需要训练环境与归档数据），只钉死：
1. `remap_hat_lines` 单类 chef_hat → 工程 hat 类重映射与防御性断言；
2. `torso_box` 与判定层 `torso_region` 同源（0.20–0.95 躯干区）；
3. `parse_approvals` 读裁决表（verdict/mask/uniform 列）；
4. `merge_approved` 去重与换行（与 phone 补集工具同约定）；
5. `commit` 的 HOLD 门禁：门禁生效时拒绝合并、dry-run 豁免。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tools import vision_kitchen_merge as vkm
from tools.vision_kitchen_merge import (
    commit,
    merge_approved,
    parse_approvals,
    remap_hat_lines,
    torso_box,
)


# ------------------------------------------------------------- remap_hat_lines
def test_remap_hat_lines_maps_class0_to_hat():
    text = "0 0.5 0.5 0.2 0.2\n0 0.1 0.1 0.1 0.1\n"
    out = remap_hat_lines(text)
    assert out == "1 0.5 0.5 0.2 0.2\n1 0.1 0.1 0.1 0.1\n"


def test_remap_hat_lines_keeps_empty_empty():
    assert remap_hat_lines("") == ""
    assert remap_hat_lines("\n\n") == ""


def test_remap_hat_lines_rejects_non_chef_hat_classes():
    with pytest.raises(AssertionError):
        remap_hat_lines("2 0.5 0.5 0.2 0.2\n")  # 归档应只有 class 0


# ------------------------------------------------------------- torso_box
def test_torso_box_matches_judgement_layer_geometry():
    # 与 postprocess.AnalysisPolicy.torso_region 同源：0.20–0.95 高度、全宽
    xc, yc, w, h = 0.5, 0.5, 0.4, 1.0
    ux, uy, uw, uh = torso_box(xc, yc, w, h)
    assert uw == 0.4  # 全宽不变
    assert abs(uy - (0.5 - 0.5 + 0.20 + (0.75 * 0.5) / 1.0)) < 1e-9
    # 顶 = y1 + 0.20h = 0.0 + 0.2；高 = 0.75h = 0.75 → 中心 = 0.2+0.375 = 0.575
    assert abs(uy - 0.575) < 1e-9
    assert abs(uh - 0.75) < 1e-9


def test_torso_box_stays_inside_person_vertical_span():
    ux, uy, uw, uh = torso_box(0.3, 0.6, 0.2, 0.5)
    y1, y2 = uy - uh / 2, uy + uh / 2
    assert y1 >= 0.6 - 0.25  # 不超出 person 上缘
    assert y2 <= 0.6 + 0.25  # 不超出 person 下缘


# ------------------------------------------------------------- parse_approvals
def _write_approvals(path: Path, rows: list[dict]) -> Path:
    fields = ["image", "cand_class", "conf", "xc", "yc", "w", "h",
              "verdict", "mask", "uniform", "tile"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def test_parse_approvals_reads_verdict_mask_uniform(tmp_path: Path):
    p = _write_approvals(tmp_path / "approvals.csv", [
        {"image": "a.jpg", "cand_class": "person", "conf": "0.9",
         "xc": "0.5", "yc": "0.5", "w": "0.3", "h": "0.8",
         "verdict": "accept", "mask": "", "uniform": "accept", "tile": "0"},
        {"image": "b.jpg", "cand_class": "person", "conf": "0.4",
         "xc": "0.1", "yc": "0.2", "w": "0.1", "h": "0.2",
         "verdict": "reject", "mask": "accept", "uniform": "", "tile": "1"},
    ])
    v = parse_approvals(p)
    assert len(v) == 2
    a = v[("a.jpg", "person", 0.5, 0.5, 0.3, 0.8)]
    assert a["verdict"] == "accept" and a["uniform"] == "accept"
    b = v[("b.jpg", "person", 0.1, 0.2, 0.1, 0.2)]
    assert b["verdict"] == "reject" and b["mask"] == "accept"


# ------------------------------------------------------------- merge_approved
def test_merge_approved_dedupes_and_keeps_trailing_newline():
    base = "1 0.5 0.5 0.2 0.2\n"
    out = merge_approved(base, ["1 0.5 0.5 0.2 0.2", "0 0.1 0.1 0.1 0.1"])
    assert out == "1 0.5 0.5 0.2 0.2\n0 0.1 0.1 0.1 0.1\n"


# ------------------------------------------------------------- commit 门禁
def _stage_fixture(tmp_path: Path, monkeypatch) -> Path:
    """构造最小暂存区：1 图（train）+ 基础 hat 标注 + 候选 + 裁决表 + HOLD。

    暂存区放在 `<root>/var/kitchen_merge`，与 commit 的门禁检查路径一致。
    """
    stage = tmp_path / "var" / "kitchen_merge"
    (stage / "images").mkdir(parents=True)
    (stage / "labels").mkdir(parents=True)
    (stage / "images" / "chef_x.jpg").write_bytes(b"fake")
    (stage / "labels" / "chef_x.txt").write_text("1 0.5 0.5 0.2 0.2\n",
                                                 encoding="utf-8")
    with (stage / "candidates.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["image", "cand_class", "conf",
                                           "xc", "yc", "w", "h"])
        w.writeheader()
        w.writerow({"image": "chef_x.jpg", "cand_class": "person", "conf": "0.9",
                    "xc": "0.5", "yc": "0.5", "w": "0.3", "h": "0.8"})
    _write_approvals(stage / "approvals.csv", [
        {"image": "chef_x.jpg", "cand_class": "person", "conf": "0.9",
         "xc": "0.5", "yc": "0.5", "w": "0.3", "h": "0.8",
         "verdict": "accept", "mask": "", "uniform": "accept", "tile": "0"},
    ])
    (stage / "ingest_audit.json").write_text(json.dumps({
        "items": [{"image": "chef_x.jpg", "split": "train"}]}),
        encoding="utf-8")
    (stage / "HOLD").write_text("gate", encoding="utf-8")
    monkeypatch.setattr("tools.vision_kitchen_merge.STAGE", stage)
    return stage


def test_commit_refuses_while_hold_gate_active(tmp_path: Path, monkeypatch):
    _stage_fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="HOLD"):
        commit(vkm.STAGE / "approvals.csv", root=tmp_path)


def test_commit_dry_run_runs_even_with_hold(tmp_path: Path, monkeypatch):
    stage = _stage_fixture(tmp_path, monkeypatch)
    log = commit(vkm.STAGE / "approvals.csv", root=tmp_path, dry_run=True)
    assert log["dry_run"] is True
    assert log["merged_images"] == 1
    assert log["merged_lines"] == 2  # person + uniform（躯干推导）
    assert not (tmp_path / "datasets" / "vision" / "labels" / "train").exists()


def test_commit_after_hold_released_merges_labels_and_image(
        tmp_path: Path, monkeypatch):
    stage = _stage_fixture(tmp_path, monkeypatch)
    (stage / "HOLD").unlink()
    log = commit(vkm.STAGE / "approvals.csv", root=tmp_path)
    assert log["merged_images"] == 1
    target = tmp_path / "datasets" / "vision" / "labels" / "train" / "chef_x.txt"
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    # hat 底表 + person + uniform(躯干) = 3 行；uniform 框 = 躯干区
    assert len(lines) == 3
    assert lines[0] == "1 0.5 0.5 0.2 0.2"
    assert lines[1].startswith("0 0.5 0.5 0.3 0.8")
    ux, uy, uw, uh = lines[2].split()[1:]
    assert uw == "0.30000" and abs(float(uh) - 0.8 * 0.75) < 1e-4
    assert (tmp_path / "datasets" / "vision" / "images" / "train"
            / "chef_x.jpg").exists()
    assert Path(log["log"]).name.startswith("commit_")
