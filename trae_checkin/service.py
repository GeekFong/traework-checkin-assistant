# -*- coding: utf-8 -*-
"""双平台批量签到编排。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import json
import random
import time
from typing import Any, Optional
from .accounts import (
    _get_account_record, _record_platform, _update_account_secret,
    _update_account_secret_obj, list_accounts, save_current_account,
)
from .constants import PLATFORM_LABELS, PLAT_WORKBUDDY
from .crypto import dpapi_decrypt
from .runtime import log
from .platforms.traework import run_checkin_with_credential
from .platforms.workbuddy import run_wb_checkin_with_credential


# 判定一次失败是否属于“凭证/登录态失效”，命中后才触发快照自动更新。
_AUTH_ERROR_HINTS = (
    "token", "令牌", "凭证", "刷新", "401", "403", "unauthorized",
    "未登录", "登录态", "会话", "auth",
)


def _is_auth_error(message: str) -> bool:
    msg = (message or "").lower()
    return any(hint in msg for hint in _AUTH_ERROR_HINTS)


def _sign_record(record: dict, *, key: str, plat: str, label: str,
                 display: str, note: str, status_only: bool) -> dict:
    """用给定账号记录执行一次签到（自动按平台分派）。"""
    try:
        secret = json.loads(dpapi_decrypt(record["secret"]).decode("utf-8"))
    except Exception as e:
        return {"username": display, "ok": False, "already": False,
                "platform": plat, "platform_label": label, "note": note,
                "message": f"凭证解密失败：{e}"}

    if plat == PLAT_WORKBUDDY:
        def _persist_wb(new_secret: dict, _key: str = key):
            _update_account_secret_obj(_key, new_secret)

        r = run_wb_checkin_with_credential(
            secret, display_name=record.get("display_name", display),
            status_only=status_only, on_token_refreshed=_persist_wb)
        r["note"] = note
        return r

    # TraeWork
    auth_info = {
        "token": secret.get("token", ""),
        "refreshToken": secret.get("refreshToken", ""),
        "expiredAt": secret.get("expiredAt", ""),
        "region": record.get("region", "CN"),
    }

    def _persist(new_token: str, new_rt: str, new_exp: str,
                 _key: str = key):
        _update_account_secret(_key, new_token, new_rt, new_exp)

    r = run_checkin_with_credential(
        auth_info, record.get("device_id", ""),
        username=record.get("display_name", display),
        status_only=status_only, on_token_refreshed=_persist)
    r["note"] = note
    return r


def run_batch_checkin(accounts: Optional[list[dict]] = None,
                      status_only: bool = False) -> list[dict]:
    """
    对所有已保存的账号快照逐个签到（自动按平台分派 TraeWork / WorkBuddy）。
    每个元素返回统一结构的结果（含 platform / username）。

    当快照凭证失效且自动续期失败时，会自动读取桌面客户端的实时登录态、
    覆盖刷新快照并重试一次，无需手动「保存当前登录账号」。
    """
    if accounts is None:
        accounts = list_accounts()
    # 跳过被用户禁用的账号（enabled=False）
    accounts = [a for a in accounts if a.get("enabled", True)]
    results: list[dict] = []
    for idx, meta in enumerate(accounts):
        if idx > 0:
            # 账号之间随机间隔 2~6 秒，降低风控概率
            time.sleep(random.uniform(2, 6))
        key = meta.get("key", "")
        display = meta.get("display_name") or meta.get("username", "")
        plat = meta.get("platform") or _record_platform(
            _get_account_record(key) or {})
        record = _get_account_record(key)
        if not record:
            results.append({"username": display, "ok": False, "already": False,
                            "platform": plat,
                            "platform_label": PLATFORM_LABELS.get(plat, plat),
                            "note": meta.get("note", ""),
                            "message": "账号快照不存在"})
            continue
        plat = _record_platform(record)
        label = PLATFORM_LABELS.get(plat, plat)
        note = meta.get("note") or record.get("note") or ""

        r = _sign_record(record, key=key, plat=plat, label=label,
                         display=display, note=note, status_only=status_only)

        # 凭证失效 → 自动从客户端实时登录态刷新快照，并重试一次
        if not r.get("ok") and not r.get("already") and \
                _is_auth_error(r.get("message", "")):
            log.info(f"{label}/{display} 凭证失效，自动读取客户端登录态更新快照")
            refreshed = False
            try:
                refreshed, _ = save_current_account(plat)
            except Exception as e:
                log.warning(f"{label}/{display} 自动更新快照失败：{e}")
            if refreshed:
                new_record = _get_account_record(key)
                secret_changed = isinstance(new_record, dict) and \
                    new_record.get("secret") != record.get("secret")
                if secret_changed:
                    time.sleep(random.uniform(1, 2))
                    r2 = _sign_record(new_record, key=key, plat=plat,
                                      label=label, display=display, note=note,
                                      status_only=status_only)
                    if r2.get("ok") or r2.get("already"):
                        r2["auto_refreshed"] = True
                        r2["message"] = "凭证已自动更新；" + r2.get("message", "")
                    r = r2
                else:
                    log.info(f"{label}/{display} 客户端登录态同样失效，跳过自动重试")

        results.append(r)
    return results
