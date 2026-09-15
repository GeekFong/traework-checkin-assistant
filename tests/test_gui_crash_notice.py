# -*- coding: utf-8 -*-
"""t24 崩溃记录启动提示：none/cancel/no/yes/skip 五场景。

迁移自旧 gui_t24_crash_driver.py（子进程 + JSON 结果），现改为进程内 pytest。
崩溃提示在启动后 after(300) 执行，messagebox/filedialog 全部打桩：
  none   无崩溃记录 → 不弹窗
  cancel askyesnocancel 返回 None → 记录保留
  no     返回 False → 记录被清除
  yes    返回 True → 选路径导出诊断包 → showinfo → 记录清除，zip 内含 crashes/
  skip   TRAESIGN_NO_CRASH_NOTICE=1 → 不弹窗、记录保留
"""
from __future__ import annotations

import sys
import zipfile

import pytest
import tkinter.filedialog as fdlg
import tkinter.messagebox as mbox

from _gui_helpers import run_gui_steps

pytestmark = pytest.mark.gui

SCENES = ("none", "cancel", "no", "yes", "skip")


@pytest.mark.parametrize("scene", SCENES)
def test_crash_notice(scene, data_home, monkeypatch):
    from trae_checkin import crashhandlers

    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    if scene == "skip":
        monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")
    else:
        monkeypatch.delenv("TRAESIGN_NO_CRASH_NOTICE", raising=False)

    # 预置崩溃记录（none 不预置）
    if scene in ("cancel", "no", "yes", "skip"):
        try:
            raise RuntimeError("冒烟诱发崩溃")
        except RuntimeError:
            crashhandlers.write_crash_dump(*sys.exc_info(), "冒烟测试位置")
        assert len(crashhandlers.list_crash_dumps()) == 1, "预置崩溃记录失败"

    bundle_path = data_home / "smoke_diag.zip"
    calls = {"ask": 0, "save": 0, "info": 0, "ask_title": ""}

    def fake_ask(title, message, **kw):
        calls["ask"] += 1
        calls["ask_title"] = str(title)
        if scene == "yes":
            return True
        if scene == "no":
            return False
        return None

    monkeypatch.setattr(mbox, "askyesnocancel", fake_ask)
    monkeypatch.setattr(fdlg, "asksaveasfilename",
                        lambda **kw: (calls.__setitem__("save", calls["save"] + 1),
                                      str(bundle_path))[1])
    monkeypatch.setattr(mbox, "showinfo",
                        lambda *a, **kw: calls.__setitem__("info", calls["info"] + 1))

    errors: list[str] = []
    rc = run_gui_steps([], errors, quit_ms=1400)
    assert rc == 0
    assert errors == []

    remaining = len(crashhandlers.list_crash_dumps())

    if scene == "none":
        assert calls["ask"] == 0, "无崩溃记录时不应弹窗"
    elif scene == "skip":
        assert calls["ask"] == 0, "跳过开关生效时不应弹窗"
        assert remaining == 1, "跳过场景应保留崩溃记录"
    elif scene == "cancel":
        assert calls["ask"] == 1, "取消场景应弹窗 1 次"
        assert "异常退出" in calls["ask_title"], "弹窗标题不符合预期"
        assert remaining == 1, "取消场景应保留崩溃记录"
    elif scene == "no":
        assert calls["ask"] == 1, "否场景应弹窗 1 次"
        assert remaining == 0, "选否后应清除崩溃记录"
    elif scene == "yes":
        assert calls["ask"] == 1, "是场景应弹窗 1 次"
        assert calls["save"] == 1, "是场景应弹出保存对话框"
        assert calls["info"] == 1, "导出后应弹完成提示"
        assert remaining == 0, "导出后应清除崩溃记录"
        assert bundle_path.exists(), "诊断包应已导出"
        with zipfile.ZipFile(bundle_path) as zf:
            assert any(n.startswith("crashes/") for n in zf.namelist()), (
                "诊断包内应含 crashes/ 崩溃文件")
