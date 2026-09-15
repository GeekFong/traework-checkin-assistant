# -*- coding: utf-8 -*-
"""连续失败「疑似掉线」预警测试。

迁移自旧单文件脚本式测试 test_t22.py。

红线：
  - 每个用例经 data_home 夹具使用独立临时 HOME，绝不触碰真实数据文件；
  - 阈值用 TRAESIGN_OFFLINE_DAYS 压缩；推送全部打桩，不发真实网络请求；
  - 历史桶直接写临时 HOME 下的 checkin_history.json。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest import mock

import pytest

from trae_checkin import (
    constants,
    history,
    jsonstore,
    settings as settings_mod,
    silent,
)


# ── 公共夹具与小工具 ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def offline_days(monkeypatch):
    monkeypatch.setenv("TRAESIGN_OFFLINE_DAYS", "3")


@pytest.fixture
def ctx(data_home, monkeypatch):
    """封装旧 OfflineBase 的写历史 / 造结果 / 打桩推送能力。"""

    class Ctx:
        today = datetime.now().date()
        pushes: list[dict] = []

        def dstr(self, delta_days):
            return (self.today - timedelta(days=delta_days)).strftime("%Y-%m-%d")

        def write_history(self, days_map):
            """days_map: {delta_days: {ident: item}}"""
            buckets = {}
            for delta, items in days_map.items():
                buckets[self.dstr(delta)] = items
            jsonstore._save_json_file(
                history.HISTORY_FILE, {"days": buckets})

        def item(self, platform="traework", username="u1", ok=False,
                 message="签到失败", note="", already=False):
            label = constants.PLATFORM_LABELS.get(platform, platform)
            return {"platform": platform, "platform_label": label,
                    "username": username, "note": note, "ok": ok,
                    "already": already, "credits": None, "message": message,
                    "time": "09:00"}

        def enabled_settings(self):
            return {"version": 2, "push_enabled": True,
                    "channels": ["serverchan"],
                    "creds": {"serverchan": {"key": "SCT123", "secret": ""}},
                    "only_failures": False}

        def patch_push(self, ok=True, msg="✓ 推送成功"):
            def _fake(settings, title, content, kind="签到结果"):
                self.pushes.append({"title": title, "content": content,
                                    "kind": kind})
                return ok, msg

            monkeypatch.setattr(silent, "push_wechat", mock.Mock(side_effect=_fake))

        def read_settings(self):
            return json.loads(
                settings_mod.SETTINGS_FILE.read_text(encoding="utf-8"))

    c = Ctx()
    return c


# ── 检测口径（8 项） ──────────────────────────────────────────────────

def test_exact_threshold_hits(ctx):
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False, message="err2")},
        1: {"traework::u1": ctx.item(ok=False, message="err1")},
        2: {"traework::u1": ctx.item(ok=False, message="最早一次失败")},
    })
    hit = silent.detect_offline_accounts()
    assert 1 == len(hit)
    assert "traework::u1" == hit[0]["ident"]
    assert 3 == hit[0]["streak"]
    # last_message 取最近一天（今天，delta=0）的报错
    assert "err2" == hit[0]["last_message"]


def test_one_day_short_does_not_hit(ctx):
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
    })
    assert [] == silent.detect_offline_accounts()


def test_success_breaks_streak(ctx):
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        # 第 3 天成功：连续中断，之后即便失败也不命中
        2: {"traework::u1": ctx.item(ok=True, message="签到成功")},
        3: {"traework::u1": ctx.item(ok=False)},
    })
    assert [] == silent.detect_offline_accounts()


def test_already_checked_in_counts_as_success(ctx):
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=True, already=True,
                                      message="今日已签到")},
    })
    assert [] == silent.detect_offline_accounts()


def test_missing_whole_bucket_breaks_streak(ctx):
    # 今天、昨天失败；前天整天无桶（机器没跑）；大前天失败
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        3: {"traework::u1": ctx.item(ok=False)},
    })
    assert [] == silent.detect_offline_accounts()


def test_bucket_exists_but_account_absent_breaks_streak(ctx):
    # 机器连续在跑，但目标账号第 2 天没有结果（未参与/被移除）
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"workbuddy::w1": ctx.item(platform="workbuddy",
                                       username="w1", ok=True)},
    })
    assert [] == silent.detect_offline_accounts()


def test_multiple_accounts_one_offline_one_healthy(ctx):
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False),
            "traework::u2": ctx.item(username="u2", ok=True)},
        1: {"traework::u1": ctx.item(ok=False),
            "traework::u2": ctx.item(username="u2", ok=True)},
        2: {"traework::u1": ctx.item(ok=False),
            "traework::u2": ctx.item(username="u2", ok=True)},
    })
    hit = silent.detect_offline_accounts()
    assert ["traework::u1"] == [h["ident"] for h in hit]


def test_threshold_parameter_override(ctx):
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
    })
    assert 1 == len(silent.detect_offline_accounts(threshold=1))
    assert [] == silent.detect_offline_accounts(threshold=2)


# ── 预警推送（10 项） ─────────────────────────────────────────────────

def test_first_push_marks_alert_and_returns_count(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False, note="我的主号")},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False, message="登录态失效")},
    })
    settings = ctx.enabled_settings()
    n = silent.check_and_push_offline_alerts(settings)
    assert 1 == n
    assert 1 == len(ctx.pushes)
    p = ctx.pushes[0]
    assert "掉线预警" == p["kind"]
    assert "疑似掉线" in p["title"]
    assert "我的主号" in p["content"]
    assert "登录态失效" in p["content"]
    assert "处理步骤" in p["content"]
    # 标记已落盘
    saved = ctx.read_settings()
    today = ctx.today.strftime("%Y-%m-%d")
    assert today == saved["offline_alerts"]["traework::u1"]


def test_same_day_second_call_dedupes(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
    })
    settings = ctx.enabled_settings()
    assert 1 == silent.check_and_push_offline_alerts(settings)
    assert 0 == silent.check_and_push_offline_alerts(settings)
    assert 1 == len(ctx.pushes)


def test_next_day_pushes_again(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
    })
    settings = ctx.enabled_settings()
    assert 1 == silent.check_and_push_offline_alerts(settings)
    # 模拟次日：把标记日期改成昨天，连续失败仍在持续
    yesterday = ctx.dstr(1)
    settings["offline_alerts"]["traework::u1"] = yesterday
    assert 1 == silent.check_and_push_offline_alerts(settings)
    assert 2 == len(ctx.pushes)


def test_recovered_account_not_detected(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=True, message="签到成功")},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
        3: {"traework::u1": ctx.item(ok=False)},
    })
    settings = ctx.enabled_settings()
    # 预置很旧的预警标记，也不应再命中
    settings["offline_alerts"] = {"traework::u1": ctx.dstr(10)}
    assert 0 == silent.check_and_push_offline_alerts(settings)
    assert 0 == len(ctx.pushes)


def test_push_disabled_returns_zero(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
    })
    settings = ctx.enabled_settings()
    settings["push_enabled"] = False
    assert 0 == silent.check_and_push_offline_alerts(settings)
    assert 0 == len(ctx.pushes)


def test_push_unconfigured_returns_zero(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
    })
    settings = ctx.enabled_settings()
    settings["creds"] = {"serverchan": {"key": "", "secret": ""}}
    assert 0 == silent.check_and_push_offline_alerts(settings)
    assert 0 == len(ctx.pushes)


def test_no_offline_accounts_returns_zero(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=True)},
    })
    assert 0 == silent.check_and_push_offline_alerts(ctx.enabled_settings())
    assert 0 == len(ctx.pushes)


def test_push_failure_does_not_mark(ctx, monkeypatch):
    monkeypatch.setattr(silent, "push_wechat",
                        mock.Mock(return_value=(False, "✗ 网络错误")))
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
    })
    settings = ctx.enabled_settings()
    assert 0 == silent.check_and_push_offline_alerts(settings)
    assert "offline_alerts" not in settings


def test_old_markers_cleaned_after_sixty_days(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False),
            "traework::u2": ctx.item(username="u2", ok=False)},
        1: {"traework::u1": ctx.item(ok=False),
            "traework::u2": ctx.item(username="u2", ok=False)},
        2: {"traework::u1": ctx.item(ok=False),
            "traework::u2": ctx.item(username="u2", ok=False)},
    })
    settings = ctx.enabled_settings()
    old = (ctx.today - timedelta(days=90)).strftime("%Y-%m-%d")
    settings["offline_alerts"] = {"traework::gone": old}
    assert 2 == silent.check_and_push_offline_alerts(settings)
    saved = ctx.read_settings()
    assert "traework::gone" not in saved["offline_alerts"]
    assert "traework::u1" in saved["offline_alerts"]


def test_multiple_offline_accounts_single_message(ctx):
    ctx.patch_push()
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False),
            "workbuddy::w1": ctx.item(platform="workbuddy",
                                       username="w1", ok=False)},
        1: {"traework::u1": ctx.item(ok=False),
            "workbuddy::w1": ctx.item(platform="workbuddy",
                                       username="w1", ok=False)},
        2: {"traework::u1": ctx.item(ok=False),
            "workbuddy::w1": ctx.item(platform="workbuddy",
                                       username="w1", ok=False)},
    })
    settings = ctx.enabled_settings()
    assert 2 == silent.check_and_push_offline_alerts(settings)
    assert 1 == len(ctx.pushes)
    assert "2 个账号" in ctx.pushes[0]["title"]
    assert "TraeWork" in ctx.pushes[0]["content"]
    assert "WorkBuddy" in ctx.pushes[0]["content"]


# ── 静默接线（3 项） ──────────────────────────────────────────────────

def test_daily_report_and_offline_alert_both_pushed(ctx, monkeypatch):
    # 历史预置连续失败，当天结果再失败 → 日报 + 掉线预警各推一次
    ctx.write_history({
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
    })
    settings = ctx.enabled_settings()
    rec = mock.Mock()
    pw = mock.Mock(return_value=(True, "ok"))
    det = mock.Mock(return_value=[{
        "ident": "traework::u1", "platform": "traework",
        "platform_label": "TraeWork", "username": "u1",
        "note": "", "streak": 3, "alive": True,
        "last_message": "失败"}])
    monkeypatch.setattr(silent, "record_checkin_history", rec)
    monkeypatch.setattr(silent, "load_settings",
                        mock.Mock(return_value=settings))
    monkeypatch.setattr(silent, "build_checkin_report",
                        mock.Mock(return_value=("日报标题", "日报正文")))
    monkeypatch.setattr(silent, "push_wechat", pw)
    monkeypatch.setattr(silent, "detect_offline_accounts", det)
    # save_settings_dict 走真实落盘（临时 HOME），验证标记写回同一对象
    results = [{"platform": "traework", "username": "u1",
                "ok": False, "message": "失败"}]
    silent._silent_record_and_push(results)
    rec.assert_called_once_with(results)
    assert 2 == pw.call_count
    kinds = [c.kwargs.get("kind") for c in pw.call_args_list]
    assert ["签到结果", "掉线预警"] == kinds
    # 预警标记写回了同一份 settings
    assert "traework::u1" in settings[settings_mod.OFFLINE_ALERT_KEY]


def test_offline_alert_exception_does_not_break_main_flow(ctx, monkeypatch):
    monkeypatch.setattr(silent, "record_checkin_history", mock.Mock())
    monkeypatch.setattr(silent, "load_settings",
                        mock.Mock(return_value=ctx.enabled_settings()))
    monkeypatch.setattr(silent, "build_checkin_report",
                        mock.Mock(return_value=("t", "c")))
    monkeypatch.setattr(silent, "push_wechat",
                        mock.Mock(return_value=(True, "ok")))
    monkeypatch.setattr(silent, "detect_offline_accounts",
                        mock.Mock(side_effect=RuntimeError("boom")))
    # 不抛异常即通过
    silent._silent_record_and_push(
        [{"platform": "traework", "username": "u1", "ok": True}])


def test_dedup_marker_roundtrip_via_load_settings(ctx):
    """跨进程语义：save_settings_dict 落盘 → load_settings 回读标记。"""
    ctx.write_history({
        0: {"traework::u1": ctx.item(ok=False)},
        1: {"traework::u1": ctx.item(ok=False)},
        2: {"traework::u1": ctx.item(ok=False)},
    })
    ctx.patch_push()
    # 先把启用的推送配置落盘（模拟用户已在 GUI 保存过设置）
    settings_mod.save_settings_dict(ctx.enabled_settings())
    n1 = silent.check_and_push_offline_alerts(settings_mod.load_settings())
    assert 1 == n1
    # 模拟全新进程：重新从磁盘加载 settings
    fresh_settings = settings_mod.load_settings()
    n2 = silent.check_and_push_offline_alerts(fresh_settings)
    assert 0 == n2
    assert 1 == len(ctx.pushes)
    # 明文密钥不得出现在落盘文件中
    raw = ctx.read_settings()
    assert "creds" not in raw
