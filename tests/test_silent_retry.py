# -*- coding: utf-8 -*-
"""t4：失败分轮重试 + 周报/月报标记防重发。全程不触网、不碰真实数据。"""

import json
from datetime import datetime as real_datetime
from unittest import mock

import pytest

from trae_checkin import constants, silent


@pytest.fixture(autouse=True)
def _retry_env(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_JITTER", "1")
    monkeypatch.setenv("TRAESIGN_RETRY_ROUNDS", "2")
    monkeypatch.setenv("TRAESIGN_RETRY_WAIT_MIN", "0")
    monkeypatch.setenv("TRAESIGN_RETRY_WAIT_MAX", "0")


def test_merge_results():
    old = [
        {"platform": "traework", "username": "a", "ok": True, "message": "ok1"},
        {"platform": "workbuddy", "username": "b", "ok": False, "message": "net"},
    ]
    new = [
        {"platform": "traework", "username": "a", "ok": False,
         "message": "should-not-overwrite"},
        {"platform": "workbuddy", "username": "b", "ok": True, "message": "recovered"},
    ]
    merged = silent._merge_results(old, new)
    ma = next(r for r in merged if r["username"] == "a")
    mb = next(r for r in merged if r["username"] == "b")
    assert ma["ok"] and ma["message"] == "ok1"
    assert mb["ok"] and mb["message"] == "recovered"


def test_first_round_all_success_no_retry(data_home):
    calls = {"batch": 0}

    def fake_batch_ok(accounts, status_only=False):
        calls["batch"] += 1
        return [{
            "platform": "traework", "platform_label": "TraeWork",
            "username": a.get("username", "x"), "ok": True,
            "message": "签到成功",
        } for a in accounts]

    accounts = [{"key": "k1", "platform": "traework", "username": "user1",
                 "display_name": "user1", "enabled": True}]
    with mock.patch.object(silent, "run_batch_checkin", side_effect=fake_batch_ok), \
            mock.patch.object(silent, "_silent_append_live",
                              side_effect=lambda res, sp=None, **kw: res):
        res = silent._silent_run_with_retry(accounts)
    assert calls["batch"] == 1
    assert all("_acct_key" not in r for r in res)
    assert all(r["ok"] for r in res)


def test_failed_account_only_retried(data_home):
    seq = []

    def fake_batch_seq(accounts, status_only=False):
        seq.append([a["key"] for a in accounts])
        out = []
        round_no = len(seq)
        for a in accounts:
            ok = (round_no >= 2) if a["key"] == "k1" else False
            out.append({"platform": a.get("platform", "traework"),
                        "platform_label": "L", "username": a["username"],
                        "ok": ok, "message": "ok" if ok else "网络错误"})
        return out

    accounts2 = [
        {"key": "k1", "platform": "traework", "username": "u1", "enabled": True},
        {"key": "k2", "platform": "workbuddy", "username": "u2", "enabled": True},
    ]
    with mock.patch.object(silent, "run_batch_checkin", side_effect=fake_batch_seq), \
            mock.patch.object(silent, "_silent_append_live",
                              side_effect=lambda res, sp=None, **kw: res), \
            mock.patch.object(silent.time, "sleep"):
        res2 = silent._silent_run_with_retry(accounts2)

    assert seq[0] == ["k1", "k2"]
    assert seq[1] == ["k1", "k2"] and seq[2] == ["k2"]
    assert len(seq) == 3
    r1 = next(r for r in res2 if r["username"] == "u1")
    r2 = next(r for r in res2 if r["username"] == "u2")
    assert r1["ok"]
    assert not r2["ok"]


def test_live_failed_platform_retried_once(data_home):
    live_calls = []

    def fake_append(results, saved_platforms=None, status_only=False):
        live_calls.append(set(saved_platforms or set()))
        if len(live_calls) == 1:
            results.append({"platform": constants.PLAT_TRAEWORK,
                            "platform_label": "TraeWork", "username": "live1",
                            "ok": False, "message": "登录失效"})
        else:
            results.append({"platform": constants.PLAT_TRAEWORK,
                            "platform_label": "TraeWork", "username": "live1",
                            "ok": True, "message": "签到成功"})
        return results

    with mock.patch.object(silent, "_silent_append_live", side_effect=fake_append), \
            mock.patch.object(silent.time, "sleep"):
        res3 = silent._silent_run_with_retry([])
    assert len(live_calls) == 2
    assert res3[0]["ok"]


class _FixedDateTime(real_datetime):
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _patch_datetime(y, m, d):
    obj = type("_DT", (_FixedDateTime,), {})
    obj._fixed = _FixedDateTime(y, m, d, 9, 0, 0)
    return mock.patch.object(silent, "datetime", obj)


def test_no_periodic_report_on_weekday(data_home):
    pushed = []
    with _patch_datetime(2026, 9, 16):  # 周三
        silent._send_periodic_reports()
    assert pushed == []


def _settings_with_push():
    return {
        "version": 2, "push_enabled": True,
        "channels": ["serverchan"], "only_failures": False,
        "creds": {"serverchan": {"key": "SCTKEY", "secret": ""}},
    }


def test_weekly_report_sent_once_and_marked(data_home):
    pushed = []

    def fake_push(s, title, content, **kwargs):
        pushed.append(title)
        return True, "已推送 1 个渠道。"

    with _patch_datetime(2026, 9, 14), \
            mock.patch.object(silent, "load_settings",
                              return_value=_settings_with_push()), \
            mock.patch.object(silent, "push_wechat", side_effect=fake_push):
        silent._send_periodic_reports()
        silent._send_periodic_reports()  # 同日再触发

    assert len(pushed) == 1 and "周报" in pushed[0]
    markers = json.loads(
        (data_home / "period_markers.json").read_text(encoding="utf-8"))
    assert "weekly_2026-09-14" in markers.get("sent", {})


def test_month_start_and_both_reports(data_home):
    pushed = []

    def fake_push(s, title, content, **kwargs):
        pushed.append(title)
        return True, "已推送 1 个渠道。"

    # 月初（1 号、周二）只发月报
    with _patch_datetime(2026, 9, 1), \
            mock.patch.object(silent, "load_settings",
                              return_value=_settings_with_push()), \
            mock.patch.object(silent, "push_wechat", side_effect=fake_push):
        silent._send_periodic_reports()
    assert len(pushed) == 1 and "月报" in pushed[0]

    # 周一 + 月初同日（2026-06-01）发周报 + 月报
    pushed.clear()
    (data_home / "period_markers.json").unlink()
    with _patch_datetime(2026, 6, 1), \
            mock.patch.object(silent, "load_settings",
                              return_value=_settings_with_push()), \
            mock.patch.object(silent, "push_wechat", side_effect=fake_push):
        silent._send_periodic_reports()
    assert len(pushed) == 2


def test_periodic_skipped_when_push_disabled(data_home):
    pushed = []

    def fake_push(s, title, content, **kwargs):
        pushed.append(title)
        return True, ""

    off = {"version": 2, "push_enabled": False, "channels": [],
           "only_failures": False, "creds": {}}
    with _patch_datetime(2026, 9, 14), \
            mock.patch.object(silent, "load_settings", return_value=off), \
            mock.patch.object(silent, "push_wechat", side_effect=fake_push):
        silent._send_periodic_reports()
    assert pushed == []
    assert not (data_home / "period_markers.json").exists()
