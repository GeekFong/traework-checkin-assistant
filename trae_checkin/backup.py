# -*- coding: utf-8 -*-
"""用户数据备份 / 恢复 / 诊断包导出。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import json
from typing import Any, Optional
from .accounts import ACCOUNTS_FILE, _record_platform
from .constants import APP_NAME
from .crashhandlers import list_crash_dumps
from .history import load_history
from .jsonstore import _load_json_file
from .push import load_push_history
from .runtime import APP_VERSION, LOG_FILE, _data_dir
from .settings import PUSH_CHANNELS, SETTINGS_FILE


BACKUP_DATA_FILES = ("accounts.json", "settings.json",
                     "checkin_history.json", "push_history.json")


BACKUP_MANIFEST = "manifest.json"


BACKUP_FORMAT_VERSION = 1


def backup_user_data(path: str) -> dict:
    """
    把账号快照 / 推送设置 / 签到历史打包为单个 .zip 备份。
    注意：账号与推送凭据经 DPAPI 加密，仅能在本机当前 Windows 用户下恢复。
    返回 {"accounts","settings","history"} 各项是否存在。
    """
    import zipfile
    base = _data_dir()
    present = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in BACKUP_DATA_FILES:
            src = base / name
            if src.exists():
                zf.write(src, name)
                present[name] = True
            else:
                present[name] = False
        manifest = {
            "format": BACKUP_FORMAT_VERSION,
            "app_version": APP_VERSION,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "machine_warning": "凭据经 Windows DPAPI 加密，仅可在本机同用户下恢复使用。",
            "files": {n: present.get(n, False) for n in BACKUP_DATA_FILES},
        }
        zf.writestr(BACKUP_MANIFEST,
                    json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"accounts": present.get("accounts.json", False),
            "settings": present.get("settings.json", False),
            "history": present.get("checkin_history.json", False),
            "push_log": present.get("push_history.json", False)}


def restore_user_data(path: str) -> dict:
    """
    从 .zip 备份恢复数据（恢复前把当前同名文件另存为 .restore-bak）。
    仅恢复本工具自身的数据文件（含推送历史），不触碰日志。
    """
    import zipfile
    base = _data_dir()
    restored = []
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if BACKUP_MANIFEST not in names and not (names & set(BACKUP_DATA_FILES)):
            raise ValueError("不是有效的备份文件（缺少清单与数据文件）。")
        for name in BACKUP_DATA_FILES:
            if name not in names:
                continue
            dst = base / name
            if dst.exists():
                dst.replace(base / (name + ".restore-bak"))
            with zf.open(name) as src, open(dst, "wb") as out:
                out.write(src.read())
            restored.append(name)
    if not restored:
        raise ValueError("备份包中没有可恢复的数据文件。")
    return {"restored": restored}


def export_diagnostic_bundle(path: str) -> dict:
    """
    一键导出诊断包（.zip）：版本/环境信息、最近日志、脱敏设置、数据概况。
    不含 DPAPI 凭据密文，可安全发送给他人协助排查。
    """
    import zipfile
    import platform as _pf
    base = _data_dir()

    # 脱敏设置：只保留渠道开关与"是否已填凭据"，剔除任何密文/明文
    settings = _load_json_file(SETTINGS_FILE)
    masked = {
        "version": settings.get("version"),
        "push_enabled": settings.get("push_enabled"),
        "channels": settings.get("channels"),
        "only_failures": settings.get("only_failures"),
        "credential_present": {
            ch: bool((settings.get("creds_enc") or {}).get(ch))
            for ch in PUSH_CHANNELS},
        "sign_secret_present": {
            ch: bool((settings.get("secret_enc") or {}).get(ch))
            for ch in PUSH_CHANNELS},
    }
    # 账号概况：平台/是否启用/有无备注，绝不含 secret
    acc_overview = []
    for key, acc in _load_json_file(ACCOUNTS_FILE).items():
        if not isinstance(acc, dict):
            continue
        acc_overview.append({
            "key_hint": str(key)[:2] + "***",
            "platform": _record_platform(acc),
            "enabled": bool(acc.get("enabled", True)),
            "has_note": bool(acc.get("note")),
            "has_secret": bool(acc.get("secret")),
            "saved_at": acc.get("saved_at", ""),
        })
    hist = load_history().get("days", {})
    # 推送历史仅汇总数量/类型，不导出任何标题与详情
    push_items = load_push_history(limit=100000)
    push_summary = {"total": len(push_items)}
    if push_items:
        by_kind, ok_n, latest = {}, 0, ""
        for it in push_items:
            by_kind[it.get("kind") or "其他"] = \
                by_kind.get(it.get("kind") or "其他", 0) + 1
            ok_n += 1 if it.get("ok") else 0
            ts = str(it.get("ts") or "")
            if ts > latest:
                latest = ts
        push_summary.update({"success": ok_n, "failed": len(push_items) - ok_n,
                             "by_kind": by_kind, "latest": latest})
    # 崩溃转存（仅含堆栈与环境信息，无账号凭据）
    crashes = list_crash_dumps()
    info = {
        "app_version": APP_VERSION,
        "app_name": APP_NAME,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": _pf.python_version(),
        "os": f"{_pf.platform()} {_pf.release()}",
        "data_dir": str(base),
        "accounts_count": len(acc_overview),
        "accounts": acc_overview,
        "history_days": len(hist),
        "push_history": push_summary,
        "crash_dumps": len(crashes),
        "settings_masked": masked,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("diagnostic_info.json",
                    json.dumps(info, ensure_ascii=False, indent=2))
        log_path = LOG_FILE
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                zf.writestr("trae_checkin_tail.log",
                            "".join(lines[-2000:]))
            except OSError:
                pass
        for cp in crashes:
            try:
                zf.write(cp, f"crashes/{cp.name}")
            except OSError:
                pass
    return {"accounts": len(acc_overview), "history_days": len(hist),
            "crash_dumps": len(crashes)}
