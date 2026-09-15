# -*- coding: utf-8 -*-
"""t19 首次启动风险声明门禁：同意 / 拒绝 / 已确认 三场景。

迁移自旧 gui_t19_gate_driver.py（子进程 + JSON 结果），现改为进程内 pytest。

难点：声明窗在 mainloop 之前用 wait_window 同步阻塞，run_gui_steps 的
after 链此时尚未进入事件循环，因此必须 patch tk.Toplevel.wait_window：
识别到声明窗时，经 nametowidget(".") 拿到 root 并 root.after 注入按钮
点击，再调用原始 wait_window。

场景：
  agree    全新 HOME → 弹窗 → 点「我已了解并同意」→ 进主界面，标记落盘
  disagree 全新 HOME → 弹窗 → 点「不同意并退出」→ run_gui 返回 0，不进主循环
  seen     settings.json 已含 disclaimer_accepted=true → 不再弹窗
"""
from __future__ import annotations

import json

import pytest

from _gui_helpers import find_button, find_toplevel, run_gui_steps

pytestmark = pytest.mark.gui

DIALOG_TITLE = "首次使用 · 风险与隐私声明"


@pytest.fixture(autouse=True)
def _no_crash_notice(monkeypatch):
    # 本用例专门测试声明门禁，不能设 TRAESIGN_NO_DISCLAIMER
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")


@pytest.mark.parametrize("scene", ["agree", "disagree", "seen"])
def test_disclaimer_gate(scene, data_home, monkeypatch):
    import tkinter as tk

    from trae_checkin import settings

    if scene == "seen":
        (data_home / "settings.json").write_text(json.dumps({
            "version": 2, "push_enabled": False, "channels": ["serverchan"],
            "only_failures": False, "theme": "light",
            "disclaimer_accepted": True,
            "creds_enc": {}, "secret_enc": {},
        }), encoding="utf-8")

    errors: list[str] = []
    wait_seen = {"n": 0}
    original_wait_window = tk.Toplevel.wait_window

    def patched_wait_window(self, window=None):
        target = window or self
        try:
            title = str(target.title())
        except tk.TclError:
            title = ""
        if title == DIALOG_TITLE:
            wait_seen["n"] += 1
            root = self.nametowidget(".")

            def _act():
                try:
                    if scene == "disagree":
                        btn = find_button(target, "不同意并退出")
                        assert btn is not None, "声明窗缺少「不同意并退出」按钮"
                    else:
                        btn = find_button(target, "我已了解并同意")
                        assert btn is not None, "声明窗缺少「我已了解并同意」按钮"
                    btn.invoke()
                except Exception as e:  # noqa: BLE001 - 收集后统一断言
                    errors.append(repr(e))

            root.after(300, _act)
        return original_wait_window(self, window)

    monkeypatch.setattr(tk.Toplevel, "wait_window", patched_wait_window)

    # 声明窗在整棵 UI 构建完成后才弹出，弹出时刻距 root 创建不确定，
    # 因此不能用固定延时断言（旧 driver 从 mainloop 起计时，这里从
    # root.__init__ 起计时）。改为 100ms 轮询，满足条件后立即结束主循环。
    def verify(root):
        state = {"tries": 0}

        def _accepted() -> bool:
            return bool(settings.load_settings().get("disclaimer_accepted"))

        def _poll():
            state["tries"] += 1
            try:
                if scene == "seen":
                    assert wait_seen["n"] == 0, "已确认声明后不应再次弹窗"
                    assert find_toplevel(root, DIALOG_TITLE) is None
                    assert _accepted() is True, (
                        "seen 场景 disclaimer_accepted 应为 true")
                    root.destroy()
                    return
                # agree：等待弹窗出现并被点击关闭后再校验落盘
                if wait_seen["n"] == 1 and find_toplevel(
                        root, DIALOG_TITLE) is None:
                    assert _accepted() is True, (
                        "同意后 disclaimer_accepted 未落盘为 true")
                    root.destroy()
                    return
            except Exception as e:  # noqa: BLE001 - 收集后统一断言
                errors.append(repr(e))
                root.destroy()
                return
            if state["tries"] >= 40:
                errors.append("超时：声明窗未按预期关闭/落盘")
                root.destroy()
            else:
                root.after(100, _poll)

        root.after(150, _poll)

    if scene == "disagree":
        # 拒绝后 run_gui 在 mainloop 之前销毁 root 并返回 0，
        # after 校验步骤没有事件循环可跑，直接同步断言
        rc = run_gui_steps([], errors)
        assert rc == 0, f"拒绝后 run_gui 应返回 0，实际 {rc}"
        assert wait_seen["n"] == 1, (
            f"声明窗应出现 1 次，实际 {wait_seen['n']}")
        accepted = bool(settings.load_settings().get("disclaimer_accepted"))
        assert accepted is False, "拒绝后不应写入 disclaimer_accepted=true"
    else:
        # 轮询成功会自行 destroy，quit_ms 仅作 5s 安全网
        rc = run_gui_steps([(100, verify)], errors, quit_ms=5000)
        assert rc == 0
    assert errors == []
