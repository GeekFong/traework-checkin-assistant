# -*- coding: utf-8 -*-
"""双平台批量签到编排。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import json
import random
import time
from typing import Any, Optional
from .accounts import _get_account_record, _record_platform, _update_account_secret, _update_account_secret_obj, list_accounts
from .constants import PLATFORM_LABELS, PLAT_WORKBUDDY
from .crypto import dpapi_decrypt
from .platforms.traework import run_checkin_with_credential
from .platforms.workbuddy import run_wb_checkin_with_credential


def run_batch_checkin(accounts: Optional[list[dict]] = None,
                      status_only: bool = False) -> list[dict]:
    """
    对所有已保存的账号快照逐个签到（自动按平台分派 TraeWork / WorkBuddy）。
    每个元素返回统一结构的结果（含 platform / username）。
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
        try:
            secret = json.loads(dpapi_decrypt(record["secret"]).decode("utf-8"))
        except Exception as e:
            results.append({"username": display, "ok": False, "already": False,
                            "platform": plat, "platform_label": label,
                            "note": note,
                            "message": f"凭证解密失败：{e}"})
            continue

        if plat == PLAT_WORKBUDDY:
            def _persist_wb(new_secret: dict, _key: str = key):
                _update_account_secret_obj(_key, new_secret)

            r = run_wb_checkin_with_credential(
                secret, display_name=record.get("display_name", display),
                status_only=status_only, on_token_refreshed=_persist_wb)
            r["note"] = note
            results.append(r)
            continue

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
        results.append(r)
    return results
