# -*- coding: utf-8 -*-
"""桌面客户端安装探测、版本探测与启动引导。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from pathlib import Path
import os
import platform
import subprocess
from typing import Any, Optional
from .constants import APPDATA_DIR_NAMES, EXE_NAMES, INSTALL_DIR_NAMES, PLATFORM_LABELS, PLAT_WORKBUDDY
from .runtime import _run


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
