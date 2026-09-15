# -*- coding: utf-8 -*-
"""pytest 公共夹具。

核心目标：所有写入性测试都重定向到临时目录，绝不污染真实的
accounts.json / settings.json / checkin_history.json / push_history.json。

拆分后的包把数据路径常量在导入期固化到各自模块（accounts.SETTINGS_FILE
等），因此这里不是简单设置 TRAESIGN_HOME 后重新 import，而是把各模块的
路径常量与 runtime._data_dir() 一并 monkeypatch 到临时目录。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    """把全部数据文件路径重定向到临时目录并返回该目录。

    覆盖：
      - 各模块导入期固化的路径常量
      - runtime.LOG_FILE（日志）
      - runtime._data_dir()（backup 模块动态取目录用）
      - TRAESIGN_HOME 环境变量（双保险）
    """
    home = tmp_path / "data"
    home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("TRAESIGN_HOME", str(home))

    from trae_checkin import (
        accounts,
        crashhandlers,
        history,
        push,
        runtime,
        settings,
        silent,
    )

    monkeypatch.setattr(runtime, "_data_dir", lambda: home)
    monkeypatch.setattr(runtime, "_log_dir", lambda: home)
    monkeypatch.setattr(runtime, "LOG_FILE", home / "trae_checkin.log")

    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", home / "accounts.json")
    monkeypatch.setattr(settings, "SETTINGS_FILE", home / "settings.json")
    monkeypatch.setattr(history, "HISTORY_FILE", home / "checkin_history.json")
    monkeypatch.setattr(push, "PUSH_LOG_FILE", home / "push_history.json")
    monkeypatch.setattr(crashhandlers, "CRASH_DIR", home / "crashes")
    monkeypatch.setattr(silent, "PERIOD_MARKERS_FILE", home / "period_markers.json")

    # backup / health 等模块在 import 时把路径常量复制进了自己的命名空间，
    # 因此还要重定向这些消费方模块里的同名绑定。
    from trae_checkin import backup, gui, health, reports, service

    consumers = [backup, gui.app, health, reports, service]
    for mod in consumers:
        if hasattr(mod, "ACCOUNTS_FILE"):
            monkeypatch.setattr(mod, "ACCOUNTS_FILE", home / "accounts.json")
        if hasattr(mod, "HISTORY_FILE"):
            monkeypatch.setattr(mod, "HISTORY_FILE", home / "checkin_history.json")
        if hasattr(mod, "SETTINGS_FILE"):
            monkeypatch.setattr(mod, "SETTINGS_FILE", home / "settings.json")
        if hasattr(mod, "PUSH_LOG_FILE"):
            monkeypatch.setattr(mod, "PUSH_LOG_FILE", home / "push_history.json")
        if hasattr(mod, "CRASH_DIR"):
            monkeypatch.setattr(mod, "CRASH_DIR", home / "crashes")
        if hasattr(mod, "LOG_FILE"):
            monkeypatch.setattr(mod, "LOG_FILE", home / "trae_checkin.log")

    yield home
