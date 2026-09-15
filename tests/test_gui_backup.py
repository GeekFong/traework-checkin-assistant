# -*- coding: utf-8 -*-
"""GUI 冒烟 t9：自动点击 备份 / 恢复 / 诊断包 三个按钮。

迁移自旧单文件脚本 test_gui_t9.py。临时 HOME 全程无人工交互：
filedialog/messagebox 全部打桩，不弹真实对话框。
"""

import zipfile

import pytest
import tkinter.filedialog as fdlg
import tkinter.messagebox as mbox

from trae_checkin import accounts, constants, history
from _gui_helpers import click_button, run_gui_steps

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _skip_dialogs(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")


def test_backup_restore_diagnostic(data_home, monkeypatch):
    # 预置账号（含一个可识别的假密文标记，用于诊断包脱敏断言）+ 历史
    accounts._save_account_record("smoke", {
        "platform": constants.PLAT_TRAEWORK,
        "display_name": "smoke", "region": "CN",
        "note": "冒烟号", "secret": "DPAPI_BLOB",
        "saved_at": "2026-09-10 09:00"})
    history.record_checkin_history([{
        "platform": constants.PLAT_TRAEWORK,
        "platform_label": constants.PLATFORM_LABELS[constants.PLAT_TRAEWORK],
        "username": "smoke", "note": "冒烟号", "ok": True,
        "message": "签到成功"}])

    backup_path = data_home / "gui_backup.zip"
    diag_path = data_home / "gui_diag.zip"

    errors: list = []
    info_shown: list = []

    def fake_save(title=None, **kw):
        if title and "诊断" in title:
            return str(diag_path)
        return str(backup_path)

    monkeypatch.setattr(fdlg, "asksaveasfilename", fake_save)
    monkeypatch.setattr(fdlg, "askopenfilename", lambda **kw: str(backup_path))
    monkeypatch.setattr(mbox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(mbox, "showinfo",
                        lambda title, msg, **kw: info_shown.append(msg))
    monkeypatch.setattr(mbox, "showerror",
                        lambda title, msg, **kw: errors.append(msg))

    def do_backup(root):
        click_button(root, "备份", errors)

    def tamper_then_restore(root):
        # 备份后塞入"入侵者"账号；恢复成功应把它冲掉并留下 .restore-bak
        accounts._save_account_record("intruder", {
            "platform": constants.PLAT_TRAEWORK,
            "display_name": "intruder", "secret": "X"})
        click_button(root, "恢复", errors)

    def do_diag(root):
        click_button(root, "诊断包", errors)

    rc = run_gui_steps(
        [(400, do_backup), (1000, tamper_then_restore),
         (1700, do_diag)], errors, quit_ms=2500)

    assert rc == 0
    assert errors == []

    # 备份 zip 内容完整
    assert backup_path.exists()
    with zipfile.ZipFile(backup_path) as zf:
        assert {"accounts.json", "settings.json", "checkin_history.json",
                "manifest.json"} <= set(zf.namelist())

    # 恢复后旧文件转存 .restore-bak，账号回到备份时状态
    assert (data_home / "accounts.json.restore-bak").exists()
    keys = {a["key"] for a in accounts.list_accounts()}
    assert "intruder" not in keys and "smoke" in keys

    # 诊断包无密文泄露
    assert diag_path.exists()
    with zipfile.ZipFile(diag_path) as zf:
        txt = zf.read("diagnostic_info.json").decode("utf-8")
    assert "DPAPI_BLOB" not in txt

    # 三类成功提示均已弹出
    assert any("备份完成" in s for s in info_shown)
    assert any("已恢复" in s for s in info_shown)
    assert any("诊断包已导出" in s for s in info_shown)
