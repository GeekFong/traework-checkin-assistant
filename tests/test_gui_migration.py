# -*- coding: utf-8 -*-
"""t16 换机迁移向导：4 步流程、跳过备份拦截、向导内真实备份、回退与完成关窗。

迁移自旧 gui_t16_driver.py（子进程 + JSON 结果），现改为进程内 pytest，
浅/深两种冷启动主题各跑一遍。全程打桩 filedialog/messagebox，不弹真实模态框。
"""
from __future__ import annotations

import json

import pytest
import tkinter.filedialog as fdlg
import tkinter.messagebox as mbox

from _gui_helpers import (
    collect_texts,
    find_button,
    find_toplevel,
    run_gui_steps,
)

pytestmark = pytest.mark.gui

WIZARD_TITLE = "换机迁移向导"


@pytest.fixture(autouse=True)
def _skip_popups(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")


@pytest.mark.parametrize("initial", ["light", "dark"])
def test_migration_wizard_flow(initial, data_home, monkeypatch):
    if initial == "dark":
        (data_home / "settings.json").write_text(json.dumps({
            "version": 2, "push_enabled": False, "channels": ["serverchan"],
            "only_failures": False, "theme": "dark",
            "creds_enc": {}, "secret_enc": {},
        }), encoding="utf-8")

    zip_path = data_home / "wizard_backup.zip"
    askyesno_calls: list[tuple] = []
    errors: list[str] = []

    monkeypatch.setattr(fdlg, "asksaveasfilename",
                        lambda **kw: str(zip_path))
    monkeypatch.setattr(fdlg, "askopenfilename", lambda **kw: "")
    monkeypatch.setattr(
        mbox, "askyesno",
        lambda title, message="", **kw: (
            askyesno_calls.append((title, message)), False)[1])
    monkeypatch.setattr(mbox, "showerror",
                        lambda title, msg="", **kw: errors.append(msg))

    def _texts(root):
        win = find_toplevel(root, WIZARD_TITLE)
        assert win is not None, "向导 Toplevel 未出现或标题不符"
        return win, collect_texts(win)

    def s1(root):
        btn = find_button(root, "换机迁移")
        assert btn is not None, "未找到「换机迁移」入口按钮"
        btn.invoke()

    def s2(root):
        win, texts = _texts(root)
        assert "步骤 1 / 4" in texts, f"未显示步骤指示：{texts[:6]}"
        assert any("确认账号可手动登录" in t for t in texts), "缺少第 1 步标题"
        assert any("DPAPI" in t for t in texts), "缺少 DPAPI 风险警告"
        prev = find_button(win, "上一步")
        nxt = find_button(win, "下一步")
        assert prev is not None and str(prev.cget("state")) == "disabled", (
            "第 1 步「上一步」应为 disabled")
        nxt.invoke()

    def s3(root):
        win, texts = _texts(root)
        assert "步骤 2 / 4" in texts, f"未进入第 2 步：{texts[:6]}"
        assert find_button(win, "① 生成备份包（另存为…）") is not None, (
            "第 2 步缺少生成备份包按钮")
        askyesno_calls.clear()
        find_button(win, "下一步").invoke()

    def s4(root):
        win, texts = _texts(root)
        assert len(askyesno_calls) == 1 and askyesno_calls[0][0] == "确认跳过备份", (
            f"未弹跳过备份确认：{askyesno_calls}")
        assert "步骤 2 / 4" in texts, "拒绝跳过后不应离开第 2 步"
        find_button(win, "① 生成备份包（另存为…）").invoke()

    def s5(root):
        win, texts = _texts(root)
        assert zip_path.exists() and zip_path.stat().st_size > 0, (
            f"向导内备份包未生成：{zip_path}")
        assert any("已生成备份" in t for t in texts), "缺少备份成功提示"
        askyesno_calls.clear()
        find_button(win, "下一步").invoke()

    def s6(root):
        win, texts = _texts(root)
        assert len(askyesno_calls) == 0, "已备份后不应再弹跳过确认"
        assert "步骤 3 / 4" in texts, f"未进入第 3 步：{texts[:6]}"
        assert find_button(win, "② 选择备份包并恢复") is not None, (
            "第 3 步缺少恢复按钮")
        find_button(win, "下一步").invoke()

    def s7(root):
        win, texts = _texts(root)
        assert "步骤 4 / 4" in texts, f"未进入第 4 步：{texts[:6]}"
        assert find_button(win, "完成") is not None, "末步按钮未变为「完成」"
        assert find_button(win, "下一步") is None, "末步不应再有「下一步」"
        find_button(win, "上一步").invoke()

    def s8(root):
        win, texts = _texts(root)
        assert "步骤 3 / 4" in texts, "「上一步」未回到第 3 步"
        assert find_button(win, "下一步") is not None, (
            "回到第 3 步后应显示「下一步」")
        find_button(win, "下一步").invoke()

    def s9(root):
        win, texts = _texts(root)
        assert "步骤 4 / 4" in texts, "再次前进后应在第 4 步"
        find_button(win, "完成").invoke()

    def s10(root):
        assert find_toplevel(root, WIZARD_TITLE) is None, (
            "点「完成」后向导窗口未关闭")

    steps = [
        (400, s1), (700, s2), (1000, s3), (1300, s4), (1600, s5),
        (2000, s6), (2350, s7), (2700, s8), (3050, s9), (3400, s10),
    ]
    rc = run_gui_steps(steps, errors, quit_ms=4000)
    assert rc == 0
    assert errors == []
