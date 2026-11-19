# -*- coding: utf-8 -*-
"""工程规则资产守卫（审查报告 §4.2 / §5.3 / §5.4）。

这些文件本身不参与运行时，但它们是"零评审单人项目"里替代 reviewer 的
机器化决策记录。一旦被误删，CI 里跑不到、测试也不会红，所以在这里钉死。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_r5_lint_passes_on_current_tree():
    from tools.rule_lint import lint
    issues = lint()
    assert issues == [], f"R5 时钟守卫失败：{issues}"


def test_r5_lint_detects_old_method_name(tmp_path):
    # 造一个假的 api/ 目录，确认 lint 真能抓到 state.now() 回归而不是永远绿
    from tools.rule_lint import BAD_METHOD_REF
    assert BAD_METHOD_REF.search("moment = state.now()")
    assert BAD_METHOD_REF.search("clock=self.now,")
    assert not BAD_METHOD_REF.search("moment = state.monotonic()")
    assert not BAD_METHOD_REF.search("datetime.now()")


def test_r5_lint_detects_epoch_into_now():
    from tools.rule_lint import EPOCH_INTO_NOW
    assert EPOCH_INTO_NOW.search("close(t, now=state.wall_epoch())")
    assert not EPOCH_INTO_NOW.search("close(t, wall_epoch=state.wall_epoch())")


def test_pr_template_documents_all_six_rules():
    tpl = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    for rule in ("R1", "R2", "R3", "R4", "R5", "R6"):
        assert f"**{rule}**" in tpl, f"PR 模板缺少 {rule} 自检项"
    assert "只出建议" in tpl
    assert "docs/requirements.json" in tpl


def test_prepush_hook_runs_ci_and_guards_deploy():
    hook = (ROOT / "tools" / "git-hooks" / "pre-push").read_text(encoding="utf-8")
    assert "tools/ci.py" in hook
    assert "docker compose" in hook
    assert "KITCHEN_SKIP_PREPUSH" in hook
    installer = (ROOT / "tools" / "install-hooks.sh").read_text(encoding="utf-8")
    assert "core.hooksPath" in installer


def test_single_production_entry_and_demos_archived():
    # 审查报告 §3 "入口泛滥"：唯一生产入口 + demo 全部收进 tools/demos/
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.scripts]' in pyproject
    assert 'kitchen = "kitchen.api.server:main"' in pyproject
    demos = sorted(p.name for p in (ROOT / "tools" / "demos").glob("*_demo.py"))
    assert len(demos) == 12, f"应有 12 个 demo，实际 {len(demos)}"
    assert not list((ROOT / "tools").glob("*_demo.py")), "tools/ 顶层不应再有 demo"

    # demo 移动后路径层级要跟着改；parents[1] 会指到 tools/ 而不是代码根
    for demo in (ROOT / "tools" / "demos").glob("*.py"):
        text = demo.read_text(encoding="utf-8")
        if "sys.path.insert" in text or "_CODE_ROOT" in text:
            assert "parents[2]" in text, f"{demo.name} 的路径层级没跟着移动更新"


def test_kitchen_bat_serve_is_the_real_server():
    bat = (ROOT / "tools" / "kitchen.bat").read_text(encoding="utf-8")
    serve_block = bat.split(":serve\n", 1)[1].split(":serve-miniapp", 1)[0]
    assert "kitchen.api.server" in serve_block
    assert "miniapp_check.py" not in serve_block
    miniapp_block = bat.split(":serve-miniapp\n", 1)[1].split(":backup", 1)[0]
    assert "miniapp_check.py" in miniapp_block


def test_legacy_deploy_files_archived_not_deleted():
    # 审查报告 §2.1 P2：归档不删除，引用同步改到 legacy/
    legacy = ROOT / "deploy" / "legacy"
    assert (legacy / "nginx.conf").is_file()
    assert (legacy / "kitchen-api.service").is_file()
    assert not (ROOT / "deploy" / "nginx.conf").exists()
    assert not (ROOT / "deploy" / "kitchen-api.service").exists()
    setup = (ROOT / "deploy" / "aliyun-setup.sh").read_text(encoding="utf-8")
    assert "deploy/legacy/nginx.conf" in setup
    assert "deploy/legacy/kitchen-api.service" in setup
    readme = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "legacy/" in readme and "云端未使用" in readme
