# -*- coding: utf-8 -*-
"""t18 关于窗口 + 空状态引导卡。

迁移自旧 gui_t18_driver.py（子进程 + JSON 结果）。全新临时 HOME 下：
  1. 断言空状态 4 步引导卡与「立即打开客户端」按钮；
  2. 打开关于窗口，断言版本号 / DPAPI 隐私 / 免责声明 / 数据目录；
  3. 「打开数据目录」打桩 os.startfile，只验证被调用；
  4. 点「知道了」关闭窗口。

关于窗口末尾 wait_window() 会同步阻塞 after 链，这里 patch 为非阻塞。
"""
from __future__ import annotations

import os
import tkinter as tk

import pytest

from _gui_helpers import (
    collect_texts,
    find_button,
    find_toplevel,
    run_gui_steps,
)

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _skip_popups(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")
    # 关于窗 wait_window 同步等待会卡住 after 注入链，改为非阻塞
    monkeypatch.setattr(tk.Toplevel, "wait_window",
                        lambda self, window=None: None)


def test_about_dialog_and_empty_state(data_home, monkeypatch):
    from trae_checkin.runtime import APP_VERSION

    startfile_calls: list[str] = []
    monkeypatch.setattr(os, "startfile",
                        lambda p: startfile_calls.append(str(p)))

    errors: list[str] = []

    def s1_empty_state(root):
        texts = collect_texts(root)
        assert any("4 步" in t for t in texts), "空状态缺少 4 步引导标题"
        for expect in ("打开客户端并登录", "保存当前登录账号",
                       "配置微信推送", "签到并开启自动任务"):
            assert any(expect in t for t in texts), f"空状态缺少步骤：{expect}"
        assert find_button(root, "立即打开客户端（第 ① 步）") is not None
        about = find_button(root, "关于")
        assert about is not None, "未找到顶部「关于」按钮"
        about.invoke()

    def s2_about(root):
        win = find_toplevel(root, "关于本工具")
        assert win is not None, "关于窗口未出现或标题不符"
        texts = collect_texts(win)
        assert any(f"版本 v{APP_VERSION}" in t for t in texts), "未显示版本号"
        assert any("DPAPI" in t for t in texts), "缺少 DPAPI 隐私说明"
        assert any("免责声明" in t for t in texts), "缺少免责声明"
        assert any(str(data_home) in t for t in texts), "缺少数据目录"
        assert find_button(win, "打开数据目录") is not None
        assert find_button(win, "知道了") is not None
        find_button(win, "打开数据目录").invoke()

    def s3_open_dir_then_close(root):
        assert len(startfile_calls) == 1, (
            f"「打开数据目录」未触发 startfile：{startfile_calls}")
        win = find_toplevel(root, "关于本工具")
        find_button(win, "知道了").invoke()

    def s4_closed(root):
        assert find_toplevel(root, "关于本工具") is None, "关于窗口未关闭"

    rc = run_gui_steps(
        [(800, s1_empty_state), (1200, s2_about),
         (1500, s3_open_dir_then_close), (1900, s4_closed)],
        errors, quit_ms=2300)
    assert rc == 0
    assert errors == []
