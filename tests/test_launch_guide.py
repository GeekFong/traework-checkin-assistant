# -*- coding: utf-8 -*-
"""失败引导测试：按平台查找/启动客户端 + 登录失效识别。

迁移自旧单文件 test_t11.py。所有 subprocess.Popen 均被打桩，
不会真正启动任何客户端可执行文件。
"""
from pathlib import Path
from unittest import mock

from trae_checkin import constants, launcher, reports

PLAT_TW = constants.PLAT_TRAEWORK
PLAT_WB = constants.PLAT_WORKBUDDY


def test_find_platform_exes_dispatch():
    with mock.patch.object(launcher, "find_wb_install_exes",
                           return_value=["WB"]), \
            mock.patch.object(launcher, "find_install_exes",
                              return_value=["TW"]):
        assert launcher.find_platform_exes(PLAT_WB) == ["WB"]
        assert launcher.find_platform_exes(PLAT_TW) == ["TW"]


def test_launch_platform_app_success():
    fake_exe = Path(r"C:\fake\Trae SOLO CN.exe")
    with mock.patch.object(launcher, "find_platform_exes",
                           return_value=[fake_exe]), \
            mock.patch.object(launcher.subprocess, "Popen") as popen:
        ok, info = launcher.launch_platform_app(PLAT_TW)
    assert ok and info.endswith("Trae SOLO CN.exe")
    popen.assert_called_once()


def test_launch_platform_app_not_found():
    with mock.patch.object(launcher, "find_platform_exes", return_value=[]):
        ok, info = launcher.launch_platform_app(PLAT_WB)
    assert not ok and "WorkBuddy" in info and "安装" in info


def test_launch_platform_app_all_candidates_fail():
    exes = [Path(r"C:\bad1.exe"), Path(r"C:\bad2.exe")]
    with mock.patch.object(launcher, "find_platform_exes",
                           return_value=exes), \
            mock.patch.object(launcher.subprocess, "Popen",
                              side_effect=OSError("denied")):
        ok, info = launcher.launch_platform_app(PLAT_TW)
    assert not ok and "启动失败" in info


def _relogin(msg, plat=PLAT_TW):
    return reports._is_relogin_failure(
        {"ok": False, "platform": plat, "message": msg})


def test_relogin_failure_detection():
    assert _relogin("登录已失效，请重新登录")
    assert _relogin("403 无权限访问")
    assert reports._is_relogin_failure(
        {"ok": True, "message": "登录已失效"}) is False
    assert _relogin("网络连接异常：超时") is False


def test_relogin_guide_content():
    items = [{"platform": PLAT_TW, "platform_label": "TraeWork",
              "username": "alice", "note": "主力号", "ok": False,
              "message": "登录已失效"}]
    joined = "\n".join(reports._relogin_guide(items))
    assert "主力号（alice）" in joined
    assert "打开 TraeWork 桌面客户端" in joined
    assert "保存当前登录账号" in joined
    assert reports._relogin_guide([]) == []


def test_relogin_guide_both_platforms():
    items = [
        {"platform": PLAT_TW, "platform_label": "TraeWork",
         "username": "alice", "ok": False, "message": "登录已失效"},
        {"platform": PLAT_WB, "platform_label": "WorkBuddy",
         "username": "bob", "ok": False, "message": "请重新登录"},
    ]
    joined = "\n".join(reports._relogin_guide(items))
    assert "TraeWork" in joined and "WorkBuddy" in joined
