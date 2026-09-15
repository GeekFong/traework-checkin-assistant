# -*- coding: utf-8 -*-
"""深色模式设置测试：theme 字段、持久化往返、凭据保留、色板完整性。

迁移自旧单文件 test_t15.py。每个用例通过 data_home 夹具使用独立临时目录，
绝不触碰真实 settings.json。GUI 动态换色由 GUI 冒烟另行覆盖。
"""
import json

import pytest

from trae_checkin import gui, settings as settings_mod
from trae_checkin.runtime import package_root


def _raw_settings():
    return json.loads(settings_mod.SETTINGS_FILE.read_text(encoding="utf-8"))


def test_default_theme_light(data_home):
    assert settings_mod.load_settings()["theme"] == "light"


def test_themes_tuple():
    assert settings_mod.THEMES == ("light", "dark")


def test_illegal_theme_falls_back(data_home):
    s = settings_mod.load_settings()
    s["theme"] = "neon"
    settings_mod.save_settings_dict(s)
    assert settings_mod.load_settings()["theme"] == "light"


def test_dark_roundtrip(data_home):
    s = settings_mod.load_settings()
    s["theme"] = "dark"
    settings_mod.save_settings_dict(s)
    assert settings_mod.load_settings()["theme"] == "dark"
    assert _raw_settings()["theme"] == "dark"


def test_missing_theme_in_existing_v2_file_defaults_light(data_home):
    data = {"version": settings_mod.SETTINGS_VERSION,
            "push_enabled": True, "channels": ["wecom"],
            "only_failures": False, "creds_enc": {}, "secret_enc": {}}
    settings_mod.SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert settings_mod.load_settings()["theme"] == "light"
    # load 不应强制回写（保持惰性），字段缺失也能正常启动
    assert "theme" not in _raw_settings()


def test_save_theme_preference_basic(data_home):
    settings_mod.save_theme_preference("dark")
    assert _raw_settings()["theme"] == "dark"
    assert settings_mod.load_settings()["theme"] == "dark"
    settings_mod.save_theme_preference("light")
    assert settings_mod.load_settings()["theme"] == "light"


def test_save_theme_preference_illegal(data_home):
    settings_mod.save_theme_preference("pink")
    assert _raw_settings()["theme"] == "light"


def test_save_theme_preserves_credentials(data_home):
    s = settings_mod.load_settings()
    s["channels"] = ["serverchan"]
    s["creds"] = {
        "serverchan": {"key": "SCT-secret-key-9527", "secret": ""},
        "pushplus": {"key": "", "secret": ""},
        "wecom": {"key": "", "secret": ""},
        "dingtalk": {"key": "", "secret": ""}}
    settings_mod.save_settings_dict(s)
    enc_before = _raw_settings()["creds_enc"].get("serverchan")
    assert enc_before
    settings_mod.save_theme_preference("dark")
    raw_after = _raw_settings()
    assert raw_after["creds_enc"].get("serverchan") == enc_before
    assert raw_after["channels"] == ["serverchan"]
    assert raw_after["theme"] == "dark"


def test_save_theme_when_no_file(data_home):
    assert not settings_mod.SETTINGS_FILE.exists()
    settings_mod.save_theme_preference("dark")
    raw = _raw_settings()
    assert raw["theme"] == "dark"
    assert raw["version"] == settings_mod.SETTINGS_VERSION


def test_gui_palette_source_consistency():
    """run_gui 内部色板通过函数源码自检（不启动 Tk）。"""
    src = (package_root() / "gui" / "app.py").read_text(encoding="utf-8")
    assert '"light": {' in src
    assert '"dark": {' in src
    for token in ('"bg"', '"card"', '"primary"', '"entry_bg"',
                  '"heat_none"', '"tip_bg"', '"seg"'):
        assert src.count(token) >= 3, token
    assert "_initial_theme = load_settings()" in src
    assert "def apply_theme" in src
    assert "save_theme_preference(name)" in src
    assert "command=toggle_theme" in src
