# -*- coding: utf-8 -*-
"""多账号快照的增删改查（DPAPI 加密落盘）。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import json
import platform
from typing import Any, Optional
from .constants import PLATFORM_LABELS, PLAT_TRAEWORK, PLAT_WORKBUDDY
from .crypto import dpapi_encrypt
from .jsonstore import _load_json_file, _save_json_file
from .runtime import _log_dir, log
from .platforms.traework import load_auth_info, load_device_id, refresh_token
from .platforms.workbuddy import load_wb_session, wb_account_display, wb_account_key, wb_session_secret_obj


ACCOUNTS_FILE = _log_dir() / "accounts.json"


def _record_platform(acc: dict) -> str:
    """账号记录所属平台；旧文件无 platform 字段，一律视为 TraeWork。"""
    p = acc.get("platform")
    return p if p in PLATFORM_LABELS else PLAT_TRAEWORK


def _account_display(platform: str, key: str, acc: dict) -> str:
    name = acc.get("display_name") or acc.get("username")
    if name:
        return str(name)
    if platform == PLAT_WORKBUDDY and ":" in key:
        return key.split(":", 1)[-1]
    return key


def list_accounts() -> list[dict]:
    """返回已保存账号的公开信息（不含密钥）：key/platform/display_name/region/saved_at。"""
    data = _load_json_file(ACCOUNTS_FILE)
    out = []
    for key, acc in data.items():
        if not isinstance(acc, dict):
            continue
        plat = _record_platform(acc)
        display = _account_display(plat, key, acc)
        out.append({
            "key": key,
            "platform": plat,
            "platform_label": PLATFORM_LABELS.get(plat, plat),
            "username": display,
            "display_name": display,
            "note": str(acc.get("note") or ""),
            "region": acc.get("region", "CN"),
            "saved_at": acc.get("saved_at", ""),
            "enabled": bool(acc.get("enabled", True)),
        })
    out.sort(key=lambda a: (a["platform"], a["username"].lower()))
    return out


def _get_account_record(key: str) -> Optional[dict]:
    return _load_json_file(ACCOUNTS_FILE).get(key)


def _save_account_record(key: str, record: dict) -> bool:
    """写入一条账号记录，返回是否为覆盖更新。"""
    data = _load_json_file(ACCOUNTS_FILE)
    existed = key in data
    data[key] = record
    _save_json_file(ACCOUNTS_FILE, data)
    return existed


def save_current_account(platform: str = PLAT_TRAEWORK) -> tuple[bool, str]:
    """
    把当前客户端登录账号的凭证加密保存为快照。
    同一账号重复保存会覆盖刷新（用于续期失败后手动更新）。
    """
    if platform == PLAT_WORKBUDDY:
        return _save_current_wb_account()
    return _save_current_trae_account()


def _save_current_trae_account() -> tuple[bool, str]:
    try:
        auth_info = load_auth_info()
        device_id = load_device_id()
    except Exception as e:
        return False, str(e)

    username = auth_info.get("account", {}).get("username", "")
    if not username:
        return False, "未能从登录信息中读取账号名。"

    record = {
        "platform": PLAT_TRAEWORK,
        "display_name": username,
        "region": auth_info.get("userRegion", {}).get("region", "") or "CN",
        "device_id": device_id,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "secret": dpapi_encrypt(json.dumps({
            "token": auth_info.get("token", ""),
            "refreshToken": auth_info.get("refreshToken", ""),
            "expiredAt": auth_info.get("expiredAt", ""),
        }, ensure_ascii=False).encode("utf-8")),
    }
    old = _get_account_record(username)
    if isinstance(old, dict) and old.get("note"):
        record["note"] = old["note"]
    existed = _save_account_record(username, record)
    log.info(f"已{'更新' if existed else '保存'} TraeWork 账号快照：{username}")
    return True, username


def delete_account(key: str) -> None:
    data = _load_json_file(ACCOUNTS_FILE)
    if key in data:
        del data[key]
        _save_json_file(ACCOUNTS_FILE, data)


def set_account_enabled(key: str, enabled: bool) -> None:
    """启用/停用某个已保存账号（停用后不参与批量与每日自动签到）。"""
    data = _load_json_file(ACCOUNTS_FILE)
    acc = data.get(key)
    if isinstance(acc, dict):
        acc["enabled"] = bool(enabled)
        data[key] = acc
        _save_json_file(ACCOUNTS_FILE, data)


def set_account_note(key: str, note: str) -> None:
    """设置/清空某个账号的备注名（推送报告与账号列表中优先展示）。"""
    data = _load_json_file(ACCOUNTS_FILE)
    acc = data.get(key)
    if isinstance(acc, dict):
        note = (note or "").strip()
        if note:
            acc["note"] = note
        else:
            acc.pop("note", None)
        data[key] = acc
        _save_json_file(ACCOUNTS_FILE, data)


def _update_account_secret_obj(key: str, secret_obj: dict) -> None:
    """token 自动续期后回写加密快照（通用，secret_obj 结构按平台各自约定）。"""
    data = _load_json_file(ACCOUNTS_FILE)
    acc = data.get(key)
    if not acc:
        return
    acc["secret"] = dpapi_encrypt(
        json.dumps(secret_obj, ensure_ascii=False).encode("utf-8"))
    data[key] = acc
    _save_json_file(ACCOUNTS_FILE, data)


def _update_account_secret(username: str, token: str, refresh_token: str,
                           expired_at: str) -> None:
    """TraeWork token 续期回写。"""
    _update_account_secret_obj(username, {
        "token": token,
        "refreshToken": refresh_token,
        "expiredAt": expired_at,
    })


def _save_current_wb_account() -> tuple[bool, str]:
    sess = load_wb_session()
    key = wb_account_key(sess)
    acc = sess.get("account", {})
    secret_obj = wb_session_secret_obj(sess)
    record = {
        "platform": PLAT_WORKBUDDY,
        "display_name": wb_account_display(sess),
        "uid": acc.get("uid", ""),
        "region": "CN",
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "secret": dpapi_encrypt(json.dumps(
            secret_obj, ensure_ascii=False).encode("utf-8")),
    }
    old = _get_account_record(key)
    if isinstance(old, dict) and old.get("note"):
        record["note"] = old["note"]
    existed = _save_account_record(key, record)
    log.info(f"已{'更新' if existed else '保存'} WorkBuddy 账号快照：{record['display_name']}")
    return True, record["display_name"]
