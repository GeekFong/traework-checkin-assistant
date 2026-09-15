# -*- coding: utf-8 -*-
"""t15 主题切换：浅/深冷启动配色、切换反转、Entry 着色与 theme 落盘。

迁移自旧 gui_t15_driver.py（原为子进程 + JSON 结果），现改为进程内
run_gui_steps 注入：step1 校验冷启动配色 → invoke 主题按钮 →
step2 校验反转、持久化、card 色 Frame 与 Entry 新配色。
"""
from __future__ import annotations

import json

import pytest

from _gui_helpers import find_button, run_gui_steps, walk

pytestmark = pytest.mark.gui

BG = {"light": "#f4f6fb", "dark": "#15181f"}
CARD = {"light": "#ffffff", "dark": "#1e232c"}
ENTRY_BG = {"light": "#ffffff", "dark": "#232a35"}
LIGHT_RESIDUAL = ("#ffffff", "#f4f6fb", "#f7f9fd", "#eef1f7")


@pytest.fixture(autouse=True)
def _skip_popups(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")


@pytest.mark.parametrize("initial", ["light", "dark"])
def test_theme_cold_start_and_toggle(initial, data_home, monkeypatch):
    if initial == "dark":
        (data_home / "settings.json").write_text(json.dumps({
            "version": 2, "push_enabled": False, "channels": ["serverchan"],
            "only_failures": False, "theme": "dark",
            "creds_enc": {}, "secret_enc": {},
        }), encoding="utf-8")

    from trae_checkin import settings

    errors: list[str] = []
    expected = "dark" if initial == "light" else "light"

    def step1(root):
        assert str(root.cget("bg")).lower() == BG[initial]
        btn = find_button(root, "🌙 深色" if initial == "light" else "☀ 浅色")
        assert btn is not None, "未找到主题切换按钮"

        entry_total = entry_ok = 0
        stray_light_btn = 0
        for w in walk(root):
            try:
                cls = w.winfo_class()
                if cls == "Entry":
                    entry_total += 1
                    if str(w.cget("bg")).lower() == ENTRY_BG[initial]:
                        entry_ok += 1
                elif cls == "Button" and initial == "dark":
                    if str(w.cget("bg")).lower() in LIGHT_RESIDUAL:
                        stray_light_btn += 1
            except Exception:
                pass
        assert entry_total > 0, "冷启动树中未找到 Entry 控件"
        assert entry_total == entry_ok, (
            f"冷启动 Entry 配色不符：{entry_ok}/{entry_total} "
            f"应为 {ENTRY_BG[initial]}")
        assert stray_light_btn == 0, (
            f"深色冷启动仍有 {stray_light_btn} 个浅色残留按钮")
        btn.invoke()

    def step2(root):
        assert str(root.cget("bg")).lower() == BG[expected]
        raw = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        assert raw.get("theme") == expected

        found_card = any(
            w.winfo_class() == "Frame"
            and str(w.cget("bg")).lower() == CARD[expected]
            for w in walk(root))
        assert found_card, f"未找到 card 色 {CARD[expected]} 的 Frame"

        entry_ok = any(
            w.winfo_class() == "Entry"
            and str(w.cget("bg")).lower() == ENTRY_BG[expected]
            for w in walk(root))
        assert entry_ok, "Entry 配色未随主题变化"

    rc = run_gui_steps([(600, step1), (1100, step2)], errors, quit_ms=1600)
    assert rc == 0
    assert errors == []
