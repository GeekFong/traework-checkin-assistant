# -*- coding: utf-8 -*-
"""GUI 推送凭据回显回归测试：已保存的 Key/Webhook 在界面上直接可见。

背景：旧版界面刻意不回显明文（"留空保存沿用原凭据"），用户在设置卡片
里看不到自己保存过的 Key，无法核对配置是否正确。现改为启动/保存后
输入框直接回显生效凭据（密文仍仅以 DPAPI 加密存于本机）。
"""
from __future__ import annotations

import tkinter as tk

import pytest

from _gui_helpers import collect_texts, run_gui_steps, walk
from trae_checkin.settings import save_settings_dict

pytestmark = pytest.mark.gui

SERVERCHAN_KEY = "SCT9999TESTKEY"
WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"
WECOM_SECRET = "SECfakefakesecret"


@pytest.fixture(autouse=True)
def _skip_popups(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")


def _entry_values(root: tk.Tk) -> list[str]:
    """收集主窗口控件树里全部 Entry 的当前文本。"""
    vals: list[str] = []
    for w in walk(root):
        try:
            if w.winfo_class() == "Entry":
                vals.append(w.get())
        except tk.TclError:
            continue
    return vals


def _seed_settings():
    save_settings_dict({
        "version": 2,
        "push_enabled": True,
        "channels": ["serverchan", "wecom"],
        "only_failures": False,
        "creds": {
            "serverchan": {"key": SERVERCHAN_KEY, "secret": ""},
            "wecom": {"key": WECOM_WEBHOOK, "secret": WECOM_SECRET},
            "dingtalk": {"key": "", "secret": ""},
            "pushplus": {"key": "", "secret": ""},
        },
    })


def test_saved_credentials_are_echoed_in_entries(data_home):
    """启动后：Server酱 Key、企微 Webhook 与加签密钥均回显到输入框。"""
    _seed_settings()
    errors: list[str] = []
    seen: dict[str, list] = {}

    def s1(root):
        seen["entries"] = _entry_values(root)
        seen["labels"] = collect_texts(root)

    steps = [(500, s1)]
    rc = run_gui_steps(steps, errors, quit_ms=1200)
    assert rc == 0
    assert errors == []
    entries = seen["entries"]
    assert SERVERCHAN_KEY in entries, f"Server酱 Key 未回显：{entries}"
    assert WECOM_WEBHOOK in entries, f"企微 Webhook 未回显：{entries}"
    assert WECOM_SECRET in entries, f"企微加签密钥未回显：{entries}"
    # 状态行提示「当前生效凭据」而非旧版「界面不回显」
    labels = seen["labels"]
    assert any("当前生效的凭据" in t for t in labels), (
        f"未显示新的凭据状态提示：{[t for t in labels if '凭据' in t]}")
    assert not any("不回显" in t for t in labels), "旧版「不回显」文案应已移除"
