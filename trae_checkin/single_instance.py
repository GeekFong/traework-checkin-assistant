# -*- coding: utf-8 -*-
"""Windows 命名互斥量单实例保护。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import ctypes
import platform
from typing import Any, Optional
from .constants import APP_NAME


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
