# -*- coding: utf-8 -*-
"""「开始签到」通知：到点与开机补签统一发送、每日去重、异常不阻断。

全程不触网、不碰真实数据（data_home 夹具重定向全部路径常量）。
"""

import json
from datetime import datetime
from unittest import mock

import pytest

from trae_checkin import silent


ACCOUNTS = [
    {"key": "k1", "platform": "traework", "username": "user1",
     "display_name": "user1", "enabled": True},
    {"key": "k2", "platform": "workbuddy", "username": "user2",
     "display_name": "user2", "enabled": True},
    {"key": "k3", "platform": "traework", "username": "off1",
     "display_name": "off1", "enabled": False},
]


def _settings(only_failures=False, push_enabled=True):
    return {
        "version": 2, "push_enabled": push_enabled,
        "channels": ["serverchan"], "only_failures": only_failures,
        "creds": {"serverchan": {"key": "SCTKEY", "secret": ""}},
    }


@pytest.fixture
def push_spy(data_home, monkeypatch):
    """打桩 load_settings / push_wechat，返回 (设置 dict, 推送记录 list)。"""
    holder = {"settings": _settings(), "pushed": []}

    def fake_push(s, title, content, **kwargs):
        holder["pushed"].append({
            "title": title, "content": content, "kind": kwargs.get("kind")})
        return True, "已推送 1 个渠道。"

    monkeypatch.setattr(silent, "load_settings",
                        lambda: dict(holder["settings"]))
    monkeypatch.setattr(silent, "push_wechat", fake_push)
    return holder


def _markers(data_home):
    p = data_home / "period_markers.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("sent", {})


def test_start_message_sent_with_kind_and_scope(push_spy, data_home):
    silent._push_checkin_start(ACCOUNTS)
    assert len(push_spy["pushed"]) == 1
    msg = push_spy["pushed"][0]
    assert msg["kind"] == "开始签到"
    assert "2 个账号" in msg["title"]          # 未启用的 k3 不计入
    assert "user1" in msg["content"] and "user2" in msg["content"]
    assert "off1" not in msg["content"]
    key = "start_" + datetime.now().strftime("%Y-%m-%d")
    assert key in _markers(data_home)          # 成功推送后落去重标记


def test_start_message_once_per_day(push_spy, data_home):
    silent._push_checkin_start(ACCOUNTS)
    silent._push_checkin_start(ACCOUNTS)       # 同日第二个触发器
    assert len(push_spy["pushed"]) == 1


def test_start_message_force_bypasses_dedupe(push_spy, data_home, monkeypatch):
    monkeypatch.setenv("TRAESIGN_FORCE", "1")
    silent._push_checkin_start(ACCOUNTS)
    silent._push_checkin_start(ACCOUNTS)
    assert len(push_spy["pushed"]) == 2


def test_start_message_ignores_only_failures(push_spy, data_home):
    push_spy["settings"]["only_failures"] = True
    silent._push_checkin_start(ACCOUNTS)
    assert len(push_spy["pushed"]) == 1        # 独立消息，不受开关影响


def test_start_message_skipped_when_push_off(push_spy, data_home):
    push_spy["settings"]["push_enabled"] = False
    silent._push_checkin_start(ACCOUNTS)
    push_spy["settings"]["creds"] = {}         # 开启但无凭据
    push_spy["settings"]["push_enabled"] = True
    silent._push_checkin_start(ACCOUNTS)
    assert push_spy["pushed"] == []
    assert _markers(data_home) == {}


def test_start_message_push_failure_no_marker_no_raise(data_home, monkeypatch):
    monkeypatch.setattr(silent, "load_settings", lambda: _settings())
    monkeypatch.setattr(silent, "push_wechat",
                        lambda *a, **kw: (False, "Server酱返回：额度用尽"))
    silent._push_checkin_start(ACCOUNTS)       # 不抛异常
    assert _markers(data_home) == {}           # 失败不留标记，下次触发可重试

    def boom(*a, **kw):
        raise RuntimeError("网络中断")
    monkeypatch.setattr(silent, "push_wechat", boom)
    silent._push_checkin_start(ACCOUNTS)       # 异常同样被吞掉


def _run_silent(monkeypatch, start_spy, retry_results):
    """以打桩方式驱动 silent_run，返回退出码。"""
    monkeypatch.setattr(silent, "list_accounts", lambda: list(ACCOUNTS))
    monkeypatch.setattr(silent, "_send_periodic_reports", lambda: None)
    monkeypatch.setattr(silent, "_push_checkin_start", start_spy)
    monkeypatch.setattr(silent, "_silent_run_with_retry",
                        lambda accounts: retry_results)


def test_silent_run_sends_start_when_checkin_needed(data_home, monkeypatch):
    called = []
    monkeypatch.setenv("TRAESIGN_NO_JITTER", "1")
    _run_silent(monkeypatch,
                lambda accs: called.append(len(accs)),
                [{"platform": "traework", "username": "user1", "ok": True}])
    rc = silent.silent_run()
    assert rc == 0
    assert called == [3]                       # 正常路径发送一次开始通知


def test_silent_run_skips_start_when_today_all_done(data_home, monkeypatch):
    """今日已全部成功签到 → 早退，不发开始消息也不签到。"""
    called = []
    monkeypatch.setattr(silent, "list_accounts", lambda: list(ACCOUNTS))
    monkeypatch.setattr(silent, "_send_periodic_reports", lambda: None)
    monkeypatch.setattr(silent, "_push_checkin_start",
                        lambda accs: called.append(1))
    ident = silent.history_identity
    done = {ident(a["platform"], a["username"]) for a in ACCOUNTS}
    monkeypatch.setattr(silent, "_today_done_identities", lambda: done)

    def retry_should_not_run(accounts):
        raise AssertionError("早退后不应进入签到流程")
    monkeypatch.setattr(silent, "_silent_run_with_retry", retry_should_not_run)

    rc = silent.silent_run()
    assert rc == 0
    assert called == []
