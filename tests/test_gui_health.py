# -*- coding: utf-8 -*-
"""t21 健康自检中心：开窗 loading、6 张检查卡、重检全异常渲染、关窗销毁。

迁移自旧 gui_t21_driver.py（子进程 + JSON 结果），现改为进程内 pytest。
后台线程回灌时机不确定，用 200ms 轮询状态机驱动；网络为真实 TCP 探测，
TRAESIGN_HEALTH_TIMEOUT 压到 0.1 秒保证用例快速结束（连通与否不影响断言）。
首轮打桩 health 模块的客户端探测（已安装）与计划任务（未开启），
重检时打桩 gui.dialogs.run_health_checks 返回 2 条异常。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from _gui_helpers import (
    collect_texts,
    find_button,
    find_toplevel,
    run_gui_steps,
)

pytestmark = pytest.mark.gui

HEALTH_TITLE = "健康自检"
WANTED_TITLES = (
    "每日自动签到任务",
    "TraeWork 桌面客户端",
    "WorkBuddy 桌面客户端",
    "TraeWork 服务器连通性",
    "WorkBuddy 服务器连通性",
    "本地数据文件",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_DISCLAIMER", "1")
    monkeypatch.setenv("TRAESIGN_NO_CRASH_NOTICE", "1")
    monkeypatch.setenv("TRAESIGN_HEALTH_TIMEOUT", "0.1")


def test_health_dialog(data_home, monkeypatch):
    from trae_checkin import gui, health

    # 本机探测结果不稳定：客户端统一给「已安装」，任务统一给「未开启」
    monkeypatch.setattr(health, "find_platform_exes",
                        lambda plat: [Path(f"C:/fake/{plat}.exe")])
    monkeypatch.setattr(health, "task_exists", lambda: False)

    errors: list[str] = []
    state = {"phase": 0, "tries": 0}

    def driver(root):
        def _fail(msg):
            errors.append(msg)
            root.destroy()

        def _tick():
            state["tries"] += 1
            try:
                win = find_toplevel(root, HEALTH_TITLE)
                phase = state["phase"]

                if phase == 0:
                    btn = find_button(root, "健康自检")
                    if btn is None:
                        _fail("头部缺少「健康自检」按钮")
                        return
                    btn.invoke()
                    state["phase"] = 1
                    state["tries"] = 0

                elif phase == 1:
                    # 等窗口出现并检查初始结构
                    if win is None:
                        if state["tries"] >= 10:
                            _fail("健康自检窗口未出现")
                            return
                    else:
                        texts = collect_texts(win)
                        assert any("健康自检" in t for t in texts), "窗口缺少标题"
                        assert find_button(win, "重新检查") is not None, (
                            "缺少重新检查按钮")
                        assert find_button(win, "关闭") is not None, "缺少关闭按钮"
                        state["phase"] = 2
                        state["tries"] = 0

                elif phase == 2:
                    # 等后台线程回灌 6 张检查卡
                    if win is None:
                        _fail("结果回灌前窗口已消失")
                        return
                    texts = collect_texts(win)
                    missing = [w for w in WANTED_TITLES
                               if not any(w in t for t in texts)]
                    if missing:
                        if state["tries"] >= 40:
                            _fail(f"检查卡未渲染完整，缺少：{missing}")
                            return
                    else:
                        assert any(
                            "项正常" in t or "项需关注" in t or "项异常" in t
                            for t in texts), "缺少汇总文案"
                        assert any("未开启自动签到" in t for t in texts), (
                            "任务未开启时应显示对应明细")
                        assert any("已安装" in t for t in texts), (
                            "客户端应显示已安装")
                        # 打桩全异常结果后重检
                        fake = [
                            {"key": "task", "title": "每日自动签到任务",
                             "status": health.HEALTH_ERROR,
                             "detail": "检查失败：boom",
                             "hint": "重启电脑"},
                            {"key": "data", "title": "本地数据文件",
                             "status": health.HEALTH_ERROR,
                             "detail": "已损坏", "hint": "恢复备份"},
                        ]
                        monkeypatch.setattr(gui.dialogs, "run_health_checks",
                                            lambda: fake)
                        find_button(win, "重新检查").invoke()
                        state["phase"] = 3
                        state["tries"] = 0

                elif phase == 3:
                    # 等重检结果（✗ ≥ 2、2 项异常、建议渲染）
                    if win is None:
                        _fail("重检期间窗口消失")
                        return
                    texts = collect_texts(win)
                    rerun_done = (
                        any("2 项异常" in t for t in texts)
                        and texts.count("✗") >= 2
                        and any("重启电脑" in t for t in texts)
                    )
                    if not rerun_done:
                        if state["tries"] >= 40:
                            _fail("重检全异常结果未渲染："
                                  f"{[t for t in texts if '项' in t]}")
                            return
                    else:
                        find_button(win, "关闭").invoke()
                        state["phase"] = 4
                        state["tries"] = 0

                elif phase == 4:
                    if find_toplevel(root, HEALTH_TITLE) is None:
                        root.destroy()
                        return
                    if state["tries"] >= 10:
                        _fail("点「关闭」后健康自检窗口未销毁")
                        return
            except Exception as e:  # noqa: BLE001 - 收集后统一断言
                errors.append(repr(e))
                root.destroy()
                return
            root.after(200, _tick)

        root.after(300, _tick)

    rc = run_gui_steps([(200, driver)], errors, quit_ms=20000)
    assert rc == 0
    assert errors == []
