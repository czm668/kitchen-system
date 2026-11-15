# -*- coding: utf-8 -*-
"""旧工程素材取用器测试：禁取护栏、路径穿越、取用台账、哈希校验。

全部使用临时沙箱素材库，不依赖真实旧工程是否存在。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools import legacy


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """造一个 6 文件的假素材库 + 最小台账，完全隔离真实旧目录。"""
    root = tmp_path / "legacy"
    for rel, text in {
        "kernel/bus/topics.py": "TOPICS = 1\n",
        "kernel/bus/schemas.py": "SCHEMAS = 1\n",
        "kernel/commands/service.py": "CMD = 1\n",
        "plugins/mingchu/drivers/dht11.py": "DHT11 = 1\n",
        "plugins/mingchu/drivers/mq2.py": "MQ2 = 1\n",
        "plugins/mingchu/drivers/relay.py": "RELAY = 1\n",
        "datasets/chef_hat/raw/a.jpg": "frozen asset\n",
        "scenarios/mingchu.json": "{}\n",
        "tools/s33_prelabel.py": "PRE = 1\n",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    spec = {
        "schema": 1,
        "legacy_root": "../legacy",
        "policy": {
            "mode": "copy", "vendor_dir": "vendor", "path_prefix": "proj",
            "record": "vendor/SOURCES.json", "max_file_mb": 20,
            "preserve_relative_path": True,
        },
        "forbidden": {
            "zones": ["FROZEN"],
            "path_prefixes": ["scenarios/mingchu.json", "datasets/", "plugins/mingchu/"],
            "allow_exact": ["plugins/mingchu/drivers/dht11.py",
                            "plugins/mingchu/drivers/mq2.py"],
            "must_block": ["datasets/chef_hat/raw/a.jpg",
                           "scenarios/mingchu.json",
                           "plugins/mingchu/drivers/relay.py"],
        },
        "bundles": {
            "bus": {"zone": "CORE", "paths": ["kernel/bus/**"], "reason": "MQTT"},
            "commands": {"zone": "CORE", "paths": ["kernel/commands/**"]},
            "sensors": {"zone": "PORT", "paths": [
                "plugins/mingchu/drivers/dht11.py",
                "plugins/mingchu/drivers/mq2.py"]},
            "prelabel": {"zone": "PORT", "paths": ["tools/s33_prelabel.py"]},
        },
    }
    (project / "legacy_sources.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(legacy, "PROJECT_ROOT", project)
    monkeypatch.setattr(legacy, "SPEC_PATH", project / "legacy_sources.json")
    monkeypatch.setenv(legacy.ENV_ROOT, str(root))
    return SimpleNamespace(root=root, project=project, spec=spec)


# ---------------------------------------------------------------- 台账与路径
def test_real_spec_is_structurally_valid():
    spec = legacy.load_spec()
    assert set(spec["bundles"]) >= {"platform-bus", "rules-engine", "sensor-drivers"}
    assert spec["forbidden"]["zones"] == ["FROZEN"]
    assert "datasets/" in spec["forbidden"]["path_prefixes"]


def test_load_spec_rejects_frozen_bundle(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "legacy_root": "x",
        "policy": {"mode": "copy"},
        "forbidden": {"zones": ["FROZEN"]},
        "bundles": {"old": {"zone": "FROZEN", "paths": ["datasets/**"]}},
    }), encoding="utf-8")
    with pytest.raises(legacy.LegacyError, match="禁止取用分区"):
        legacy.load_spec(bad)


@pytest.mark.parametrize("raw", ["../outside.py", "a/../../b.py", "/abs.py", ""])
def test_normalize_rel_rejects_traversal(raw):
    with pytest.raises(legacy.LegacyError, match="非法素材相对路径"):
        legacy.normalize_rel(raw)


def test_normalize_rel_keeps_normal_paths():
    assert legacy.normalize_rel("kernel\\bus\\topics.py") == "kernel/bus/topics.py"
    assert legacy.normalize_rel("./kernel/bus/./topics.py") == "kernel/bus/topics.py"


# ---------------------------------------------------------------- 禁取护栏
def test_forbidden_blocks_frozen_but_allows_port_whitelist(sandbox):
    spec = sandbox.spec
    assert legacy.is_forbidden(spec, "datasets/chef_hat/raw/a.jpg")
    assert legacy.is_forbidden(spec, "scenarios/mingchu.json")
    assert legacy.is_forbidden(spec, "plugins/mingchu/drivers/relay.py")
    # PORT 白名单：采样逻辑允许取用后拷进 kitchen 改造
    assert not legacy.is_forbidden(spec, "plugins/mingchu/drivers/dht11.py")
    assert not legacy.is_forbidden(spec, "plugins/mingchu/drivers/mq2.py")


def test_resolve_files_expands_glob_and_sorts(sandbox):
    files = legacy.resolve_files(sandbox.spec, "bus", sandbox.root)
    assert files == ["kernel/bus/schemas.py", "kernel/bus/topics.py"]


def test_字节码与缓存不进取用清单(sandbox):
    """本机 pyc 清理一次就会让台账「缺文件」——护栏一误报就没人看了。

    真实事故：清 `__pycache__` 之后 `legacy.py check` 报 10 个 missing，
    全是 pyc。台账不该记编译产物。
    """
    assert legacy.is_cache_artifact("kernel/db/__pycache__/models.cpython-311.pyc")
    assert legacy.is_cache_artifact(".pytest_cache/v/cache/lastfailed")
    assert not legacy.is_cache_artifact("kernel/db/models.py")
    assert not legacy.is_cache_artifact("docs/pycache.md")

    # 素材库里存在 pyc 也不该被取用
    cache = sandbox.root / "kernel" / "bus" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "topics.cpython-311.pyc").write_bytes(b"\x00")
    assert legacy.resolve_files(sandbox.spec, "bus", sandbox.root) == [
        "kernel/bus/schemas.py", "kernel/bus/topics.py"
    ]


def test_台账不含字节码记录():
    """真实台账的护栏：`vendor/SOURCES.json` 里出现 pyc 就说明过滤被绕过了。"""
    record = json.loads(
        (legacy.PROJECT_ROOT / "vendor" / "SOURCES.json").read_text(encoding="utf-8")
    )
    stray = [rel for rel in record["files"] if legacy.is_cache_artifact(rel)]
    assert stray == []


def test_resolve_bundle_that_hits_frozen_is_rejected_as_whole(sandbox):
    spec = json.loads(json.dumps(sandbox.spec))
    spec["bundles"]["bad-frozen"] = {"zone": "CORE", "paths": ["datasets/**"]}
    with pytest.raises(legacy.LegacyError, match="FROZEN 禁取素材"):
        legacy.resolve_files(spec, "bad-frozen", sandbox.root)


def test_resolve_unknown_bundle_lists_candidates(sandbox):
    with pytest.raises(legacy.LegacyError, match="未知 bundle"):
        legacy.resolve_files(sandbox.spec, "nope", sandbox.root)


def test_resolve_files_respects_exclude_rules(sandbox):
    """web/node_modules 这类依赖树必须能排除，否则一取就是上万文件。"""
    junk = sandbox.root / "kernel/bus/node_modules"
    junk.mkdir(parents=True)
    (junk / "x.js").write_text("x", encoding="utf-8")
    spec = json.loads(json.dumps(sandbox.spec))
    spec["bundles"]["bus"]["exclude"] = ["kernel/bus/node_modules"]
    assert legacy.resolve_files(spec, "bus", sandbox.root) == [
        "kernel/bus/schemas.py", "kernel/bus/topics.py"
    ]


def test_resolve_files_enforces_bundle_size_limit(sandbox):
    spec = json.loads(json.dumps(sandbox.spec))
    spec["policy"]["max_bundle_files"] = 1
    with pytest.raises(legacy.LegacyError, match="超过单包上限"):
        legacy.resolve_files(spec, "bus", sandbox.root)


# ---------------------------------------------------------------- 取用
def test_fetch_dry_run_writes_nothing(sandbox):
    result = legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root,
                                 project_root=sandbox.project, dry_run=True)
    assert result["total"] == 2 and result["dry_run"] is True
    assert not (sandbox.project / "vendor").exists()


def test_fetch_copies_files_and_records_sha256(sandbox):
    result = legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root,
                                 project_root=sandbox.project)
    assert sorted(result["copied"]) == ["kernel/bus/schemas.py", "kernel/bus/topics.py"]
    target = sandbox.project / "vendor/proj/kernel/bus/topics.py"
    assert target.read_text(encoding="utf-8") == "TOPICS = 1\n"

    record = json.loads((sandbox.project / "vendor/SOURCES.json").read_text(encoding="utf-8"))
    meta = record["files"]["kernel/bus/topics.py"]
    assert meta["bundle"] == "bus" and meta["zone"] == "CORE"
    assert meta["sha256"] == legacy.sha256_file(sandbox.root / "kernel/bus/topics.py")
    assert meta["size"] > 0 and meta["fetched_at"]


def test_fetch_twice_is_idempotent(sandbox):
    legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root, project_root=sandbox.project)
    again = legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root, project_root=sandbox.project)
    assert again["copied"] == []
    assert len(again["unchanged"]) == 2


def test_fetch_refuses_to_overwrite_local_edits_without_force(sandbox):
    legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root, project_root=sandbox.project)
    target = sandbox.project / "vendor/proj/kernel/bus/topics.py"
    target.write_text("手工改造\n", encoding="utf-8")

    with pytest.raises(legacy.LegacyError, match="--force"):
        legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root, project_root=sandbox.project)

    forced = legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root,
                                 project_root=sandbox.project, force=True)
    assert forced["overwritten"] == ["kernel/bus/topics.py"]
    assert target.read_text(encoding="utf-8") == "TOPICS = 1\n"


def test_fetch_port_bundle_passes_whitelist(sandbox):
    result = legacy.fetch_bundle(sandbox.spec, "sensors", sandbox.root,
                                 project_root=sandbox.project)
    assert result["total"] == 2
    assert (sandbox.project / "vendor/proj/plugins/mingchu/drivers/dht11.py").is_file()
    assert not (sandbox.project / "vendor/proj/plugins/mingchu/drivers/relay.py").exists()


# ---------------------------------------------------------------- 校验
def test_verify_detects_missing_and_local_changes(sandbox):
    legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root, project_root=sandbox.project)
    (sandbox.project / "vendor/proj/kernel/bus/topics.py").unlink()
    (sandbox.project / "vendor/proj/kernel/bus/schemas.py").write_text("改过", encoding="utf-8")

    result = legacy.verify_record(sandbox.spec, sandbox.root, sandbox.project)
    assert result["missing"] == ["kernel/bus/topics.py"]
    assert result["changed_local"] == ["kernel/bus/schemas.py"]
    assert result["passed"] is False


def test_verify_detects_upstream_change(sandbox):
    legacy.fetch_bundle(sandbox.spec, "bus", sandbox.root, project_root=sandbox.project)
    (sandbox.root / "kernel/bus/topics.py").write_text("素材库更新了\n", encoding="utf-8")
    result = legacy.verify_record(sandbox.spec, sandbox.root, sandbox.project)
    assert result["changed_upstream"] == ["kernel/bus/topics.py"]
    assert result["passed"] is True  # 本地仍是取用时的版本，不算失败


def test_check_passes_on_clean_sandbox(sandbox):
    result = legacy.check(sandbox.spec, sandbox.root, sandbox.project)
    assert result["passed"] is True, result["problems"]
    assert result["bundles"]["bus"] == 2
    assert result["verified_files"] == 0


def test_check_catches_guard_misconfiguration(sandbox):
    broken = json.loads(json.dumps(sandbox.spec))
    broken["forbidden"]["zones"] = []
    broken["forbidden"]["path_prefixes"].remove("datasets/")
    result = legacy.check(broken, sandbox.root, sandbox.project)
    assert result["passed"] is False
    assert any("必须包含 FROZEN" in item for item in result["problems"])
    assert any("漏判" in item for item in result["problems"])


def test_check_warns_when_legacy_root_unreachable(sandbox, monkeypatch):
    monkeypatch.setenv(legacy.ENV_ROOT, str(sandbox.root / "gone"))
    result = legacy.check(sandbox.spec, sandbox.root / "gone", sandbox.project)
    assert any("不可达" in item for item in result["warnings"])


# ---------------------------------------------------------------- CLI
def test_cli_list_json(sandbox, capsys):
    assert legacy.main(["--json", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reachable"] is True
    assert {row["bundle"] for row in payload["bundles"]} >= {"bus", "sensors"}


def test_cli_fetch_then_verify(sandbox, capsys):
    assert legacy.main(["fetch", "bus", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 2
    assert legacy.main(["verify"]) == 0
    assert "缺失 0" in capsys.readouterr().out


def test_cli_check_fails_on_unknown_bundle(sandbox, capsys):
    assert legacy.main(["fetch", "no_such_bundle"]) == 1
    assert "未知 bundle" in capsys.readouterr().err
