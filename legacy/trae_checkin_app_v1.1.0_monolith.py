#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TraeWork CN 每日签到助手（图形界面 / 单文件可执行版）
=====================================================

面向最终用户的一体化签到工具，无需安装 Python，双击即可使用：

  - 自动检测 TraeWork CN 是否安装、是否已登录
  - 未登录时提供图形化登录引导（一键打开应用、自动等待登录完成）
  - 一键立即签到，实时显示签到状态与积分
  - 一键开启 / 关闭「每日自动签到」Windows 定时任务
  - 被定时任务调用时以 --silent 静默后台执行，不弹窗

工作原理:
  读取 TraeWork CN 本地加密存储，解密获取登录 Token 与设备 ID，
  直接调用官方签到接口（与官方客户端 v1.107+ 协议一致）。

用法:
  双击 exe 或运行 python trae_checkin_app.py   打开图形界面
  ... --silent                                 静默签到（供定时任务调用）
  ... --status                                 命令行查询状态
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import locale
import logging
import logging.handlers
import os
import platform
import random
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# ──────────────────────────── 基本信息 ────────────────────────────

APP_NAME = "每日签到助手"
APP_DISPLAY_NAME = "TraeWork CN / WorkBuddy 每日签到助手"
APP_VERSION = "1.1.0"
APP_COPYRIGHT = "个人开源工具 · 免费使用 · 数据仅存本机"
TASK_NAME = "TraeWorkDailyCheckin"
REQ_SOURCE = 2  # 1=IDE, 2=Work/Solo Lite

# ── 支持的平台 ──
PLAT_TRAEWORK = "traework"
PLAT_WORKBUDDY = "workbuddy"
PLATFORM_LABELS = {
    PLAT_TRAEWORK: "TraeWork",
    PLAT_WORKBUDDY: "WorkBuddy",
}

API_BASE = "https://api.trae.cn"
CHECKIN_STATUS_URL = f"{API_BASE}/trae/api/v2/ug/checkin_credits/status"
CHECKIN_CLAIM_URL = f"{API_BASE}/trae/api/v2/ug/checkin_credits/claim"
REFRESH_TOKEN_URL = f"{API_BASE}/trae/api/v3/oauth/ExchangeToken"

# ── WorkBuddy（腾讯 CodeBuddy 体系）──
WB_API_BASE = "https://copilot.tencent.com"
WB_CHECKIN_STATUS_URL = f"{WB_API_BASE}/v2/billing/meter/checkin-activity-status"
WB_CHECKIN_CLAIM_URL = f"{WB_API_BASE}/v2/billing/meter/daily-checkin"
WB_REFRESH_TOKEN_URL = f"{WB_API_BASE}/v2/auth/token/refresh"
WB_USER_AGENT_FALLBACK = "WorkBuddy/5.5.6"
_wb_ua_cache: Optional[str] = None

# 数据目录候选名（Roaming 下）
APPDATA_DIR_NAMES = ["TRAE SOLO CN", "TRAE SOLO", "Trae CN", "Trae", "TRAE"]
# 安装目录候选（目录名）
INSTALL_DIR_NAMES = ["TRAE SOLO CN", "TRAE SOLO", "Trae CN", "Trae"]
# 安装目录下的可执行文件名
EXE_NAMES = ["Trae SOLO CN.exe", "TRAE SOLO CN.exe", "Trae.exe", "TRAE.exe"]


# ──────────────────────────── 运行环境路径 ────────────────────────────

def is_frozen() -> bool:
    """是否为 PyInstaller 打包后的 exe。"""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """可执行文件 / 脚本所在目录（定时任务依赖此固定位置）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def exe_path() -> Path:
    """当前可执行文件路径（打包后为 exe，开发时为 python + 脚本）。"""
    if is_frozen():
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


# ──────────────────────────── 日志（写入用户目录） ────────────────────────────

def _data_dir() -> Path:
    """
    数据目录（日志/账号快照/设置/历史）：
      1. 环境变量 TRAESIGN_HOME 指定的目录（便于统一管理或测试隔离）；
      2. 程序所在目录（可写时，免安装单文件体验）；
      3. 用户 AppData\\Local\\TraeCheckinApp 兜底。
    """
    override = (os.environ.get("TRAESIGN_HOME") or "").strip()
    if override:
        d = Path(override).expanduser()
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            # 此处 logger 可能尚未初始化，不记录日志，静默回退默认目录
            pass
    candidate = app_dir()
    try:
        test = candidate / ".write_test"
        test.touch()
        test.unlink()
        return candidate
    except Exception:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / "TraeCheckinApp"
        d.mkdir(parents=True, exist_ok=True)
        return d


# 兼容旧名称
_log_dir = _data_dir


LOG_FILE = _log_dir() / "trae_checkin.log"


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("trae-checkin-app")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_048_576, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    # 命令行模式额外输出到控制台
    try:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    except Exception:
        pass
    return logger


log = setup_logger()


# ──────────────────────────── 全局异常兜底 ────────────────────────────

# 崩溃转存目录：每次未捕获异常单独成文，便于下次启动时提示用户
CRASH_DIR = _log_dir() / "crashes"
CRASH_KEEP_FILES = 10


def write_crash_dump(exc_type, exc_value, exc_tb, where: str) -> Optional[Path]:
    """把一次未捕获异常转存为 crashes/crash-YYYYMMDD-HHMMSS-xxxx.log。

    与普通运行日志分开存放：普通日志会轮转覆盖，崩溃文件只保留最近若干份，
    供下次启动弹窗与诊断包导出使用。任何失败都静默吞掉，绝不能再抛。
    """
    try:
        ts = datetime.now()
        # 同秒内可能连续多次崩溃，加随机后缀避免文件名碰撞互相覆盖
        suffix = secrets.token_hex(2)
        fname = (f"crash-{ts.strftime('%Y%m%d-%H%M%S')}-"
                 f"{os.getpid()}-{suffix}.log")
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        path = CRASH_DIR / fname
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        header = (
            f"应用：{APP_NAME} {APP_VERSION}\n"
            f"时间：{ts.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"位置：{where}\n"
            f"Python：{sys.version.split()[0]}\n"
            f"系统：{platform.platform()}\n"
            f"打包态：{bool(getattr(sys, 'frozen', False))}\n"
            f"{'─' * 50}\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + detail)
        # 仅保留最近 CRASH_KEEP_FILES 份，避免长期累积
        try:
            files = sorted(CRASH_DIR.glob("crash-*.log"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[CRASH_KEEP_FILES:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        return path
    except Exception:
        return None


def list_crash_dumps(limit: int = CRASH_KEEP_FILES) -> list[Path]:
    """返回崩溃转存文件，最新的在前；目录不存在/读取失败返回空列表。"""
    try:
        if not CRASH_DIR.is_dir():
            return []
        files = [p for p in CRASH_DIR.glob("crash-*.log") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]
    except OSError:
        return []


def clear_crash_dumps() -> int:
    """删除全部崩溃转存文件，返回删除数量（用户已确认或导出后调用）。"""
    n = 0
    for p in list_crash_dumps(limit=10000):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n

# 异常弹窗节流：同一类异常 60 秒内只弹一次，避免后台线程连环报错刷屏
_error_popups: dict[str, float] = {}
_ERROR_POPUP_INTERVAL = 60.0


def _show_error_dialog(exc_type, exc_value, exc_tb, where: str) -> None:
    """在主线程弹窗提示用户，同时完整堆栈已写入日志文件。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        key = getattr(exc_type, "__name__", str(exc_type))
        now = time.time()
        last = _error_popups.get(key, 0.0)
        if now - last < _ERROR_POPUP_INTERVAL:
            return
        _error_popups[key] = now
        detail = "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb))[-1500:]
        try:
            top = tk.Tk()
            top.withdraw()
            top.attributes("-topmost", True)
            messagebox.showerror(
                f"{APP_NAME}出现异常",
                f"程序在{where}发生未处理的异常（已记录到日志，不影响其他功能）：\n\n"
                f"{key}: {exc_value}\n\n"
                f"日志文件：{LOG_FILE}\n\n详细堆栈：\n{detail}")
            top.destroy()
        except Exception:
            pass
    except Exception:
        # 弹窗本身失败时不要再抛，静默吞掉（日志里已有记录）
        pass


def _log_uncaught(exc_type, exc_value, exc_tb, where: str,
                  popup: bool = False) -> None:
    """统一记录未捕获异常；GUI 模式可选弹窗提示。"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    try:
        log.error("未处理异常（%s）：%s: %s\n%s", where,
                  getattr(exc_type, "__name__", str(exc_type)),
                  exc_value,
                  "".join(traceback.format_exception(
                      exc_type, exc_value, exc_tb)))
    except Exception:
        pass
    # 独立崩溃转存：普通日志会轮转，崩溃文件单独保留供下次启动提示
    write_crash_dump(exc_type, exc_value, exc_tb, where)
    if popup and threading.current_thread() is threading.main_thread():
        _show_error_dialog(exc_type, exc_value, exc_tb, where)


def install_exception_hooks(popup: bool = False) -> None:
    """安装主线程 / 后台线程未捕获异常钩子（重复安装安全）。"""
    def _sys_hook(exc_type, exc_value, exc_tb):
        _log_uncaught(exc_type, exc_value, exc_tb, "主线程", popup=popup)

    def _thread_hook(args):
        _log_uncaught(args.exc_type, args.exc_value, args.exc_traceback,
                      f"后台线程 {args.thread.name if args.thread else '?'}",
                      popup=False)  # 后台线程不直接弹窗，避免跨线程操作 Tk

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


def install_tk_error_handler(root, popup: bool = True) -> None:
    """
    接管 Tk 主循环回调中的异常：Tk 默认会把回调异常打印到 stderr 后继续运行，
    noconsole 打包后这些错误完全不可见，导致按钮"点了没反应"无法排查。
    """
    def _report_callback_exception(exc, val, tb):
        _log_uncaught(exc, val, tb, "界面操作", popup=False)
        if popup:
            # Tk 回调本身就在主线程，可安全弹窗
            _show_error_dialog(exc, val, tb, "界面操作")
    root.report_callback_exception = _report_callback_exception


# ──────────────────────────── 加密常量与解密 ────────────────────────────

MAGIC = b'tc'
SEED_OFFSET = 6
SEED_LENGTH = 32
CIPHERTEXT_OFFSET = SEED_OFFSET + SEED_LENGTH
CHECKSUM_LENGTH = 64

XOR_MASK = bytes([
    77, 212, 194, 230, 184, 49, 98, 9, 14, 82, 179, 199, 166, 115, 59, 164,
    28, 178, 70, 43, 130, 154, 181, 138, 25, 107, 57, 219, 87, 23, 117, 36,
    244, 155, 175, 127, 8, 232, 214, 141, 38, 167, 46, 55, 193, 169, 90, 47,
    31, 5, 165, 24, 146, 174, 242, 148, 151, 50, 182, 42, 56, 170, 221, 88
])


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def decrypt_bytecrypto(b64_data: str) -> bytes:
    raw = base64.b64decode(b64_data)
    if len(raw) < CIPHERTEXT_OFFSET:
        raise ValueError("密文数据过短")
    if raw[0:2] != MAGIC:
        raise ValueError("无效的密文格式")
    if raw[2] != 5:
        raise ValueError(f"不支持的加密版本: {raw[2]}")
    seed = raw[SEED_OFFSET:CIPHERTEXT_OFFSET]
    h1 = hashlib.sha512(seed).digest()
    h2 = hashlib.sha512(h1 + XOR_MASK).digest()
    aes_key, iv = h2[:16], h2[16:32]
    decrypted = _aes_cbc_decrypt(aes_key, iv, raw[CIPHERTEXT_OFFSET:])
    if len(decrypted) < CHECKSUM_LENGTH:
        raise ValueError("解密数据过短")
    stored_checksum, plaintext = decrypted[:CHECKSUM_LENGTH], decrypted[CHECKSUM_LENGTH:]
    if stored_checksum != hashlib.sha512(plaintext).digest():
        raise ValueError("校验和验证失败——应用可能已更新")
    return plaintext


def decrypt_to_json(b64_data: str) -> Any:
    return json.loads(decrypt_bytecrypto(b64_data).decode("utf-8"))


# ──────────────────────────── 环境检测 ────────────────────────────

def get_app_data_dir() -> Optional[Path]:
    """返回存在 storage.json 的应用数据目录。"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    for name in APPDATA_DIR_NAMES:
        p = Path(appdata) / name
        if (p / "User" / "globalStorage" / "storage.json").exists():
            return p
    return None


def get_storage_path() -> Optional[Path]:
    d = get_app_data_dir()
    return (d / "User" / "globalStorage" / "storage.json") if d else None


def get_tiny_storage_path() -> Optional[Path]:
    d = get_app_data_dir()
    return (d / "aha" / "TinyStorage") if d else None


def find_install_exes() -> list[Path]:
    """查找所有可能的 TraeWork 可执行文件（含正在运行的进程路径）。"""
    found: list[Path] = []
    roots: list[Path] = []

    pf = os.environ.get("ProgramFiles", "")
    pf86 = os.environ.get("ProgramFiles(x86)", "")
    local = os.environ.get("LOCALAPPDATA", "")
    drives = ["C:\\", "D:\\", "E:\\", "F:\\"]

    for base in [pf, pf86]:
        if base:
            for name in INSTALL_DIR_NAMES:
                roots.append(Path(base) / name)
    if local:
        for name in INSTALL_DIR_NAMES:
            roots.append(Path(local) / "Programs" / name)
    # 常见自定义盘根目录
    for drv in drives:
        for name in INSTALL_DIR_NAMES:
            roots.append(Path(drv) / name)

    for root in roots:
        if not root.exists():
            continue
        for exe_name in EXE_NAMES:
            cand = root / exe_name
            if cand.exists():
                found.append(cand)
        # 兼容版本子目录 / current 链接
        try:
            for sub in root.iterdir():
                if sub.is_dir():
                    for exe_name in EXE_NAMES:
                        cand = sub / exe_name
                        if cand.exists() and cand not in found:
                            found.append(cand)
        except Exception:
            pass

    # 从正在运行的进程推断安装路径（作为补充，wmic 在新版系统可能缺失）
    try:
        names = ["Trae SOLO CN.exe", "TRAE SOLO CN.exe", "Trae.exe"]
        for nm in names:
            r = _run(["wmic", "process", "where", f"name='{nm}'", "get",
                      "ExecutablePath", "/FORMAT:LIST"], timeout=10)
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("ExecutablePath=") and line.endswith(".exe"):
                    p = Path(line.split("=", 1)[1])
                    if p.exists() and p not in found:
                        found.append(p)
    except Exception:
        pass

    # 去重
    uniq: list[Path] = []
    for p in found:
        if p not in uniq:
            uniq.append(p)
    return uniq


def detect_app_version() -> str:
    """从安装目录 product.json 读取版本号。"""
    pf = os.environ.get("ProgramFiles", "")
    pf86 = os.environ.get("ProgramFiles(x86)", "")
    local = os.environ.get("LOCALAPPDATA", "")
    roots: list[Path] = []
    for base in [r"D:\\", pf, pf86]:
        if base:
            for name in INSTALL_DIR_NAMES:
                roots.append(Path(base) / name)
    if local:
        for name in INSTALL_DIR_NAMES:
            roots.append(Path(local) / "Programs" / name)
    for root in roots:
        pj = root / "resources" / "app" / "product.json"
        try:
            if pj.exists():
                with open(pj, "r", encoding="utf-8") as f:
                    ver = json.load(f).get("version", "")
                if ver:
                    return ver
        except Exception:
            continue
    return "1.107.1"


APP_VERSION = detect_app_version()


# ──────────────────────────── 凭证读取 ────────────────────────────

def load_auth_info() -> dict:
    sp = get_storage_path()
    if not sp:
        raise FileNotFoundError("未找到 TraeWork CN 的应用数据，请先安装并至少登录一次。")
    with open(sp, "r", encoding="utf-8") as f:
        storage = json.load(f)
    encrypted = storage.get("iCubeAuthInfo://icube.cloudide")
    if not encrypted:
        raise KeyError("未找到登录凭证，请先登录 TraeWork CN。")
    info = decrypt_to_json(encrypted)
    if not info.get("token"):
        raise ValueError("登录凭证中缺少 Token，请重新登录。")
    return info


def load_device_id() -> str:
    tp = get_tiny_storage_path()
    if not tp or not tp.exists():
        raise FileNotFoundError("找不到设备标识文件，请先打开一次 TraeWork CN。")
    with open(tp, "r", encoding="utf-8") as f:
        tiny = json.load(f)
    data = tiny.get("tiny_storage_data", tiny)
    if isinstance(data, str):
        data = json.loads(data)
    encrypted_did = data.get("aha.device.device_id")
    if not encrypted_did:
        raise KeyError("未找到设备 ID，请先打开一次 TraeWork CN。")
    info = decrypt_to_json(encrypted_did)
    did = info.get("device_id_str")
    if not did:
        raise ValueError("设备 ID 为空。")
    return did


def is_logged_in() -> tuple[bool, str]:
    """快速判断登录状态，返回 (是否登录, 原因说明)。"""
    try:
        load_auth_info()
        load_device_id()
        return True, ""
    except FileNotFoundError as e:
        return False, str(e)
    except KeyError as e:
        return False, str(e)
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"读取登录信息失败：{e}"


# ──────────────────────────── 多账号存储（DPAPI 加密） ────────────────────────────

# 账号快照与推送设置（与日志同目录，随 exe 拷贝，删除即清空）
ACCOUNTS_FILE = _log_dir() / "accounts.json"
SETTINGS_FILE = _log_dir() / "settings.json"
HISTORY_FILE = _log_dir() / "checkin_history.json"
# 推送历史（仅记录标题/渠道/结果，不含任何密钥内容）
PUSH_LOG_FILE = _log_dir() / "push_history.json"
# 周报/月报当天是否已推送的标记文件（防止同一天多次触发重复推送）
PERIOD_MARKERS_FILE = _log_dir() / "period_markers.json"
HISTORY_KEEP_DAYS = 90
PUSH_LOG_KEEP_DAYS = 90
PUSH_LOG_KEEP_ITEMS = 300


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_encrypt(data: bytes) -> str:
    """用 Windows DPAPI（当前用户凭据）加密，返回 base64 字符串。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("凭证加密失败（CryptProtectData）")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(b64: str) -> bytes:
    """解密 dpapi_encrypt 产生的 base64 字符串。"""
    raw = base64.b64decode(b64)
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("凭证解密失败（文件可能来自其他电脑或系统账户，请重新保存账号）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _load_json_file(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


# ── 推送渠道定义（多渠道可同时启用） ──
PUSH_CHANNELS = ("serverchan", "pushplus", "wecom", "dingtalk")
PUSH_CHANNEL_LABELS = {
    "serverchan": "Server酱（推荐）",
    "pushplus": "PushPlus",
    "wecom": "企业微信群机器人",
    "dingtalk": "钉钉群机器人",
}
# 新版每渠道两个字段：<ch>_enc 主凭据（DPAPI 加密），<ch>_secret_enc 加签密钥
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


# ──────────────────────────── 签到历史记录 ────────────────────────────

def history_identity(platform: str, username: str) -> str:
    """历史记录中账号的稳定标识。"""
    return f"{platform or '?'}::{username or '?'}"


def record_checkin_history(results: list[dict]) -> None:
    """把一次批量/单账号签到结果按账号、按天写入历史（每天每账号仅一条，覆盖当天）。"""
    try:
        data = _load_json_file(HISTORY_FILE)
    except Exception:
        data = {}
    days = data.get("days")
    if not isinstance(days, dict):
        days = {}
    today = datetime.now().strftime("%Y-%m-%d")
    now_hm = datetime.now().strftime("%H:%M")
    bucket = days.setdefault(today, {})
    for r in results:
        if not isinstance(r, dict):
            continue
        plat = r.get("platform") or PLAT_TRAEWORK
        label = r.get("platform_label") or PLATFORM_LABELS.get(plat, plat)
        uname = r.get("username") or "未知账号"
        ident = history_identity(plat, uname)
        existing = bucket.get(ident)
        # 已成功记录不被后续失败覆盖（静默兜底可能对同一平台跑两次）
        if existing and existing.get("ok") and not r.get("ok"):
            continue
        # 新结果未带备注时沿用当天已有备注
        note = r.get("note") or (existing.get("note") if existing else "") or ""
        bucket[ident] = {
            "platform": plat, "platform_label": label, "username": uname,
            "note": note,
            "ok": bool(r.get("ok")), "already": bool(r.get("already")),
            "suspicious": bool(r.get("suspicious")),
            "credits": r.get("credits"), "message": r.get("message", ""),
            "time": now_hm,
        }
    # 仅保留最近 HISTORY_KEEP_DAYS 天
    cutoff = time.time() - HISTORY_KEEP_DAYS * 86400
    for d in list(days.keys()):
        try:
            if datetime.strptime(d, "%Y-%m-%d").timestamp() < cutoff:
                days.pop(d, None)
        except ValueError:
            days.pop(d, None)
    data["days"] = days
    try:
        _save_json_file(HISTORY_FILE, data)
    except Exception as e:
        log.error(f"写入签到历史失败：{e}")


def load_history() -> dict:
    try:
        data = _load_json_file(HISTORY_FILE)
        if isinstance(data.get("days"), dict):
            return data
    except Exception:
        pass
    return {"days": {}}


def history_summary(days_back: int = 30) -> dict:
    """汇总近 N 天统计：每账号连续签到天数、窗口内成功率、今日概况。"""
    data = load_history()
    days: dict = data.get("days", {})
    today = datetime.now().date()
    window = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(days_back)]
    accounts: dict[str, dict] = {}
    today_ok = today_total = 0
    for d in window:
        bucket = days.get(d) or {}
        for ident, item in bucket.items():
            acc = accounts.setdefault(ident, {
                "platform": item.get("platform", ""),
                "platform_label": item.get("platform_label", ""),
                "username": item.get("username", ""),
                "note": item.get("note", ""),
                "last_date": "", "last_time": "",
                "success": 0, "fail": 0, "checked_days": set()})
            # window 从今天向过去排列，首次遇到即最近一条记录
            if not acc["last_date"]:
                acc["last_date"] = d
                acc["last_time"] = item.get("time", "")
            if item.get("note") and not acc["note"]:
                acc["note"] = item.get("note")
            if item.get("ok"):
                acc["success"] += 1
                acc["checked_days"].add(d)
            else:
                acc["fail"] += 1
            if d == window[0]:
                today_total += 1
                if item.get("ok"):
                    today_ok += 1
    out_accounts = []
    for ident, acc in accounts.items():
        total = acc["success"] + acc["fail"]
        rate = round(acc["success"] * 100.0 / total) if total else 0
        # 连续天数：从今天（若今天还没记录则从昨天起算）往回数
        streak = 0
        cursor = today
        checked = acc["checked_days"]
        if cursor.strftime("%Y-%m-%d") not in checked:
            cursor = cursor - timedelta(days=1)
        while cursor.strftime("%Y-%m-%d") in checked:
            streak += 1
            cursor = cursor - timedelta(days=1)
        out_accounts.append({
            "identity": ident,
            "platform_label": acc["platform_label"], "username": acc["username"],
            "note": acc["note"], "last_date": acc["last_date"],
            "last_time": acc["last_time"],
            "streak": streak, "rate": rate, "success": acc["success"],
            "fail": acc["fail"], "total": total})
    out_accounts.sort(key=lambda a: (a["platform_label"], a["username"]))
    return {"accounts": out_accounts, "today_ok": today_ok,
            "today_total": today_total, "days_back": days_back}


def account_status_lookup(days_back: int = 90) -> dict:
    """{history_identity: {note,last_date,last_time,streak}}，供 GUI 账号行展示。"""
    out: dict[str, dict] = {}
    try:
        for a in history_summary(days_back).get("accounts") or []:
            out[a["identity"]] = {
                "note": a.get("note", ""),
                "last_date": a.get("last_date", ""),
                "last_time": a.get("last_time", ""),
                "streak": a.get("streak", 0),
            }
    except Exception:
        pass
    return out


def history_calendar(days_back: int = 90) -> dict:
    """近 N 天日历聚合（供热力图）。

    返回 {date: {state: none|all_ok|partial|all_fail,
                 success, fail, total, items}}，并含起止日期。
    """
    days: dict = load_history().get("days", {})
    today = datetime.now().date()
    out: dict = {}
    for i in range(days_back):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        bucket = days.get(d) or {}
        items = list(bucket.values())
        success = sum(1 for it in items if it.get("ok"))
        fail = len(items) - success
        if not items:
            state = "none"
        elif fail == 0:
            state = "all_ok"
        elif success == 0:
            state = "all_fail"
        else:
            state = "partial"
        out[d] = {"state": state, "success": success, "fail": fail,
                  "total": len(items), "items": items}
    return out


def history_recent_rows(limit: int = 14) -> list[dict]:
    """返回最近 limit 天（按日期倒序）的行：{date, items:[...]}。"""
    days: dict = load_history().get("days", {})
    dates = sorted(days.keys(), reverse=True)[:limit]
    rows = []
    for d in dates:
        items = sorted(days[d].values(),
                       key=lambda x: (x.get("platform_label", ""), x.get("username", "")))
        rows.append({"date": d, "items": items})
    return rows


def history_monthly_stats(months: int = 3) -> list[dict]:
    """最近 months 个自然月的每月每账号统计：成功/失败/成功率。"""
    days: dict = load_history().get("days", {})
    today = datetime.now().date()
    # 生成最近 months 个月份键（YYYY-MM），倒序
    keys: list[str] = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    # {月份: {ident: [success, fail, label, uname]}}
    bucket: dict[str, dict] = {k: {} for k in keys}
    for d, items in days.items():
        mkey = d[:7]
        if mkey not in bucket or not isinstance(items, dict):
            continue
        for ident, item in items.items():
            cell = bucket[mkey].setdefault(ident, [0, 0,
                                                    item.get("platform_label", ""),
                                                    item.get("username", "")])
            cell[0 if item.get("ok") else 1] += 1
    out: list[dict] = []
    for mkey in keys:
        accounts = []
        for ident, (succ, fail, label, uname) in bucket[mkey].items():
            total = succ + fail
            accounts.append({
                "identity": ident, "platform_label": label, "username": uname,
                "success": succ, "fail": fail, "total": total,
                "rate": round(succ * 100.0 / total) if total else 0,
            })
        accounts.sort(key=lambda a: (a["platform_label"], a["username"]))
        out.append({"month": mkey, "accounts": accounts})
    return out


def export_history_csv(path: str) -> int:
    """
    把全部本地签到历史导出为 Excel 友好的 CSV（UTF-8 BOM）。
    返回导出的记录条数。
    """
    days: dict = load_history().get("days", {})
    rows = ["日期,星期,平台,账号,结果,类型,积分,时间,说明"]
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    count = 0

    def _csv_escape(v: Any) -> str:
        s = "" if v is None else str(v)
        return '"' + s.replace('"', '""') + '"'

    for d in sorted(days.keys()):
        try:
            wd = weekday_cn[datetime.strptime(d, "%Y-%m-%d").weekday()]
        except Exception:
            wd = ""
        for item in sorted(days[d].values(),
                           key=lambda x: (x.get("platform_label", ""), x.get("username", ""))):
            ok = bool(item.get("ok"))
            susp = bool(item.get("suspicious"))
            if not ok:
                kind = "失败"
            elif item.get("already"):
                kind = "已签到（幂等）"
            elif susp:
                kind = "成功但无成功信号（疑似改版）"
            else:
                kind = "签到成功"
            result_txt = "失败" if not ok else ("成功（待人工确认）" if susp else "成功")
            rows.append(",".join([
                _csv_escape(d), _csv_escape(wd),
                _csv_escape(item.get("platform_label", "")),
                _csv_escape(item.get("username", "")),
                _csv_escape(result_txt),
                _csv_escape(kind),
                _csv_escape(item.get("credits")),
                _csv_escape(item.get("time", "")),
                _csv_escape(item.get("message", "")),
            ]))
            count += 1
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(rows))
    return count


# 趋势折线最多展示的账号线数（过多会无法分辨）
TREND_MAX_LINES = 6
TREND_LINE_COLORS = ("#2f6bff", "#16a34a", "#e8870e", "#9b59b6",
                     "#0ea5b7", "#e0457b")


def history_credit_trend(days_back: int = 30) -> dict:
    """近 N 天每个账号的积分走势（历史里的 credits 是签到后积分余额）。

    返回 {dates:[升序日期],
          series:[{identity,label,color,points:[None|int]}],
          max_credit, has_data}。缺失日期补 None（断线处理由绘制端决定）。
    """
    days: dict = load_history().get("days", {})
    today = datetime.now().date()
    dates = [(today - timedelta(days=days_back - 1 - i)).strftime("%Y-%m-%d")
             for i in range(days_back)]
    # identity -> {label, {date: credit}}
    raw: dict[str, dict] = {}
    max_credit = 0
    for d in dates:
        for ident, item in (days.get(d) or {}).items():
            c = item.get("credits")
            if not isinstance(c, (int, float)):
                continue
            c = int(c)
            cell = raw.setdefault(ident, {"label": "", "pts": {}})
            cell["label"] = _trend_label(item)
            cell["pts"][d] = c
            if c > max_credit:
                max_credit = c
    # 账号顺序：按窗口内最后一次出现的积分余额倒序（积分高的靠前）
    def _last_val(cell: dict) -> int:
        for d in reversed(dates):
            v = cell["pts"].get(d)
            if v is not None:
                return v
        return -1
    ordered = sorted(raw.items(), key=lambda kv: _last_val(kv[1]), reverse=True)
    series = []
    for i, (ident, cell) in enumerate(ordered[:TREND_MAX_LINES]):
        series.append({
            "identity": ident,
            "label": cell["label"],
            "color": TREND_LINE_COLORS[i % len(TREND_LINE_COLORS)],
            "points": [cell["pts"].get(d) for d in dates],
        })
    return {"dates": dates, "series": series,
            "max_credit": max_credit, "has_data": bool(series),
            "truncated": len(raw) > TREND_MAX_LINES}


def _trend_label(item: dict) -> str:
    """趋势线图例名：备注 或 用户名（带平台前缀）。"""
    label = item.get("platform_label") or ""
    who = item.get("note") or item.get("username") or "未知账号"
    return f"{label}·{who}" if label else str(who)


# ──────────────────────────── 数据备份 / 恢复 / 诊断 ────────────────────────────

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


# ──────────────────────────── 子进程工具 ────────────────────────────

def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    creationflags = 0x08000000 if os.name == "nt" else 0
    try:
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            encoding=locale.getpreferredencoding(False), errors="replace",
            creationflags=creationflags,
        )
    except (LookupError, TypeError):
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=creationflags)


def launch_app(exe: Optional[Path] = None) -> bool:
    """启动 TraeWork 应用（不等待）。"""
    targets = [exe] if exe else []
    targets.extend(find_install_exes())
    for t in targets:
        if t and t.exists():
            try:
                subprocess.Popen([str(t)], close_fds=True)
                return True
            except Exception:
                continue
    return False


def find_wb_install_exes() -> list[Path]:
    """查找腾讯 WorkBuddy 桌面端可执行文件（含用户机上的 G:\\workb 自定义位置）。"""
    cands: list[Path] = []
    for base in (Path(os.environ.get("LOCALAPPDATA", "")),
                 Path(os.environ.get("PROGRAMFILES", "")),
                 Path(os.environ.get("PROGRAMFILES(X86)", ""))):
        if not str(base):
            continue
        for pat in ("WorkBuddy*/WorkBuddy.exe",
                    "Programs/WorkBuddy*/WorkBuddy.exe"):
            try:
                cands.extend(p for p in base.glob(pat) if p.is_file())
            except Exception:
                pass
    for p in (Path("G:/workb/WorkBuddy/WorkBuddy.exe"),):
        if p.is_file() and p not in cands:
            cands.append(p)
    return cands


def find_platform_exes(platform: str) -> list[Path]:
    """按平台返回候选客户端可执行文件列表。"""
    if platform == PLAT_WORKBUDDY:
        return find_wb_install_exes()
    return find_install_exes()


def launch_platform_app(platform: str) -> tuple[bool, str]:
    """启动指定平台的桌面客户端。

    返回 (是否成功, 提示信息)；失败时信息用于 GUI/日志展示。
    """
    label = PLATFORM_LABELS.get(platform, "客户端")
    exes = find_platform_exes(platform)
    if not exes:
        return False, (f"未能自动找到 {label} 客户端，请确认已安装桌面端；"
                       "也可以手动启动客户端后再回来刷新凭据。")
    for exe in exes:
        try:
            subprocess.Popen([str(exe)], close_fds=True)
            return True, str(exe)
        except Exception:
            continue
    return False, f"找到了 {label}，但启动失败，请手动打开客户端。"


# ──────────────────────────── HTTP 与签到 ────────────────────────────

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _friendly_net_error(err: BaseException) -> str:
    """把底层网络异常翻译成人话，方便用户自行排查（断网/DNS/超时/代理/证书）。"""
    reason = getattr(err, "reason", err)
    text = str(reason).lower()
    if isinstance(reason, socket.timeout) or "timed out" in text or "timeout" in text:
        return "请求超时：网络较慢或对方服务器暂时无响应，请稍后重试"
    if isinstance(reason, ssl.SSLError) or "certificate" in text:
        return "HTTPS 证书校验失败：请检查系统时间是否正确，或代理/杀毒软件是否在拦截加密连接"
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in text or "name or service" in text \
            or "gethostbyname" in text or "dns" in text:
        return "无法解析服务器域名（DNS 故障）：请检查网络连接或更换 DNS（如 223.5.5.5）"
    if isinstance(reason, ConnectionRefusedError) or "refused" in text:
        return "服务器拒绝连接：服务暂时不可用，请稍后重试"
    if isinstance(reason, ConnectionResetError) or "reset" in text or "forcibly closed" in text:
        return "连接被远端重置：网络不稳定或被代理/防火墙中断，请稍后重试"
    if "proxy" in text:
        return "代理连接失败：请检查系统代理设置，或关闭代理后重试"
    if (isinstance(reason, OSError) and getattr(reason, "winerror", None) == 12029) \
            or "winerror 12029" in text or "12029" in text:
        return "无法连接服务器：电脑当前似乎没有联网，请检查网络后重试"
    # WinError 100xx 系列统一提示检查网络
    if "winerror 100" in text or "network is unreachable" in text or "unreachable" in text:
        return "网络不可达：请检查电脑是否已联网、Wi-Fi/网线是否正常"
    return f"网络连接异常：{reason}"


def http_post(url: str, headers: dict, data: Optional[dict] = None,
              timeout: int = 30, retries: int = 3) -> tuple[int, dict]:
    body = json.dumps(data or {}).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
                return resp.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"raw": text}
            if e.code in RETRYABLE_STATUS and attempt < retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 0.8)
                hint = "服务器繁忙（限流）" if e.code == 429 else f"服务器临时错误 HTTP {e.code}"
                log.warning(f"请求 {url} {hint}，{delay:.1f}s 后重试"
                            f"（{attempt + 1}/{retries}）")
                time.sleep(delay)
                continue
            return e.code, parsed
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 0.8)
                log.warning(f"请求 {url} 网络异常（{_friendly_net_error(e)}），"
                            f"{delay:.1f}s 后重试（{attempt + 1}/{retries}）")
                time.sleep(delay)
                continue
    raise ConnectionError(f"{_friendly_net_error(last_err)}（已重试 {retries} 次仍失败）")


def build_headers(token: str, device_id: str, region: str = "") -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "x-device-id": device_id,
        "x-device-brand": "Windows",
        "x-device-type": "windows",
        "x-os-version": platform.version(),
        "x-app-version": APP_VERSION,
        "X-User-Region": region or "CN",
    }


def refresh_token(refresh_token_str: str) -> dict:
    log.info("登录凭证已过期，正在自动刷新...")
    body = {"refresh_token": refresh_token_str, "grant_type": "refresh_token"}
    status, resp = http_post(REFRESH_TOKEN_URL,
                             {"Content-Type": "application/json"}, body)
    if status != 200:
        raise RuntimeError("刷新凭证失败，请重新打开 TraeWork CN 登录。")
    data_obj = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    new_token = (resp.get("access_token") or resp.get("token")
                 or data_obj.get("access_token") or data_obj.get("token"))
    new_refresh = (resp.get("refresh_token") or resp.get("refreshToken")
                   or data_obj.get("refresh_token")
                   or data_obj.get("refreshToken"))
    if not new_token:
        raise RuntimeError("刷新凭证响应异常")
    out: dict[str, Any] = {"token": new_token,
                          "refreshToken": new_refresh or refresh_token_str}
    expired_at = (resp.get("expiredAt") or resp.get("expires_at")
                  or data_obj.get("expiredAt") or data_obj.get("expires_at"))
    if expired_at:
        out["expiredAt"] = expired_at
    ttl = (resp.get("expires_in") or resp.get("expiresIn")
           or data_obj.get("expires_in") or data_obj.get("expiresIn"))
    if ttl:
        out["expires_in"] = ttl
    return out


def is_token_expired(auth_info: dict) -> bool:
    expired_at = auth_info.get("expiredAt")
    if expired_at:
        try:
            dt = datetime.fromisoformat(expired_at.replace("Z", "+00:00"))
            return datetime.now(timezone.utc).timestamp() > dt.timestamp() - 300
        except Exception:
            pass
    token = auth_info.get("token", "")
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            data = json.loads(base64.b64decode(payload))
            return time.time() > data.get("exp", 0) - 300
        except Exception:
            return False
    return False


def _http_status_hint(status: int, action: str) -> str:
    """业务接口收到非 200 状态码时的中文说明。"""
    if status == 429:
        return f"{action}失败：请求过于频繁被限流，请稍后再试"
    if status in (401, 403):
        return f"{action}失败：登录已失效或无权限（HTTP {status}），请重新登录该账号后再保存一次"
    if status >= 500:
        return f"{action}失败：对方服务器暂时异常（HTTP {status}），请稍后再试"
    if status == 404:
        return f"{action}失败：接口地址不存在（HTTP 404），可能是客户端升级后接口变更"
    return f"{action}失败（HTTP {status}）"


def check_status(token: str, device_id: str, region: str = "") -> dict:
    status, resp = http_post(CHECKIN_STATUS_URL,
                             build_headers(token, device_id, region),
                             {"req_source": REQ_SOURCE})
    if status != 200:
        raise RuntimeError(_http_status_hint(status, "查询签到状态"))
    code = resp.get("code")
    if isinstance(code, (int, float)) and code != 0:
        raise RuntimeError(f"查询返回错误码 {code}：{resp.get('message', '')}")
    return resp


def claim_checkin(token: str, device_id: str, region: str = "") -> dict:
    status, resp = http_post(CHECKIN_CLAIM_URL,
                             build_headers(token, device_id, region),
                             {"req_source": REQ_SOURCE})
    if status != 200:
        raise RuntimeError(_http_status_hint(status, "领取积分"))
    code = resp.get("code")
    if isinstance(code, (int, float)) and code != 0:
        raise RuntimeError(f"领取失败（{code}）：{resp.get('message', '')}")
    return resp


def run_checkin_with_credential(auth_info: dict, device_id: str,
                                username: str = "",
                                status_only: bool = False,
                                on_token_refreshed=None) -> dict:
    """
    用显式传入的凭证执行签到（当前客户端账号与已保存快照共用此核心逻辑）。

    auth_info 至少含 token / refreshToken / expiredAt；
    on_token_refreshed(token, refreshToken, expiredAt) 在 token 自动续期后回调，
    供批量签到回写快照。
    返回结构化结果字典：
      ok, message, already, credits, extra_credits, username, checked_in, enabled
    """
    result: dict[str, Any] = {"ok": False, "message": "", "already": False}
    result["platform"] = PLAT_TRAEWORK
    result["platform_label"] = "TraeWork"
    result["username"] = username or auth_info.get("account", {}) \
        .get("username", "未知用户")
    region = auth_info.get("userRegion", {}).get("region", "") \
        or auth_info.get("region", "") or "CN"

    token = auth_info.get("token", "")
    if not token:
        result["message"] = "凭证中缺少 Token，请重新保存该账号。"
        log.error(result["message"])
        return result

    if is_token_expired(auth_info):
        try:
            refreshed = refresh_token(auth_info.get("refreshToken", ""))
            token = refreshed["token"]
            new_rt = refreshed.get("refreshToken", auth_info.get("refreshToken", ""))
            # 优先采用响应里的真实过期时间，没有再回退为 +1 小时
            new_exp = refreshed.get("expiredAt") or refreshed.get("expires_at", "")
            if not new_exp:
                ttl = refreshed.get("expires_in") or refreshed.get("expiresIn")
                try:
                    ttl_sec = int(ttl) if ttl else 3600
                except (TypeError, ValueError):
                    ttl_sec = 3600
                new_exp = datetime.fromtimestamp(
                    time.time() + ttl_sec, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            auth_info["token"] = token
            auth_info["refreshToken"] = new_rt
            auth_info["expiredAt"] = new_exp
            if on_token_refreshed:
                try:
                    on_token_refreshed(token, new_rt, new_exp)
                except Exception:
                    pass
        except Exception as e:
            result["message"] = str(e)
            log.error(result["message"])
            return result

    try:
        st = check_status(token, device_id, region)
    except Exception as e:
        result["message"] = f"查询签到状态失败：{e}"
        log.error(result["message"])
        return result

    enabled = st.get("enable", False)
    checked_in = st.get("checked_in", False)
    credits = st.get("credits")
    extra = st.get("extra_credits")
    result.update({"enabled": enabled, "checked_in": checked_in,
                   "credits": credits, "extra_credits": extra})

    if status_only:
        result["ok"] = True
        result["message"] = "已签到" if checked_in else "今日尚未签到"
        return result

    if checked_in:
        result["ok"] = True
        result["already"] = True
        result["message"] = "今日已签到"
        return result

    if not enabled:
        result["message"] = "签到功能未启用"
        return result

    try:
        claim = claim_checkin(token, device_id, region)
        result["ok"] = True
        gained = claim.get("credits", credits)
        g_extra = claim.get("extra_credits", extra)
        result["credits"] = gained
        result["extra_credits"] = g_extra
        msg = "签到成功"
        if gained is not None:
            msg += f"，获得 {gained} 积分"
            if g_extra:
                msg += f"（含额外奖励 {g_extra}）"
        else:
            # 防假成功（t23）：领取接口 200 但既无积分也无任何成功信号，
            # 极可能是官方接口/客户端改版导致旧解析规则失效
            claim_confirmed = bool(claim.get("checked_in")
                                   or claim.get("success") is True
                                   or claim.get("claimed") is True)
            if not claim_confirmed:
                result["suspicious"] = True
                msg = ("⚠ 签到流程已执行，但未抓到积分/成功信号，"
                       "疑似客户端改版，规则待更新，请人工打开客户端确认")
                log.warning(f"[{result['username']}] {msg}；"
                            f"领取响应字段：{sorted(claim.keys())}")
        result["message"] = msg
        log.info(f"[{result['username']}] {msg}")
        return result
    except Exception as e:
        # 兜底：领取请求可能已在服务端生效（超时等），回查状态确认真实结果
        try:
            recheck = check_status(token, device_id, region)
            if recheck.get("checked_in"):
                result["ok"] = True
                result["already"] = True
                result["checked_in"] = True
                result["credits"] = recheck.get("credits")
                result["extra_credits"] = recheck.get("extra_credits")
                result["message"] = "今日已签到（领取响应异常，回查确认已到账）"
                log.warning(f"[{result['username']}] 领取异常但回查已签：{e}")
                return result
        except Exception as re:
            log.error(f"[{result['username']}] 领取后回查也失败：{re}")
        result["message"] = f"领取积分失败：{e}"
        log.error(result["message"])
        return result


def run_checkin(status_only: bool = False) -> dict:
    """读取当前客户端登录凭证并签到（单账号传统流程）。"""
    result: dict[str, Any] = {"ok": False, "message": "", "already": False}
    try:
        auth_info = load_auth_info()
        device_id = load_device_id()
    except Exception as e:
        result["message"] = str(e)
        log.error(result["message"])
        return result
    username = auth_info.get("account", {}).get("username", "未知用户")
    return run_checkin_with_credential(auth_info, device_id,
                                       username=username, status_only=status_only)


# ──────────────────────── WorkBuddy 凭证与签到 ────────────────────────

# 明文会话文件（桌面端登录后自动写入并持续刷新），与开源方案同源
WB_AUTH_REL = Path("CodeBuddyExtension") / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
# DPAPI 降级：Electron Local State 主密钥 + vscdb 内 v10 密文
WB_LOCAL_STATE_REL = Path("CodeBuddy CN") / "Local State"
WB_VSCDB_REL = Path("CodeBuddy CN") / "User" / "globalStorage" / "state.vscdb"
WB_SECRET_KEY = ('secret://{"extensionId":"tencent-cloud.coding-copilot",'
                 '"key":"planning-genie.new.accessTokencn"}')


def wb_auth_file_candidates() -> list[Path]:
    """返回 WorkBuddy 明文会话文件候选路径（Windows / macOS / Linux）。"""
    cands: list[Path] = []
    env_file = os.environ.get("WORKBUDDY_AUTH_FILE")
    if env_file:
        cands.append(Path(env_file))
    local = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    if local:
        cands.append(Path(local) / WB_AUTH_REL)
    cands.append(home / "Library" / "Application Support" / WB_AUTH_REL)
    cands.append(home / ".config" / WB_AUTH_REL)
    cands.append(home / ".workbuddy" / "auth" / "workbuddy-desktop.info")
    return cands


def wb_find_auth_file() -> Optional[Path]:
    for p in wb_auth_file_candidates():
        try:
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def wb_load_session_file(path: Path, retries: int = 4) -> dict:
    """读取明文会话；桌面端刷新时可能短暂加锁或写入一半，需重试。"""
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data.get("auth", {}).get("accessToken"):
                raise ValueError("会话文件内容不完整")
            return data
        except (PermissionError, OSError, json.JSONDecodeError, ValueError) as e:
            last_err = e
            time.sleep(0.25 * (i + 1))
    raise FileNotFoundError(f"读取 WorkBuddy 会话失败：{last_err}")


def wb_local_state_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / WB_LOCAL_STATE_REL
    return p if p.exists() else None


def wb_vscdb_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / WB_VSCDB_REL
    return p if p.exists() else None


def wb_get_master_key() -> bytes:
    """从 CodeBuddy CN 的 Local State 解出 Chromium os_crypt 主密钥。"""
    ls_path = wb_local_state_path()
    if not ls_path:
        raise FileNotFoundError("未找到 WorkBuddy 的 Local State")
    with open(ls_path, "r", encoding="utf-8") as f:
        ls = json.load(f)
    enc = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise ValueError("Local State 主密钥格式异常")
    return dpapi_decrypt_blob(enc[5:])


def dpapi_decrypt_blob(raw: bytes) -> bytes:
    """对一段 DPAPI 密文执行 CryptUnprotectData（区别于 base64 版 dpapi_decrypt）。"""
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI 解密失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def wb_decrypt_v10(blob: bytes, key: bytes) -> bytes:
    """解密 Chromium v10/v20：3 字节前缀 + 12 字节 nonce + 密文 + 16 字节 GCM tag。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] not in (b"v10", b"v20"):
        raise ValueError("不是 v10/v20 密文")
    nonce, ct = blob[3:15], blob[15:]
    return AESGCM(key).decrypt(nonce, ct, None)


def wb_extract_blob(v: Any) -> bytes:
    """state.vscdb 中 secret 值可能是 Node Buffer JSON，也可能是裸 v10 字节/字符串。"""
    if isinstance(v, str):
        s = v.strip()
        try:
            o = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            o = None
        if isinstance(o, dict) and o.get("type") == "Buffer":
            return bytes(o.get("data", []))
        return s.encode("latin1", errors="ignore")
    if isinstance(v, (bytes, bytearray)):
        try:
            o = json.loads(bytes(v).decode("utf-8"))
            if isinstance(o, dict) and o.get("type") == "Buffer":
                return bytes(o.get("data", []))
        except Exception:
            pass
        return bytes(v)
    raise ValueError("无法识别的密文包装格式")


def wb_load_session_vscdb() -> dict:
    """DPAPI 降级路径：从 state.vscdb 解密当前会话。拷贝后读取以避免占用。"""
    import sqlite3
    import tempfile
    src = wb_vscdb_path()
    if not src:
        raise FileNotFoundError("未找到 WorkBuddy 的 state.vscdb")
    key = wb_get_master_key()
    tmp_dir = Path(tempfile.gettempdir())
    db_copy = tmp_dir / "wb_state_checkin.vscdb"
    try:
        shutil_copy_file(src, db_copy)
    except Exception:
        db_copy = src  # 拷贝失败则直接只读打开
    con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (WB_SECRET_KEY,)).fetchone()
    finally:
        con.close()
    if not row:
        raise KeyError("state.vscdb 中未找到 WorkBuddy 登录凭证")
    plain = wb_decrypt_v10(wb_extract_blob(row[0]), key)
    data = json.loads(plain.decode("utf-8"))
    if not data.get("auth", {}).get("accessToken"):
        raise ValueError("解密得到的会话不完整")
    return data


def shutil_copy_file(src: Path, dst: Path) -> None:
    import shutil
    shutil.copy2(src, dst)


def load_wb_session() -> dict:
    """优先明文会话文件，失败时降级到 DPAPI 解密 vscdb。"""
    path = wb_find_auth_file()
    if path:
        try:
            return wb_load_session_file(path)
        except Exception as e:
            log.warning(f"明文 WorkBuddy 会话读取失败，尝试 DPAPI 降级：{e}")
    return wb_load_session_vscdb()


def wb_is_logged_in() -> tuple[bool, str]:
    try:
        load_wb_session()
        return True, ""
    except Exception as e:
        return False, str(e)


def wb_session_secret_obj(sess: dict) -> dict:
    """从会话中抽取需要加密快照的最小字段。"""
    auth = sess.get("auth", {})
    acc = sess.get("account", {})
    return {
        "token": auth.get("accessToken", ""),
        "refreshToken": auth.get("refreshToken", ""),
        "expiresAt": auth.get("expiresAt", 0),
        "refreshExpiresAt": auth.get("refreshExpiresAt", 0),
        "uid": acc.get("uid", ""),
        "domain": auth.get("domain", ""),
        "enterpriseId": acc.get("enterpriseId", "") or acc.get("tenantId", ""),
        "tenantId": acc.get("tenantId", "") or acc.get("enterpriseId", ""),
    }


def wb_account_key(sess: dict) -> str:
    uid = sess.get("account", {}).get("uid", "")
    if not uid:
        raise ValueError("会话中缺少账号 uid")
    return f"wb:{uid}"


def wb_account_display(sess: dict) -> str:
    acc = sess.get("account", {})
    return acc.get("nickname") or acc.get("phoneNumber") or acc.get("uin") \
        or acc.get("uid", "WorkBuddy 账号")


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


def wb_detect_version() -> str:
    """从已安装的 WorkBuddy.exe 文件版本号动态获取版本，读不到时回退默认。"""
    global _wb_ua_cache
    if _wb_ua_cache is not None:
        return _wb_ua_cache
    ver = ""
    # 优先读 Windows 文件版本资源
    try:
        exes: list[Path] = []
        for env_name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if not base:
                continue
            b = Path(base)
            if env_name == "LOCALAPPDATA":
                exes.extend(b.glob("Programs/WorkBuddy*/WorkBuddy.exe"))
                exes.extend(b.glob("WorkBuddy*/WorkBuddy.exe"))
            else:
                exes.extend(b.glob("WorkBuddy*/WorkBuddy.exe"))
        for p in [Path("G:/workb/WorkBuddy/WorkBuddy.exe")] + exes:
            if p.exists():
                size = (ctypes.windll.version.GetFileVersionInfoSizeW(str(p), None)
                        if os.name == "nt" else 0)
                if not size:
                    continue
                buf = ctypes.create_string_buffer(size)
                h = ctypes.windll.kernel32
                if ctypes.windll.version.GetFileVersionInfoW(str(p), 0, size, buf):
                    trans = ctypes.c_void_p()
                    ulen = ctypes.c_uint()
                    if ctypes.windll.version.VerQueryValueW(
                            buf, "\\VarFileInfo\\Translation",
                            ctypes.byref(trans), ctypes.byref(ulen)) and ulen.value:
                        arr = ctypes.cast(trans,
                            ctypes.POINTER(ctypes.c_ushort * 2)).contents
                        sub = (f"\\StringFileInfo\\{arr[0]:04x}{arr[1]:04x}"
                               f"\\ProductVersion")
                        val = ctypes.c_wchar_p()
                        vlen = ctypes.c_uint()
                        if ctypes.windll.version.VerQueryValueW(
                                buf, sub, ctypes.byref(val), ctypes.byref(vlen)) \
                                and val.value:
                            ver = val.value
                            break
    except Exception:
        ver = ""
    # 兜底：resources/app/package.json
    if not ver:
        try:
            for base_env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
                base = os.environ.get(base_env)
                if not base:
                    continue
                b = Path(base)
                cands = list(b.glob("**/WorkBuddy/resources/app/package.json"))
                for pj in cands[:3]:
                    if pj.exists():
                        with open(pj, "r", encoding="utf-8") as f:
                            ver = json.load(f).get("version", "")
                        if ver:
                            break
                if ver:
                    break
        except Exception:
            ver = ""
    if ver:
        # 规范化为三段版本号（如 5.5.6.0 -> 5.5.6）
        parts = ver.strip().split(".")
        ver = ".".join(parts[:3]) if len(parts) >= 3 else ver.strip()
    _wb_ua_cache = ver or "5.5.6"
    return _wb_ua_cache


def wb_user_agent() -> str:
    return f"WorkBuddy/{wb_detect_version()}"


def wb_build_headers(secret: dict) -> dict:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {secret.get('token', '')}",
        "X-User-Id": secret.get("uid", ""),
        "User-Agent": wb_user_agent(),
    }
    if secret.get("domain"):
        h["X-Domain"] = secret["domain"]
    if secret.get("enterpriseId"):
        h["X-Enterprise-Id"] = str(secret["enterpriseId"])
    if secret.get("tenantId"):
        h["X-Tenant-Id"] = str(secret["tenantId"])
    return h


def wb_is_expired(secret: dict, skew: int = 300) -> bool:
    exp = secret.get("expiresAt")
    try:
        if exp:
            # 桌面端存的是毫秒时间戳
            exp_ms = int(exp)
            if exp_ms > 10_000_000_000:
                return time.time() * 1000 > exp_ms - skew * 1000
            return time.time() > exp_ms - skew
    except (TypeError, ValueError):
        pass
    token = secret.get("token", "")
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            data = json.loads(base64.b64decode(payload))
            return time.time() > data.get("exp", 0) - skew
        except Exception:
            return False
    return False


def wb_refresh_token(secret: dict) -> dict:
    """用 refreshToken 换新 accessToken；返回更新后的 secret 对象。"""
    log.info("WorkBuddy 凭证即将过期，正在自动刷新...")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Refresh-Token": secret.get("refreshToken", ""),
        "X-Auth-Refresh-Source": "plugin",
        "User-Agent": wb_user_agent(),
    }
    if secret.get("token"):
        headers["Authorization"] = f"Bearer {secret['token']}"
    if secret.get("domain"):
        headers["X-Domain"] = secret["domain"]
    status, resp = http_post(WB_REFRESH_TOKEN_URL, headers, {})
    if status in (401, 403):
        raise RuntimeError("WorkBuddy 登录态已失效，请重新打开桌面端登录后再次保存该账号。")
    if status != 200:
        raise RuntimeError(f"刷新 WorkBuddy 凭证失败 (HTTP {status})")
    new_auth = resp.get("data") if isinstance(resp.get("data"), dict) else None
    if not new_auth:
        new_auth = resp if isinstance(resp, dict) and resp.get("accessToken") else None
    if not new_auth or not new_auth.get("accessToken"):
        raise RuntimeError("刷新 WorkBuddy 凭证响应异常")
    now_ms = int(time.time() * 1000)
    expires_in = int(new_auth.get("expiresIn", 259200) or 259200)
    refresh_in = int(new_auth.get("refreshExpiresIn", 604799) or 604799)
    updated = dict(secret)
    updated["token"] = new_auth.get("accessToken")
    if new_auth.get("refreshToken"):
        updated["refreshToken"] = new_auth.get("refreshToken")
    updated["expiresAt"] = new_auth.get("expiresAt") or (now_ms + expires_in * 1000)
    updated["refreshExpiresAt"] = (new_auth.get("refreshExpiresAt")
                                   or (now_ms + refresh_in * 1000))
    if new_auth.get("domain"):
        updated["domain"] = new_auth["domain"]
    return updated


def wb_is_already_checked_in(code: Any, msg: str, data: Any) -> bool:
    """兼容三种已签形态：HTTP 400 code=10001、业务 code 1001、data=null。"""
    if code in (10001, 1001):
        return True
    if msg and ("已签到" in msg or "已领取" in msg or "明天再来" in msg):
        return True
    return False


def _wb_recheck(headers: dict) -> Optional[dict]:
    """领取后回查状态：已签返回结果片段，未签/回查失败返回 None。"""
    try:
        rc, rr = http_post(WB_CHECKIN_STATUS_URL, headers, {})
    except Exception:
        return None
    if rc in (401, 403):
        return None
    data = rr.get("data") if isinstance(rr, dict) else None
    if rc == 200 and isinstance(data, dict) and data.get("today_checked_in"):
        msg = "今日已签到（领取响应异常，回查确认已到账）"
        streak = data.get("streak_days")
        if streak:
            msg += f"，连续 {streak} 天"
        return {"ok": True, "already": True, "checked_in": True,
                "enabled": bool(data.get("active", True)),
                "credits": data.get("total_credits"), "message": msg}
    return None


def run_wb_checkin_with_credential(secret: dict, display_name: str = "",
                                   status_only: bool = False,
                                   on_token_refreshed=None) -> dict:
    """用快照凭证执行 WorkBuddy 签到，返回与 TraeWork 统一结构的结果。"""
    result: dict[str, Any] = {
        "ok": False, "message": "", "already": False,
        "platform": PLAT_WORKBUDDY, "platform_label": "WorkBuddy",
        "username": display_name or secret.get("uid") or "WorkBuddy 账号",
    }
    if not secret.get("token"):
        result["message"] = "凭证中缺少 Token，请重新保存该账号。"
        return result

    if wb_is_expired(secret):
        try:
            secret = wb_refresh_token(secret)
            if on_token_refreshed:
                try:
                    on_token_refreshed(secret)
                except Exception:
                    pass
        except Exception as e:
            result["message"] = str(e)
            log.error(result["message"])
            return result

    headers = wb_build_headers(secret)
    try:
        st_code, st_resp = http_post(WB_CHECKIN_STATUS_URL, headers, {})
    except Exception as e:
        result["message"] = f"查询签到状态失败：{e}"
        return result
    if st_code in (401, 403):
        result["message"] = "WorkBuddy 登录态已失效，请重新登录后再次保存。"
        return result

    st_data = st_resp.get("data") if isinstance(st_resp, dict) else None
    st_env_code = st_resp.get("code") if isinstance(st_resp, dict) else None
    if st_code != 200 or not isinstance(st_data, dict):
        # 某些情况下状态接口直接回已签
        msg = (st_resp.get("msg") or st_resp.get("message") or "") if isinstance(st_resp, dict) else ""
        if wb_is_already_checked_in(st_env_code, msg, st_data):
            result.update(ok=True, already=True, checked_in=True,
                          credits=None, message="今日已签到")
            return result
        result["message"] = f"查询签到状态失败 (HTTP {st_code} {st_env_code})"
        return result

    active = st_data.get("active", True)
    checked_in = bool(st_data.get("today_checked_in"))
    credits = st_data.get("total_credits")
    today_credit = st_data.get("today_credit")
    result.update({"enabled": bool(active), "checked_in": checked_in,
                   "credits": credits})

    if status_only:
        result["ok"] = True
        streak_days = st_data.get("streak_days")
        if not active:
            result["message"] = "签到活动暂未开启"
        elif checked_in:
            result["message"] = "今日已签到" + (
                f"，连续 {streak_days} 天" if streak_days else "")
            if today_credit is not None:
                result["message"] += f"，今日 {today_credit} 积分"
            result["already"] = True
        else:
            result["message"] = "今日尚未签到"
        return result

    if not active:
        result["message"] = "签到活动暂未开启或已结束"
        return result

    if checked_in:
        result.update(ok=True, already=True,
                      message="今日已签到" + (
                          f"，连续 {st_data.get('streak_days')} 天"
                          if st_data.get('streak_days') else ""))
        return result

    # 执行签到
    try:
        c_code, c_resp = http_post(WB_CHECKIN_CLAIM_URL, headers, {})
    except Exception as e:
        # 兜底：网络异常可能服务端已到账，回查状态确认真实结果
        recheck = _wb_recheck(headers)
        if recheck is not None:
            result.update(recheck)
            if result.get("ok"):
                log.warning(f"[WorkBuddy/{result['username']}] "
                            f"签到异常但回查已签：{e}")
                return result
        result["message"] = f"签到请求失败：{e}"
        return result
    if c_code in (401, 403):
        result["message"] = "WorkBuddy 登录态已失效，请重新登录后再次保存。"
        return result
    c_data = c_resp.get("data") if isinstance(c_resp, dict) else None
    c_env = c_resp.get("code") if isinstance(c_resp, dict) else None
    c_msg = (c_resp.get("msg") or c_resp.get("message") or "") if isinstance(c_resp, dict) else ""
    if wb_is_already_checked_in(c_env, c_msg, c_data):
        result.update(ok=True, already=True, checked_in=True, message="今日已签到")
        return result
    if c_code != 200 or not isinstance(c_data, dict):
        # 兜底：非预期响应也回查一次，避免漏报已到账
        recheck = _wb_recheck(headers)
        if recheck is not None and recheck.get("ok"):
            log.warning(f"[WorkBuddy/{result['username']}] "
                        f"签到响应异常(HTTP {c_code})但回查已签")
            result.update(recheck)
            return result
        result["message"] = f"签到失败（HTTP {c_code} {c_env} {c_msg}）".strip()
        return result

    gained = c_data.get("credit")
    streak = c_data.get("streak_days")
    result.update(ok=True, checked_in=True, credits=c_data.get("total_credits", credits))
    msg = "签到成功"
    if gained is not None:
        msg += f"，获得 {gained} 积分"
        if streak:
            msg += f"，连续签到 {streak} 天"
    else:
        # 防假成功（t23）：领取 200 但无积分，且无任何已到账/成功确认信号
        confirmed = bool(c_data.get("today_checked_in")
                         or c_data.get("checked_in")
                         or c_data.get("success") is True
                         or c_data.get("total_credits") is not None)
        if not confirmed:
            result["suspicious"] = True
            msg = ("⚠ 签到流程已执行，但未抓到积分/成功信号，"
                   "疑似客户端改版，规则待更新，请人工打开客户端确认")
            log.warning(f"[WorkBuddy/{result['username']}] {msg}；"
                        f"领取响应字段：{sorted(c_data.keys())}")
    result["message"] = msg
    log.info(f"[WorkBuddy/{result['username']}] {msg}")
    return result


def run_wb_checkin_live(status_only: bool = False) -> dict:
    """读取当前桌面端登录的 WorkBuddy 账号并签到（单账号即时流程）。"""
    try:
        sess = load_wb_session()
    except Exception as e:
        return {"ok": False, "message": str(e), "already": False,
                "platform": PLAT_WORKBUDDY, "platform_label": "WorkBuddy",
                "username": "WorkBuddy 账号"}
    secret = wb_session_secret_obj(sess)
    return run_wb_checkin_with_credential(
        secret, display_name=wb_account_display(sess), status_only=status_only)


# ──────────────────────────── 批量签到（多账号） ────────────────────────────

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


# ──────────────────────────── 微信推送（Server酱 / PushPlus） ────────────────────────────

def _http_post_raw(url: str, body: bytes, headers: dict,
                   timeout: int = 20) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, {"raw": text}
    except urllib.error.URLError as e:
        raise ConnectionError(_friendly_net_error(e)) from e


def _extract_webhook_param(raw: str, name: str) -> str:
    """从机器人完整 webhook URL 中提取参数（如 key= / access_token=），否则原样返回。"""
    raw = raw.strip()
    marker = f"{name}="
    if marker in raw:
        tail = raw.split(marker, 1)[1]
        return tail.split("&", 1)[0].strip()
    return raw


def _md_to_plain(content_md: str) -> str:
    """Markdown 极简转纯文本，供企业微信/钉钉 text 消息使用。"""
    txt = re.sub(r"^#{1,6}\s*", "", content_md, flags=re.MULTILINE)
    txt = re.sub(r"\*\*(.+?)\*\*", r"\1", txt)
    txt = txt.replace("---\n", "").replace("\n\n\n+", "\n\n")
    return txt.strip()


def _robot_sign(secret: str) -> tuple[str, str]:
    """群机器人加签：返回 (timestamp毫秒串, URL编码后的sign)，钉钉/企微算法一致。"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    return ts, sign


def _push_one_channel(channel: str, key: str, secret: str,
                      title: str, content_md: str) -> tuple[bool, str]:
    """向单个渠道推送一条消息，返回 (是否成功, 说明)。"""
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key:
        return False, "未填写凭据"
    try:
        if channel == "wecom":
            webhook_key = _extract_webhook_param(key, "key")
            url = (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                   f"?key={webhook_key}")
            if secret:
                ts, sign = _robot_sign(secret)
                url += f"&timestamp={ts}&sign={sign}"
            text = title + "\n\n" + _md_to_plain(content_md)
            body = json.dumps(
                {"msgtype": "text", "text": {"content": text}},
                ensure_ascii=False).encode("utf-8")
            status, resp = _http_post_raw(
                url, body, {"Content-Type": "application/json"})
            if status == 200 and resp.get("errcode") == 0:
                return True, "企业微信机器人推送成功"
            return False, f"企业微信返回：{resp.get('errmsg') or status}"

        if channel == "dingtalk":
            token = _extract_webhook_param(key, "access_token")
            url = ("https://oapi.dingtalk.com/robot/send"
                   f"?access_token={token}")
            if secret:
                ts, sign = _robot_sign(secret)
                url += f"&timestamp={ts}&sign={sign}"
            text = title + "\n\n" + _md_to_plain(content_md)
            body = json.dumps(
                {"msgtype": "text", "text": {"content": text}},
                ensure_ascii=False).encode("utf-8")
            status, resp = _http_post_raw(
                url, body, {"Content-Type": "application/json"})
            if status == 200 and resp.get("errcode") == 0:
                return True, "钉钉机器人推送成功"
            return False, f"钉钉返回：{resp.get('errmsg') or status}"

        if channel == "pushplus":
            # Markdown 极简转 HTML，保证微信端可读
            html = content_md.replace("&", "&amp;").replace("<", "&lt;") \
                .replace(">", "&gt;")
            html = html.replace("\n", "<br/>")
            body = json.dumps({
                "token": key, "title": title,
                "content": html, "template": "html",
            }, ensure_ascii=False).encode("utf-8")
            status, resp = _http_post_raw(
                "https://www.pushplus.plus/send", body,
                {"Content-Type": "application/json"})
            code = resp.get("code")
            if status == 200 and code == 200:
                return True, "PushPlus 推送成功"
            return False, f"PushPlus 返回：{resp.get('msg') or resp.get('message') or code}"

        if channel == "serverchan":
            form = urllib.parse.urlencode(
                {"title": title, "desp": content_md}).encode("utf-8")
            status, resp = _http_post_raw(
                f"https://sctapi.ftqq.com/{key}.send", form,
                {"Content-Type": "application/x-www-form-urlencoded"})
            if status == 200 and resp.get("code") == 0:
                return True, "Server酱推送成功"
            msg = resp.get("message") or resp.get("error") or f"HTTP {status}"
            return False, f"Server酱返回：{msg}"

        return False, f"未知渠道：{channel}"
    except Exception as e:
        return False, f"{PUSH_CHANNEL_LABELS.get(channel, channel)}推送失败：{e}"


def load_push_history(limit: int = 100) -> list[dict]:
    """读取推送历史（按时间倒序），最多返回 limit 条。只含标题/渠道/结果，不含正文。"""
    data = _load_json_file(PUSH_LOG_FILE)
    items = data.get("items")
    if not isinstance(items, list):
        return []
    out = [it for it in items if isinstance(it, dict)]
    out.sort(key=lambda it: str(it.get("ts") or ""), reverse=True)
    return out[:limit]


def clear_push_history() -> int:
    """清空推送历史，返回被清除的条数（文件不存在或异常时返回 0）。"""
    try:
        n = len(load_push_history(limit=100000))
        _save_json_file(PUSH_LOG_FILE, {"items": []})
        return n
    except Exception as e:
        log.warning(f"清空推送历史失败：{e}")
        return 0


def record_push_history(title: str, targets: list[str], success: bool,
                        detail: str, kind: str = "签到结果") -> None:
    """追加一条推送历史（90 天 / 300 条上限，异常静默，绝不影响主流程）。"""
    try:
        data = _load_json_file(PUSH_LOG_FILE)
        items = data.get("items")
        if not isinstance(items, list):
            items = []
        now = datetime.now()
        items.append({
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "kind": str(kind or "签到结果"),
            "title": str(title or "")[:120],
            "channels": [PUSH_CHANNEL_LABELS.get(c, c) for c in targets if c],
            "ok": bool(success),
            "detail": str(detail or "")[:300],
        })
        cutoff = time.time() - PUSH_LOG_KEEP_DAYS * 86400
        kept = []
        for it in items:
            try:
                if datetime.strptime(it.get("ts", "")[:19],
                                     "%Y-%m-%d %H:%M:%S").timestamp() < cutoff:
                    continue
            except Exception:
                continue
            kept.append(it)
        if len(kept) > PUSH_LOG_KEEP_ITEMS:
            kept = kept[-PUSH_LOG_KEEP_ITEMS:]
        _save_json_file(PUSH_LOG_FILE, {"items": kept})
    except Exception as e:
        log.warning(f"写入推送历史失败（不影响推送）：{e}")


def push_wechat(settings: dict, title: str, content_md: str,
                kind: str = "签到结果") -> tuple[bool, str]:
    """
    向所有已勾选且已配置凭据的渠道顺序推送（任一渠道失败不影响其他渠道）。
    返回 (是否至少一个渠道成功, 汇总说明)。兼容旧版单渠道设置结构。
    每次调用都会在本地推送历史中追加一条（不含正文与密钥）。
    """
    if settings.get("version") == SETTINGS_VERSION or "channels" in settings:
        channels = [c for c in settings.get("channels", [])
                    if c in PUSH_CHANNELS]
        creds = settings.get("creds") or {}
    else:
        # 旧版内存结构（如 GUI 中手工构造的 dict）
        channels = [settings.get("channel", "serverchan")]
        creds = {channels[0]: {"key": settings.get("key", ""), "secret": ""}}
    targets = [c for c in channels
               if (creds.get(c) or {}).get("key")]
    if not targets:
        record_push_history(title, channels, False, "未填写推送凭据", kind)
        return False, "未填写推送凭据"
    oks, msgs = [], []
    for ch in targets:
        ok, msg = _push_one_channel(
            ch, creds[ch].get("key", ""), creds[ch].get("secret", ""),
            title, content_md)
        oks.append(ok)
        msgs.append(("✓ " if ok else "✗ ") + msg)
    success = any(oks)
    detail = "；".join(msgs)
    if success and all(oks):
        summary = f"已推送 {len(oks)} 个渠道。"
    elif success:
        summary = f"部分渠道成功（{sum(oks)}/{len(oks)}）：{detail}"
    else:
        summary = detail
    record_push_history(title, targets, success, summary, kind)
    if success and all(oks):
        return True, summary
    return success, summary


_RELOGIN_HINTS = ("登录已失效", "登录态已失效", "请重新登录", "无权限", "凭证")


def _is_relogin_failure(r: dict) -> bool:
    """识别因登录态失效导致的失败（用于在推送中给出重新登录指引）。"""
    if r.get("ok"):
        return False
    msg = str(r.get("message") or "")
    return any(h in msg for h in _RELOGIN_HINTS)


def _result_account_display(r: dict) -> str:
    """报告中单个账号的展示名：[平台] 备注（用户名）。"""
    label = r.get("platform_label") or PLATFORM_LABELS.get(
        r.get("platform", PLAT_TRAEWORK), "")
    user = r.get("username") or r.get("display_name") or "未知账号"
    note = r.get("note")
    if note:
        user = f"{note}（{user}）"
    return f"[{label}] {user}" if label else user


def _result_detail(r: dict) -> str:
    """单个账号签到结果的一句话说明。"""
    if r.get("ok"):
        if r.get("platform") == PLAT_WORKBUDDY:
            return r.get("message", "完成")
        credits = r.get("credits")
        if credits is not None and r.get("checked_in"):
            extra = r.get("extra_credits")
            detail = f"今日已签到，当前 {credits} 积分"
            if extra:
                detail += f"（本次额外 {extra}）"
            return detail
        return r.get("message", "完成")
    return r.get("message", "失败")


def _stats_lookup() -> dict:
    """{identity: {streak, rate30, month_rate, month_success, month_total}}。"""
    out: dict[str, dict] = {}
    try:
        for a in history_summary(30).get("accounts") or []:
            out[history_identity(a.get("platform", ""), a.get("username", ""))] = {
                "streak": a.get("streak", 0), "rate30": a.get("rate", 0)}
    except Exception:
        pass
    try:
        for a in (history_monthly_stats(1)[0].get("accounts") or []):
            cell = out.setdefault(a.get("identity", ""), {})
            cell.update({"month_rate": a.get("rate", 0),
                         "month_success": a.get("success", 0),
                         "month_total": a.get("total", 0)})
    except Exception:
        pass
    return out


def _relogin_guide(relogin_items: list[dict]) -> list[str]:
    """登录失效账号的分步操作指引（推送内置）。"""
    if not relogin_items:
        return []
    lines = ["---", "### ⚠️ 登录已失效，请按以下步骤处理", ""]
    for r in relogin_items:
        label = r.get("platform_label") or PLATFORM_LABELS.get(
            r.get("platform", PLAT_TRAEWORK), "客户端")
        lines.append(f"**{_result_account_display(r)}**")
        lines.append(f"1. 在电脑上打开 {label} 桌面客户端；")
        lines.append("2. 重新登录该账号（登录成功后保持客户端在线 10 秒）；")
        lines.append("3. 打开本签到助手 → 在账号卡片点「保存当前登录账号」刷新凭据；")
        lines.append("4. 点「立即签到全部」验证，或等待次日自动执行。")
        lines.append("")
    return lines


def build_checkin_report(results: list[dict], test: bool = False) -> tuple[str, str]:
    """根据批量签到结果生成 (标题, Markdown 正文)。失败账号置顶并附处理指引。"""
    if test:
        return "签到助手：推送测试成功", (
            "这是一条测试消息。\n\n收到此消息说明推送渠道已配置成功，"
            "今后每日自动签到（TraeWork / WorkBuddy）的结果会汇总推送到这里。\n\n"
            "可同时开启多个推送渠道；登录态失效时消息内会附重新登录操作步骤。")
    total = len(results)
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = total - ok_n
    suspicious_n = sum(1 for r in results if r.get("ok") and r.get("suspicious"))
    now = datetime.now()
    today = now.strftime("%m-%d")
    plats = {r.get("platform", PLAT_TRAEWORK) for r in results}
    scope = "多平台" if len(plats) > 1 else PLATFORM_LABELS.get(
        next(iter(plats)), "签到")
    if fail_n == 0 and suspicious_n == 0:
        title = f"✅ {scope}签到全部成功（{ok_n}/{total}）{today}"
    elif fail_n == 0:
        title = f"⚠ {scope}签到完成但有 {suspicious_n} 个账号需人工确认 {today}"
    else:
        title = f"❌ {scope}签到有失败（成功 {ok_n}/{total}）{today}"
        if suspicious_n:
            title += f"，另有 {suspicious_n} 个需确认"

    # 失败（尤其登录失效）置顶，可疑成功紧随其后
    relogin = [r for r in results if _is_relogin_failure(r)]
    other_fail = [r for r in results if not r.get("ok") and not _is_relogin_failure(r)]
    suspicious = [r for r in results if r.get("ok") and r.get("suspicious")]
    oks = [r for r in results if r.get("ok") and not r.get("suspicious")]
    ordered = relogin + other_fail + suspicious + oks
    stats = _stats_lookup()

    lines = [f"**{now.strftime('%Y年%m月%d日')} 签到报告**", ""]
    lines.append(f"共 {total} 个账号：✅ 成功 {ok_n}，❌ 失败 {fail_n}。"
                 + (f"其中 {suspicious_n} 个未抓到成功信号，需人工确认。"
                    if suspicious_n else ""))
    # 积分速览：列出本次返回积分的账号
    credit_parts = []
    for r in oks:
        c = r.get("credits")
        if c is not None:
            credit_parts.append(f"{_result_account_display(r)} {c}")
    if credit_parts:
        lines.append("当前积分：" + "；".join(credit_parts) + "。")
    lines.append("")

    for r in ordered:
        if r.get("ok") and r.get("suspicious"):
            mark = "⚠️"
        else:
            mark = "✅" if r.get("ok") else "🔴"
        tag = "（登录失效）" if _is_relogin_failure(r) else ""
        if r.get("ok") and r.get("suspicious"):
            tag = "（疑似客户端改版，需确认）"
        lines.append(f"### {mark} {_result_account_display(r)}{tag}")
        lines.append(_result_detail(r))
        ident = history_identity(r.get("platform", ""),
                                 r.get("username") or r.get("display_name") or "")
        st = stats.get(ident)
        if st:
            bits = []
            if st.get("streak"):
                bits.append(f"连续签到 {st['streak']} 天")
            if st.get("month_total"):
                bits.append(f"本月成功率 {st['month_rate']}%"
                            f"（{st['month_success']}/{st['month_total']} 天）")
            elif st.get("rate30") is not None:
                bits.append(f"近30天成功率 {st['rate30']}%")
            if bits:
                lines.append("　" + "，".join(bits))
        lines.append("")

    lines.extend(_relogin_guide(relogin))
    if suspicious:
        lines += ["---", "### ⚠️ 疑似客户端 / 接口改版，请人工确认", "",
                  "以下账号的签到流程虽返回成功，但未抓到积分等成功信号，"
                  "可能是官方更新了签到接口：", ""]
        for r in suspicious:
            lines.append(f"- {_result_account_display(r)}")
        lines += [
            "",
            "1. 打开对应桌面客户端，手动查看今天是否真的已签到、积分是否到账；",
            "2. 若实际未签到，可先在客户端内手动签到；",
            "3. 多个账号同时出现此提示，通常说明客户端已改版，"
            "请更新本签到助手到最新版。", ""]
    if other_fail and not relogin:
        lines += ["---", "### 失败排查建议", "",
                  "1. 检查电脑网络是否正常（公司网络可能拦截接口）；",
                  "2. 打开本工具「打开运行日志」查看具体报错；",
                  "3. 稍后在工具内手动点「立即签到全部」重试。", ""]
    lines.append(f"签到时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    return title, "\n".join(lines)


def build_weekly_report() -> tuple[str, str]:
    """周一周报：近 7 天每账号签到情况 + 连续天数。"""
    now = datetime.now()
    rows = history_recent_rows(7)
    per: dict[str, dict] = {}
    for row in rows:
        for it in row.get("items", []):
            ident = history_identity(it.get("platform", ""),
                                     it.get("username", ""))
            cell = per.setdefault(ident, {
                "label": it.get("platform_label", ""),
                "username": it.get("username", ""), "ok": 0, "fail": 0})
            cell["ok" if it.get("ok") else "fail"] += 1
    stats = _stats_lookup()
    lines = [f"**签到周报（{rows[-1]['date'][5:] if rows else ''} ~ "
             f"{rows[0]['date'][5:] if rows else ''}）**", ""]
    if not per:
        lines.append("近 7 天没有签到记录。请确认定时任务是否正常开启。")
    for ident, c in sorted(per.items(), key=lambda kv: (kv[1]["label"],
                                                        kv[1]["username"])):
        st = stats.get(ident, {})
        lines.append(
            f"- [{c['label']}] {c['username']}：成功 {c['ok']} 天、失败 {c['fail']} 天，"
            f"当前连续 {st.get('streak', 0)} 天")
    lines.append("")
    lines.append(f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}")
    return f"📊 签到周报 {now.strftime('%m-%d')}", "\n".join(lines)


def build_monthly_report() -> tuple[str, str]:
    """月初月报：上月每账号成功率。"""
    now = datetime.now()
    months = history_monthly_stats(2)
    last = months[-1] if len(months) >= 2 else (months[0] if months else None)
    lines = [f"**{last['month']} 签到月报**" if last else "**签到月报**", ""]
    if not last or not last.get("accounts"):
        lines.append("上个月没有签到记录。")
    else:
        for a in last["accounts"]:
            lines.append(
                f"- [{a['platform_label']}] {a['username']}："
                f"签到 {a['success']} 天、失败 {a['fail']} 天，"
                f"成功率 {a['rate']}%")
    lines.append("")
    lines.append(f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}")
    mlabel = last["month"] if last else ""
    return f"📅 {mlabel} 签到月报", "\n".join(lines)


# ──────────────────────────── 定时任务管理 ────────────────────────────

def task_exists() -> bool:
    try:
        return _run(["schtasks", "/Query", "/TN", TASK_NAME], timeout=15).returncode == 0
    except Exception:
        return False


def _task_command() -> str:
    """定时任务要执行的命令。"""
    if is_frozen():
        return f'"{sys.executable}" --silent'
    return f'"{sys.executable}" "{exe_path()}" --silent'


def create_scheduled_task(run_time: str = "09:00") -> tuple[bool, str]:
    try:
        h, m = run_time.split(":")
        int(h); int(m)
    except Exception:
        return False, f"时间格式无效：{run_time}（应为 HH:MM）"

    if task_exists():
        delete_scheduled_task()

    cmd = ["schtasks", "/Create", "/TN", TASK_NAME,
           "/TR", _task_command(), "/SC", "DAILY", "/ST", run_time,
           "/F", "/RL", "LIMITED"]
    r = _run(cmd, timeout=30)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "拒绝访问" in err or "denied" in err.lower():
            return False, "权限不足，请以管理员身份运行本程序后重试。"
        return False, f"创建失败：{err or '未知错误'}"

    # 高级设置：
    #   1) StartWhenAvailable：错过的每日签到在开机后自动补跑；
    #   2) 额外增加「用户登录后延迟 3 分钟」触发器，作为关机一整天的兜底补签；
    #   3) 失败重试 3 次（间隔 10 分钟），电池供电也允许运行。
    # 静默运行内部会先判断今日是否已全部签到，已签则立即退出、不重复推送，
    # 因此登录触发器不会造成打扰。
    exe = _task_command().split(" ", 1)[0].strip('"')
    args = "--silent" if is_frozen() else f'"{exe_path()}" --silent'
    ps = (
        "$t = Get-ScheduledTask -TaskName '" + TASK_NAME + "' -ErrorAction SilentlyContinue; "
        "if ($t) { "
        "$daily = New-ScheduledTaskTrigger -Daily -At '" + run_time + "'; "
        "$logon = New-ScheduledTaskTrigger -AtLogOn; "
        "$logon.Delay = 'PT3M'; "
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 30) "
        "-MultipleInstances IgnoreNew; "
        "$a = New-ScheduledTaskAction -Execute '" + exe + "' -Argument '" + args + "'; "
        "Set-ScheduledTask -TaskName '" + TASK_NAME + "' -Action $a "
        "-Trigger @($daily, $logon) -Settings $s | Out-Null }"
    )
    try:
        r2 = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-Command", ps], timeout=25)
        if r2.returncode != 0:
            log.warning(f"登录触发器设置失败（每日定时仍有效）："
                        f"{(r2.stderr or '').strip()[:200]}")
    except Exception as e:
        log.warning(f"登录触发器设置异常（每日定时仍有效）：{e}")
    return True, f"已开启每日 {run_time} 自动签到（开机/登录后自动补签）"


def delete_scheduled_task() -> bool:
    try:
        return _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                    timeout=15).returncode == 0
    except Exception:
        return False


# ──────────────────────────── 健康自检中心 ────────────────────────────

HEALTH_OK = "ok"
HEALTH_WARN = "warn"
HEALTH_ERROR = "error"


def _health_timeout() -> float:
    """网络探测超时（秒），默认 4 秒；TRAESIGN_HEALTH_TIMEOUT 可压缩以便测试。"""
    try:
        return max(0.05, float(os.environ.get("TRAESIGN_HEALTH_TIMEOUT", "4")))
    except (TypeError, ValueError):
        return 4.0


def _tcp_reachable(host: str, port: int = 443) -> tuple[bool, str]:
    """TCP 连通性探测，返回 (是否连通, 耗时或错误说明)。"""
    t0 = time.time()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_health_timeout())
    try:
        sock.connect((host, port))
        return True, f"{(time.time() - t0) * 1000:.0f} ms"
    except Exception as e:
        return False, str(e) or e.__class__.__name__
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _scheduled_task_info() -> Optional[dict]:
    """读取计划任务上次/下次运行信息；任务不存在或读取失败返回 None。"""
    ps = (
        "$t = Get-ScheduledTask -TaskName '" + TASK_NAME + "' -ErrorAction SilentlyContinue; "
        "if ($t) { "
        "$i = Get-ScheduledTaskInfo -TaskName '" + TASK_NAME + "' -ErrorAction SilentlyContinue; "
        "$o = [ordered]@{ State = [string]$t.State; "
        "LastRunTime = if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }; "
        "LastTaskResult = if ($null -ne $i.LastTaskResult) { [int]$i.LastTaskResult } else { -1 }; "
        "NextRunTime = if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' } }; "
        "$o | ConvertTo-Json -Compress }"
    )
    try:
        r = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                  "-Command", ps], timeout=20)
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        data = json.loads(r.stdout.strip())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def run_health_checks() -> list[dict]:
    """执行全套健康自检，返回结构化结果列表。

    每项：{key, title, status(ok/warn/error), detail, hint}
    """
    results: list[dict] = []

    # 1) 每日自动签到计划任务
    try:
        if task_exists():
            info = _scheduled_task_info()
            last_result = None if not info else info.get("LastTaskResult")
            # 0/267009=任务尚未运行；267011=任务尚未到下次运行时间（均非错误）
            benign = (None, 0, 267009, 267011)
            last_run = (info or {}).get("LastRunTime") or ""
            nxt = (info or {}).get("NextRunTime") or ""
            state_txt = (info or {}).get("State") or ""
            detail_parts = ["计划任务已存在"]
            if state_txt:
                detail_parts.append(f"状态：{state_txt}")
            if last_run:
                detail_parts.append(f"上次运行：{last_run}")
            if last_result not in (None, -1):
                detail_parts.append(f"结果码：{last_result}")
            if nxt:
                detail_parts.append(f"下次运行：{nxt}")
            if last_result in benign:
                results.append({"key": "task", "title": "每日自动签到任务",
                                "status": HEALTH_OK,
                                "detail": "；".join(detail_parts), "hint": ""})
            else:
                results.append({"key": "task", "title": "每日自动签到任务",
                                "status": HEALTH_WARN,
                                "detail": "；".join(detail_parts),
                                "hint": ("计划任务上次执行返回了异常结果码，"
                                         "可手动签到一次或查看运行日志排查；"
                                         "267014 通常表示错过执行时间（开机后会自动补跑）。")})
        else:
            results.append({"key": "task", "title": "每日自动签到任务",
                            "status": HEALTH_WARN,
                            "detail": "未开启自动签到",
                            "hint": "在「每日自动签到」卡片设置时间并点「开启」后，"
                                    "将每天自动后台签到，无需手动操作。"})
    except Exception as e:
        results.append({"key": "task", "title": "每日自动签到任务",
                        "status": HEALTH_ERROR,
                        "detail": f"检查失败：{e}",
                        "hint": "可能是系统计划任务服务（Task Scheduler）异常，请重启电脑后重试。"})

    # 2) 两个平台的桌面客户端
    for plat, host in ((PLAT_TRAEWORK, "api.trae.cn"),
                       (PLAT_WORKBUDDY, "copilot.tencent.com")):
        label = PLATFORM_LABELS.get(plat, plat)
        try:
            exes = find_platform_exes(plat)
            if exes:
                results.append({
                    "key": f"client_{plat}", "title": f"{label} 桌面客户端",
                    "status": HEALTH_OK,
                    "detail": f"已安装：{exes[0]}", "hint": ""})
            else:
                accounts = [a for a in list_accounts() if a.get("platform") == plat]
                if accounts:
                    results.append({
                        "key": f"client_{plat}", "title": f"{label} 桌面客户端",
                        "status": HEALTH_ERROR,
                        "detail": f"未检测到客户端，但保存了 {len(accounts)} 个该平台账号",
                        "hint": (f"请重新安装 {label} 桌面端并重新登录，"
                                 "否则刷新凭据和启动引导功能无法使用。")})
                else:
                    results.append({
                        "key": f"client_{plat}", "title": f"{label} 桌面客户端",
                        "status": HEALTH_WARN,
                        "detail": "未检测到客户端（也未保存该平台账号）",
                        "hint": f"如不使用 {label}，可忽略此项。"})
        except Exception as e:
            results.append({"key": f"client_{plat}", "title": f"{label} 桌面客户端",
                            "status": HEALTH_ERROR,
                            "detail": f"检查失败：{e}", "hint": ""})

    # 3) 网络连通性（官方 API 域名 443）
    for host, plat in (("api.trae.cn", PLAT_TRAEWORK),
                       ("copilot.tencent.com", PLAT_WORKBUDDY)):
        label = PLATFORM_LABELS.get(plat, plat)
        try:
            ok, info_txt = _tcp_reachable(host, 443)
            if ok:
                results.append({"key": f"net_{plat}",
                                "title": f"{label} 服务器连通性",
                                "status": HEALTH_OK,
                                "detail": f"{host}:443 可达（{info_txt}）",
                                "hint": ""})
            else:
                results.append({"key": f"net_{plat}",
                                "title": f"{label} 服务器连通性",
                                "status": HEALTH_ERROR,
                                "detail": f"{host}:443 不可达（{info_txt}）",
                                "hint": "请检查网络连接、代理或防火墙设置；"
                                        "公司网络可能拦截了该域名。"})
        except Exception as e:
            results.append({"key": f"net_{plat}",
                            "title": f"{label} 服务器连通性",
                            "status": HEALTH_ERROR,
                            "detail": f"检查失败：{e}", "hint": ""})

    # 4) 本地数据文件完整性
    #    全新用户文件尚不存在属正常（首次保存时自动创建），只报告：
    #    JSON 结构损坏 / 无法读取 / 历史文件结构异常。
    accs = list_accounts()
    data_issues: list[str] = []
    for name, path in (("账号数据", ACCOUNTS_FILE),
                       ("设置", SETTINGS_FILE),
                       ("签到历史", HISTORY_FILE)):
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if not isinstance(parsed, dict):
                data_issues.append(f"{name}文件结构异常（顶层不是对象）")
            elif name == "签到历史" and "days" in parsed \
                    and not isinstance(parsed["days"], dict):
                data_issues.append("签到历史 days 结构异常")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            data_issues.append(f"{name}文件已损坏：{type(e).__name__}")
        except OSError as e:
            data_issues.append(f"{name}无法读取：{e}")
    today_key = datetime.now().date().strftime("%Y-%m-%d")
    today_bucket = (load_history().get("days") or {}).get(today_key) or {}
    today_ok_n = sum(1 for v in today_bucket.values()
                     if isinstance(v, dict) and v.get("success"))
    today_total = len(today_bucket)
    if data_issues:
        results.append({"key": "data", "title": "本地数据文件",
                        "status": HEALTH_WARN,
                        "detail": "；".join(data_issues),
                        "hint": "可用「备份 / 恢复」导出数据后联系排查；"
                                "损坏文件不会影响账号凭据本身。"})
    else:
        detail = f"共 {len(accs)} 个账号，数据文件正常"
        if today_total:
            detail += f"；今日已签到 {today_ok_n}/{today_total}"
        results.append({"key": "data", "title": "本地数据文件",
                        "status": HEALTH_OK,
                        "detail": detail,
                        "hint": ("今天还有账号未签到，点「全部账号签到」即可。"
                                 if today_total and today_ok_n < today_total else "")})

    return results


# ═══════════════════════════════ GUI ═══════════════════════════════

def run_gui() -> int:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, simpledialog

    FONT = "Microsoft YaHei UI"
    # ── 主题色板：所有界面颜色统一走语义 token，深色模式整树重配 ──
    PALETTES = {
        "light": {
            "bg": "#f4f6fb", "card": "#ffffff", "primary": "#2f6bff",
            "primary_d": "#1f56e0", "green": "#1a9e5c", "gray": "#6b7280",
            "dark": "#1f2937", "border": "#e3e8f0", "seg": "#dfe5f0",
            "seg_active": "#cfd7ea", "row": "#f7f9fd", "btn_gray": "#eef1f7",
            "btn_gray_a": "#dde3ee", "hint": "#b6bdca", "footer": "#9aa3b2",
            "csv_bg": "#eef3ff", "csv_bg_a": "#dde7ff", "blue_txt": "#2f6bff",
            "info_bg": "#e8f4ff", "info_bg_a": "#d2e9ff", "info_txt": "#1172b8",
            "diag_bg": "#f3f0ff", "diag_bg_a": "#e6e0ff", "diag_txt": "#6d4aff",
            "warn_bg": "#fff4e5", "warn_bg_a": "#ffe7c2", "warn_txt": "#c7771f",
            "del_bg": "#fdecec", "del_bg_a": "#f9d6d6", "del_txt": "#c0392b",
            "note_bg": "#eef3fb", "note_txt": "#2c5fa8",
            "ok_bg": "#eafaf1", "ok_bg_a": "#d4f2e0",
            "entry_bg": "#ffffff", "entry_fg": "#1f2937",
            "grid": "#e9edf5", "axis": "#c7cedb", "heat_none": "#eef1f6",
            "tip_bg": "#1f2937", "tip_fg": "#ffffff",
        },
        "dark": {
            "bg": "#15181f", "card": "#1e232c", "primary": "#5b8cff",
            "primary_d": "#3f6fe0", "green": "#34c47a", "gray": "#9aa4b2",
            "dark": "#e6eaf0", "border": "#333a47", "seg": "#2a303b",
            "seg_active": "#3a4354", "row": "#262c37", "btn_gray": "#2c333f",
            "btn_gray_a": "#39424f", "hint": "#6b7482", "footer": "#7c8594",
            "csv_bg": "#22304a", "csv_bg_a": "#2d3f60", "blue_txt": "#8ab0ff",
            "info_bg": "#1f3145", "info_bg_a": "#2a415c", "info_txt": "#6fb6ef",
            "diag_bg": "#2e2a45", "diag_bg_a": "#3b3558", "diag_txt": "#a994ff",
            "warn_bg": "#3d2f17", "warn_bg_a": "#4e3c1d", "warn_txt": "#e0a53d",
            "del_bg": "#40242a", "del_bg_a": "#522e35", "del_txt": "#f08a8a",
            "note_bg": "#243247", "note_txt": "#8ab0dd",
            "ok_bg": "#1d3a2a", "ok_bg_a": "#264b36",
            "entry_bg": "#232a35", "entry_fg": "#e6eaf0",
            "grid": "#2c333f", "axis": "#46505f", "heat_none": "#2a303b",
            "tip_bg": "#e6eaf0", "tip_fg": "#15181f",
        },
    }
    # 语义状态色（两种主题下都醒目，不参与重映射）
    STATUS_RED, STATUS_AMBER = "#e0533d", "#e0a53d"
    HEAT_OK, HEAT_PARTIAL, HEAT_FAIL = "#22c55e", "#f59e0b", "#ef4444"
    _initial_theme = "light"
    try:
        _initial_theme = load_settings().get("theme", "light")
        if _initial_theme not in PALETTES:
            _initial_theme = "light"
    except Exception:
        pass
    P = dict(PALETTES[_initial_theme])
    current_theme = {"name": _initial_theme}

    def _pal():
        return P

    BG = P["bg"]
    CARD = P["card"]
    PRIMARY = P["primary"]
    PRIMARY_D = P["primary_d"]
    GREEN = P["green"]
    GRAY = P["gray"]
    DARK = P["dark"]

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("600x760")
    root.configure(bg=BG)
    root.minsize(560, 640)

    # 窗口图标：打包后取 exe 内嵌图标；开发态取同目录 app.ico；失败则用 Tk 默认图标
    try:
        if getattr(sys, "frozen", False):
            root.iconbitmap(default=sys.executable)
        else:
            _ico = Path(__file__).resolve().parent / "app.ico"
            if _ico.is_file():
                root.iconbitmap(default=str(_ico))
    except Exception:
        log.debug("窗口图标加载失败，使用默认图标", exc_info=True)

    # 全局异常兜底：后台线程写日志，界面回调异常写日志并弹窗提示
    install_exception_hooks(popup=False)
    install_tk_error_handler(root, popup=True)

    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass

    # 浅色 hex → 语义 token 映射（递归换色用）
    _HEX_TO_TOKEN = {
        "#f4f6fb": "bg", "#ffffff": "card", "#2f6bff": "primary",
        "#1f56e0": "primary_d", "#1a9e5c": "green", "#6b7280": "gray",
        "#1f2937": "dark", "#e3e8f0": "border", "#dfe5f0": "seg",
        "#cfd7ea": "seg_active", "#f7f9fd": "row", "#eef1f7": "btn_gray",
        "#dde3ee": "btn_gray_a", "#b6bdca": "hint", "#9aa3b2": "footer",
        "#eef3ff": "csv_bg", "#dde7ff": "csv_bg_a",
        "#e8f4ff": "info_bg", "#d2e9ff": "info_bg_a", "#1172b8": "info_txt",
        "#f3f0ff": "diag_bg", "#e6e0ff": "diag_bg_a", "#6d4aff": "diag_txt",
        "#fff4e5": "warn_bg", "#ffe7c2": "warn_bg_a", "#c7771f": "warn_txt",
        "#fdecec": "del_bg", "#f9d6d6": "del_bg_a", "#c0392b": "del_txt",
        "#eef3fb": "note_bg", "#2c5fa8": "note_txt",
        "#eafaf1": "ok_bg", "#d4f2e0": "ok_bg_a",
        "#e9edf5": "grid", "#c7cedb": "axis", "#eef1f6": "heat_none",
        "#15804c": "green",  # 绿色按钮按下色
    }
    # 深色 hex → token（用于二次切换：先把深色 hex 也认出来）
    _HEX_TO_TOKEN.update({v: k for k, v in PALETTES["dark"].items()})

    def _mapped_color(hexv: str):
        if not isinstance(hexv, str):
            return hexv
        key = hexv.lower()
        if key in _HEX_TO_TOKEN:
            return P[_HEX_TO_TOKEN[key]]
        return hexv  # 状态色（红/琥珀/热力色等）保持不变

    def _config_widget(w):
        cls = w.winfo_class()
        try:
            if cls in ("Frame", "Toplevel", "Canvas"):
                if "bg" in w.keys() and str(w.cget("bg")).lower() in _HEX_TO_TOKEN:
                    w.configure(bg=_mapped_color(w.cget("bg")))
                if cls == "Frame" and "highlightbackground" in w.keys():
                    hb = str(w.cget("highlightbackground")).lower()
                    if hb in _HEX_TO_TOKEN:
                        w.configure(highlightbackground=_mapped_color(hb))
            elif cls in ("Label", "Button"):
                kw = {}
                bgv = str(w.cget("bg")).lower()
                if bgv in _HEX_TO_TOKEN:
                    kw["bg"] = _mapped_color(bgv)
                fgv = str(w.cget("fg")).lower()
                if fgv in _HEX_TO_TOKEN:
                    kw["fg"] = _mapped_color(fgv)
                if "activebackground" in w.keys():
                    ab = str(w.cget("activebackground")).lower()
                    if ab in _HEX_TO_TOKEN:
                        kw["activebackground"] = _mapped_color(ab)
                if "activeforeground" in w.keys():
                    af = str(w.cget("activeforeground")).lower()
                    if af in _HEX_TO_TOKEN:
                        kw["activeforeground"] = _mapped_color(af)
                if "selectcolor" in w.keys():
                    sc = str(w.cget("selectcolor")).lower()
                    if sc in _HEX_TO_TOKEN:
                        kw["selectcolor"] = _mapped_color(sc)
                if kw:
                    w.configure(**kw)
            elif cls == "Entry":
                w.configure(bg=P["entry_bg"], fg=P["entry_fg"],
                            insertbackground=P["entry_fg"],
                            highlightbackground=P["border"])
            elif cls in ("Checkbutton", "Radiobutton"):
                kw = {}
                bgv = str(w.cget("bg")).lower()
                if bgv in _HEX_TO_TOKEN:
                    kw["bg"] = _mapped_color(bgv)
                fgv = str(w.cget("fg")).lower()
                if fgv in _HEX_TO_TOKEN:
                    kw["fg"] = _mapped_color(fgv)
                sc = str(w.cget("selectcolor")).lower()
                if sc in _HEX_TO_TOKEN:
                    kw["selectcolor"] = _mapped_color(sc)
                if "activebackground" in w.keys():
                    ab = str(w.cget("activebackground")).lower()
                    if ab in _HEX_TO_TOKEN:
                        kw["activebackground"] = _mapped_color(ab)
                if kw:
                    w.configure(**kw)
        except Exception:
            pass

    def apply_theme(name: str, persist: bool = True):
        """整树换色并重绘历史区；name ∈ {'light','dark'}。"""
        nonlocal BG, CARD, PRIMARY, PRIMARY_D, GREEN, GRAY, DARK
        if name not in PALETTES:
            name = "light"
        P.update(PALETTES[name])
        BG, CARD = P["bg"], P["card"]
        PRIMARY, PRIMARY_D = P["primary"], P["primary_d"]
        GREEN, GRAY, DARK = P["green"], P["gray"], P["dark"]
        current_theme["name"] = name
        root.configure(bg=BG)
        try:
            style.configure("Vertical.TScrollbar",
                            background=P["seg"], troughcolor=P["bg"])
        except Exception:
            pass
        _walk_and_theme(root)
        # 动态内容区重绘（热力图/趋势图/账号行/统计条都含一次性画色）
        try:
            render_accounts()
        except Exception:
            pass
        try:
            render_history()
        except Exception:
            pass
        if persist:
            save_theme_preference(name)

    def _walk_and_theme(win):
        _config_widget(win)
        for ch in win.winfo_children():
            _walk_and_theme(ch)

    def toggle_theme():
        apply_theme("dark" if current_theme["name"] == "light" else "light")
        try:
            theme_btn.config(
                text="☀ 浅色" if current_theme["name"] == "dark" else "🌙 深色")
        except Exception:
            pass

    def card(parent) -> tk.Frame:
        f = tk.Frame(parent, bg=CARD, highlightbackground=P["border"],
                     highlightthickness=1, bd=0)
        return f

    def on_show_about():
        from tkinter import messagebox
        win = tk.Toplevel(root)
        win.title("关于本工具")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(root)
        win.grab_set()
        box = tk.Frame(win, bg=CARD, highlightbackground=P["border"],
                       highlightthickness=1, bd=0)
        box.pack(padx=20, pady=20, fill="both", expand=True)
        tk.Label(box, text=APP_NAME, bg=CARD, fg=DARK,
                 font=(FONT, 15, "bold")).pack(anchor="w", padx=20,
                                                pady=(18, 2))
        tk.Label(box, text=f"版本 v{APP_VERSION}", bg=CARD, fg=PRIMARY,
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=20)
        for line in (
            "支持平台：TraeWork CN / 腾讯 WorkBuddy",
            "功能：多账号批量签到 · 推送通知 · 历史统计 · 自动任务",
            "",
            "数据与隐私：",
            f"· 所有账号、密钥、历史仅保存在本机：{_log_dir()}",
            "· 凭据使用 Windows DPAPI 加密，不上传任何第三方服务器",
            "· 本工具不收集、不上传任何个人信息",
            "",
            "免责声明：本工具为个人学习用途的免费开源工具，",
            "自动化签到可能违反对应平台的服务条款，使用风险",
            "（包括但不限于账号受限）由使用者自行承担。",
            "",
            APP_COPYRIGHT,
        ):
            tk.Label(box, text=line or " ", bg=CARD,
                     fg=(GRAY if line.startswith(("·", "支持", "功能"))
                         else DARK),
                     font=(FONT, 9), justify="left", anchor="w",
                     wraplength=460).pack(anchor="w", padx=20)
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", padx=20, pady=(14, 18))

        def _open_data_dir():
            try:
                os.startfile(str(_log_dir()))  # type: ignore[attr-defined]
            except Exception as e:
                messagebox.showerror("打开失败", f"无法打开数据目录：{e}",
                                     parent=win)

        tk.Button(row, text="打开数据目录", bg=P["seg"], fg=DARK,
                  font=(FONT, 9), relief="flat", cursor="hand2", bd=0,
                  activebackground=P["seg_active"], padx=12, pady=5,
                  command=_open_data_dir).pack(side="left")
        tk.Button(row, text="知道了", bg=PRIMARY, fg="white",
                  font=(FONT, 9, "bold"), relief="flat", cursor="hand2",
                  bd=0, activebackground=PRIMARY_D, activeforeground="white",
                  padx=18, pady=5, command=win.destroy).pack(side="right")
        win.update_idletasks()
        win.geometry(f"+{root.winfo_rootx() + 80}"
                     f"+{root.winfo_rooty() + 80}")
        win.wait_window()

    def show_disclaimer_dialog() -> bool:
        """首次启动风险声明弹窗：返回用户是否同意（同意才能进入主界面）。"""
        win = tk.Toplevel(root)
        win.title("首次使用 · 风险与隐私声明")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(root)
        win.grab_set()
        state = {"agree": None}

        def _close():
            if state["agree"] is None:
                state["agree"] = False
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _close)
        box = tk.Frame(win, bg=CARD, highlightbackground=P["border"],
                       highlightthickness=1, bd=0)
        box.pack(padx=18, pady=18)
        tk.Label(box, text="欢迎使用每日签到助手", bg=CARD, fg=DARK,
                 font=(FONT, 14, "bold")).pack(anchor="w", padx=22, pady=(18, 4))
        tk.Label(box, text=f"版本 v{APP_VERSION} · {APP_COPYRIGHT}",
                 bg=CARD, fg=GRAY, font=(FONT, 9)).pack(anchor="w", padx=22)
        lines = (
            "",
            "使用前请知悉并确认：",
            "1. 本工具为个人学习用途的免费开源工具，与 TraeWork、",
            "    腾讯 WorkBuddy 官方无关；自动化签到可能违反对应平台",
            "    的服务条款，账号受限等风险由使用者自行承担。",
            "2. 本工具直接调用平台官方接口完成签到，不经过任何第三方",
            "    服务器；账号凭据使用 Windows DPAPI 加密，仅保存在本机。",
            f"3. 数据目录：{_log_dir()}",
            "    卸载程序不会删除该目录，换电脑可用内置备份 / 换机向导迁移。",
            "4. 建议合理使用（每日一次即可），不要高频请求。",
        )
        for line in lines:
            tk.Label(box, text=line or " ", bg=CARD,
                     fg=(DARK if line and line[0].isdigit() else GRAY),
                     font=(FONT, 9), justify="left", anchor="w",
                     wraplength=470).pack(anchor="w", padx=22)
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", padx=22, pady=(16, 18))

        def _agree():
            state["agree"] = True
            win.destroy()

        def _disagree():
            state["agree"] = False
            win.destroy()

        tk.Button(row, text="不同意并退出", bg=P["seg"], fg=DARK,
                  font=(FONT, 9), relief="flat", bd=0, cursor="hand2",
                  activebackground=P["seg_active"], padx=12, pady=6,
                  command=_disagree).pack(side="left")
        tk.Button(row, text="我已了解并同意", bg=PRIMARY, fg="white",
                  font=(FONT, 9, "bold"), relief="flat", bd=0, cursor="hand2",
                  activebackground=PRIMARY_D, activeforeground="white",
                  padx=16, pady=6, command=_agree).pack(side="right")
        win.update_idletasks()
        win.geometry(f"{max(win.winfo_reqwidth(), 520)}x{win.winfo_reqheight()}"
                     f"+{root.winfo_rootx() + 40}+{root.winfo_rooty() + 60}")
        win.wait_window()
        return state["agree"] is True

    # ── 健康自检中心 ──
    HEALTH_META = {
        HEALTH_OK: ("✓", GREEN, "正常"),
        HEALTH_WARN: ("⚠", "#c98a12", "需关注"),
        HEALTH_ERROR: ("✗", "#d33b3b", "异常"),
    }

    def on_show_health():
        win = tk.Toplevel(root)
        win.title("健康自检")
        win.configure(bg=BG)
        win.transient(root)
        win.geometry("560x520")
        win.minsize(520, 460)
        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(head, text="健康自检", bg=BG, fg=DARK,
                 font=(FONT, 14, "bold")).pack(side="left")
        summary_var = tk.StringVar(value="正在检查…")
        tk.Label(head, textvariable=summary_var, bg=BG, fg=GRAY,
                 font=(FONT, 9)).pack(side="right")

        canvas_wrap = tk.Frame(win, bg=BG)
        canvas_wrap.pack(fill="both", expand=True, padx=18, pady=(6, 6))
        canvas = tk.Canvas(canvas_wrap, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(canvas_wrap, orient="vertical",
                            command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        list_frame = tk.Frame(canvas, bg=BG)
        canvas_window = canvas.create_window((0, 0), window=list_frame,
                                             anchor="nw")

        def _on_configure(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(canvas_window, width=canvas.winfo_width())

        list_frame.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", _on_configure)

        # 滚轮绑定跟随鼠标进出 canvas，窗口销毁后不再残留全局回调，
        # 避免 "invalid command name" 类 TclError（参见历史崩溃记录）
        def _on_wheel(e):
            try:
                canvas.yview_scroll(int(-e.delta / 120), "units")
            except tk.TclError:
                pass

        canvas.bind("<Enter>",
                    lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>",
                    lambda _e: canvas.unbind_all("<MouseWheel>"))
        win.bind("<Destroy>",
                 lambda _e: canvas.unbind_all("<MouseWheel>"), add="+")

        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=18, pady=(0, 14))
        rerun_btn = tk.Button(foot, text="重新检查", bg=PRIMARY, fg="white",
                              font=(FONT, 9, "bold"), relief="flat", bd=0,
                              activebackground=PRIMARY_D, activeforeground="white",
                              cursor="hand2", padx=16, pady=6)
        rerun_btn.pack(side="right")
        tk.Button(foot, text="关闭", bg=P["seg"], fg=DARK,
                  font=(FONT, 9), relief="flat", bd=0, cursor="hand2",
                  activebackground=P["seg_active"], padx=14, pady=6,
                  command=win.destroy).pack(side="right", padx=(0, 8))

        def _clear_children(parent):
            for child in parent.winfo_children():
                child.destroy()

        def _render(results):
            _clear_children(list_frame)
            counts = {HEALTH_OK: 0, HEALTH_WARN: 0, HEALTH_ERROR: 0}
            for item in results:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
                icon, color, _label = HEALTH_META[item["status"]]
                cardf = tk.Frame(list_frame, bg=CARD,
                                 highlightbackground=P["border"],
                                 highlightthickness=1, bd=0)
                cardf.pack(fill="x", pady=(0, 8))
                top_row = tk.Frame(cardf, bg=CARD)
                top_row.pack(fill="x", padx=12, pady=(10, 0))
                tk.Label(top_row, text=icon, bg=CARD, fg=color,
                         font=(FONT, 12, "bold"), width=2).pack(side="left")
                tk.Label(top_row, text=item["title"], bg=CARD, fg=DARK,
                         font=(FONT, 10, "bold")).pack(side="left")
                tk.Label(top_row, text=HEALTH_META[item["status"]][2],
                         bg=CARD, fg=color,
                         font=(FONT, 8, "bold")).pack(side="right")
                tk.Label(cardf, text=item["detail"], bg=CARD, fg=GRAY,
                         font=(FONT, 8), justify="left", anchor="w",
                         wraplength=480).pack(anchor="w", padx=(38, 12))
                if item.get("hint"):
                    tk.Label(cardf, text="建议：" + item["hint"], bg=CARD,
                             fg=color, font=(FONT, 8), justify="left",
                             anchor="w", wraplength=480).pack(
                        anchor="w", padx=(38, 12), pady=(2, 10))
                else:
                    tk.Frame(cardf, bg=CARD, height=8).pack()
            if counts[HEALTH_ERROR]:
                summary_var.set(
                    f"{counts[HEALTH_ERROR]} 项异常 · "
                    f"{counts[HEALTH_WARN]} 项需关注 · "
                    f"{counts[HEALTH_OK]} 项正常")
            elif counts[HEALTH_WARN]:
                summary_var.set(
                    f"{counts[HEALTH_WARN]} 项需关注 · "
                    f"{counts[HEALTH_OK]} 项正常")
            else:
                summary_var.set(f"全部 {counts[HEALTH_OK]} 项正常")

        state = {"running": False}

        def _run():
            if state["running"]:
                return
            state["running"] = True
            rerun_btn.config(state="disabled", text="检查中…")
            summary_var.set("正在检查客户端、网络与任务…")
            _clear_children(list_frame)
            tk.Label(list_frame, text="检测大约需要几秒，请稍候…",
                     bg=BG, fg=GRAY, font=(FONT, 9)).pack(pady=30)

            def worker():
                try:
                    results = run_health_checks()
                except Exception as e:
                    results = [{"key": "fatal", "title": "健康自检",
                                "status": HEALTH_ERROR,
                                "detail": f"自检流程异常：{e}", "hint": ""}]

                def done():
                    if not win.winfo_exists():
                        return
                    _render(results)
                    rerun_btn.config(state="normal", text="重新检查")
                    state["running"] = False
                root.after(0, done)

            threading.Thread(target=worker, daemon=True).start()

        rerun_btn.config(command=_run)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.update_idletasks()
        win.geometry(f"+{root.winfo_rootx() + 100}"
                     f"+{root.winfo_rooty() + 60}")
        _run()

    # 顶部标题
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=24, pady=(22, 6))
    theme_btn = tk.Button(
        header, text="☀ 浅色" if _initial_theme == "dark" else "🌙 深色",
        command=toggle_theme, bg=CARD, fg=DARK, relief="flat", bd=0,
        activebackground=P["seg"], activeforeground=DARK,
        font=(FONT, 10), padx=10, pady=4, cursor="hand2")
    theme_btn.pack(side="right", anchor="e")
    about_btn = tk.Button(
        header, text="关于", command=on_show_about, bg=CARD, fg=DARK,
        relief="flat", bd=0, activebackground=P["seg"],
        activeforeground=DARK, font=(FONT, 10), padx=10, pady=4,
        cursor="hand2")
    about_btn.pack(side="right", anchor="e", padx=(0, 6))
    health_btn = tk.Button(
        header, text="健康自检", command=on_show_health, bg=CARD, fg=DARK,
        relief="flat", bd=0, activebackground=P["seg"],
        activeforeground=DARK, font=(FONT, 10), padx=10, pady=4,
        cursor="hand2")
    health_btn.pack(side="right", anchor="e", padx=(0, 6))
    tk.Label(header, text=APP_NAME, bg=BG, fg=DARK,
             font=(FONT, 19, "bold")).pack(anchor="w")
    tk.Label(header, text="TraeWork CN / 腾讯 WorkBuddy · 多账号批量签到 · 推送微信 · 开机补签",
             bg=BG, fg=GRAY, font=(FONT, 10)).pack(anchor="w", pady=(2, 0))

    # 平台切换
    plat_row = tk.Frame(root, bg=BG)
    plat_row.pack(fill="x", padx=24, pady=(8, 0))
    platform_var = tk.StringVar(value=PLAT_TRAEWORK)
    tk.Label(plat_row, text="当前平台：", bg=BG, fg=DARK,
             font=(FONT, 10)).pack(side="left")
    plat_seg = tk.Frame(plat_row, bg=P["seg"], highlightthickness=0, bd=0)
    plat_seg.pack(side="left")

    def on_platform_change(*_args):
        cur = platform_var.get()
        for pf, btn in plat_buttons.items():
            if pf == cur:
                btn.config(bg=PRIMARY, fg="white", activebackground=PRIMARY_D,
                           activeforeground="white", relief="flat")
            else:
                btn.config(bg=P["seg"], fg=DARK, activebackground=P["seg_active"],
                           activeforeground=DARK, relief="flat")
        result_var.set("")
        try:
            update_accounts_hint()
        except Exception:
            pass
        stop_waiting()
        threading.Thread(target=refresh_ui, daemon=True).start()

    plat_buttons = {}
    for pf, label in ((PLAT_TRAEWORK, "TraeWork CN"), (PLAT_WORKBUDDY, "WorkBuddy")):
        active = pf == platform_var.get()
        b = tk.Radiobutton(
            plat_seg, text=label, variable=platform_var, value=pf,
            indicatoron=0, bg=(PRIMARY if active else P["seg"]),
            fg=("white" if active else DARK), selectcolor=PRIMARY,
            activebackground=(PRIMARY_D if active else P["seg_active"]),
            activeforeground="white",
            font=(FONT, 9, "bold"), bd=0, padx=16, pady=4, cursor="hand2")
        b.pack(side="left")
        plat_buttons[pf] = b
    platform_var.trace_add("write", on_platform_change)

    # 可滚动内容区
    outer = tk.Frame(root, bg=BG)
    outer.pack(fill="both", expand=True, padx=(24, 12), pady=10)
    canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0, width=536)
    vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    body = tk.Frame(canvas, bg=BG)
    body_id = canvas.create_window((0, 0), window=body, anchor="nw", width=536)

    def _on_body_config(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_config(e):
        canvas.itemconfigure(body_id, width=e.width)

    body.bind("<Configure>", _on_body_config)
    canvas.bind("<Configure>", _on_canvas_config)

    def _on_wheel(e):
        canvas.yview_scroll(int(-e.delta / 120), "units")

    def _bind_wheel(_e=None):
        canvas.bind_all("<MouseWheel>", _on_wheel)

    def _unbind_wheel(_e=None):
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", _bind_wheel)
    canvas.bind("<Leave>", _unbind_wheel)

    # ── 状态卡片 ──
    status_card = card(body)
    status_card.pack(fill="x")
    st_dot = tk.Label(status_card, text="●", bg=CARD, fg=GRAY,
                      font=(FONT, 14))
    st_dot.grid(row=0, column=0, padx=(18, 6), pady=16, sticky="n")
    st_text = tk.Label(status_card, text="正在检测环境…", bg=CARD, fg=DARK,
                       font=(FONT, 12, "bold"), justify="left", anchor="w")
    st_text.grid(row=0, column=1, sticky="w", pady=(16, 0))
    st_sub = tk.Label(status_card, text="", bg=CARD, fg=GRAY,
                      font=(FONT, 9), justify="left", anchor="w", wraplength=420)
    st_sub.grid(row=1, column=1, sticky="w", pady=(0, 16))
    status_card.grid_columnconfigure(1, weight=1)

    # ── 登录引导卡片（默认隐藏）──
    login_card = card(body)
    login_title = tk.Label(login_card, text="需要先登录 TraeWork CN",
                           bg=CARD, fg=DARK, font=(FONT, 12, "bold"),
                           anchor="w", justify="left")
    login_title.pack(fill="x", padx=18, pady=(16, 4))
    login_hint = tk.Label(login_card, text="", bg=CARD, fg=GRAY,
                          font=(FONT, 9), justify="left", anchor="w",
                          wraplength=470)
    login_hint.pack(fill="x", padx=18)

    btn_row = tk.Frame(login_card, bg=CARD)
    btn_row.pack(fill="x", padx=18, pady=14)
    waiting_var = tk.StringVar(value="")

    def set_status(dot_color: str, title: str, sub: str = ""):
        st_dot.config(fg=dot_color)
        st_text.config(text=title)
        st_sub.config(text=sub)

    login_poll = {"on": False}

    def stop_waiting():
        login_poll["on"] = False
        waiting_var.set("")
        for b in btn_row.winfo_children():
            try:
                b.config(state="normal")
            except Exception:
                pass

    def _start_polling(hint: str):
        login_poll["on"] = True
        waiting_var.set(hint)
        for b in btn_row.winfo_children():
            try:
                b.config(state="disabled")
            except Exception:
                pass
        poll_login(90)

    def poll_login(times: int):
        if not login_poll["on"]:
            return
        pf = platform_var.get()
        if pf == PLAT_WORKBUDDY:
            ok_logged, _ = wb_is_logged_in()
            label = "WorkBuddy"
        else:
            ok_logged, _ = is_logged_in()
            label = "TraeWork CN"
        if ok_logged:
            stop_waiting()
            messagebox.showinfo(APP_NAME, f"检测到 {label} 登录成功！")
            refresh_ui()
            return
        if times <= 0:
            stop_waiting()
            messagebox.showinfo(APP_NAME, "暂未检测到登录，可稍后点击「重新检测」。")
            return
        root.after(2000, lambda: poll_login(times - 1))

    def on_open_app():
        pf = platform_var.get()
        if pf == PLAT_WORKBUDDY:
            exes = find_wb_install_exes()
            if not exes:
                messagebox.showwarning(APP_NAME, "未能自动找到 WorkBuddy，请先安装腾讯 WorkBuddy 桌面端。")
                return
            if launch_app(exes[0]):
                _start_polling("已打开 WorkBuddy，请在窗口中完成登录，登录后此处会自动继续…（最多等待 3 分钟）")
            else:
                messagebox.showerror(APP_NAME, "无法启动 WorkBuddy，请手动打开后登录。")
            return
        exes = find_install_exes()
        chosen: Optional[Path] = exes[0] if exes else None
        if not chosen:
            messagebox.showwarning(APP_NAME, "未能自动找到 TraeWork CN，请先安装客户端。")
            return
        if launch_app(chosen):
            _start_polling("已打开应用，请在弹出的窗口中登录，登录后此处会自动继续…（最多等待 3 分钟）")
        else:
            messagebox.showerror(APP_NAME, "无法启动应用，请手动打开 TraeWork CN 登录。")

    def on_manual_exe():
        pf = platform_var.get()
        name = "WorkBuddy" if pf == PLAT_WORKBUDDY else "TraeWork CN"
        path = filedialog.askopenfilename(
            title=f"请选择 {name} 主程序（.exe）",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            if launch_app(Path(path)):
                _start_polling(f"已启动所选程序，请完成 {name} 登录…")

    open_btn = tk.Button(btn_row, text="打开并登录", bg=PRIMARY,
                         fg="white", font=(FONT, 10, "bold"), relief="flat",
                         activebackground=PRIMARY_D, activeforeground="white",
                         cursor="hand2", padx=14, pady=7, bd=0,
                         command=lambda: threading.Thread(target=on_open_app,
                                                          daemon=True).start())
    open_btn.pack(side="left")
    tk.Button(btn_row, text="手动选择程序位置", bg="#eef1f7", fg=DARK,
              font=(FONT, 9), relief="flat", cursor="hand2", padx=12, pady=7,
              bd=0, command=on_manual_exe).pack(side="left", padx=(10, 0))
    tk.Label(login_card, textvariable=waiting_var, bg=CARD, fg=PRIMARY,
             font=(FONT, 9), wraplength=470, justify="left", anchor="w").pack(
        fill="x", padx=18, pady=(0, 8))

    # ── 操作卡片（登录后显示）──
    action_card = card(body)
    ac_title = tk.Label(action_card, text="签到", bg=CARD, fg=DARK,
                        font=(FONT, 12, "bold"), anchor="w")
    ac_title.pack(fill="x", padx=18, pady=(16, 8))
    result_var = tk.StringVar(value="")
    tk.Label(action_card, textvariable=result_var, bg=CARD, fg=DARK,
             font=(FONT, 10), justify="left", anchor="w",
             wraplength=470).pack(fill="x", padx=18)

    op_row = tk.Frame(action_card, bg=CARD)
    op_row.pack(fill="x", padx=18, pady=14)
    checkin_btn = tk.Button(op_row, text="立即签到", bg=PRIMARY, fg="white",
                            font=(FONT, 10, "bold"), relief="flat",
                            activebackground=PRIMARY_D, activeforeground="white",
                            cursor="hand2", padx=16, pady=7, bd=0)
    checkin_btn.pack(side="left")
    refresh_btn = tk.Button(op_row, text="重新检测", bg="#eef1f7", fg=DARK,
                            font=(FONT, 9), relief="flat", cursor="hand2",
                            padx=12, pady=7, bd=0)
    refresh_btn.pack(side="left", padx=(10, 0))

    # ── 多账号管理卡片 ──
    accounts_card = card(body)
    accounts_card.pack(fill="x", pady=(14, 0))
    tk.Label(accounts_card, text="多账号批量签到", bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=18,
                                                        pady=(16, 2))
    accounts_hint_var = tk.StringVar(value="")
    tk.Label(accounts_card, textvariable=accounts_hint_var,
             bg=CARD, fg=GRAY, font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18)

    acc_list_frame = tk.Frame(accounts_card, bg=CARD)
    acc_list_frame.pack(fill="x", padx=18, pady=(10, 4))
    accounts_state_var = tk.StringVar(value="尚未保存任何账号")
    tk.Label(accounts_card, textvariable=accounts_state_var, bg=CARD, fg=DARK,
             font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18)

    acc_row = tk.Frame(accounts_card, bg=CARD)
    acc_row.pack(fill="x", padx=18, pady=12)
    save_acc_btn = tk.Button(acc_row, text="保存当前登录账号", bg=PRIMARY,
                             fg="white", font=(FONT, 10, "bold"), relief="flat",
                             activebackground=PRIMARY_D, activeforeground="white",
                             cursor="hand2", padx=14, pady=7, bd=0)
    save_acc_btn.pack(side="left")
    batch_btn = tk.Button(acc_row, text="全部账号签到", bg=GREEN, fg="white",
                          font=(FONT, 10, "bold"), relief="flat",
                          activebackground="#15804c", activeforeground="white",
                          cursor="hand2", padx=14, pady=7, bd=0)
    batch_btn.pack(side="left", padx=(10, 0))
    batch_result_var = tk.StringVar(value="")
    tk.Label(accounts_card, textvariable=batch_result_var, bg=CARD, fg=DARK,
             font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18, pady=(0, 4))
    # 登录失效时按平台出现的"一键打开重登"按钮容器
    batch_guide_frame = tk.Frame(accounts_card, bg=CARD)
    batch_guide_frame.pack(fill="x", padx=18, pady=(0, 12))

    # ── 签到历史卡片 ──
    history_card = card(body)
    history_card.pack(fill="x", pady=(14, 0))
    hist_head = tk.Frame(history_card, bg=CARD)
    hist_head.pack(fill="x", padx=18, pady=(16, 2))
    tk.Label(hist_head, text="签到历史与统计", bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(side="left")

    def on_export_csv():
        try:
            n = len(load_history().get("days", {}))
            if not n:
                messagebox.showinfo(APP_NAME, "暂无历史记录可导出，完成一次签到后再来。")
                return
            default_name = f"签到历史_{datetime.now().strftime('%Y%m%d')}.csv"
            path = filedialog.asksaveasfilename(
                title="导出签到历史",
                initialdir=str(Path.home() / "Desktop"),
                initialfile=default_name,
                defaultextension=".csv",
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
            if not path:
                return
            count = export_history_csv(path)
            messagebox.showinfo(
                APP_NAME, f"已导出 {count} 条签到记录到：\n{path}\n\n"
                          "CSV 采用 UTF-8 编码，可直接用 Excel 打开。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"导出失败：{e}")

    export_btn = tk.Button(hist_head, text="导出 CSV", bg="#eef3ff", fg=PRIMARY,
                           font=(FONT, 9), relief="flat", cursor="hand2",
                           activebackground="#dde7ff", padx=10, pady=3, bd=0,
                           command=on_export_csv)
    export_btn.pack(side="right")

    def on_backup_data():
        try:
            default_name = f"签到助手备份_{datetime.now().strftime('%Y%m%d')}.zip"
            path = filedialog.asksaveasfilename(
                title="备份账号与设置",
                initialdir=str(Path.home() / "Desktop"),
                initialfile=default_name, defaultextension=".zip",
                filetypes=[("备份压缩包", "*.zip")])
            if not path:
                return
            info = backup_user_data(path)
            bits = []
            bits.append("账号 " + ("已备份" if info["accounts"] else "无"))
            bits.append("设置 " + ("已备份" if info["settings"] else "无"))
            bits.append("历史 " + ("已备份" if info["history"] else "无"))
            bits.append("推送记录 " + ("已备份" if info.get("push_log") else "无"))
            messagebox.showinfo(
                APP_NAME, "备份完成：\n" + "，".join(bits) +
                f"\n\n文件：{path}\n\n"
                "重要提示：账号与推送凭据经 Windows DPAPI 加密，\n"
                "该备份只能在本机同一 Windows 用户下恢复；\n"
                "换机或重装系统前请先在各客户端确认可手动登录。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"备份失败：{e}")

    def on_restore_data():
        if not messagebox.askyesno(
                APP_NAME, "恢复会用备份包覆盖当前的账号、设置与历史\n"
                          "（当前文件会自动另存为 .restore-bak）。\n\n"
                          "确定选择备份文件并恢复吗？"):
            return
        try:
            path = filedialog.askopenfilename(
                title="选择备份压缩包",
                initialdir=str(Path.home() / "Desktop"),
                filetypes=[("备份压缩包", "*.zip"), ("所有文件", "*.*")])
            if not path:
                return
            res = restore_user_data(path)
            names = "、".join(res["restored"])
            messagebox.showinfo(
                APP_NAME, f"已恢复：{names}\n\n设置即时生效；如界面显示异常，"
                          "关闭后重新打开本工具即可。")
            render_accounts()
            render_history()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"恢复失败：{e}")

    def on_export_diag():
        try:
            default_name = f"签到助手诊断_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
            path = filedialog.asksaveasfilename(
                title="导出诊断包",
                initialdir=str(Path.home() / "Desktop"),
                initialfile=default_name, defaultextension=".zip",
                filetypes=[("诊断压缩包", "*.zip")])
            if not path:
                return
            info = export_diagnostic_bundle(path)
            messagebox.showinfo(
                APP_NAME, f"诊断包已导出：\n{path}\n\n"
                          f"含版本/环境、{info['accounts']} 个账号概况、"
                          f"{info['history_days']} 天历史概况与最近日志。\n"
                          "凭据已脱敏，可安全发送给他人协助排查。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"诊断包导出失败：{e}")

    def on_show_push_history():
        """推送历史弹窗：只显示标题/类型/渠道/结果/时间，不显示正文。"""
        win = tk.Toplevel(root)
        win.title("消息推送历史")
        win.configure(bg=BG)
        win.transient(root)
        win.geometry("560x480")
        win.minsize(480, 360)
        try:
            win.grab_set()
        except Exception:
            pass
        tk.Label(win, text="消息推送历史（仅记录标题、渠道与结果，不含正文与密钥）",
                 bg=BG, fg=GRAY, font=(FONT, 9), anchor="w"
                 ).pack(fill="x", padx=14, pady=(12, 6))
        box = tk.Frame(win, bg=CARD)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        inner = tk.Frame(box, bg=CARD)
        inner.pack(fill="both", expand=True)
        items = load_push_history(limit=200)
        if not items:
            tk.Label(inner, text="暂无推送记录。\n开启推送后，测试消息与每日签到结果都会记录在这里。",
                     bg=CARD, fg=GRAY, font=(FONT, 10), justify="left"
                     ).pack(anchor="w", padx=14, pady=14)
        else:
            for it in items:
                row = tk.Frame(inner, bg=CARD)
                row.pack(fill="x", padx=10, pady=3)
                ok_col = GREEN if it.get("ok") else STATUS_RED
                tk.Label(row, text="✓" if it.get("ok") else "✗",
                         bg=CARD, fg=ok_col, font=(FONT, 10, "bold"),
                         width=2).pack(side="left")
                meta = (f"{it.get('ts', '')}　[{it.get('kind') or '其他'}]"
                        f"　{'、'.join(it.get('channels') or []) or '—'}")
                tk.Label(row, text=meta, bg=CARD, fg=GRAY,
                         font=(FONT, 8), anchor="w").pack(fill="x")
                title = str(it.get("title") or "")
                if len(title) > 60:
                    title = title[:59] + "…"
                tk.Label(row, text=title, bg=CARD, fg=DARK,
                         font=(FONT, 9), anchor="w").pack(fill="x")
                detail = str(it.get("detail") or "")
                if detail:
                    if len(detail) > 90:
                        detail = detail[:89] + "…"
                    tk.Label(row, text=detail, bg=CARD, fg=GRAY,
                             font=(FONT, 8), anchor="w", wraplength=500,
                             justify="left").pack(fill="x")

        def on_clear():
            if not messagebox.askyesno(
                    APP_NAME, "确定清空全部推送历史吗？此操作不可撤销。",
                    parent=win):
                return
            clear_push_history()
            win.destroy()

        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=14, pady=(0, 12))
        tk.Button(foot, text="清空历史", bg=P["del_bg"], fg=P["del_txt"],
                  font=(FONT, 9), relief="flat", cursor="hand2",
                  activebackground=P["del_bg_a"], padx=10, pady=3, bd=0,
                  command=on_clear).pack(side="left")
        tk.Button(foot, text="关闭", bg=P["btn_gray"], fg=DARK,
                  font=(FONT, 9), relief="flat", cursor="hand2",
                  activebackground=P["btn_gray_a"], padx=14, pady=3, bd=0,
                  command=win.destroy).pack(side="right")

    push_hist_btn = tk.Button(hist_head, text="推送记录", bg="#e8f4ff",
                              fg="#1172b8", font=(FONT, 9), relief="flat",
                              cursor="hand2", activebackground="#d2e9ff",
                              padx=8, pady=3, bd=0,
                              command=on_show_push_history)
    push_hist_btn.pack(side="right", padx=(0, 6))

    diag_btn = tk.Button(hist_head, text="诊断包", bg="#f3f0ff", fg="#6d4aff",
                         font=(FONT, 9), relief="flat", cursor="hand2",
                         activebackground="#e6e0ff", padx=8, pady=3, bd=0,
                         command=on_export_diag)
    diag_btn.pack(side="right", padx=(0, 6))
    restore_btn = tk.Button(hist_head, text="恢复", bg="#fff4e5", fg="#c7771f",
                            font=(FONT, 9), relief="flat", cursor="hand2",
                            activebackground="#ffe7c2", padx=8, pady=3, bd=0,
                            command=on_restore_data)
    restore_btn.pack(side="right", padx=(0, 6))
    backup_btn = tk.Button(hist_head, text="备份", bg="#eafaf1", fg=GREEN,
                           font=(FONT, 9), relief="flat", cursor="hand2",
                           activebackground="#d4f2e0", padx=8, pady=3, bd=0,
                           command=on_backup_data)
    backup_btn.pack(side="right", padx=(0, 6))

    def on_show_migration():
        """换机迁移向导：4 步引导，内嵌备份/恢复入口与 DPAPI 风险提示。"""
        win = tk.Toplevel(root)
        win.title("换机迁移向导")
        win.configure(bg=BG)
        win.transient(root)
        win.geometry("600x520")
        win.minsize(540, 460)
        try:
            win.grab_set()
        except Exception:
            pass

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 4))
        title_var = tk.StringVar()
        tk.Label(head, textvariable=title_var, bg=BG, fg=DARK,
                 font=(FONT, 13, "bold"), anchor="w").pack(anchor="w")
        step_var = tk.StringVar()
        tk.Label(head, textvariable=step_var, bg=BG, fg=GRAY,
                 font=(FONT, 9), anchor="w").pack(anchor="w", pady=(2, 0))

        content = tk.Frame(win, bg=BG)
        content.pack(fill="both", expand=True, padx=18, pady=8)

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=18, pady=(0, 14))
        prev_btn = tk.Button(nav, text="上一步", bg=P["btn_gray"], fg=DARK,
                             font=(FONT, 9), relief="flat", cursor="hand2",
                             activebackground=P["btn_gray_a"],
                             padx=14, pady=4, bd=0)
        prev_btn.pack(side="left")
        next_btn = tk.Button(nav, text="下一步", bg=PRIMARY, fg="white",
                             font=(FONT, 9, "bold"), relief="flat", cursor="hand2",
                             activebackground=PRIMARY_D, activeforeground="white",
                             padx=18, pady=4, bd=0)
        next_btn.pack(side="right")
        close_btn = tk.Button(nav, text="关闭", bg=P["btn_gray"], fg=DARK,
                              font=(FONT, 9), relief="flat", cursor="hand2",
                              activebackground=P["btn_gray_a"],
                              padx=14, pady=4, bd=0, command=win.destroy)
        close_btn.pack(side="right", padx=(0, 8))

        state = {"step": 0, "backup_path": ""}

        def _para(parent, text, color=None, bold=False):
            tk.Label(parent, text=text, bg=BG, fg=color or DARK,
                     font=(FONT, 10, "bold" if bold else "normal"),
                     justify="left", anchor="w", wraplength=540
                     ).pack(anchor="w", pady=3)

        def _warn_box(parent, text):
            box = tk.Frame(parent, bg=P["warn_bg"], highlightthickness=0)
            box.pack(fill="x", pady=8)
            tk.Label(box, text=text, bg=P["warn_bg"], fg=P["warn_txt"],
                     font=(FONT, 9, "bold"), justify="left", anchor="w",
                     wraplength=512, padx=12, pady=10).pack(anchor="w")

        def _ok_box(parent, text):
            box = tk.Frame(parent, bg=P["ok_bg"], highlightthickness=0)
            box.pack(fill="x", pady=8)
            tk.Label(box, text=text, bg=P["ok_bg"], fg=GREEN,
                     font=(FONT, 9), justify="left", anchor="w",
                     wraplength=512, padx=12, pady=10).pack(anchor="w")

        def _do_backup_in_wizard():
            try:
                default_name = f"签到助手备份_{datetime.now().strftime('%Y%m%d')}.zip"
                path = filedialog.asksaveasfilename(
                    parent=win, title="把备份保存到 U 盘 / 网盘 / 非系统盘",
                    initialdir=str(Path.home() / "Desktop"),
                    initialfile=default_name, defaultextension=".zip",
                    filetypes=[("备份压缩包", "*.zip")])
                if not path:
                    return
                info = backup_user_data(path)
                state["backup_path"] = path
                n = sum(1 for v in info.values() if v)
                _ok_box(content, f"已生成备份（含 {n} 类数据）：\n{path}\n"
                                 "请确认该文件已放到 U 盘或网盘，可在新电脑访问。")
            except Exception as e:
                messagebox.showerror(APP_NAME, f"备份失败：{e}", parent=win)

        def _do_restore_in_wizard():
            try:
                path = filedialog.askopenfilename(
                    parent=win, title="选择从旧电脑带来的备份压缩包",
                    filetypes=[("备份压缩包", "*.zip"), ("所有文件", "*.*")])
                if not path:
                    return
                res = restore_user_data(path)
                state["restored"] = "、".join(res["restored"])
                _ok_box(content, f"已恢复：{state['restored']}\n"
                                 "设置即时生效，建议关闭后重新打开本工具。")
                render_accounts()
                render_history()
            except Exception as e:
                messagebox.showerror(APP_NAME, f"恢复失败：{e}", parent=win)

        STEPS = [
            {
                "title": "第 1 步（旧电脑）：确认账号可手动登录",
                "sub": "迁移前最重要的准备",
                "render": lambda: (
                    _warn_box(content, "重要：账号与推送凭据经 Windows DPAPI 加密，"
                                      "绑定旧电脑的当前用户，恢复到新电脑后需重新登录验证，"
                                      "无法直接解密使用。"),
                    _para(content, "请在旧电脑上逐一确认："),
                    _para(content, "1. TraeWork CN、腾讯 WorkBuddy 客户端均可正常打开并已登录；"),
                    _para(content, "2. 你记得各账号的登录方式（手机号 / 邮箱 / 扫码）；"),
                    _para(content, "3. Server酱 / PushPlus / 企业微信 / 钉钉 的推送密钥可在对应平台重新获取。"),
                    _para(content, "建议先在旧电脑完成一次手动签到，确认账号状态正常后再继续。",
                          color=GRAY),
                ),
            },
            {
                "title": "第 2 步（旧电脑）：备份并拷出数据",
                "sub": "一键打包账号、设置、历史与推送记录",
                "render": lambda: (
                    _para(content, "点击下方按钮生成备份压缩包，并保存到 U 盘、移动硬盘"
                                  "或网盘（不要只放在旧电脑桌面）："),
                    tk.Button(content, text="① 生成备份包（另存为…）",
                              bg=P["ok_bg"], fg=GREEN, font=(FONT, 10, "bold"),
                              relief="flat", cursor="hand2",
                              activebackground=P["ok_bg_a"],
                              padx=14, pady=6, bd=0,
                              command=_do_backup_in_wizard).pack(anchor="w", pady=6),
                    _para(content, "备份包含：accounts.json、settings.json、"
                                  "checkin_history.json、push_history.json。", color=GRAY),
                    _warn_box(content, "再次提醒：凭据密文随备份带走，但只能在"
                                      "「同一台电脑同一用户」下解密；换机后必须重新登录。"),
                ),
            },
            {
                "title": "第 3 步（新电脑）：安装并恢复",
                "sub": "在新电脑完成安装后恢复数据",
                "render": lambda: (
                    _para(content, "在新电脑上："),
                    _para(content, "1. 安装 TraeWork CN 与腾讯 WorkBuddy 客户端；"),
                    _para(content, "2. 把本工具（exe 或源码）放到新电脑，先启动一次；"),
                    _para(content, "3. 把备份压缩包拷到新电脑本地磁盘（U 盘内也可直接选）；"),
                    _para(content, "4. 点击下方按钮选择备份包恢复（当前同名文件会自动另存"
                                  "为 .restore-bak）："),
                    tk.Button(content, text="② 选择备份包并恢复",
                              bg=P["note_bg"], fg=P["note_txt"],
                              font=(FONT, 10, "bold"), relief="flat", cursor="hand2",
                              padx=14, pady=6, bd=0,
                              command=_do_restore_in_wizard).pack(anchor="w", pady=6),
                ),
            },
            {
                "title": "第 4 步（新电脑）：逐账号重新登录确认",
                "sub": "完成迁移的最后一步",
                "render": lambda: (
                    _para(content, "恢复后请逐个账号完成："),
                    _para(content, "1. 在客户端登录对应账号，点「重登」按钮可一键唤起引导；"),
                    _para(content, "2. 登录后在本工具点「保存当前账号」刷新凭据快照；"),
                    _para(content, "3. 在「推送设置」中重新填写各渠道密钥并发一条测试推送；"),
                    _para(content, "4. 确认「开机自动签到」计划任务时间（换机后需重新开启）；"),
                    _para(content, "5. 手动执行一次「全部账号签到」验证全流程。"),
                    _ok_box(content, "签到历史与推送记录会原样保留；新凭据写入后，"
                                    "自动签到与微信推送即恢复正常。"),
                ),
            },
        ]

        def render_step():
            for w in content.winfo_children():
                w.destroy()
            st = STEPS[state["step"]]
            title_var.set(st["title"])
            step_var.set(f"步骤 {state['step'] + 1} / {len(STEPS)}")
            st["render"]()
            prev_btn.config(state=("disabled" if state["step"] == 0 else "normal"))
            next_btn.config(text=("完成" if state["step"] == len(STEPS) - 1 else "下一步"))

        def go_next():
            if state["step"] == len(STEPS) - 1:
                win.destroy()
                return
            if state["step"] == 1 and not state.get("backup_path"):
                if not messagebox.askyesno(
                        "确认跳过备份", "尚未在本向导中生成备份包。\n"
                                      "如果你已在「备份」按钮中自行备份，请选「是」继续；"
                                      "否则建议选「否」先生成备份。", parent=win):
                    return
            state["step"] += 1
            render_step()

        def go_prev():
            if state["step"] > 0:
                state["step"] -= 1
                render_step()

        prev_btn.config(command=go_prev)
        next_btn.config(command=go_next)
        render_step()

    migrate_btn = tk.Button(hist_head, text="换机迁移", bg=P["note_bg"],
                            fg=P["note_txt"], font=(FONT, 9), relief="flat",
                            cursor="hand2", activebackground=P["seg"],
                            padx=8, pady=3, bd=0, command=on_show_migration)
    migrate_btn.pack(side="right", padx=(0, 6))

    history_summary_var = tk.StringVar(value="")
    tk.Label(history_card, textvariable=history_summary_var,
             bg=CARD, fg=DARK, font=(FONT, 9), justify="left", anchor="w",
             wraplength=492).pack(fill="x", padx=18)
    history_frame = tk.Frame(history_card, bg=CARD)
    history_frame.pack(fill="x", padx=18, pady=(10, 14))

    # ── 消息通知卡片（多渠道） ──
    notify_card = card(body)
    notify_card.pack(fill="x", pady=(14, 0))
    tk.Label(notify_card, text="签到结果消息推送（可同时开启多个渠道）",
             bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=18,
                                                        pady=(16, 2))
    tk.Label(notify_card,
             text="勾选要使用的渠道并填写凭据，保存后每日所有账号的签到结果会汇总为一条消息，"
                  "同时发往所有已勾选渠道；某个渠道发送失败不影响其他渠道。凭据经 Windows "
                  "DPAPI 加密后仅保存在本机。",
             bg=CARD, fg=GRAY, font=(FONT, 9), justify="left", anchor="w",
             wraplength=500).pack(fill="x", padx=18)

    nrow0 = tk.Frame(notify_card, bg=CARD)
    nrow0.pack(fill="x", padx=18, pady=(12, 4))
    push_enabled_var = tk.IntVar(value=0)
    tk.Checkbutton(nrow0, text="开启消息推送", variable=push_enabled_var,
                   bg=CARD, fg=DARK, font=(FONT, 10, "bold"), selectcolor=CARD,
                   activebackground=CARD, bd=0).pack(side="left")
    only_failures_var = tk.IntVar(value=0)
    tk.Checkbutton(nrow0, text="仅在有账号失败时推送", variable=only_failures_var,
                   bg=CARD, fg=GRAY, font=(FONT, 9), selectcolor=CARD,
                   activebackground=CARD, bd=0).pack(side="left", padx=(20, 0))

    # 每个渠道一行：勾选框 + 名称 + 凭据输入；群机器人额外显示加签密钥输入
    ch_enable_vars: dict[str, tk.IntVar] = {}
    ch_key_vars: dict[str, tk.StringVar] = {}
    ch_secret_vars: dict[str, tk.StringVar] = {}
    ch_secret_labels: dict[str, tk.Label] = {}
    for ch in PUSH_CHANNELS:
        row = tk.Frame(notify_card, bg=CARD)
        row.pack(fill="x", padx=18, pady=3)
        var = tk.IntVar(value=0)
        ch_enable_vars[ch] = var
        tk.Checkbutton(row, text=PUSH_CHANNEL_LABELS[ch], variable=var,
                       bg=CARD, fg=DARK, font=(FONT, 9, "bold"), width=16,
                       anchor="w", selectcolor=CARD, activebackground=CARD,
                       bd=0).pack(side="left")
        key_var = tk.StringVar(value="")
        ch_key_vars[ch] = key_var
        cred_hint = "Webhook 地址" if ch in ("wecom", "dingtalk") else "Key / Token"
        tk.Entry(row, textvariable=key_var, font=(FONT, 9), width=34
                 ).pack(side="left", padx=(2, 8))
        secret_var = tk.StringVar(value="")
        ch_secret_vars[ch] = secret_var
        sec_lbl = tk.Label(row, text="加签密钥：", bg=CARD, fg=GRAY,
                           font=(FONT, 9))
        ch_secret_labels[ch] = sec_lbl
        sec_lbl.pack(side="left")
        tk.Entry(row, textvariable=secret_var, font=(FONT, 9), width=18
                 ).pack(side="left")
        if ch not in ("wecom", "dingtalk"):
            sec_lbl.pack_forget()
        tk.Label(row, text=cred_hint, bg=CARD, fg="#b6bdca",
                 font=(FONT, 8)).pack(side="left", padx=(4, 0))

    nrow4 = tk.Frame(notify_card, bg=CARD)
    nrow4.pack(fill="x", padx=18, pady=(8, 4))
    save_key_btn = tk.Button(nrow4, text="保存推送设置", bg=PRIMARY, fg="white",
                             font=(FONT, 9, "bold"), relief="flat",
                             activebackground=PRIMARY_D, activeforeground="white",
                             cursor="hand2", padx=12, pady=6, bd=0)
    save_key_btn.pack(side="left")
    test_push_btn = tk.Button(nrow4, text="发送测试消息（发往所有已勾选渠道）",
                              bg="#eef1f7", fg=DARK,
                              font=(FONT, 9), relief="flat", cursor="hand2",
                              padx=12, pady=6, bd=0)
    test_push_btn.pack(side="left", padx=(10, 0))
    getkey_btn = tk.Button(nrow4, text="如何获取凭据？", bg=BG, fg=PRIMARY,
                           font=(FONT, 9), relief="flat", cursor="hand2",
                           padx=6, pady=6, bd=0)
    getkey_btn.pack(side="left")
    push_state_var = tk.StringVar(value="")
    tk.Label(notify_card, textvariable=push_state_var, bg=CARD, fg=GRAY,
             font=(FONT, 9), justify="left", anchor="w",
             wraplength=500).pack(fill="x", padx=18, pady=(0, 12))

    # ── 自动任务卡片 ──
    task_card = card(body)
    task_card.pack(fill="x", pady=(14, 0))
    tk.Label(task_card, text="每日自动签到", bg=CARD, fg=DARK,
             font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=18,
                                                        pady=(16, 2))
    task_state_var = tk.StringVar(value="检测中…")
    tk.Label(task_card, textvariable=task_state_var, bg=CARD, fg=GRAY,
             font=(FONT, 9), anchor="w", justify="left").pack(fill="x", padx=18)

    trow = tk.Frame(task_card, bg=CARD)
    trow.pack(fill="x", padx=18, pady=12)
    tk.Label(trow, text="每天", bg=CARD, fg=DARK, font=(FONT, 10)).pack(side="left")
    time_var = tk.StringVar(value="09:00")
    time_entry = tk.Entry(trow, textvariable=time_var, width=6,
                          font=(FONT, 10), justify="center")
    time_entry.pack(side="left", padx=6)
    tk.Label(trow, text="自动签到（24小时制，如 09:00；修改时间后点击下方按钮生效）",
             bg=CARD, fg=GRAY, font=(FONT, 9)).pack(side="left")

    trow2 = tk.Frame(task_card, bg=CARD)
    trow2.pack(fill="x", padx=18, pady=(0, 16))
    enable_task_btn = tk.Button(trow2, text="开启 / 更新签到时间", bg=GREEN, fg="white",
                                font=(FONT, 10, "bold"), relief="flat",
                                activebackground="#15804c", activeforeground="white",
                                cursor="hand2", padx=14, pady=7, bd=0)
    enable_task_btn.pack(side="left")
    disable_task_btn = tk.Button(trow2, text="关闭自动签到", bg="#eef1f7", fg=DARK,
                                 font=(FONT, 9), relief="flat", cursor="hand2",
                                 padx=12, pady=7, bd=0)
    disable_task_btn.pack(side="left", padx=(10, 0))

    # 底部：打开日志
    bottom = tk.Frame(root, bg=BG)
    bottom.pack(fill="x", padx=24, pady=(0, 14))

    def open_log():
        try:
            os.startfile(str(LOG_FILE))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo(APP_NAME, f"日志位置：\n{LOG_FILE}")

    tk.Button(bottom, text="打开运行日志", bg=BG, fg=GRAY, font=(FONT, 8),
              relief="flat", bd=0, cursor="hand2",
              command=open_log).pack(side="left")
    tk.Label(bottom, text=f"客户端版本 {APP_VERSION}", bg=BG, fg="#9aa3b2",
             font=(FONT, 8)).pack(side="right")

    # ── 交互逻辑 ──

    def show_only(frame: tk.Frame):
        login_card.pack_forget()
        action_card.pack_forget()
        if frame is not None:
            # before=accounts_card 保证登录/操作卡始终位于多账号卡之前
            frame.pack(fill="x", pady=(14, 0), before=accounts_card)

    def do_checkin():
        checkin_btn.config(state="disabled", text="签到中…")
        result_var.set("正在与服务器通信，请稍候…")

        def worker():
            pf = platform_var.get()
            if pf == PLAT_WORKBUDDY:
                r = run_wb_checkin_live(status_only=False)
            else:
                r = run_checkin(status_only=False)
            if r.get("platform") is None:
                r["platform"] = pf
                r["platform_label"] = PLATFORM_LABELS.get(pf, pf)
            record_checkin_history([r])

            def done():
                checkin_btn.config(state="normal", text="立即签到")
                if r.get("ok") and r.get("suspicious"):
                    # 防假成功（t23）：流程 200 但缺成功信号，用警示图标区别于正常 ✓
                    result_var.set("⚠ " + r.get("message", "需人工确认"))
                elif r.get("ok"):
                    result_var.set("✓ " + r.get("message", "签到成功"))
                else:
                    result_var.set("✗ " + r.get("message", "签到失败"))
                refresh_task_state()
                render_history()
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    checkin_btn.config(command=do_checkin)
    refresh_btn.config(command=lambda: threading.Thread(target=refresh_ui,
                                                        daemon=True).start())

    def refresh_task_state():
        exists = task_exists()
        if exists:
            task_state_var.set("● 已开启：每天到点自动后台签到，关机错过会在开机后补签")
            enable_task_btn.config(state="normal")
            disable_task_btn.config(state="normal")
        else:
            task_state_var.set("○ 未开启：开启后将每天自动签到，无需手动操作")
            enable_task_btn.config(state="normal")
            disable_task_btn.config(state="normal")

    def on_enable_task():
        t = time_var.get().strip()
        enable_task_btn.config(state="disabled")
        task_state_var.set("正在创建定时任务…")

        def worker():
            ok_create, msg = create_scheduled_task(t)

            def done():
                refresh_task_state()
                if ok_create:
                    messagebox.showinfo(APP_NAME, msg + "\n关机错过时开机会自动补签。")
                else:
                    messagebox.showerror(APP_NAME, msg)
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def on_disable_task():
        disable_task_btn.config(state="disabled")

        def worker():
            delete_scheduled_task()
            root.after(0, lambda: (refresh_task_state(),
                                   messagebox.showinfo(APP_NAME, "已关闭每日自动签到。")))

        threading.Thread(target=worker, daemon=True).start()

    enable_task_btn.config(command=on_enable_task)
    disable_task_btn.config(command=on_disable_task)

    # ── 多账号交互 ──

    HINT_BY_PLATFORM = {
        PLAT_TRAEWORK: "TraeWork 客户端同一时间只能登录一个账号。请在客户端登录某账号后，"
                       "点击「保存当前登录账号」；切换账号登录后再次保存，即可收集多个账号。",
        PLAT_WORKBUDDY: "请先在 WorkBuddy 桌面端登录某账号，再点击「保存当前登录账号」；"
                        "退出后换另一个账号登录，再次保存即可收集多个 WorkBuddy 账号。",
    }

    def update_accounts_hint():
        pf = platform_var.get()
        label = PLATFORM_LABELS.get(pf, "")
        save_acc_btn.config(text=f"保存当前{label}账号")
        accounts_hint_var.set(HINT_BY_PLATFORM.get(pf, "") +
                              "全部已保存账号（含两个平台）会统一批量签到，无需保持登录。")

    def guide_relogin(pf: str, who: str = ""):
        """一键打开对应平台客户端，并给出重新登录→刷新凭据→重试的分步引导。"""
        label = PLATFORM_LABELS.get(pf, "客户端")
        ok, info = launch_platform_app(pf)
        if ok:
            log.info(f"已为账号 {who} 打开 {label} 客户端：{info}")
            messagebox.showinfo(
                APP_NAME,
                f"已打开 {label} 客户端（{info}）。\n\n"
                f"请按以下步骤刷新账号「{who or label}」：\n"
                f"1. 在客户端里重新登录该账号，登录成功后保持在线约 10 秒；\n"
                f"2. 回到本助手，顶部切换到{label}页签并点「保存当前登录账号」；\n"
                f"3. 点「全部账号签到」验证。\n\n"
                "提示：同一客户端同时只能登录一个账号，多个账号需逐个切换处理。")
        else:
            log.warning(f"打开 {label} 客户端失败：{info}")
            if messagebox.askyesno(
                    APP_NAME,
                    f"{info}\n\n是否手动选择 {label}.exe 的位置？"):
                path = filedialog.askopenfilename(
                    title=f"请选择 {label} 主程序（.exe）",
                    filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
                if path and launch_app(Path(path)):
                    messagebox.showinfo(
                        APP_NAME,
                        f"已启动所选程序。请登录账号后，\n"
                        "回到本助手点「保存当前登录账号」再重新签到。")

    def render_accounts():
        for w in acc_list_frame.winfo_children():
            w.destroy()
        accs = list_accounts()
        if not accs:
            accounts_state_var.set("还没有账号，按下面 4 步完成第一次签到：")
            batch_btn.config(state="disabled")
            update_accounts_hint()
            guide = tk.Frame(acc_list_frame, bg=P["row"],
                             highlightbackground=P["border"], highlightthickness=1)
            guide.pack(fill="x", pady=(2, 6))
            steps = (
                ("①", "打开客户端并登录",
                 "启动 TraeWork CN / WorkBuddy 桌面端，完成登录后保持在线约 10 秒"),
                ("②", "保存当前登录账号",
                 "回到本助手点下方「保存当前登录账号」，凭据经 DPAPI 加密保存到本机"),
                ("③", "配置微信推送（可选）",
                 "在「推送设置」里填好 Server酱 / 企业微信等密钥，签到结果自动推送到微信"),
                ("④", "签到并开启自动任务",
                 "点「全部账号签到」验证成功后，到「自动任务」开启每日定时自动签到"),
            )
            for num, step_title, step_desc in steps:
                srow = tk.Frame(guide, bg=P["row"])
                srow.pack(fill="x", padx=14, pady=(12, 0))
                badge = tk.Label(srow, text=num, bg=PRIMARY, fg="white",
                                 font=(FONT, 10, "bold"), width=2, height=1)
                badge.pack(side="left", anchor="n")
                txt = tk.Frame(srow, bg=P["row"])
                txt.pack(side="left", padx=(10, 0), fill="x", expand=True)
                tk.Label(txt, text=step_title, bg=P["row"], fg=DARK,
                         font=(FONT, 10, "bold"), anchor="w").pack(anchor="w")
                tk.Label(txt, text=step_desc, bg=P["row"], fg=GRAY,
                         font=(FONT, 8), anchor="w", wraplength=440,
                         justify="left").pack(anchor="w")
            btnrow = tk.Frame(guide, bg=P["row"])
            btnrow.pack(fill="x", padx=14, pady=14)

            def _empty_open_client():
                pf = platform_var.get()
                label = PLATFORM_LABELS.get(pf, "客户端")
                ok, info = launch_platform_app(pf)
                if ok:
                    messagebox.showinfo(
                        APP_NAME,
                        f"已打开 {label} 客户端（{info}）。\n\n"
                        "登录成功并保持在线约 10 秒后，\n"
                        "回到本助手点「保存当前登录账号」。", parent=root)
                else:
                    messagebox.showwarning(
                        "未找到客户端",
                        f"{info}\n\n请确认已安装 {label} 桌面端；也可以手动启动客户端、"
                        "登录后再回来保存账号。", parent=root)

            tk.Button(btnrow, text="立即打开客户端（第 ① 步）", bg=PRIMARY,
                      fg="white", font=(FONT, 9, "bold"), relief="flat", bd=0,
                      activebackground=PRIMARY_D, activeforeground="white",
                      cursor="hand2", padx=14, pady=6,
                      command=_empty_open_client).pack(side="left")
            return
        active_n = sum(1 for a in accs if a.get("enabled", True))
        if active_n == len(accs):
            accounts_state_var.set(f"已保存 {len(accs)} 个账号（跨平台统一批量签到）：")
        else:
            accounts_state_var.set(
                f"已保存 {len(accs)} 个账号，其中 {active_n} 个参与签到"
                "（取消勾选可临时停用）：")
        batch_btn.config(state="normal" if active_n else "disabled")
        update_accounts_hint()
        status_map = account_status_lookup(90)
        for i, a in enumerate(accs):
            row = tk.Frame(acc_list_frame, bg=P["row"],
                           highlightbackground=P["border"], highlightthickness=1)
            row.pack(fill="x", pady=3)
            info = tk.Frame(row, bg=P["row"])
            info.pack(side="left", fill="x", expand=True, padx=10, pady=6)
            uname = a.get('display_name') or a['username']
            note = (a.get("note") or "").strip()
            title = f"[{a.get('platform_label') or ''}] " + (
                f"{note}（{uname}）" if note else uname)
            name_color = DARK if a.get("enabled", True) else GRAY
            tk.Label(info, text=title, bg=P["row"], fg=name_color,
                     font=(FONT, 10, "bold"), anchor="w").pack(anchor="w")
            sub_parts = [f"地区 {a.get('region') or 'CN'}"]
            st = status_map.get(history_identity(a.get("platform", ""), uname))
            if st:
                if st.get("streak"):
                    sub_parts.append(f"连续 {st['streak']} 天")
                if st.get("last_date"):
                    sub_parts.append(f"上次签到 {st['last_date']} {st.get('last_time', '')}".rstrip())
            if a.get("saved_at"):
                sub_parts.append(f"保存于 {a['saved_at']}")
            if not a.get("enabled", True):
                sub_parts.append("已停用（不参与批量/自动签到）")
            tk.Label(info, text="　·　".join(sub_parts), bg=P["row"], fg=GRAY,
                     font=(FONT, 8), anchor="w").pack(anchor="w")

            def _toggle(on_var, key=a["key"]):
                set_account_enabled(key, bool(on_var.get()))
                render_accounts()

            enable_var = tk.IntVar(value=1 if a.get("enabled", True) else 0)
            tk.Checkbutton(row, text="参与签到", variable=enable_var,
                           bg=P["row"], fg=DARK, font=(FONT, 8),
                           selectcolor=P["row"], activebackground=P["row"],
                           bd=0, command=lambda v=enable_var: _toggle(v)
                           ).pack(side="right", padx=(0, 4), pady=6)

            def _edit_note(key=a["key"], name=uname, cur=note):
                val = simpledialog.askstring(
                    "账号备注", f"为账号「{name}」设置备注名（留空清除）：",
                    initialvalue=cur, parent=root)
                if val is not None:
                    set_account_note(key, val.strip())
                    render_accounts()

            tk.Button(row, text="备注", bg=P["note_bg"], fg=P["note_txt"],
                      font=(FONT, 8), relief="flat", cursor="hand2",
                      padx=10, pady=3, bd=0, command=_edit_note).pack(
                side="right", padx=(0, 2))

            def _relogin(pf=a.get("platform", PLAT_TRAEWORK), name=title):
                guide_relogin(pf, name)

            tk.Button(row, text="重登", bg=P["warn_bg"], fg=P["warn_txt"],
                      font=(FONT, 8), relief="flat", cursor="hand2",
                      activebackground=P["warn_bg_a"],
                      padx=10, pady=3, bd=0, command=_relogin).pack(
                side="right", padx=(0, 2))

            def _del(key=a["key"], name=uname):
                if messagebox.askyesno(APP_NAME, f"确定删除账号「{name}」的保存凭证吗？\n"
                                                 "（不会影响客户端登录，仅删除本工具的快照）"):
                    delete_account(key)
                    render_accounts()

            tk.Button(row, text="删除", bg=P["del_bg"], fg=P["del_txt"],
                      font=(FONT, 8), relief="flat", cursor="hand2",
                      activebackground=P["del_bg_a"],
                      padx=10, pady=3, bd=0, command=_del).pack(
                side="right", padx=10)

    def render_history():
        for w in history_frame.winfo_children():
            w.destroy()
        try:
            summ = history_summary(30)
            rows = history_recent_rows(14)
        except Exception as e:
            history_summary_var.set("历史记录读取失败。")
            tk.Label(history_frame, text=str(e), bg=CARD, fg=GRAY,
                     font=(FONT, 9), anchor="w").pack(anchor="w")
            return
        accs = summ.get("accounts") or []
        if not accs:
            history_summary_var.set("暂无历史记录。完成一次签到后，这里会展示连续天数与近 30 天成功率。")
            return
        head_parts = []
        if summ.get("today_total"):
            head_parts.append(f"今日 {summ['today_ok']}/{summ['today_total']} 个账号成功")
        else:
            head_parts.append("今日尚无签到记录")
        head_parts.append("近 30 天：")
        history_summary_var.set("　".join(head_parts))
        # 账号统计条
        stat_frame = tk.Frame(history_frame, bg=CARD)
        stat_frame.pack(fill="x")
        for a in accs:
            name = a["username"]
            short = name if len(name) <= 14 else name[:13] + "…"
            txt = (f"[{a['platform_label']}] {short}　连续 {a['streak']} 天　"
                   f"成功率 {a['rate']}%（{a['success']}/{a['total']}）")
            color = GREEN if a["rate"] >= 90 else (STATUS_AMBER if a["rate"] >= 50
                                                   else STATUS_RED)
            tk.Label(stat_frame, text=txt, bg=CARD, fg=color,
                     font=(FONT, 9, "bold"), anchor="w").pack(anchor="w", pady=1)
        # 近 90 天签到热力图（两行 × 15 周，GitHub 风格）
        try:
            tk.Label(history_frame, text="近 90 天签到热力图", bg=CARD, fg=GRAY,
                     font=(FONT, 9, "bold"), anchor="w").pack(
                anchor="w", pady=(10, 2))
            cal = history_calendar(90)
            dates_desc = sorted(cal.keys(), reverse=True)  # 今天 → 90 天前
            weeks = [dates_desc[i:i + 7] for i in range(0, 90, 7)]
            CELL, GAP = 13, 3
            HEAT_COLORS = {"all_ok": HEAT_OK, "partial": HEAT_PARTIAL,
                           "all_fail": HEAT_FAIL, "none": P["heat_none"]}
            HEAT_DESC = {"all_ok": "全部成功", "partial": "部分失败",
                         "all_fail": "全部失败", "none": "无记录"}
            cols = max(len(w) for w in weeks)
            rows_n = len(weeks)
            cv_w = cols * (CELL + GAP) + 4
            cv_h = rows_n * (CELL + GAP) + 4
            heat = tk.Canvas(history_frame, width=cv_w, height=cv_h,
                             bg=CARD, highlightthickness=0)
            heat.pack(anchor="w")
            tip_var = tk.StringVar(value="悬停色块可查看当天详情")
            tk.Label(history_frame, textvariable=tip_var, bg=CARD, fg=GRAY,
                     font=(FONT, 8), anchor="w").pack(anchor="w")

            def _date_items(d):
                cell = cal.get(d)
                if not cell:
                    return []
                out = []
                for it in cell.get("items", []):
                    nm = it.get("note") or it.get("username") or "?"
                    if it.get("ok") and it.get("suspicious"):
                        icon = "⚠ "
                    elif it.get("ok"):
                        icon = "✓ "
                    else:
                        icon = "✗ "
                    out.append(icon + str(nm))
                return out

            for r, week in enumerate(weeks):
                for c, d in enumerate(week):
                    cell = cal[d]
                    x0 = 2 + c * (CELL + GAP)
                    y0 = 2 + r * (CELL + GAP)
                    rid = heat.create_rectangle(
                        x0, y0, x0 + CELL, y0 + CELL,
                        fill=HEAT_COLORS[cell["state"]], outline="", width=0)
                    detail = "、".join(_date_items(d))

                    def _enter(_e, dd=d, cc=cell, dx=detail):
                        if cc["total"]:
                            tip_var.set(f"{dd}　{HEAT_DESC[cc['state']]}"
                                        f"（{cc['success']}/{cc['total']}）　{dx}")
                        else:
                            tip_var.set(f"{dd}　无签到记录")

                    def _leave(_e):
                        tip_var.set("悬停色块可查看当天详情")
                    heat.tag_bind(rid, "<Enter>", _enter)
                    heat.tag_bind(rid, "<Leave>", _leave)

            legend = tk.Frame(history_frame, bg=CARD)
            legend.pack(anchor="w", pady=(2, 0))
            for st_key in ("all_ok", "partial", "all_fail", "none"):
                tk.Label(legend, text="  ", bg=HEAT_COLORS[st_key],
                         font=(FONT, 8), padx=6).pack(side="left", padx=(8, 2))
                tk.Label(legend, text=HEAT_DESC[st_key], bg=CARD, fg=GRAY,
                         font=(FONT, 8)).pack(side="left")
        except Exception as e:
            log.warning(f"热力图渲染失败：{e}")

        # 积分趋势折线图（近 30 / 90 天，最多 6 条线）
        try:
            tk.Label(history_frame, text="积分趋势（签到后积分余额）",
                     bg=CARD, fg=GRAY, font=(FONT, 9, "bold"),
                     anchor="w").pack(anchor="w", pady=(10, 2))
            trend_seg = tk.Frame(history_frame, bg=CARD)
            trend_seg.pack(anchor="w")
            trend_days_var = tk.IntVar(value=30)
            trend_box = tk.Frame(history_frame, bg=CARD)
            trend_box.pack(fill="x")

            def render_trend():
                for w in trend_box.winfo_children():
                    w.destroy()
                trend = history_credit_trend(trend_days_var.get())
                if not trend.get("has_data"):
                    tk.Label(trend_box, text="历史记录中暂无积分数据，无法绘制趋势。",
                             bg=CARD, fg=GRAY, font=(FONT, 8),
                             anchor="w").pack(anchor="w")
                    return
                W, H = 520, 170
                # BY 为 X 轴（绘图区底边），必须靠近画布底部；
                # 早期版本误写成 24，绘图区仅 12px 高，刻度与折线全挤在顶部
                LX, RX, TY, BY = 38, 10, 20, H - 24
                dates = trend["dates"]
                n = len(dates)
                max_c = trend["max_credit"]
                # 整齐刻度（取 1/2/5×10^k）且保证 vmax >= max_c：
                # 既让刻度显示为 50/100/150…，又避免数据点画出绘图区顶边
                raw_step = max(1, (max_c + 3) // 4)
                unit = 1
                while raw_step > unit * 5:
                    unit *= 10
                step = unit * 5
                for cand in (unit, unit * 2, unit * 5):
                    if cand >= raw_step:
                        step = cand
                        break
                vmax = step * 4
                cv = tk.Canvas(trend_box, width=W, height=H, bg=CARD,
                               highlightthickness=0)
                cv.pack(anchor="w")
                grid_col = P["grid"]
                axis_col = P["axis"]
                for g in range(5):
                    v = step * g
                    yy = BY + (TY - BY) * v // vmax
                    cv.create_line(LX, yy, W - RX, yy, fill=grid_col)
                    cv.create_text(LX - 6, yy, text=str(v), anchor="e",
                                   fill=GRAY, font=(FONT, 7))
                cv.create_line(LX, TY, LX, BY, fill=axis_col)
                cv.create_line(LX, BY, W - RX, BY, fill=axis_col)
                # X 轴日期刻度（30 天：每 5 天；90 天：每 15 天）
                tick_step = 5 if n <= 45 else 15
                for i in range(0, n, tick_step):
                    xx = LX + (W - RX - LX) * i // (n - 1)
                    cv.create_text(xx, BY + 12, text=dates[i][5:],
                                   anchor="n", fill=GRAY, font=(FONT, 7))
                seg_ids = []

                def x_of(i):
                    return LX + (W - RX - LX) * i // (n - 1)

                def y_of(v):
                    return BY + (TY - BY) * v // vmax

                for s in trend["series"]:
                    pts, prev = [], None
                    for i, v in enumerate(s["points"]):
                        if v is None:
                            # pts 为扁平坐标（每点 2 个数字），至少 2 个点
                            # （4 个数字）才能画线，单点只保留数据点圆点
                            if len(pts) >= 4:
                                cv.create_line(pts, fill=s["color"], width=2,
                                               capstyle="round", joinstyle="round")
                            pts = []
                            prev = None
                            continue
                        pts.extend([x_of(i), y_of(v)])
                        prev = (x_of(i), y_of(i), v, i)
                    if len(pts) >= 4:
                        cv.create_line(pts, fill=s["color"], width=2,
                                       capstyle="round", joinstyle="round")
                    # 数据点（悬停命中区）
                    for i, v in enumerate(s["points"]):
                        if v is None:
                            continue
                        cx, cy = x_of(i), y_of(v)
                        dot = cv.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                                             fill=s["color"], outline=CARD)
                        seg_ids.append(dot)
                        lbl = s["label"]

                        def _enter(_e, cx=cx, cy=cy, v=v, dd=dates[i],
                                   lbl=lbl):
                            tip_var2.set(f"{dd}　{lbl}：{v} 积分")
                            cv.coords(tip_bg, cx + 6, cy - 22,
                                      cx + 6 + 9 * len(tip_var2.get()) // 2 + 10,
                                      cy - 4)
                            cv.coords(tip_txt, cx + 11, cy - 13)
                            cv.itemconfigure(tip_bg, state="normal")
                            cv.itemconfigure(tip_txt, state="normal")

                        def _leave(_e):
                            tip_var2.set("")
                            cv.itemconfigure(tip_bg, state="hidden")
                            cv.itemconfigure(tip_txt, state="hidden")
                        cv.tag_bind(dot, "<Enter>", _enter)
                        cv.tag_bind(dot, "<Leave>", _leave)
                tip_var2 = tk.StringVar(value="")
                tip_bg = cv.create_rectangle(0, 0, 0, 0, fill=P["tip_bg"],
                                             outline="", state="hidden")
                tip_txt = cv.create_text(0, 0, text="", anchor="w",
                                         fill=P["tip_fg"], font=(FONT, 8),
                                         state="hidden")
                cv.create_text(LX, 4, anchor="nw",
                               text="悬停数据点查看详情（缺失日期不断线补画）",
                               fill=GRAY, font=(FONT, 7))
                # 图例
                lg = tk.Frame(trend_box, bg=CARD)
                lg.pack(anchor="w", pady=(2, 0))
                for s in trend["series"]:
                    item = tk.Frame(lg, bg=CARD)
                    item.pack(side="left", padx=(0, 12))
                    tk.Label(item, text="  ", bg=s["color"],
                             font=(FONT, 8)).pack(side="left")
                    tk.Label(item, text=s["label"], bg=CARD, fg=DARK,
                             font=(FONT, 8)).pack(side="left", padx=(3, 0))
                if trend.get("truncated"):
                    tk.Label(trend_box,
                             text=f"账号较多，仅展示最近有记录的 {TREND_MAX_LINES} 个账号。",
                             bg=CARD, fg=GRAY, font=(FONT, 8),
                             anchor="w").pack(anchor="w")

            def _seg_flush():
                for b in (btn30, btn90):
                    active = b["days"] == trend_days_var.get()
                    b["w"].config(bg=PRIMARY if active else P["seg"],
                                  fg="#ffffff" if active else DARK,
                                  relief="flat", cursor="hand2",
                                  font=(FONT, 8, "bold" if active else "normal"))

            for label, days in (("近 30 天", 30), ("近 90 天", 90)):
                b = tk.Button(trend_seg, text=label, bd=0, padx=10, pady=2,
                              bg=P["seg"], fg=DARK, font=(FONT, 8),
                              command=lambda d=days: (
                                  trend_days_var.set(d), _seg_flush(),
                                  render_trend()))
                b.pack(side="left", padx=(0, 6))
                if days == 30:
                    btn30 = {"w": b, "days": days}
                else:
                    btn90 = {"w": b, "days": days}
            _seg_flush()
            render_trend()
        except Exception as e:
            log.warning(f"积分趋势渲染失败：{e}")

        # 近 3 个月月度统计
        try:
            monthly = history_monthly_stats(3)
            month_rows = [m for m in monthly if m.get("accounts")]
            if month_rows:
                tk.Label(history_frame, text="月度统计", bg=CARD, fg=GRAY,
                         font=(FONT, 9, "bold"), anchor="w").pack(
                    anchor="w", pady=(10, 2))
                month_box = tk.Frame(history_frame, bg=CARD)
                month_box.pack(fill="x")
                for mrow in month_rows:
                    line = tk.Frame(month_box, bg=CARD)
                    line.pack(fill="x", pady=1)
                    tk.Label(line, text=mrow["month"], bg=CARD, fg=GRAY,
                             font=(FONT, 8), width=9, anchor="w").pack(side="left")
                    for ma in mrow["accounts"]:
                        nm = ma["username"]
                        nm = nm if len(nm) <= 8 else nm[:7] + "…"
                        mcol = GREEN if ma["rate"] >= 90 else (
                            STATUS_AMBER if ma["rate"] >= 50 else STATUS_RED)
                        tk.Label(line,
                                 text=f"[{ma['platform_label']}] {nm} "
                                      f"{ma['success']}/{ma['total']}（{ma['rate']}%）",
                                 bg=CARD, fg=mcol, font=(FONT, 8),
                                 padx=6).pack(side="left")
        except Exception as e:
            log.warning(f"月度统计渲染失败：{e}")
        # 近 14 天明细
        tk.Label(history_frame, text="近 14 天明细", bg=CARD, fg=GRAY,
                 font=(FONT, 9, "bold"), anchor="w").pack(anchor="w",
                                                           pady=(10, 2))
        list_box = tk.Frame(history_frame, bg=CARD)
        list_box.pack(fill="x")
        weekday_cn = "周一 周二 周三 周四 周五 周六 周日".split()
        for row in rows:
            try:
                dt = datetime.strptime(row["date"], "%Y-%m-%d")
                date_txt = dt.strftime("%m-%d") + " " + weekday_cn[dt.weekday()]
            except Exception:
                date_txt = row["date"]
            line = tk.Frame(list_box, bg=CARD)
            line.pack(fill="x", pady=1)
            tk.Label(line, text=date_txt, bg=CARD, fg=GRAY,
                     font=(FONT, 8), width=10, anchor="w").pack(side="left")
            for it in row["items"]:
                mark = "✓" if it.get("ok") else "✗"
                col = GREEN if it.get("ok") else STATUS_RED
                nm = it.get("username") or "?"
                nm = nm if len(nm) <= 10 else nm[:9] + "…"
                tag = f"{mark} {nm}"
                tk.Label(line, text=tag, bg=CARD, fg=col,
                         font=(FONT, 8), padx=6).pack(side="left")

    def on_save_account():
        pf = platform_var.get()
        label = PLATFORM_LABELS.get(pf, "")
        save_acc_btn.config(state="disabled", text="保存中…")

        def worker():
            ok, msg = save_current_account(pf)

            def done():
                save_acc_btn.config(state="normal")
                update_accounts_hint()
                if ok:
                    messagebox.showinfo(APP_NAME, f"已保存{label}账号：{msg}\n\n"
                                                 f"如需添加更多账号，请在{label}客户端"
                                                 "退出并登录另一个账号后，再次点击保存。")
                    render_accounts()
                else:
                    messagebox.showwarning(APP_NAME, "保存失败：" + msg)
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def render_batch_guides(results: list[dict]):
        """批量签到后，为登录失效的平台渲染"一键打开重登"按钮。"""
        for w in batch_guide_frame.winfo_children():
            w.destroy()
        # 按平台聚合失效账号（仅引导可识别为登录态问题的失败）
        bad: dict[str, list[str]] = {}
        for r in results:
            if not _is_relogin_failure(r):
                continue
            pf = r.get("platform", PLAT_TRAEWORK)
            who = r.get("note") or r.get("username") or "未知账号"
            bad.setdefault(pf, []).append(who)
        if not bad:
            return
        tk.Label(batch_guide_frame,
                 text="检测到登录态失效，可一键打开客户端处理：",
                 bg=CARD, fg=P["warn_txt"], font=(FONT, 9, "bold"),
                 anchor="w").pack(anchor="w")
        grow = tk.Frame(batch_guide_frame, bg=CARD)
        grow.pack(fill="x", pady=(4, 0))
        for pf, names in bad.items():
            label = PLATFORM_LABELS.get(pf, "客户端")
            shown = "、".join(names[:3]) + ("…" if len(names) > 3 else "")

            def _open(pf=pf, shown=shown):
                guide_relogin(pf, shown)

            tk.Button(grow, text=f"打开{label}重登（{len(names)} 个账号）",
                      bg=P["warn_bg"], fg=P["warn_txt"], font=(FONT, 9),
                      relief="flat", cursor="hand2", padx=10, pady=4, bd=0,
                      activebackground=P["warn_bg_a"], command=_open
                      ).pack(side="left", padx=(0, 8))

    def on_batch_checkin():
        accs = [a for a in list_accounts() if a.get("enabled", True)]
        if not accs:
            messagebox.showinfo(APP_NAME,
                                "没有可签到的账号：请先保存账号，或勾选账号的「参与签到」。")
            return
        batch_btn.config(state="disabled", text="签到中…")
        save_acc_btn.config(state="disabled")
        batch_result_var.set(f"正在为 {len(accs)} 个账号逐个签到，请稍候…")

        def worker():
            results = run_batch_checkin(accs, status_only=False)
            record_checkin_history(results)

            def done():
                batch_btn.config(state="normal", text="全部账号签到")
                save_acc_btn.config(state="normal")
                ok_n = sum(1 for r in results if r.get("ok"))
                susp_n = sum(1 for r in results
                             if r.get("ok") and r.get("suspicious"))
                head = f"完成：{ok_n}/{len(results)} 个账号成功"
                if susp_n:
                    head += f"，其中 {susp_n} 个未抓到成功信号，需人工确认（见 ⚠ 行）"
                lines = [head]
                for r in results:
                    if r.get("ok") and r.get("suspicious"):
                        mark = "⚠"
                    elif r.get("ok"):
                        mark = "✓"
                    else:
                        mark = "✗"
                    who = f"[{r.get('platform_label', '')}] {r.get('username')}"
                    lines.append(f"{mark} {who}：{r.get('message')}")
                batch_result_var.set("\n".join(lines))
                render_batch_guides(results)
                render_history()
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    save_acc_btn.config(command=on_save_account)
    batch_btn.config(command=on_batch_checkin)

    # ── 消息推送交互（多渠道） ──

    # 已保存的明文凭据缓存：输入框留空保存时保留原值（凭据不回显）
    saved_creds: dict[str, dict] = {}

    def load_notify_settings():
        s = load_settings()
        saved_creds.clear()
        for ch in PUSH_CHANNELS:
            saved_creds[ch] = dict(s.get("creds", {}).get(ch)
                                   or {"key": "", "secret": ""})
        push_enabled_var.set(1 if s.get("push_enabled") else 0)
        only_failures_var.set(1 if s.get("only_failures") else 0)
        enabled = set(s.get("channels") or [])
        for ch in PUSH_CHANNELS:
            ch_enable_vars[ch].set(1 if ch in enabled else 0)
            # 安全考虑不回显明文；留空保存即沿用已保存凭据
            ch_key_vars[ch].set("")
            ch_secret_vars[ch].set("")
        configured = [PUSH_CHANNEL_LABELS[ch] for ch in PUSH_CHANNELS
                      if saved_creds.get(ch, {}).get("key")]
        if configured:
            push_state_var.set("已配置凭据（本机加密保存，界面不回显）："
                               + "、".join(configured)
                               + "。输入框留空保存可保留原凭据。")
        else:
            push_state_var.set("尚未配置任何推送凭据。")
        return s

    def _collect_settings():
        creds: dict[str, dict] = {}
        enabled_channels: list[str] = []
        for ch in PUSH_CHANNELS:
            old = saved_creds.get(ch) or {"key": "", "secret": ""}
            key = ch_key_vars[ch].get().strip() or old.get("key", "")
            secret = ch_secret_vars[ch].get().strip() or old.get("secret", "")
            creds[ch] = {"key": key, "secret": secret}
            if ch_enable_vars[ch].get():
                enabled_channels.append(ch)
        return {
            "version": SETTINGS_VERSION,
            "push_enabled": bool(push_enabled_var.get()),
            "channels": enabled_channels,
            "only_failures": bool(only_failures_var.get()),
            "creds": creds,
        }

    def _persist_loaded(s: dict):
        saved_creds.clear()
        for ch in PUSH_CHANNELS:
            saved_creds[ch] = dict(s.get("creds", {}).get(ch)
                                   or {"key": "", "secret": ""})
            ch_key_vars[ch].set("")
            ch_secret_vars[ch].set("")

    def on_save_push():
        s = _collect_settings()
        enabled = s["channels"]
        if s["push_enabled"]:
            if not enabled:
                messagebox.showwarning(APP_NAME, "请至少勾选一个推送渠道。")
                return
            missing = [PUSH_CHANNEL_LABELS[ch] for ch in enabled
                       if not s["creds"][ch]["key"]]
            if missing:
                messagebox.showwarning(
                    APP_NAME,
                    "以下已勾选渠道尚未填写凭据：\n" + "、".join(missing))
                return
        try:
            save_settings_dict(s)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存失败：{e}")
            return
        _persist_loaded(s)
        if s["push_enabled"] and enabled:
            push_state_var.set(
                "设置已保存，推送已开启，发往："
                + "、".join(PUSH_CHANNEL_LABELS[c] for c in enabled) + "。")
        else:
            push_state_var.set("设置已保存，推送当前为关闭状态。")
        messagebox.showinfo(APP_NAME, "推送设置已保存。")

    def on_test_push():
        s = _collect_settings()
        enabled = [c for c in s["channels"] if s["creds"][c]["key"]]
        if not enabled:
            messagebox.showwarning(
                APP_NAME,
                "请先勾选渠道并填写凭据（或先保存已配置的渠道）后再测试。")
            return
        test_push_btn.config(state="disabled", text="发送中…")
        push_state_var.set("正在向 " + "、".join(
            PUSH_CHANNEL_LABELS[c] for c in enabled) + " 发送测试消息…")

        def worker():
            title, content = build_checkin_report([], test=True)
            ok, msg = push_wechat(s, title, content, kind="推送测试")

            def done():
                test_push_btn.config(
                    state="normal", text="发送测试消息（发往所有已勾选渠道）")
                push_state_var.set(("✓ " if ok else "✗ ") + msg +
                                   ("" if ok else "（请检查凭据、加签设置与网络）"))
            root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def on_get_key():
        import webbrowser
        guides = {
            "serverchan": (
                "https://sct.ftqq.com/",
                "Server酱获取步骤：\n"
                "1. 浏览器打开 sct.ftqq.com，点击「登入」用微信扫码\n"
                "2. 按提示关注「方糖」服务号\n"
                "3. 在「Key & API」页面复制 SCT 开头的 SendKey\n"
                "4. 粘贴回本工具对应输入框，点击「保存推送设置」"),
            "pushplus": (
                "https://www.pushplus.plus/",
                "PushPlus 获取步骤：\n"
                "1. 浏览器打开 pushplus.plus 并用微信登录\n"
                "2. 在「一对一推送」页面复制 token\n"
                "3. 粘贴到输入框并保存\n"
                "注意：PushPlus 需实名后才有推送额度。"),
            "wecom": (
                "",
                "企业微信群机器人获取步骤：\n"
                "1. 在企业微信中打开要接收通知的群聊 → 右上角「…」→「群机器人」\n"
                "2. 点击「添加机器人」，名称可填「签到助手」\n"
                "3. 复制生成的 Webhook 地址"
                "（https://qyapi.weixin.qq.com/...?key=xxxx），整条粘贴到凭据框\n"
                "4. 安全设置二选一：\n"
                "   · 自定义关键词（填写：签到），此时加签密钥留空；\n"
                "   · 或勾选「加签」，把生成的密钥填入「加签密钥」框。\n"
                "5. 点击「保存推送设置」"),
            "dingtalk": (
                "",
                "钉钉群机器人获取步骤：\n"
                "1. 在钉钉中打开要接收通知的群聊 →「群设置」→「机器人」→「添加机器人」\n"
                "2. 选择「自定义」机器人，名称可填「签到助手」\n"
                "3. 安全设置二选一或多选：\n"
                "   · 自定义关键词：签到（本工具消息均含该词）；\n"
                "   · 或勾选「加签」，复制 SEC 开头的密钥填入「加签密钥」框。\n"
                "4. 创建后复制 Webhook 地址"
                "（https://oapi.dingtalk.com/...?access_token=xxxx），整条粘贴\n"
                "5. 点击「保存推送设置」"),
        }
        enabled = [ch for ch in PUSH_CHANNELS if ch_enable_vars[ch].get()]
        chosen = enabled or ["serverchan"]
        parts = []
        for ch in chosen:
            url, guide = guides[ch]
            if url:
                try:
                    webbrowser.open(url)
                except Exception:
                    guide += f"\n（请手动在浏览器打开：{url}）"
            parts.append(guide)
        messagebox.showinfo(APP_NAME, "\n\n".join(parts))

    save_key_btn.config(command=on_save_push)
    test_push_btn.config(command=on_test_push)
    getkey_btn.config(command=on_get_key)

    render_accounts()
    render_history()
    load_notify_settings()

    def _show_status_result(r: dict):
        who = f"[{r.get('platform_label', '')}] {r.get('username', '')}"
        if r.get("ok"):
            if r.get("checked_in"):
                extra = r.get("extra_credits")
                txt = f"✓ 今日已签到（{who}）"
                if r.get("credits") is not None:
                    txt += f"，{r['credits']} 积分"
                    if extra:
                        txt += f"（含额外 {extra}）"
                result_var.set(txt)
            else:
                result_var.set(f"○ 今日尚未签到（{who}），点击「立即签到」领取。")
        else:
            result_var.set("✗ " + r.get("message", "状态查询失败"))

    def refresh_ui():
        stop_waiting()
        pf = platform_var.get()
        if pf == PLAT_WORKBUDDY:
            refresh_ui_wb()
        else:
            refresh_ui_trae()

    def refresh_ui_trae():
        # 环境检测
        data_dir = get_app_data_dir()
        if data_dir is None and not find_install_exes():
            set_status(STATUS_RED, "未检测到 TraeWork CN 客户端",
                       "请先下载并安装 TraeWork CN（TRAE SOLO CN）桌面端，安装登录后重新打开本助手。")
            show_only(None)
            refresh_task_state()
            return

        logged_in, reason = is_logged_in()
        if not logged_in:
            exes = find_install_exes()
            login_title.config(text="需要先登录 TraeWork CN")
            if exes:
                set_status(STATUS_AMBER, "已安装，但尚未登录",
                           "点击下方按钮打开 TraeWork CN 并登录账号，助手会自动检测登录结果。")
                login_hint.config(
                    text="步骤：1) 点击「打开并登录」  2) 在应用中完成登录  "
                         "3) 登录成功后本助手自动继续。\n\n"
                         f"检测到程序：{exes[0].name}")
            else:
                set_status(STATUS_AMBER, "尚未登录",
                           "请打开 TraeWork CN 完成登录；若未安装请先安装客户端。")
                login_hint.config(text="未能自动定位程序，可点击「手动选择程序位置」指定主程序。")
            show_only(login_card)
            refresh_task_state()
            return

        # 已登录：查询状态
        login_title.config(text="需要先登录 TraeWork CN")
        set_status(GREEN, "环境正常，已登录 TraeWork CN",
                   "可在下方立即签到；点击「保存当前 TraeWork 账号」可把该账号加入批量签到列表")
        show_only(action_card)
        result_var.set("正在查询今日签到状态…")

        def worker():
            r = run_checkin(status_only=True)
            root.after(0, lambda: (_show_status_result(r), refresh_task_state()))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_ui_wb():
        auth_file = wb_find_auth_file()
        if not auth_file:
            exes = find_wb_install_exes()
            login_title.config(text="需要先登录腾讯 WorkBuddy")
            if exes:
                set_status(STATUS_AMBER, "已安装 WorkBuddy，但尚未登录",
                           "点击下方按钮打开 WorkBuddy 并登录账号，助手会自动检测登录结果。")
                login_hint.config(
                    text="步骤：1) 点击「打开并登录」  2) 在 WorkBuddy 中完成登录  "
                         "3) 登录成功后本助手自动继续。\n\n"
                         f"检测到程序：{exes[0].name}")
            else:
                set_status(STATUS_RED, "未检测到 WorkBuddy 客户端",
                           "请先下载并安装腾讯 WorkBuddy 桌面端（G:\\workb 或默认安装目录），登录后重新检测。")
                login_hint.config(text="未能自动定位 WorkBuddy，可先安装客户端，"
                                       "或点击「手动选择程序位置」指定 WorkBuddy.exe。")
            show_only(login_card)
            refresh_task_state()
            return

        logged_in, reason = wb_is_logged_in()
        if not logged_in:
            login_title.config(text="需要先登录腾讯 WorkBuddy")
            set_status(STATUS_AMBER, "检测到 WorkBuddy，但尚未登录",
                       "请打开 WorkBuddy 完成登录，登录后点击「重新检测」。")
            login_hint.config(
                text=f"凭证文件：{auth_file}\n点击「打开并登录」启动 WorkBuddy 客户端。")
            show_only(login_card)
            refresh_task_state()
            return

        login_title.config(text="需要先登录腾讯 WorkBuddy")
        set_status(GREEN, "环境正常，已登录 WorkBuddy",
                   "可在下方立即签到；点击「保存当前 WorkBuddy 账号」可把该账号加入批量签到列表")
        show_only(action_card)
        result_var.set("正在查询今日签到状态…")

        def worker():
            r = run_wb_checkin_live(status_only=True)
            root.after(0, lambda: (_show_status_result(r), refresh_task_state()))

        threading.Thread(target=worker, daemon=True).start()

    # 深色冷启动：静态控件创建时用的是字面浅色，这里按持久化主题统一着色一次（不回写）
    apply_theme(_initial_theme, persist=False)

    def notify_previous_crashes() -> None:
        """上次运行若留有崩溃转存，启动后提示一次，并提供一键诊断包入口。"""
        try:
            dumps = list_crash_dumps()
            if not dumps:
                return
            latest = dumps[0]
            mtime = datetime.fromtimestamp(
                latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            more = f"\n（共发现 {len(dumps)} 份崩溃记录）" if len(dumps) > 1 else ""
            choice = messagebox.askyesnocancel(
                f"{APP_NAME}上次运行异常退出",
                f"检测到程序在 {mtime} 发生过一次未处理异常。{more}\n\n"
                "「是」：立即导出诊断包（含崩溃堆栈，已脱敏，可发给开发者）\n"
                "「否」：查看后清除崩溃记录，不再提醒\n"
                "「取消」：暂不处理，下次启动继续提醒")
            if choice is None:
                return
            if choice:
                path = filedialog.asksaveasfilename(
                    title="导出诊断包",
                    initialfile=f"traecheckin_diagnostic_"
                                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                    defaultextension=".zip",
                    filetypes=[("诊断包", "*.zip")])
                if path:
                    export_diagnostic_bundle(path)
                    messagebox.showinfo(
                        APP_NAME, f"诊断包已导出：\n{path}\n\n"
                                  "其中包含崩溃堆栈与脱敏后的环境信息，"
                                  "可发送给开发者协助排查。")
                    clear_crash_dumps()
            else:
                clear_crash_dumps()
        except Exception as e:
            log.warning(f"崩溃记录提示流程异常：{e}")

    # 首次启动：风险与隐私声明确认；测试可用 TRAESIGN_NO_DISCLAIMER=1 跳过
    if os.environ.get("TRAESIGN_NO_DISCLAIMER") != "1":
        try:
            if not load_settings().get("disclaimer_accepted"):
                root.withdraw()
                if not show_disclaimer_dialog():
                    root.destroy()
                    return 0
                accept_disclaimer()
                root.deiconify()
        except Exception as e:
            log.warning(f"首次启动声明流程异常：{e}")

    # 上次运行若异常退出，启动后提示并可一键导出含崩溃堆栈的诊断包
    if os.environ.get("TRAESIGN_NO_CRASH_NOTICE") != "1":
        root.after(300, notify_previous_crashes)

    # 初次检测放到后台，避免读取/解密阻塞 UI
    root.after(100, lambda: threading.Thread(target=refresh_ui, daemon=True).start())
    root.mainloop()
    return 0


# ──────────────────────────── 入口 ────────────────────────────

# 全局互斥锁句柄，必须在整个程序生命周期内保持引用
_mutex_handle = None
ERROR_ALREADY_EXISTS = 183


def acquire_single_instance() -> bool:
    """获取全局命名互斥锁，保证只运行一个 GUI 实例。

    返回 True 表示当前为唯一实例；False 表示已有实例在运行。
    仅在 Windows 上生效，其他平台直接放行。
    """
    global _mutex_handle
    if platform.system() != "Windows":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _mutex_handle = kernel32.CreateMutexW(None, 0, "Local\\TraeWorkCheckinApp_SingleInstance_v1")
        last_error = ctypes.get_last_error()
        if last_error == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception:
        # 检测失败时不影响正常使用
        return True


def bring_existing_window_to_front() -> None:
    """已有实例运行时，尝试把它的主窗口置顶还原。"""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _today_done_identities() -> set:
    """今日历史桶中已成功签到的账号标识集合。"""
    today = datetime.now().strftime("%Y-%m-%d")
    bucket = load_history().get("days", {}).get(today) or {}
    return {ident for ident, item in bucket.items()
            if isinstance(item, dict) and item.get("ok")}


def _silent_expected_identities(accounts: list[dict]) -> set:
    """
    静默运行预期会签到的账号集合：
      - 所有启用的账号快照；
      - 近 7 天历史中出现过、但其平台未被快照覆盖的桌面端 live 兜底账号
        （例如只保存了 TraeWork 快照，但 WorkBuddy 也一直靠登录态自动签）。
    """
    enabled = [a for a in accounts if a.get("enabled", True)]
    expected = {history_identity(a.get("platform", ""), a.get("username", ""))
                for a in enabled}
    saved_platforms = {a.get("platform") for a in enabled}
    for row in history_recent_rows(7):
        for item in row.get("items", []):
            if item.get("platform") not in saved_platforms:
                expected.add(history_identity(
                    item.get("platform", ""), item.get("username", "")))
    return expected


def _silent_retry_config() -> tuple[int, int, int]:
    """
    失败分轮重试配置，返回 (重试轮数, 最小等待秒, 最大等待秒)。
    可用环境变量覆盖以便测试：
      TRAESIGN_RETRY_ROUNDS（默认 2，即首轮失败后再试 2 轮）
      TRAESIGN_RETRY_WAIT_MIN（默认 1200 秒 = 20 分钟）
      TRAESIGN_RETRY_WAIT_MAX（默认 2400 秒 = 40 分钟）
    """
    def _int_env(name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default
    rounds = _int_env("TRAESIGN_RETRY_ROUNDS", 2)
    wait_min = _int_env("TRAESIGN_RETRY_WAIT_MIN", 1200)
    wait_max = _int_env("TRAESIGN_RETRY_WAIT_MAX", 2400)
    if wait_max < wait_min:
        wait_max = wait_min
    return rounds, wait_min, wait_max


def _merge_results(old: list[dict], new: list[dict]) -> list[dict]:
    """
    按平台+用户名合并两轮结果：成功记录永不被后续失败覆盖；
    失败记录则以最新一轮结果为准（消息可能从「网络错误」变成成功或更具体原因）。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for r in old + new:
        ident = history_identity(r.get("platform", ""), r.get("username", ""))
        if ident not in merged:
            merged[ident] = r
            order.append(ident)
            continue
        if merged[ident].get("ok"):
            continue
        merged[ident] = r
    return [merged[i] for i in order]


def _silent_run_with_retry(accounts: list[dict]) -> list[dict]:
    """
    执行一轮批量签到（含桌面端 live 兜底），失败账号分轮重试。
    每轮仅重试上一轮仍失败的账号；成功结果不被覆盖；全部轮次结束后统一返回。
    """
    active = [a for a in accounts if a.get("enabled", True)]
    saved_platforms = {a.get("platform") for a in active}

    if active:
        log.info(f"开始批量签到，共 {len(active)} 个启用账号（双平台）")
        # run_batch_checkin 对每个启用账号恰好产出一条结果，顺序与 active 对齐
        batch_results = run_batch_checkin(active, status_only=False)
        for acct, r in zip(active, batch_results):
            r["_acct_key"] = acct.get("key", "")
        results = list(batch_results)
        # live 兜底：首轮尝试未覆盖平台；后续轮次只重试失败平台
        results = _silent_append_live(results, saved_platforms)
    else:
        log.info("未配置多账号，尝试当前客户端登录账号签到（TraeWork / WorkBuddy）")
        batch_results = []
        results = _silent_append_live([])

    for r in results:
        (log.info if r.get("ok") else log.error)(
            f"[{r.get('platform_label', '?')}/{r.get('username', '?')}] "
            f"{r.get('message', '')}")

    rounds, wait_min, wait_max = _silent_retry_config()
    for round_no in range(1, rounds + 1):
        # 快照账号按 key 对齐失败结果（_acct_key 为首轮内部标记）
        ok_keys = {r.get("_acct_key") for r in results
                   if r.get("ok") and r.get("_acct_key")}
        failed_keys = {r.get("_acct_key") for r in results
                       if not r.get("ok") and r.get("_acct_key")}
        failed_saved = [a for a in active
                        if a.get("key", "") in failed_keys
                        and a.get("key", "") not in ok_keys]
        # live 平台：首轮结果中失败的平台才重试
        live_failed_platforms = {
            r.get("platform") for r in results
            if not r.get("ok") and r.get("platform") not in saved_platforms}
        if not failed_saved and not live_failed_platforms:
            break
        wait_s = random.randint(wait_min, wait_max) if wait_max else wait_min
        log.info(f"第 {round_no}/{rounds} 轮重试：{len(failed_saved)} 个快照账号、"
                 f"{len(live_failed_platforms)} 个 live 平台仍失败，"
                 f"{wait_s} 秒后重试")
        if wait_s:
            time.sleep(wait_s)
        round_results: list[dict] = []
        if failed_saved:
            retry_batch = run_batch_checkin(failed_saved, status_only=False)
            for acct, r in zip(failed_saved, retry_batch):
                r["_acct_key"] = acct.get("key", "")
            round_results.extend(retry_batch)
        if live_failed_platforms:
            round_results = _silent_append_live(
                round_results,
                saved_platforms | ({PLAT_TRAEWORK, PLAT_WORKBUDDY}
                                   - live_failed_platforms))
        for r in round_results:
            (log.info if r.get("ok") else log.error)(
                f"[重试{round_no}][{r.get('platform_label', '?')}/"
                f"{r.get('username', '?')}] {r.get('message', '')}")
        results = _merge_results(results, round_results)
    # 剥离仅供本轮重试对齐使用的内部字段
    for r in results:
        r.pop("_acct_key", None)
    return results


def _silent_record_and_push(results: list[dict]) -> None:
    """统一写入历史并按设置推送日报（全部轮次重试结束后只推一次）。"""
    ok_n = sum(1 for r in results if r.get("ok"))
    suspicious_n = sum(1 for r in results
                       if r.get("ok") and r.get("suspicious"))
    record_checkin_history(results)
    settings = None
    try:
        settings = load_settings()
        if settings.get("push_enabled") and push_configured(settings):
            fail_n = len(results) - ok_n
            # 「仅失败时推送」下，假成功（suspicious）也必须推送，否则被静默吞掉
            if not (settings.get("only_failures")
                    and fail_n == 0 and suspicious_n == 0):
                title, content = build_checkin_report(results)
                pushed, pmsg = push_wechat(settings, title, content,
                                           kind="签到结果")
                (log.info if pushed else log.error)(f"推送结果：{pmsg}")
            else:
                log.info("全部成功且开启了「仅失败时推送」，本次不推送日报。")
    except Exception as e:
        log.error(f"推送流程异常：{e}")
    # 连续失败「疑似掉线」预警：独立保护，任何异常都不影响主流程与日报
    try:
        check_and_push_offline_alerts(settings)
    except Exception as e:
        log.error(f"掉线预警异常：{e}")


# ─────────────────────── 连续失败「疑似掉线」预警 ───────────────────────

OFFLINE_ALERT_KEY = "offline_alerts"


def _offline_streak_days() -> int:
    """连续失败多少天触发预警，默认 3；TRAESIGN_OFFLINE_DAYS 可压缩以便测试。"""
    try:
        return max(1, int(os.environ.get("TRAESIGN_OFFLINE_DAYS", "3")))
    except (TypeError, ValueError):
        return 3


def detect_offline_accounts(results: Optional[list[dict]] = None,
                            threshold: Optional[int] = None) -> list[dict]:
    """扫描历史，找出截至今天已连续失败 ≥ threshold 天的账号。

    判定规则（从今天含今天向过去逐天查看，最多回溯 30 天）：
      - 整天 bucket 缺失（当天机器根本没跑）：所有人的连续计数到此中断；
      - bucket 存在但缺该账号记录（机器跑了、该账号未参与/被移除）：
        该账号的连续计数中断，避免把"没签到"误算成"签到失败"；
      - 当天该账号成功（含「已签到」）：连续失败中断；
      - 连续失败天数达到阈值即命中。
    results：保留给调用方传当天内存结果；本函数统一读落盘历史，故未使用。
    """
    threshold = threshold or _offline_streak_days()
    days = load_history().get("days") or {}
    today = datetime.now().date()
    # 静默流程在调用前已 record_checkin_history，今天的结果已在历史里
    idents: dict[str, dict] = {}
    for i in range(30):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        bucket = days.get(d)
        if not isinstance(bucket, dict):
            break  # 当天机器没跑：所有人的连续计数到此为止
        seen: set[str] = set()
        for ident, item in bucket.items():
            if not isinstance(item, dict):
                continue
            seen.add(ident)
            info = idents.setdefault(ident, {
                "ident": ident,
                "platform": item.get("platform", ""),
                "platform_label": item.get("platform_label", "")
                                  or PLATFORM_LABELS.get(item.get("platform", ""), ""),
                "username": item.get("username", ""),
                "note": item.get("note", ""),
                "streak": 0, "last_message": "", "alive": True})
            info["note"] = info["note"] or item.get("note", "") or ""
            if info["alive"]:
                if item.get("ok"):
                    info["alive"] = False  # 遇到成功，连续失败中断
                else:
                    info["streak"] += 1
                    # 遍历从今天向过去，保留最近一次（第一次遇到）的报错
                    if not info["last_message"]:
                        info["last_message"] = item.get("message", "") or ""
        # 机器当天跑了但没有该账号的结果：不能算作连续失败的一天
        for ident, info in idents.items():
            if info["alive"] and ident not in seen:
                info["alive"] = False
    return [v for v in idents.values()
            if v["alive"] and v["streak"] >= threshold]


def _offline_alert_content(accs: list[dict]) -> tuple[str, str]:
    now = datetime.now()
    min_streak = min(int(a.get("streak", 0) or 0) for a in accs)
    title = (f"⚠️ 疑似掉线：{len(accs)} 个账号连续 {min_streak} 天签到失败 "
             f"{now.strftime('%m-%d')}")
    lines = ["**疑似账号掉线 / 登录态失效，请重新登录**", "",
             f"以下账号已连续 {min_streak} 天以上签到失败：", ""]
    for a in accs:
        name = a.get("note") or a.get("username") or a.get("ident")
        plat = a.get("platform_label") or PLATFORM_LABELS.get(
            a.get("platform", ""), "")
        lines.append(f"### 🔴 [{plat}] {name}")
        lines.append(f"连续失败 {a['streak']} 天"
                     + (f"，最近报错：{a['last_message']}"
                        if a.get("last_message") else ""))
        lines.append("")
    lines += [
        "---", "### 处理步骤", "",
        "1. 打开对应桌面客户端（TraeWork CN / WorkBuddy）；",
        "2. 检查登录状态，如已退出请重新登录；",
        "3. 登录成功后保持在线约 10 秒；",
        "4. 打开本助手点「保存当前登录账号」更新凭据，",
        "    再点「全部账号签到」验证；",
        "5. 若账号正常但仍失败，可点「健康自检」排查网络与客户端。", "",
        "在恢复成功签到前，本条提醒每天最多发送一次，不会重复打扰。",
        f"提醒时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return title, "\n".join(lines)


def check_and_push_offline_alerts(settings: Optional[dict] = None) -> int:
    """检测连续失败账号并按需推送预警，返回本次推送的账号数。

    去重：settings.offline_alerts[ident] = "YYYY-MM-DD"，
    同一掉线周期（未恢复成功前）每天最多推一次；账号恢复成功后
    历史连续中断，旧标记保留也不会再次命中（命中以 streak 为准）。
    """
    try:
        settings = settings if settings is not None else load_settings()
        if not (settings.get("push_enabled") and push_configured(settings)):
            return 0
        accs = detect_offline_accounts()
        if not accs:
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        alerts = settings.get(OFFLINE_ALERT_KEY)
        if not isinstance(alerts, dict):
            alerts = {}
        fresh = [a for a in accs if alerts.get(a["ident"]) != today]
        if not fresh:
            log.info(f"{len(accs)} 个账号连续失败但今日已预警过，跳过重复推送。")
            return 0
        title, content = _offline_alert_content(fresh)
        pushed, pmsg = push_wechat(settings, title, content,
                                   kind="掉线预警")
        if pushed:
            for a in fresh:
                alerts[a["ident"]] = today
            # 清理 60 天前的旧标记
            cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            alerts = {k: v for k, v in alerts.items()
                      if isinstance(v, str) and v >= cutoff}
            settings[OFFLINE_ALERT_KEY] = alerts
            save_settings_dict(settings)
            log.info(f"掉线预警已推送：{len(fresh)} 个账号（{pmsg}）")
            return len(fresh)
        log.error(f"掉线预警推送失败：{pmsg}")
        return 0
    except Exception as e:
        log.error(f"掉线预警流程异常：{e}")
        return 0


def _send_periodic_reports() -> None:
    """
    周一周报、每月 1~3 号月报（补上月初几天机器没开机的情况）。
    用标记文件记录当天已发送项，防止同一天开机/登录触发器重复推送。
    周报/月报属于汇总消息，不受「仅失败时推送」开关影响。
    """
    try:
        now = datetime.now()
        pending = []
        if now.weekday() == 0:
            pending.append(("weekly_" + now.strftime("%Y-%m-%d"),
                            build_weekly_report))
        if now.day <= 3:
            pending.append(("monthly_" + now.strftime("%Y-%m"),
                            build_monthly_report))
        if not pending:
            return
        markers = _load_json_file(PERIOD_MARKERS_FILE)
        sent = markers.get("sent")
        if not isinstance(sent, dict):
            sent = {}
        todo = [(key, builder) for key, builder in pending if not sent.get(key)]
        if not todo:
            return
        settings = load_settings()
        if not (settings.get("push_enabled") and push_configured(settings)):
            log.info("周报/月报已到期，但推送未开启或未配置凭据，本次跳过。")
            return
        for key, builder in todo:
            try:
                title, content = builder()
                kind = "签到周报" if key.startswith("weekly_") else "签到月报"
                pushed, pmsg = push_wechat(settings, title, content, kind=kind)
                if pushed:
                    sent[key] = now.strftime("%Y-%m-%d %H:%M")
                    log.info(f"周期报告已推送：{title}（{pmsg}）")
                else:
                    log.error(f"周期报告推送失败：{pmsg}")
            except Exception as e:
                log.error(f"周期报告生成/推送异常：{e}")
        # 仅清理 60 天前的旧标记
        cutoff = (now - timedelta(days=60)).strftime("%Y-%m-%d")
        sent = {k: v for k, v in sent.items()
                if k[-10:] >= cutoff or not isinstance(v, str)}
        _save_json_file(PERIOD_MARKERS_FILE, {"sent": sent})
    except Exception as e:
        log.warning(f"周期报告检查失败（不影响签到）：{e}")


def silent_run() -> int:
    """定时任务静默执行：周期报告 → 批量签到（失败分轮重试）→ 汇总推送。"""
    log.info("=" * 40)
    # 周报/月报在早退判断之前发送：即使今天账号都已签到，周一/月初也应收报告
    try:
        _send_periodic_reports()
    except Exception as e:
        log.warning(f"周期报告流程异常：{e}")

    accounts = list_accounts()
    # 今日已全部成功签到则立即退出：每日定时任务通常先跑，开机/登录触发器
    # 当天可能再次触发，早退可避免重复请求和重复推送。
    # 设置环境变量 TRAESIGN_FORCE=1 可强制重跑（排查用）。
    if os.environ.get("TRAESIGN_FORCE") != "1":
        try:
            expected = _silent_expected_identities(accounts)
            done = _today_done_identities()
            if expected and expected <= done:
                log.info(
                    f"今日 {len(expected)} 个账号均已成功签到，静默任务直接退出"
                    "（不重复签到、不重复推送）")
                return 0
        except Exception as e:
            log.warning(f"今日签到状态判断失败，按正常流程执行：{e}")
    # 随机抖动 0~5 分钟，避免每天固定整点请求（可通过环境变量关闭，便于测试）
    if os.environ.get("TRAESIGN_NO_JITTER") != "1":
        jitter = random.randint(0, 300)
        if jitter:
            log.info(f"随机延迟 {jitter} 秒后开始签到（避免整点并发）")
            time.sleep(jitter)

    results = _silent_run_with_retry(accounts)
    if not results:
        log.error("未检测到任何已登录平台（TraeWork / WorkBuddy）。")
        return 1
    _silent_record_and_push(results)
    ok_n = sum(1 for r in results if r.get("ok"))
    return 0 if ok_n == len(results) else 1


def _silent_append_live(results: list[dict],
                        saved_platforms: Optional[set] = None,
                        status_only: bool = False) -> list[dict]:
    """对尚未保存快照的平台，补签其当前桌面端登录账号（静默兜底）。"""
    saved_platforms = saved_platforms if saved_platforms is not None else set()
    if PLAT_TRAEWORK not in saved_platforms:
        try:
            logged, _ = is_logged_in()
        except Exception:
            logged = False
        if logged:
            results.append(run_checkin(status_only=status_only))
    if PLAT_WORKBUDDY not in saved_platforms:
        try:
            wb_logged, _ = wb_is_logged_in()
        except Exception:
            wb_logged = False
        if wb_logged:
            results.append(run_wb_checkin_live(status_only=status_only))
    return results


def main() -> int:
    # 静默/命令行模式也安装异常钩子，确保计划任务里的崩溃完整写入日志
    install_exception_hooks(popup=False)
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--silent", action="store_true",
                        help="静默签到（供定时任务调用，多账号批量+微信推送）")
    parser.add_argument("--status", action="store_true",
                        help="命令行查询状态")
    args = parser.parse_args()

    if args.silent:
        return silent_run()

    if args.status:
        accounts = list_accounts()
        if accounts:
            results = run_batch_checkin(accounts, status_only=True)
            results = _silent_append_live(
                results, {a.get("platform") for a in accounts},
                status_only=True)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 0 if all(r.get("ok") for r in results) else 1
        # 无快照：依次探测两个平台当前登录态
        results = _silent_append_live([], status_only=True)
        if not results:
            print(json.dumps(
                {"ok": False, "message": "未检测到任何已登录平台"},
                ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(r.get("ok") for r in results) else 1

    # GUI 模式：单实例保护，重复双击只会激活已有窗口
    if not acquire_single_instance():
        bring_existing_window_to_front()
        return 0

    # 默认打开图形界面；无显示环境时回退命令行签到
    try:
        import tkinter  # noqa: F401
        return run_gui()
    except Exception as e:
        log.error(f"无法启动图形界面：{e}")
        r = run_checkin(status_only=False)
        print(r.get("message", "完成"))
        return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
