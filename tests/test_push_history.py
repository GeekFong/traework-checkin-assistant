# -*- coding: utf-8 -*-
"""推送历史与多渠道推送测试。

迁移自旧单文件 test_t13_t14.py 的 T14 部分。所有网络发送均被打桩，
不会发起真实 HTTP 请求；推送历史写入 data_home 临时目录。
"""
import json
import time
from unittest import mock

import pytest

from trae_checkin import backup, push, settings as settings_mod


def test_load_empty(data_home):
    assert push.load_push_history() == []


def test_record_and_read(data_home):
    push.record_push_history("测试标题", ["serverchan", "wecom"],
                             True, "已推送 2 个渠道。", kind="推送测试")
    items = push.load_push_history()
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "测试标题"
    assert it["ok"]
    assert it["kind"] == "推送测试"
    # 渠道存的是中文标签（修复 PUSH_CHANNELS.get bug 的核心断言）
    assert it["channels"] == [
        push.PUSH_CHANNEL_LABELS["serverchan"],
        push.PUSH_CHANNEL_LABELS["wecom"]]
    assert it["ts"]


def test_record_desc_order(data_home):
    push.record_push_history("t1", ["serverchan"], True, "ok1")
    time.sleep(1.1)
    push.record_push_history("t2", ["serverchan"], False, "fail2")
    items = push.load_push_history()
    assert [x["title"] for x in items] == ["t2", "t1"]
    assert items[1]["detail"] == "ok1"


def test_items_cap_300(data_home):
    for i in range(305):
        push.record_push_history(f"x{i}", ["serverchan"], True, "d")
    items = push.load_push_history(limit=100000)
    assert len(items) == push.PUSH_LOG_KEEP_ITEMS


def test_unknown_channel_label_fallback(data_home):
    push.record_push_history("t", ["weird_channel"], False, "d")
    items = push.load_push_history()
    assert items[0]["channels"] == ["weird_channel"]


def test_clear(data_home):
    push.record_push_history("t", ["serverchan"], True, "d")
    assert push.clear_push_history() == 1
    assert push.load_push_history() == []
    push.PUSH_LOG_FILE.unlink()
    assert push.clear_push_history() == 0


def test_push_wechat_no_creds_records_failure(data_home):
    s = {"version": settings_mod.SETTINGS_VERSION,
         "channels": ["serverchan"],
         "creds": {"serverchan": {"key": "", "secret": ""}},
         "push_enabled": True}
    ok, msg = push.push_wechat(s, "无凭据标题", "正文")
    assert not ok
    assert "未填写" in msg
    items = push.load_push_history()
    assert len(items) == 1
    assert not items[0]["ok"]
    assert items[0]["title"] == "无凭据标题"


def test_push_wechat_success_records(data_home, monkeypatch):
    calls = []

    def fake_one(channel, key, secret, title, content):
        calls.append((channel, key))
        return True, "ok"

    monkeypatch.setattr(push, "_push_one_channel", fake_one)
    s = {"version": settings_mod.SETTINGS_VERSION,
         "channels": ["serverchan"],
         "creds": {"serverchan": {"key": "K", "secret": ""}}}
    ok, msg = push.push_wechat(s, "真实标题", "正文", kind="签到周报")
    assert ok
    assert calls == [("serverchan", "K")]
    items = push.load_push_history()
    assert len(items) == 1
    assert items[0]["ok"]
    assert items[0]["kind"] == "签到周报"
    assert items[0]["channels"] == [
        push.PUSH_CHANNEL_LABELS["serverchan"]]


def test_push_wechat_partial(data_home, monkeypatch):
    def fake_one(channel, key, secret, title, content):
        return (True, "ok") if channel == "serverchan" else (False, "boom")

    monkeypatch.setattr(push, "_push_one_channel", fake_one)
    s = {"version": settings_mod.SETTINGS_VERSION,
         "channels": ["serverchan", "pushplus"],
         "creds": {c: {"key": "K", "secret": ""}
                   for c in ("serverchan", "pushplus")}}
    ok, msg = push.push_wechat(s, "p", "c")
    assert ok
    assert "部分渠道成功" in msg
    it = push.load_push_history()[0]
    assert it["ok"]
    assert len(it["channels"]) == 2


def test_backup_includes_push_log(data_home):
    import zipfile
    push.record_push_history("t", ["serverchan"], True, "d")
    zp = data_home / "b.zip"
    backup.backup_user_data(str(zp))
    with zipfile.ZipFile(zp) as zf:
        assert "push_history.json" in zf.namelist()
    res = backup.restore_user_data(str(zp))
    assert "push_history.json" in res["restored"]


def test_diagnostic_bundle_masked(data_home):
    import zipfile
    push.record_push_history("机密标题X", ["serverchan"], True, "机密详情Y")
    zp = data_home / "diag.zip"
    backup.export_diagnostic_bundle(str(zp))
    with zipfile.ZipFile(zp) as zf:
        raw = zf.read("diagnostic_info.json").decode("utf-8")
    info = json.loads(raw)
    assert info["push_history"]["total"] == 1
    assert info["push_history"]["success"] == 1
    assert "签到结果" in info["push_history"]["by_kind"]
    # 标题/详情绝不进诊断包
    assert "机密标题X" not in raw
    assert "机密详情Y" not in raw


def test_history_file_separate(data_home):
    push.record_push_history("t", ["serverchan"], True, "d")
    p = data_home / "push_history.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "days" not in data
    assert data["items"][0]["title"] == "t"
