# -*- coding: utf-8 -*-
"""积分趋势测试。

迁移自旧单文件 test_t13_t14.py 的 T13 部分。历史数据通过 data_home 夹具
写入临时 checkin_history.json，绝不触碰真实数据。
"""
from datetime import datetime, timedelta

import pytest

from trae_checkin import constants, history, jsonstore


def _write_history(data_home, days_map):
    jsonstore._save_json_file(history.HISTORY_FILE, {"days": days_map})


def _mk(platform=constants.PLAT_TRAEWORK, user="u1", note="大号",
        credits=100, ok=True):
    return {"platform": platform,
            "platform_label": constants.PLATFORM_LABELS.get(platform, ""),
            "username": user, "note": note, "ok": ok,
            "credits": credits, "message": "完成"}


def test_empty(data_home):
    t = history.history_credit_trend(30)
    assert not t["has_data"]
    assert len(t["dates"]) == 30
    assert t["series"] == []


def test_basic_trend_30(data_home):
    today = datetime.now().date()
    days = {}
    ident = f"{constants.PLAT_TRAEWORK}::u1"
    for i in range(10):
        d = (today - timedelta(days=9 - i)).strftime("%Y-%m-%d")
        days[d] = {ident: _mk(credits=100 + i)}
    _write_history(data_home, days)
    t = history.history_credit_trend(30)
    assert t["has_data"]
    assert len(t["series"]) == 1
    s0 = t["series"][0]
    assert "大号" in s0["label"]
    vals = [v for v in s0["points"] if v is not None]
    assert vals == list(range(100, 110))
    assert t["max_credit"] == 109
    assert s0["color"] == history.TREND_LINE_COLORS[0]


def test_missing_dates_none(data_home):
    today = datetime.now().date()
    ident = f"{constants.PLAT_TRAEWORK}::u1"
    days = {
        (today - timedelta(days=3)).strftime("%Y-%m-%d"):
            {ident: _mk(credits=10)},
        (today - timedelta(days=1)).strftime("%Y-%m-%d"):
            {ident: _mk(credits=12)},
    }
    _write_history(data_home, days)
    pts = history.history_credit_trend(5)["series"][0]["points"]
    assert len(pts) == 5
    assert pts[0] is None
    assert pts[1] == 10
    assert pts[2] is None
    assert pts[3] == 12
    assert pts[4] is None


def test_non_numeric_credits_ignored(data_home):
    today = datetime.now().date()
    ident = f"{constants.PLAT_TRAEWORK}::u1"
    rec = _mk()
    rec["credits"] = None
    _write_history(data_home,
                   {today.strftime("%Y-%m-%d"): {ident: rec}})
    assert not history.history_credit_trend(7)["has_data"]


def test_truncated_six_max(data_home):
    today = datetime.now().date()
    key = today.strftime("%Y-%m-%d")
    day = {key: {}}
    for i in range(8):
        ident = f"{constants.PLAT_TRAEWORK}::u{i}"
        day[key][ident] = _mk(user=f"u{i}", note=f"号{i}", credits=i * 10)
    _write_history(data_home, day)
    t = history.history_credit_trend(7)
    assert len(t["series"]) == history.TREND_MAX_LINES
    assert t["truncated"]
    # 按最近积分倒序：最高 70 的账号排第一
    assert t["series"][0]["points"][-1] == 70


def test_90_days_window(data_home):
    t = history.history_credit_trend(90)
    assert len(t["dates"]) == 90
    assert t["dates"][-1] == datetime.now().strftime("%Y-%m-%d")
