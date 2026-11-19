# -*- coding: utf-8 -*-
"""交付前凭据隔离守卫（审查报告 §0 P0-4）。"""
from __future__ import annotations

from tools.delivery_check import scan


def test_clean_directory_passes(tmp_path):
    (tmp_path / "README.md").write_text("# 答辩包\n正常文档", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    errors, warnings = scan(tmp_path)
    assert errors == []


def test_detects_env_and_pem(tmp_path):
    (tmp_path / ".env").write_text("KITCHEN_DB_PASSWORD=fake\n", encoding="utf-8")
    (tmp_path / "test.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    errors, _ = scan(tmp_path)
    joined = " ".join(errors)
    assert ".env" in joined
    assert "test.pem" in joined


def test_detects_api_key_without_printing_value(tmp_path):
    # 用假 key（非真实凭据）验证内容扫描 + 不回显
    fake = "sk-" + "A" * 24
    (tmp_path / "config.py").write_text(f'KEY = "{fake}"\n', encoding="utf-8")
    errors, _ = scan(tmp_path)
    assert len(errors) == 1
    assert "config.py" in errors[0]
    assert fake not in errors[0], "不能把疑似 Key 的值打印出来"


def test_env_example_is_only_a_warning_in_strict_mode(tmp_path):
    (tmp_path / ".env.example").write_text("KITCHEN_DB_PASSWORD=\n", encoding="utf-8")
    errors, warnings = scan(tmp_path)
    assert errors == [] and warnings == []
    errors, warnings = scan(tmp_path, strict=True)
    assert errors == []
    assert any(".env.example" in w for w in warnings)


def test_secrets_h_example_is_template_not_error(tmp_path):
    (tmp_path / "secrets.h.example").write_text("const char* wifi = \"\";\n",
                                                 encoding="utf-8")
    errors, warnings = scan(tmp_path)
    assert errors == [] and warnings == []
    errors, warnings = scan(tmp_path, strict=True)
    assert errors == []
    assert any("secrets.h.example" in w for w in warnings)


def test_skips_noise_directories(tmp_path):
    noise = tmp_path / "node_modules" / "pkg"
    noise.mkdir(parents=True)
    (noise / ".env").write_text("x=1\n", encoding="utf-8")
    errors, _ = scan(tmp_path)
    assert errors == [], "node_modules/.env 不该误报"
