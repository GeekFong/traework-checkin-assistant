# -*- coding: utf-8 -*-
"""GUI 冒烟：真实 Tk 窗口启动渲染 + app.ico 图标加载。

迁移自旧单文件脚本 test_gui_t7.py / test_gui_icon.py。
全程使用 data_home 临时目录，不碰真实账号与设置。
"""

import json

import pytest

from trae_checkin import accounts, constants, crypto, history
from _gui_helpers import find_buttons, run_gui_steps

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _skip_dialogs(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")


def test_gui_boot_render_with_account(data_home):
    """预置带备注账号与一条成功历史后，窗口能渲染且不抛异常。"""
    accounts._save_account_record("smoke", {
        "platform": constants.PLAT_TRAEWORK,
        "display_name": "smoke", "region": "CN",
        "device_id": "d", "note": "冒烟号",
        "secret": crypto.dpapi_encrypt(json.dumps(
            {"token": "t", "refreshToken": "r", "expiredAt": ""}).encode()),
    })
    history.record_checkin_history([{
        "platform": constants.PLAT_TRAEWORK,
        "platform_label": constants.PLATFORM_LABELS[constants.PLAT_TRAEWORK],
        "username": "smoke", "note": "冒烟号", "ok": True, "already": False,
        "credits": 1, "message": "签到成功"}])

    errors: list = []

    def assert_rendered(root):
        assert root.winfo_exists()
        root.update_idletasks()
        # 账号区已渲染出「全部账号签到」按钮
        batch = find_buttons(root, lambda b: b.cget("text") == "全部账号签到")
        assert len(batch) == 1
        assert str(batch[0].cget("state")) == "normal"

    rc = run_gui_steps([(1200, assert_rendered)], errors, quit_ms=1600)
    assert rc == 0
    assert errors == []


def test_gui_icon_loads(data_home):
    """run_gui 内部 iconbitmap 加载 assets/app.ico 不抛异常，并可再次显式加载。"""
    from trae_checkin.runtime import resource_path

    ico = resource_path("app.ico")
    assert ico.is_file(), f"缺少图标文件：{ico}"

    errors: list = []

    def assert_icon(root):
        assert root.winfo_exists()
        root.update_idletasks()
        root.iconbitmap(default=str(ico))
        root.update_idletasks()

    rc = run_gui_steps([(600, assert_icon)], errors, quit_ms=900)
    assert rc == 0
    assert errors == []
