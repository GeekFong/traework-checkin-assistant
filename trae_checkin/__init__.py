# -*- coding: utf-8 -*-
"""TraeWork CN / WorkBuddy 每日签到助手核心包。"""

# 不在这里 import 重子模块，避免 import trae_checkin 产生副作用
# （如初始化日志 / 解析安装目录）。
__all__ = ["__version__"]

try:
    from .runtime import APP_VERSION as __version__  # noqa: F401
except Exception:  # 极端环境（无安装目录等）下也要能 import 包
    __version__ = "0.0.0"
