# -*- coding: utf-8 -*-
"""崩溃转存、错误弹窗与全局异常钩子。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone, timedelta
import os
import platform
import secrets
import sys
import threading
import time
import traceback
from typing import Any, Optional
from .constants import APP_NAME
from .runtime import APP_VERSION, LOG_FILE, _log_dir, log


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
