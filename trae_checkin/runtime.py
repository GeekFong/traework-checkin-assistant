# -*- coding: utf-8 -*-
"""运行环境：打包判定、数据目录、日志、客户端版本探测、子进程封装。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from pathlib import Path
import json
import locale
import logging
import os
import subprocess
import sys
import logging.handlers
from typing import Any, Optional
from .constants import INSTALL_DIR_NAMES


APP_VERSION = "1.1.0"


def is_frozen() -> bool:
    """是否为 PyInstaller 打包后的 exe。"""
    return getattr(sys, "frozen", False)


def package_root() -> Path:
    """trae_checkin 包目录（本文件位于 trae_checkin/runtime.py）。"""
    return Path(__file__).resolve().parent


def project_root() -> Path:
    """仓库根目录（trae_checkin 的上一级）。"""
    return package_root().parent


def app_dir() -> Path:
    """程序目录：打包后为 exe 所在目录，源码态为仓库根目录。

    数据目录优先在此探写，因此源码态返回仓库根，保证免安装体验
    与单文件版一致（数据落在程序旁边而非 site-packages 内）。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def exe_path() -> Path:
    """当前可执行文件路径（打包后为 exe，源码态为包入口 __main__.py）。"""
    if is_frozen():
        return Path(sys.executable).resolve()
    return package_root() / "__main__.py"


def resource_path(name: str) -> Path:
    """资源文件（如图标）路径。

    打包态：
      · onefile：PyInstaller 把 --add-data 解压到 sys._MEIPASS；
      · onedir：资源位于 exe 旁的 assets。
    源码态：仓库根的 assets 目录。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / "assets" / name
            if candidate.is_file():
                return candidate
        return Path(sys.executable).resolve().parent / "assets" / name
    return project_root() / "assets" / name


def entry_args() -> list[str]:
    """计划任务 / 外部调用时的命令前缀（不含业务参数）。

    打包态：[exe]；源码态：[python, -m, trae_checkin]，工作目录需为仓库根。
    """
    if is_frozen():
        return [Path(sys.executable).resolve()]
    return [Path(sys.executable).resolve(), "-m", "trae_checkin"]


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


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
