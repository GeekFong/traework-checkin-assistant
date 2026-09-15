# -*- coding: utf-8 -*-
"""备份 / 恢复 / 诊断包：跨数据目录往返验证。

迁移自旧单文件脚本式测试 test_t9.py。
旧实现靠 importlib 重新加载单文件让导入期常量指向恢复目标目录；拆分后
backup 内部动态调用 runtime._data_dir()，账号/设置/历史常量则在导入期固化，
因此这里在同一个测试内用 monkeypatch 把两套路径一起切换到恢复目标目录。

红线：所有数据都在 tmp_path 下的两个临时目录间往返，绝不触碰真实数据。
"""

from __future__ import annotations

import json
import zipfile

import pytest

from trae_checkin import (
    accounts,
    backup,
    constants,
    history,
    jsonstore,
    push,
    runtime,
    settings as settings_mod,
)


def repoint(monkeypatch, home):
    """把所有数据路径常量与各命名空间的 _data_dir() 统一切到 ``home``。"""
    monkeypatch.setattr(runtime, "_data_dir", lambda: home)
    monkeypatch.setattr(runtime, "_log_dir", lambda: home)
    # backup 通过 `from .runtime import _data_dir` 持有本地引用，
    # 只改 runtime 命名空间不会影响它，必须在 backup 命名空间内一并替换。
    monkeypatch.setattr(backup, "_data_dir", lambda: home)
    monkeypatch.setenv("TRAESIGN_HOME", str(home))
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", home / "accounts.json")
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", home / "settings.json")
    monkeypatch.setattr(history, "HISTORY_FILE",
                        home / "checkin_history.json")
    monkeypatch.setattr(push, "PUSH_LOG_FILE", home / "push_history.json")
    monkeypatch.setattr(backup, "ACCOUNTS_FILE", home / "accounts.json")
    monkeypatch.setattr(backup, "SETTINGS_FILE", home / "settings.json")


def test_backup_restore_diagnostic_roundtrip(data_home, monkeypatch, tmp_path):
    src = data_home

    # ── 准备三类数据（账号假密文、双通道推送凭据、一条历史） ──────────
    accounts._save_account_record("alice", {
        "platform": constants.PLAT_TRAEWORK, "display_name": "alice",
        "note": "主力号", "region": "CN",
        "secret": "DPAPI_SECRET_BLOB_XYZ", "saved_at": "2026-09-01 09:00"})
    settings_mod.save_settings_dict({
        "push_enabled": True, "channels": ["serverchan", "dingtalk"],
        "only_failures": False,
        "creds": {"serverchan": {"key": "SCT-secretkey123", "secret": ""},
                  "dingtalk": {"key": "https://oapi.dingtalk.com/x",
                               "secret": "SEC-sign456"}},
    })
    history.record_checkin_history([{
        "platform": constants.PLAT_TRAEWORK, "platform_label": "TraeWork CN",
        "username": "alice", "note": "主力号",
        "ok": True, "message": "签到成功"}])

    backup_zip = src / "my_backup.zip"
    info = backup.backup_user_data(str(backup_zip))
    assert info["accounts"] and info["settings"] and info["history"]

    with zipfile.ZipFile(backup_zip) as zf:
        names = set(zf.namelist())
    assert {"accounts.json", "settings.json", "checkin_history.json",
            "manifest.json"} <= names
    with zipfile.ZipFile(backup_zip) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest["format"] == 1 and "machine_warning" in manifest

    # ── 篡改源目录数据，确保恢复后看到的是备份内容 ───────────────────
    accounts._save_account_record(
        "intruder",
        {"platform": constants.PLAT_TRAEWORK, "display_name": "intruder",
         "secret": "NEW"})
    accounts.set_account_note("alice", "被改掉的备注")
    settings_mod.save_settings_dict({
        "push_enabled": False, "channels": [],
        "creds": {"serverchan": {"key": "TAMPERED", "secret": ""}}})

    # ── 恢复到另一个空目录 ───────────────────────────────────────────
    restored_home = tmp_path / "restored"
    restored_home.mkdir()
    repoint(monkeypatch, restored_home)
    res = backup.restore_user_data(str(backup_zip))
    assert set(res["restored"]) == {"accounts.json", "settings.json",
                                    "checkin_history.json"}

    accs = {a["key"]: a for a in accounts.list_accounts()}
    assert "intruder" not in accs and "alice" in accs
    assert accs["alice"]["note"] == "主力号"

    s = settings_mod.load_settings()
    assert s["push_enabled"] is True
    assert s["channels"] == ["serverchan", "dingtalk"]
    assert s["creds"]["serverchan"]["key"] == "SCT-secretkey123"
    assert s["creds"]["dingtalk"]["secret"] == "SEC-sign456"
    assert history.load_history()["days"]

    bak = restored_home / "accounts.json.restore-bak"
    assert not bak.exists()

    # ── 再次恢复到有数据的目录：旧文件应转存 .restore-bak ─────────────
    accounts._save_account_record(
        "existing",
        {"platform": constants.PLAT_TRAEWORK, "display_name": "existing",
         "secret": "E"})
    backup.restore_user_data(str(backup_zip))
    assert bak.exists()
    old_data = json.loads(bak.read_text(encoding="utf-8"))
    assert "existing" in old_data

    # ── 诊断包：绝不含密文/明文密钥 ───────────────────────────────────
    diag_zip = restored_home / "diag.zip"
    dinfo = backup.export_diagnostic_bundle(str(diag_zip))
    assert dinfo["accounts"] >= 1 and dinfo["history_days"] >= 1
    with zipfile.ZipFile(diag_zip) as zf:
        dn = set(zf.namelist())
        info_txt = zf.read("diagnostic_info.json").decode("utf-8")
    assert "diagnostic_info.json" in dn
    for leak in ("DPAPI_SECRET_BLOB_XYZ", "SCT-secretkey123", "SEC-sign456",
                 "creds_enc", "secret_enc", "TAMPERED"):
        assert leak not in info_txt, f"诊断信息泄露：{leak}"
    diag = json.loads(info_txt)
    assert diag["accounts"][0]["key_hint"].endswith("***")
    assert (diag["settings_masked"]["credential_present"]["serverchan"]
            is True)

    # ── 非法备份包应拒绝 ─────────────────────────────────────────────
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("random.txt", "hello")
    with pytest.raises(ValueError):
        backup.restore_user_data(str(bad_zip))
