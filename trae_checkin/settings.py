# -*- coding: utf-8 -*-
"""设置项的默认值、迁移、读写与推送配置判断。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from typing import Any, Optional
from .crypto import dpapi_decrypt, dpapi_encrypt
from .jsonstore import _load_json_file, _save_json_file
from .runtime import _log_dir, log


SETTINGS_FILE = _log_dir() / "settings.json"


PUSH_CHANNELS = ("serverchan", "pushplus", "wecom", "dingtalk")


PUSH_CHANNEL_LABELS = {
    "serverchan": "Server酱（推荐）",
    "pushplus": "PushPlus",
    "wecom": "企业微信群机器人",
    "dingtalk": "钉钉群机器人",
}


SETTINGS_VERSION = 2


THEMES = ("light", "dark")


def _default_settings() -> dict:
    return {
        "push_enabled": False,
        "channels": ["serverchan"],
        "only_failures": False,
        "theme": "light",
        "disclaimer_accepted": False,
        "creds": {ch: {"key": "", "secret": ""} for ch in PUSH_CHANNELS},
    }


def _migrate_legacy_settings(s: dict) -> dict:
    """把旧版 {channel, key_enc} 单渠道配置迁移为新版多渠道结构。"""
    old_ch = s.get("channel", "serverchan")
    if old_ch not in PUSH_CHANNELS:
        old_ch = "serverchan"
    old_key = ""
    key_enc = s.get("key_enc", "")
    if key_enc:
        try:
            old_key = dpapi_decrypt(key_enc).decode("utf-8")
        except Exception:
            old_key = ""
    data = {
        "version": SETTINGS_VERSION,
        "push_enabled": bool(s.get("push_enabled", False)),
        "channels": [old_ch],
        "only_failures": bool(s.get("only_failures", False)),
        "theme": "light",
        "disclaimer_accepted": False,
        "creds_enc": {},
        "secret_enc": {},
    }
    if old_key:
        data["creds_enc"][old_ch] = dpapi_encrypt(
            old_key.strip().encode("utf-8"))
    return data


def load_settings() -> dict:
    """读取推送设置（自动兼容并迁移旧版单渠道配置）。"""
    s = _load_json_file(SETTINGS_FILE)
    if not isinstance(s, dict):
        return _default_settings()
    # 旧版结构：无 version 且含 channel/key_enc 字段
    if s.get("version") != SETTINGS_VERSION:
        try:
            s = _migrate_legacy_settings(s)
            _save_json_file(SETTINGS_FILE, s)
        except Exception as e:
            log.warning(f"旧版推送设置迁移失败，按默认设置启动：{e}")
            return _default_settings()
    channels = [c for c in s.get("channels", []) if c in PUSH_CHANNELS]
    if not channels:
        channels = ["serverchan"]
    creds: dict[str, dict] = {}
    for ch in PUSH_CHANNELS:
        key = secret = ""
        enc = (s.get("creds_enc") or {}).get(ch, "")
        if enc:
            try:
                key = dpapi_decrypt(enc).decode("utf-8")
            except Exception:
                key = ""
        sec_enc = (s.get("secret_enc") or {}).get(ch, "")
        if sec_enc:
            try:
                secret = dpapi_decrypt(sec_enc).decode("utf-8")
            except Exception:
                secret = ""
        creds[ch] = {"key": key, "secret": secret}
    theme = s.get("theme", "light")
    if theme not in THEMES:
        theme = "light"
    return {
        "push_enabled": bool(s.get("push_enabled", False)),
        "channels": channels,
        "only_failures": bool(s.get("only_failures", False)),
        "theme": theme,
        "disclaimer_accepted": bool(s.get("disclaimer_accepted", False)),
        "creds": creds,
        # 扩展字段：掉线预警的每日去重标记（跨进程/跨天去重依赖落盘回读）
        OFFLINE_ALERT_KEY: s.get(OFFLINE_ALERT_KEY)
        if isinstance(s.get(OFFLINE_ALERT_KEY), dict) else {},
    }


def save_settings_dict(settings: dict) -> None:
    """按新版内存结构保存推送设置（明文凭据经 DPAPI 加密落盘）。"""
    channels = [c for c in (settings.get("channels") or [])
                if c in PUSH_CHANNELS]
    if not channels:
        channels = ["serverchan"]
    creds_in = settings.get("creds") or {}
    creds_enc: dict[str, str] = {}
    secret_enc: dict[str, str] = {}
    for ch in PUSH_CHANNELS:
        c = creds_in.get(ch) or {}
        key = (c.get("key") or "").strip()
        secret = (c.get("secret") or "").strip()
        if key:
            creds_enc[ch] = dpapi_encrypt(key.encode("utf-8"))
        if secret:
            secret_enc[ch] = dpapi_encrypt(secret.encode("utf-8"))
    theme = settings.get("theme", "light")
    if theme not in THEMES:
        theme = "light"
    data = {
        "version": SETTINGS_VERSION,
        "push_enabled": bool(settings.get("push_enabled", False)),
        "channels": channels,
        "only_failures": bool(settings.get("only_failures", False)),
        "theme": theme,
        "disclaimer_accepted": bool(settings.get("disclaimer_accepted", False)),
        "creds_enc": creds_enc,
        "secret_enc": secret_enc,
    }
    # 保留其它扩展字段（如 offline_alerts 掉线预警标记），避免被固定白名单覆盖丢失；
    # 但排除运行时敏感/临时键（creds 为解密后的明文，只能以 creds_enc 落盘）
    _RUNTIME_SETTING_KEYS = {"creds"}
    for k, v in settings.items():
        if k not in data and k not in _RUNTIME_SETTING_KEYS:
            data[k] = v
    _save_json_file(SETTINGS_FILE, data)


def accept_disclaimer() -> None:
    """标记风险声明已确认（读-改-写，不动凭据与其它设置）。"""
    try:
        s = None
        if SETTINGS_FILE.exists():
            loaded = _load_json_file(SETTINGS_FILE)
            if loaded.get("version") == SETTINGS_VERSION:
                s = loaded
        if s is None:
            s = {"version": SETTINGS_VERSION, "channels": ["serverchan"],
                 "creds_enc": {}, "secret_enc": {}}
        s["disclaimer_accepted"] = True
        _save_json_file(SETTINGS_FILE, s)
    except Exception as e:
        log.warning(f"风险声明确认状态保存失败：{e}")


def save_theme_preference(theme: str) -> None:
    """单独保存主题偏好（不动凭据；读-改-写，避免覆盖其它设置）。"""
    if theme not in THEMES:
        theme = "light"
    try:
        s = None
        if SETTINGS_FILE.exists():
            loaded = _load_json_file(SETTINGS_FILE)
            if loaded.get("version") == SETTINGS_VERSION:
                s = loaded
        if s is None:
            # 文件不存在 / 损坏 / 旧版：写最小 v2 骨架，避免破坏迁移流程
            s = {"version": SETTINGS_VERSION, "channels": ["serverchan"],
                 "creds_enc": {}, "secret_enc": {}}
        s["theme"] = theme
        _save_json_file(SETTINGS_FILE, s)
    except Exception as e:
        log.warning(f"主题偏好保存失败：{e}")


def push_configured(settings: dict) -> bool:
    """是否至少有一个已勾选渠道填好了主凭据。"""
    creds = settings.get("creds") or {}
    return any(ch in (settings.get("channels") or [])
               and (creds.get(ch) or {}).get("key")
               for ch in PUSH_CHANNELS)


OFFLINE_ALERT_KEY = "offline_alerts"
