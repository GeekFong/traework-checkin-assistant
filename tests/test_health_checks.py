# -*- coding: utf-8 -*-
"""健康自检中心：run_health_checks 结构化结果与分支测试。

迁移自旧单文件脚本式测试 test_t21.py。

红线：
  - 每个用例经 data_home 夹具使用独立临时 HOME，绝不触碰真实数据文件；
  - 网络探测用打桩替代真实 socket，超时用 TRAESIGN_HEALTH_TIMEOUT 压缩；
  - schtasks / PowerShell 一律打桩，不在本机创建/查询真实计划任务。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from trae_checkin import constants, health, jsonstore


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch):
    monkeypatch.setenv("TRAESIGN_HEALTH_TIMEOUT", "0.05")


@pytest.fixture
def patch_common(monkeypatch):
    """统一打桩工厂：任务存在/详情、客户端探测、网络连通。"""

    def _patch(*, task_exists=True, task_info=None, exes=None,
               reachable=True):
        exes = {} if exes is None else exes
        monkeypatch.setattr(health, "task_exists",
                            mock.Mock(return_value=task_exists))
        monkeypatch.setattr(health, "_scheduled_task_info",
                            mock.Mock(return_value=task_info))

        def _fake_find(plat):
            return list(exes.get(plat, []))

        monkeypatch.setattr(health, "find_platform_exes",
                            mock.Mock(side_effect=_fake_find))

        def _fake_tcp(host, port=443):
            return (True, "5 ms") if reachable else (False, "timed out")

        monkeypatch.setattr(health, "_tcp_reachable",
                            mock.Mock(side_effect=_fake_tcp))

    return _patch


def by_key(results):
    return {r["key"]: r for r in results}


# ── 全绿场景 ──────────────────────────────────────────────────────────

def test_all_ok_when_task_running_clients_and_net_ok(patch_common):
    patch_common(
        task_exists=True,
        task_info={"State": "Ready", "LastRunTime": "2026-09-15 09:00:00",
                   "LastTaskResult": 0,
                   "NextRunTime": "2026-09-16 09:00:00"},
        exes={constants.PLAT_TRAEWORK: [Path("C:/Trae/Trae.exe")],
              constants.PLAT_WORKBUDDY: [Path("C:/WorkBuddy/WorkBuddy.exe")]},
        reachable=True)
    results = health.run_health_checks()
    keys = {r["key"] for r in results}
    assert keys == {"task", "client_traework", "client_workbuddy",
                    "net_traework", "net_workbuddy", "data"}
    by = by_key(results)
    for r in results:
        assert r["status"] == health.HEALTH_OK, \
            f"{r['key']} 应为 ok，实际 {r['status']}：{r['detail']}"
    assert "2026-09-15" in by["task"]["detail"]
    assert "5 ms" in by["net_traework"]["detail"]


def test_task_never_run_result_267009_is_ok(patch_common):
    patch_common(
        task_exists=True,
        task_info={"State": "Ready", "LastRunTime": "",
                   "LastTaskResult": 267009,
                   "NextRunTime": "2026-09-16 09:00:00"},
        exes={constants.PLAT_TRAEWORK: [Path("x.exe")],
              constants.PLAT_WORKBUDDY: [Path("y.exe")]})
    by = by_key(health.run_health_checks())
    assert by["task"]["status"] == health.HEALTH_OK


# ── 计划任务分支 ──────────────────────────────────────────────────────

def test_task_missing_warns(patch_common):
    patch_common(task_exists=False,
                 exes={constants.PLAT_TRAEWORK: [Path("x.exe")],
                       constants.PLAT_WORKBUDDY: [Path("y.exe")]})
    by = by_key(health.run_health_checks())
    assert by["task"]["status"] == health.HEALTH_WARN
    assert "未开启" in by["task"]["detail"]


def test_task_bad_last_result_warns_with_hint(patch_common):
    patch_common(
        task_exists=True,
        task_info={"State": "Ready", "LastRunTime": "2026-09-15 09:00:00",
                   "LastTaskResult": 1, "NextRunTime": ""},
        exes={constants.PLAT_TRAEWORK: [Path("x.exe")],
              constants.PLAT_WORKBUDDY: [Path("y.exe")]})
    by = by_key(health.run_health_checks())
    assert by["task"]["status"] == health.HEALTH_WARN
    assert by["task"]["hint"]
    assert "结果码：1" in by["task"]["detail"]


def test_task_query_exception_becomes_error(patch_common, monkeypatch):
    patch_common(exes={constants.PLAT_TRAEWORK: [Path("x.exe")],
                       constants.PLAT_WORKBUDDY: [Path("y.exe")]})
    monkeypatch.setattr(health, "task_exists",
                        mock.Mock(side_effect=OSError("svc down")))
    by = by_key(health.run_health_checks())
    assert by["task"]["status"] == health.HEALTH_ERROR
    assert "svc down" in by["task"]["detail"]


# ── 客户端分支 ────────────────────────────────────────────────────────

def test_missing_client_without_account_is_warn(data_home, patch_common):
    patch_common(task_exists=False, exes={}, reachable=True)
    by = by_key(health.run_health_checks())
    assert by["client_traework"]["status"] == health.HEALTH_WARN
    assert by["client_workbuddy"]["status"] == health.HEALTH_WARN


def test_missing_client_with_saved_account_is_error(data_home, patch_common):
    # 预置一个 workbuddy 账号
    jsonstore._save_json_file(health.ACCOUNTS_FILE, {
        "workbuddy:u@x.com": {
            "platform": "workbuddy",
            "display_name": "u@x.com",
            "creds_enc": "dummy",
        }})
    patch_common(task_exists=False, exes={}, reachable=True)
    by = by_key(health.run_health_checks())
    assert by["client_workbuddy"]["status"] == health.HEALTH_ERROR
    assert "1 个该平台账号" in by["client_workbuddy"]["detail"]
    # traework 无账号 → 仍为 warn
    assert by["client_traework"]["status"] == health.HEALTH_WARN
    # 数据检查应识别出 1 个账号
    assert "1 个账号" in by["data"]["detail"]


# ── 网络分支 ──────────────────────────────────────────────────────────

def test_network_unreachable_is_error(patch_common):
    patch_common(task_exists=False, exes={}, reachable=False)
    by = by_key(health.run_health_checks())
    assert by["net_traework"]["status"] == health.HEALTH_ERROR
    assert by["net_workbuddy"]["status"] == health.HEALTH_ERROR
    assert "timed out" in by["net_traework"]["detail"]
    assert by["net_traework"]["hint"]


def test_tcp_reachable_helper_real_socket_timeout():
    # 不打桩：直接验证 helper 行为——连接一个必定拒绝的本机端口
    ok, info = health._tcp_reachable("127.0.0.1", 1)
    assert ok is False
    assert info


# ── 数据文件分支 ──────────────────────────────────────────────────────

def test_data_ok_fresh_home(patch_common):
    patch_common(task_exists=False, exes={})
    by = by_key(health.run_health_checks())
    assert by["data"]["status"] == health.HEALTH_OK
    assert "0 个账号" in by["data"]["detail"]


def test_data_today_partial_hint(data_home, patch_common):
    today = datetime.now().date().strftime("%Y-%m-%d")
    jsonstore._save_json_file(health.HISTORY_FILE, {"days": {today: {
        "traework|a": {"platform": "traework", "username": "a",
                       "success": True},
        "traework|b": {"platform": "traework", "username": "b",
                       "success": False},
    }}})
    patch_common(task_exists=False, exes={})
    by = by_key(health.run_health_checks())
    assert by["data"]["status"] == health.HEALTH_OK
    assert "1/2" in by["data"]["detail"]
    assert "未签到" in by["data"]["hint"]


def test_corrupt_history_warns(data_home, patch_common):
    from trae_checkin.history import load_history

    health.HISTORY_FILE.write_text("{not json", encoding="utf-8")
    patch_common(task_exists=False, exes={})
    # 健康自检应直接识别磁盘文件损坏并告警：
    by = by_key(health.run_health_checks())
    assert by["data"]["status"] == health.HEALTH_WARN
    assert "已损坏" in by["data"]["detail"]
    # 而运行时读取仍保持容错（返回空桶，不崩主流程）：
    assert load_history() == {"days": {}}


def test_corrupt_accounts_warns(data_home, patch_common):
    health.ACCOUNTS_FILE.write_text("[1,2,3]", encoding="utf-8")
    patch_common(task_exists=False, exes={})
    by = by_key(health.run_health_checks())
    assert by["data"]["status"] == health.HEALTH_WARN
    assert "结构异常" in by["data"]["detail"]
