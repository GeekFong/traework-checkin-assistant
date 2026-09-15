# -*- coding: utf-8 -*-
"""t7：账号备注名贯通 + 上次签到/连续天数（写入全部限定在临时 HOME）。"""

import json
from datetime import datetime, timedelta
from unittest import mock

import pytest

from trae_checkin import (
    accounts,
    constants,
    crypto,
    history,
    jsonstore,
    reports,
    service,
)


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    monkeypatch.setenv("TRAESIGN_NO_JITTER", "1")


def test_account_note_and_batch_flow(data_home):
    # ── 1. set_account_note：设置/透出/清空 ─────────────────────────
    rec = {
        "platform": constants.PLAT_TRAEWORK,
        "display_name": "alice",
        "region": "CN",
        "device_id": "dev1",
        "saved_at": "2026-09-01 10:00",
        "secret": crypto.dpapi_encrypt(
            json.dumps({"token": "t", "refreshToken": "r", "expiredAt": ""}).encode()
        ),
    }
    accounts._save_account_record("alice", rec)
    accounts.set_account_note("alice", "  主力号  ")
    accs = {a["key"]: a for a in accounts.list_accounts()}
    assert accs["alice"]["note"] == "主力号"

    accounts.set_account_note("alice", "")
    raw = accounts._get_account_record("alice")
    assert "note" not in raw
    assert all(a["note"] == "" for a in accounts.list_accounts())
    accounts.set_account_note("alice", "主力号")

    # ── 2. run_batch_checkin 每条结果挂备注（含两个平台）─────────────
    wb_rec = {
        "platform": constants.PLAT_WORKBUDDY,
        "display_name": "bob",
        "uid": "u1",
        "region": "CN",
        "saved_at": "2026-09-01 10:00",
        "secret": crypto.dpapi_encrypt(json.dumps({"cookie": "c"}).encode()),
    }
    accounts._save_account_record("wb:u1", wb_rec)
    accounts.set_account_note("wb:u1", "小号")

    def fake_trae(auth_info, device_id, username="", status_only=False,
                  on_token_refreshed=None):
        return {
            "platform": constants.PLAT_TRAEWORK,
            "platform_label": "TraeWork CN",
            "username": username,
            "ok": True,
            "already": False,
            "credits": 10,
            "message": "签到成功",
        }

    def fake_wb(secret, display_name="", status_only=False, on_token_refreshed=None):
        return {
            "platform": constants.PLAT_WORKBUDDY,
            "platform_label": "WorkBuddy",
            "username": display_name,
            "ok": True,
            "already": False,
            "credits": 5,
            "message": "签到成功",
        }

    with mock.patch.object(service, "time") as fake_time, \
            mock.patch.object(service, "run_checkin_with_credential",
                              side_effect=fake_trae), \
            mock.patch.object(service, "run_wb_checkin_with_credential",
                              side_effect=fake_wb):
        fake_time.sleep = lambda *_: None
        results = service.run_batch_checkin()

    by_user = {r["username"]: r for r in results}
    assert len(results) == 2
    assert by_user["alice"].get("note") == "主力号"
    assert by_user["bob"].get("note") == "小号"

    # ── 3. 历史记录存备注，汇总透出 note/last_date/连续天数 ──────────
    history.record_checkin_history(results)
    today = datetime.now().strftime("%Y-%m-%d")
    hist = history.load_history()
    ident_a = history.history_identity(constants.PLAT_TRAEWORK, "alice")
    assert hist["days"][today][ident_a].get("note") == "主力号"

    # 手工补历史：昨天、前天成功，构造连续 3 天
    days = hist["days"]
    for back in (1, 2):
        d = (datetime.now() - timedelta(days=back)).strftime("%Y-%m-%d")
        days.setdefault(d, {})[ident_a] = {
            "platform": constants.PLAT_TRAEWORK,
            "platform_label": "TraeWork CN",
            "username": "alice",
            "note": "主力号",
            "ok": True,
            "already": False,
            "credits": 8,
            "message": "签到成功",
            "time": "09:00",
        }
    jsonstore._save_json_file(history.HISTORY_FILE, hist)

    summ = {a["identity"]: a for a in history.history_summary(30)["accounts"]}
    a_stat = summ[ident_a]
    assert a_stat["streak"] == 3
    assert a_stat["last_date"] == today and a_stat["note"] == "主力号"

    lookup = history.account_status_lookup(90)
    assert lookup[ident_a]["streak"] == 3 and lookup[ident_a]["last_date"] == today

    # ── 4. 推送报告显示备注 ─────────────────────────────────────────
    _, body = reports.build_checkin_report(results)
    assert "主力号" in body and "小号" in body

    # ── 5. 重新保存快照保留备注（patch 客户端读取，落在 accounts 命名空间）
    auth_info = {
        "account": {"username": "alice"},
        "userRegion": {"region": "CN"},
        "token": "t2",
        "refreshToken": "r2",
        "expiredAt": "",
    }
    with mock.patch.object(accounts, "load_auth_info", return_value=auth_info), \
            mock.patch.object(accounts, "load_device_id", return_value="dev1"):
        ok, _ = accounts._save_current_trae_account()
    assert ok
    assert accounts._get_account_record("alice").get("note") == "主力号"

    # ── 6. 新结果不带 note 时沿用当天已有备注 ───────────────────────
    history.record_checkin_history([{
        "platform": constants.PLAT_WORKBUDDY,
        "username": "bob",
        "platform_label": "WorkBuddy",
        "ok": False,
        "message": "失败",
    }])
    wb_ident = history.history_identity(constants.PLAT_WORKBUDDY, "bob")
    hist2 = history.load_history()
    # bob 之前成功记录的 note 应保留（成功不被失败覆盖）
    assert hist2["days"][today].get(wb_ident, {}).get("ok") is True
