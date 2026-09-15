# -*- coding: utf-8 -*-
"""命令行入口与参数解析（GUI / --silent / --status）。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import argparse
import json
from typing import Any, Optional
from .accounts import list_accounts
from .constants import APP_NAME
from .crashhandlers import install_exception_hooks
from .gui.app import run_gui
from .runtime import log
from .service import run_batch_checkin
from .silent import _silent_append_live, silent_run
from .single_instance import acquire_single_instance, bring_existing_window_to_front
from .platforms.traework import run_checkin


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
