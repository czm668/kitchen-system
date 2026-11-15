# -*- coding: utf-8 -*-
"""旧工程素材取用器：把「从旧文件夹调用」做成可校验、可重复、有护栏的操作。

设计取舍（重要）：
- **不在运行时把旧目录塞进 sys.path**。那会让新工程离开旧目录就跑不起来，
  答辩/交付打包时会连带拖走 473 MB 旧数据集，且旧目录被误改会悄悄串味。
- 取用 = 按 bundle 显式 copy + sha256 台账 + FROZEN 禁取护栏。
- 取用文件按素材库**原始相对路径**落到 `vendor/proj/`，保持
  `from kernel... import ...` 可用；要改造先复制进 `src/kitchen/`。

命令::

    python -m tools.legacy list [--zone CORE] [--json]
    python -m tools.legacy fetch platform-bus [--dry-run] [--force] [--json]
    python -m tools.legacy verify [--json]
    python -m tools.legacy check [--json]        # CI 自检，退出码 0/1

素材库路径解析优先级：环境变量 `KITCHEN_LEGACY_ROOT` > `legacy_sources.json`
的 `legacy_root`（相对工程根）> `legacy_root_absolute`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "legacy_sources.json"
ENV_ROOT = "KITCHEN_LEGACY_ROOT"


class LegacyError(RuntimeError):
    """取用台账/护栏/文件操作失败。"""


# ---------------------------------------------------------------- 台账
def load_spec(path: str | Path | None = None) -> dict[str, Any]:
    """加载并校验复用台账结构；结构不合法直接抛错，避免静默取错文件。"""
    spec_path = Path(path) if path is not None else SPEC_PATH
    if not spec_path.is_file():
        raise LegacyError(f"复用台账不存在：{spec_path}")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyError(f"复用台账无法读取：{exc}") from exc
    for key in ("policy", "forbidden", "bundles", "legacy_root"):
        if key not in spec:
            raise LegacyError(f"复用台账缺少 {key!r} 字段")
    for name, bundle in spec["bundles"].items():
        if not bundle.get("paths"):
            raise LegacyError(f"bundle {name!r} 没有 paths")
        zone = bundle.get("zone")
        if zone == "FROZEN":
            raise LegacyError(f"bundle {name!r} 属于禁止取用分区 FROZEN")
        if zone not in ("CORE", "ADAPT", "PORT", "NEW", "PARKED"):
            raise LegacyError(f"bundle {name!r} 的 zone 非法：{zone!r}")
    return spec


def legacy_root(spec: dict[str, Any]) -> Path:
    """解析素材库根目录；允许环境变量覆盖，便于换机器或测试注入。"""
    override = os.environ.get(ENV_ROOT, "").strip()
    if override:
        return Path(override).expanduser()
    candidate = Path(spec["legacy_root"])
    if not candidate.is_absolute():
        candidate = (PROJECT_ROOT / candidate).resolve()
    if not candidate.is_dir():
        fallback = spec.get("legacy_root_absolute")
        if fallback and Path(fallback).is_dir():
            return Path(fallback)
    return candidate


def vendor_dir(spec: dict[str, Any], project_root: Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    return root / spec["policy"]["vendor_dir"] / spec["policy"]["path_prefix"]


def record_path(spec: dict[str, Any], project_root: Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    return root / spec["policy"]["record"]


# ---------------------------------------------------------------- 护栏
def normalize_rel(raw: str) -> str:
    """规范化素材库相对路径；拒绝绝对路径与路径穿越。"""
    text = str(raw).replace("\\", "/")
    if not text or text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise LegacyError(f"非法素材相对路径（绝对路径或空值）：{raw!r}")
    if text.startswith("..") or "/.." in text:
        raise LegacyError(f"非法素材相对路径（疑似路径穿越）：{raw!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts) or not parts:
        raise LegacyError(f"非法素材相对路径（疑似路径穿越）：{raw!r}")
    return "/".join(parts)


def is_forbidden(spec: dict[str, Any], rel: str) -> bool:
    """FROZEN 禁取判定；`allow_exact` 白名单优先（DHT11/MQ-2 采样逻辑）。"""
    rule = spec["forbidden"]
    if rel in set(rule.get("allow_exact") or ()):
        return False
    return any(rel == pattern.rstrip("/") or rel.startswith(pattern.rstrip("/") + "/")
               or rel == pattern
               for pattern in rule.get("path_prefixes") or ())


def _expand(root: Path, pattern: str) -> list[str]:
    """展开一个 glob；清单里允许写 `kernel/bus/**`，内部补成 `**/*`。"""
    text = str(pattern).replace("\\", "/").lstrip("/")
    if ".." in text.split("/"):
        raise LegacyError(f"清单 pattern 含路径穿越：{pattern!r}")
    if text.endswith("/**"):
        text += "/*"
    if not root.is_dir():
        return []
    return sorted(
        hit.relative_to(root).as_posix()
        for hit in root.glob(text)
        if hit.is_file()
    )


#: 缓存/字节码：不取用、不记账（理由见 `is_cache_artifact`）
CACHE_DIRS: tuple[str, ...] = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
BYTECODE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")


def is_cache_artifact(rel: str) -> bool:
    """字节码/缓存目录产物一律不取、不记账。

    这不是洁癖。旧工程的 `kernel/**/__pycache__/*.pyc` 在台账里占了 10 条，
    而它们只是本机编译产物：换一台机器跑一次 `pytest` 就会变，清理一次缓存
    就会「缺文件」。台账记这种文件的结果是 `legacy.py check` 周期性误报，
    最后所有人都学会无视这个自检——护栏一旦被无视就等于不存在。
    """
    parts = rel.replace("\\", "/").split("/")
    if any(part in CACHE_DIRS for part in parts[:-1]):
        return True
    return rel.lower().endswith(BYTECODE_SUFFIXES)


def resolve_files(spec: dict[str, Any], bundle_name: str, root: Path) -> list[str]:
    """返回 bundle 命中且通过禁取护栏的素材相对路径列表（去重、排序）。"""
    if bundle_name not in spec["bundles"]:
        raise LegacyError(f"未知 bundle {bundle_name!r}；可用：{sorted(spec['bundles'])}")
    bundle = spec["bundles"][bundle_name]
    if bundle["zone"] in (spec["forbidden"].get("zones") or ()):
        raise LegacyError(f"bundle {bundle_name!r} 属于禁止取用分区 {bundle['zone']}")

    found: set[str] = set()
    for pattern in bundle["paths"]:
        for rel in _expand(root, pattern):
            found.add(normalize_rel(rel))
    # 字节码/缓存产物从源头就排除：它们既不该被复制进 vendor/，也不该进台账
    found = {rel for rel in found if not is_cache_artifact(rel)}

    excluded = tuple(str(item).replace("\\", "/").rstrip("/")
                     for item in bundle.get("exclude") or ())
    if excluded:
        found = {
            rel for rel in found
            if not any(rel == item or rel.startswith(item + "/") for item in excluded)
        }

    blocked = sorted(rel for rel in found if is_forbidden(spec, rel))
    if blocked:
        raise LegacyError(
            f"bundle {bundle_name!r} 命中 FROZEN 禁取素材，已整体拒绝：{blocked[:5]}"
            f"{' ...' if len(blocked) > 5 else ''}"
        )

    max_bytes = float(spec["policy"].get("max_file_mb", 20)) * 1024 * 1024
    for rel in sorted(found):
        size = (root / rel).stat().st_size
        if size > max_bytes:
            raise LegacyError(
                f"{rel} 体积 {size / 1024 / 1024:.1f} MB 超过单文件上限 "
                f"{spec['policy'].get('max_file_mb')} MB，拒绝取用"
            )
    max_files = int(spec["policy"].get("max_bundle_files", 500))
    if len(found) > max_files:
        raise LegacyError(
            f"bundle {bundle_name!r} 命中 {len(found)} 个文件，超过单包上限 {max_files}；"
            f"请在台账里补 exclude 规则（如 node_modules/dist）或拆小 bundle"
        )
    return sorted(found)


# ---------------------------------------------------------------- 取用
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_record(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": 1, "generated_at": None, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyError(f"取用台账无法读取：{path}（{exc}）") from exc
    data.setdefault("files", {})
    return data


def fetch_bundle(
    spec: dict[str, Any],
    bundle_name: str,
    root: Path,
    project_root: Path | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """取用一个 bundle；返回结构化结果，`dry_run=True` 时不写任何文件。"""
    project = Path(project_root) if project_root is not None else PROJECT_ROOT
    rels = resolve_files(spec, bundle_name, root)
    target_root = vendor_dir(spec, project)
    record_file = record_path(spec, project)
    conflicts: list[str] = []
    copied: list[str] = []
    unchanged: list[str] = []

    for rel in rels:
        source = root / rel
        target = target_root / rel
        if target.is_file() and sha256_file(target) != sha256_file(source):
            conflicts.append(rel)
        elif target.is_file():
            unchanged.append(rel)
        else:
            copied.append(rel)
    if conflicts and not force:
        raise LegacyError(
            f"本地已有 {len(conflicts)} 个文件与素材库不一致（可能被手工改造）："
            f"{conflicts[:5]}；确认要覆盖请加 --force"
        )

    entries: dict[str, dict[str, Any]] = {}
    stamp = _now()
    for rel in rels:
        source, target = root / rel, target_root / rel
        digest = sha256_file(source)
        entries[rel] = {
            "bundle": bundle_name,
            "zone": spec["bundles"][bundle_name]["zone"],
            "sha256": digest,
            "size": source.stat().st_size,
            "source": rel,
            "fetched_at": stamp,
        }
        if not dry_run and rel in (copied + (conflicts if force else [])):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    if not dry_run:
        record = _read_record(record_file)
        record["schema"] = 1
        record["generated_at"] = stamp
        record["legacy_root"] = str(root)
        record["files"].update(entries)
        record_file.parent.mkdir(parents=True, exist_ok=True)
        record_file.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return {
        "bundle": bundle_name,
        "zone": spec["bundles"][bundle_name]["zone"],
        "dry_run": dry_run,
        "total": len(rels),
        "copied": copied,
        "unchanged": unchanged,
        "overwritten": conflicts if force else [],
        "target_root": str(target_root),
        "record": str(record_file),
    }


def verify_record(
    spec: dict[str, Any], root: Path, project_root: Path | None = None
) -> dict[str, Any]:
    """核对已取用文件的本地哈希与素材库源哈希。"""
    project = Path(project_root) if project_root is not None else PROJECT_ROOT
    record = _read_record(record_path(spec, project))
    target_root = vendor_dir(spec, project)
    ok: list[str] = []
    missing: list[str] = []
    changed_local: list[str] = []
    changed_upstream: list[str] = []
    for rel, meta in sorted(record["files"].items()):
        target = target_root / rel
        if not target.is_file():
            missing.append(rel)
            continue
        if sha256_file(target) != meta.get("sha256"):
            changed_local.append(rel)
            continue
        ok.append(rel)
        source = root / rel
        if source.is_file() and sha256_file(source) != meta.get("sha256"):
            changed_upstream.append(rel)
    return {
        "ok": ok,
        "missing": missing,
        "changed_local": changed_local,
        "changed_upstream": changed_upstream,
        "total": len(record["files"]),
        "passed": not missing and not changed_local,
    }


def check(
    spec: dict[str, Any], root: Path, project_root: Path | None = None
) -> dict[str, Any]:
    """CI 自检：台账结构、bundle 可用性、禁取护栏、已取用文件哈希。"""
    warnings: list[str] = []
    problems: list[str] = []
    available: dict[str, int] = {}
    if not root.is_dir():
        warnings.append(f"素材库不可达：{root}（取用是按需的，已取用文件仍可独立验证）")
    for name in sorted(spec["bundles"]):
        try:
            available[name] = len(resolve_files(spec, name, root))
        except LegacyError as exc:
            problems.append(f"bundle {name}: {exc}")

    # 禁取护栏必须真的拦得住：逐条验证台账里的自检样本
    if "FROZEN" not in (spec["forbidden"].get("zones") or ()):
        problems.append("禁取分区清单必须包含 FROZEN")
    must_block = spec["forbidden"].get("must_block") or ()
    if not must_block:
        warnings.append("台账未配置 must_block 自检样本，禁取护栏无法自证有效")
    for rel in must_block:
        if not is_forbidden(spec, rel):
            problems.append(f"禁取护栏漏判：{rel}")
    for rel in spec["forbidden"].get("allow_exact") or ():
        if is_forbidden(spec, rel):
            problems.append(f"白名单被禁取规则覆盖：{rel}")

    project = Path(project_root) if project_root is not None else PROJECT_ROOT
    verification = verify_record(spec, root, project)
    if not verification["passed"]:
        problems.append(
            f"已取用文件校验失败：missing={verification['missing']} "
            f"changed_local={verification['changed_local']}"
        )
    return {
        "passed": not problems,
        "problems": problems,
        "warnings": warnings,
        "bundles": available,
        "verified_files": verification["total"],
    }


# ---------------------------------------------------------------- CLI
def _dump(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="旧工程素材取用器（只读素材库 + 显式取用）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出可复用素材包")
    p_list.add_argument("--zone", help="只看某分区：CORE / ADAPT / PORT")

    p_fetch = sub.add_parser("fetch", help="取用一个素材包到 vendor/")
    p_fetch.add_argument("bundle")
    p_fetch.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    p_fetch.add_argument("--force", action="store_true", help="覆盖与素材库不一致的本地文件")

    p_verify = sub.add_parser("verify", help="校验已取用文件哈希")
    p_check = sub.add_parser("check", help="CI 自检，退出码 0/1")

    # 让 `--json` 既能写在子命令前，也能写在子命令后；SUPPRESS 避免覆盖全局值
    for child in (p_list, p_fetch, p_verify, p_check):
        child.add_argument("--json", action="store_true",
                           default=argparse.SUPPRESS, help="机器可读输出")

    args = parser.parse_args(argv)
    try:
        spec = load_spec()
        root = legacy_root(spec)

        if args.command == "list":
            rows = []
            for name, bundle in sorted(spec["bundles"].items()):
                if args.zone and bundle["zone"] != args.zone:
                    continue
                try:
                    count = len(resolve_files(spec, name, root))
                except LegacyError as exc:
                    count = -1
                    rows.append({"bundle": name, "zone": bundle["zone"],
                                 "error": str(exc), "files": 0})
                    continue
                rows.append({"bundle": name, "zone": bundle["zone"],
                             "files": count, "reason": bundle.get("reason", "")})
            _dump({"legacy_root": str(root), "reachable": root.is_dir(),
                   "bundles": rows}, args.json)
            if not args.json:
                print(f"素材库：{root}  {'(可达)' if root.is_dir() else '(不可达)'}")
                print(f"{'bundle':<22} {'分区':<7} {'文件':>5}  说明")
                for row in rows:
                    print(f"{row['bundle']:<22} {row['zone']:<7} {row['files']:>5}  "
                          f"{row.get('error') or row.get('reason', '')[:52]}")
            return 0

        if args.command == "fetch":
            result = fetch_bundle(spec, args.bundle, root,
                                  force=args.force, dry_run=args.dry_run)
            _dump(result, args.json)
            if not args.json:
                tag = "[DRY-RUN] " if args.dry_run else ""
                print(f"{tag}取用 {result['bundle']}（{result['zone']}）："
                      f"命中 {result['total']} 个文件，"
                      f"新增 {len(result['copied'])}，"
                      f"已是最新 {len(result['unchanged'])}，"
                      f"覆盖 {len(result['overwritten'])}")
                print(f"落点：{result['target_root']}")
                print(f"台账：{result['record']}")
            return 0

        if args.command == "verify":
            result = verify_record(spec, root)
            _dump(result, args.json)
            if not args.json:
                print(f"已取用 {result['total']} 个文件："
                      f"OK {len(result['ok'])}，缺失 {len(result['missing'])}，"
                      f"本地被改 {len(result['changed_local'])}，"
                      f"素材库已变 {len(result['changed_upstream'])}")
                for rel in result["missing"] + result["changed_local"]:
                    print(f"  ! {rel}")
            return 0 if result["passed"] else 1

        result = check(spec, root)
        _dump(result, args.json)
        if not args.json:
            print(f"自检 {'PASS' if result['passed'] else 'FAIL'}："
                  f"{len(result['bundles'])} 个 bundle，"
                  f"已取用 {result['verified_files']} 个文件")
            for item in result["warnings"]:
                print(f"  ~ {item}")
            for item in result["problems"]:
                print(f"  ! {item}")
        return 0 if result["passed"] else 1

    except LegacyError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
