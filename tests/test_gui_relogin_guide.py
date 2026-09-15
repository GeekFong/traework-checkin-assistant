# -*- coding: utf-8 -*-
"""GUI 冒烟 t11：账号行「重登」按钮 + 批量失效后「打开客户端重登」引导。

迁移自旧单文件脚本 test_gui_t11.py。全程打桩：
不真正签到、不真正启动客户端、不弹真实对话框。
"""

import pytest
import tkinter.messagebox as mbox

from trae_checkin import accounts, constants
from trae_checkin.gui import app as gui_app
from _gui_helpers import find_buttons, run_gui_steps

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _skip_dialogs(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")


def test_relogin_buttons_for_both_platforms(data_home, monkeypatch):
    # 预置两个平台各一个账号
    accounts._save_account_record("tw_alice", {
        "platform": constants.PLAT_TRAEWORK,
        "display_name": "alice", "note": "主力号",
        "region": "CN", "secret": "BLOB1", "enabled": True})
    accounts._save_account_record("wb:bob", {
        "platform": constants.PLAT_WORKBUDDY,
        "display_name": "bob", "region": "CN",
        "secret": "BLOB2", "enabled": True})

    launched: list = []
    shown: list = []
    errors: list = []

    # 打桩：不真正启动客户端
    def fake_launch(platform):
        launched.append(platform)
        exe = "WorkBuddy.exe" if platform == constants.PLAT_WORKBUDDY \
            else "Trae SOLO CN.exe"
        return True, r"C:\fake\%s" % exe

    monkeypatch.setattr(gui_app, "launch_platform_app", fake_launch)
    monkeypatch.setattr(mbox, "showinfo",
                        lambda title, msg, **kw: shown.append(msg))
    monkeypatch.setattr(mbox, "showerror",
                        lambda title, msg, **kw: errors.append(msg))
    monkeypatch.setattr(mbox, "showwarning",
                        lambda title, msg, **kw: errors.append(msg))
    # 找不到时也不弹文件选择
    monkeypatch.setattr(mbox, "askyesno", lambda *a, **kw: False)

    # 打桩：批量签到对每个账号返回"登录已失效"
    def fake_batch(accs, status_only=False):
        out = []
        for a in accs:
            pf = a.get("platform", constants.PLAT_TRAEWORK)
            out.append({
                "platform": pf,
                "platform_label": constants.PLATFORM_LABELS.get(pf, pf),
                "username": a.get("display_name"), "note": a.get("note", ""),
                "ok": False, "already": False, "credits": None,
                "message": "登录已失效，请重新登录后再试"})
        return out

    monkeypatch.setattr(gui_app, "run_batch_checkin", fake_batch)

    def step_account_row(root):
        # 账号行的「重登」按钮应有两个，点第一个（TraeWork）
        btns = find_buttons(root, lambda b: b.cget("text") == "重登")
        assert len(btns) == 2, f"账号行重登按钮数：{len(btns)}"
        btns[0].invoke()
        # 批量前不应有「打开…重登」引导
        guides = [b for b in find_buttons(root)
                  if b.cget("text").startswith("打开") and "重登" in b.cget("text")]
        assert guides == []

    def step_run_batch(root):
        batch = [b for b in find_buttons(root)
                 if b.cget("text") == "全部账号签到"]
        assert len(batch) == 1
        batch[0].invoke()

    def step_batch_guides(root):
        # 批量完成后应出现两个平台的引导按钮，各点一次
        guides = [b for b in find_buttons(root)
                  if b.cget("text").startswith("打开") and "重登" in b.cget("text")]
        assert len(guides) == 2, f"批量后引导按钮数：{len(guides)}"
        for b in guides:
            b.invoke()

    rc = run_gui_steps(
        [(600, step_account_row), (1200, step_run_batch),
         (2000, step_batch_guides)], errors, quit_ms=2800)

    assert rc == 0
    assert errors == []
    # 账号行按钮打开 TraeWork，批量引导覆盖两个平台
    assert constants.PLAT_TRAEWORK in launched
    assert constants.PLAT_WORKBUDDY in launched
    assert launched.count(constants.PLAT_TRAEWORK) >= 2
    # 分步重登引导文案
    assert any("重新登录" in s and "保存当前登录账号" in s for s in shown)
