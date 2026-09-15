# -*- coding: utf-8 -*-
"""防假成功测试：领取接口 200 但缺积分/成功信号时标记 suspicious。

迁移自旧单文件脚本 test_t23.py，覆盖：
  - TraeWork / WorkBuddy 双平台的可疑判定与确认字段豁免；
  - 历史持久化（suspicious 标记、旧数据缺键兼容）；
  - 日报三档标题 / 排序 / 改版指引段 / 汇总；
  - 静默推送门禁（仅失败时推送仍需覆盖可疑成功）；
  - 掉线检测口径（可疑成功算存活、硬失败仍命中）；
  - CSV 导出对可疑行的标注。

红线：
  - 所有用例经 data_home 夹具重定向到临时 TRAESIGN_HOME；
  - check_status / claim_checkin / http_post / push_wechat 全部打桩，
    不发真实网络请求、不发真实推送。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from unittest import mock

from trae_checkin import constants, history, jsonstore, reports, settings as settings_mod
from trae_checkin import silent
from trae_checkin.platforms import traework, workbuddy


# ────────────────────────── 公共夹具与工厂 ──────────────────────────

@pytest.fixture(autouse=True)
def offline_threshold(monkeypatch):
    """固定掉线阈值为 3 天，与旧脚本行为一致。"""
    monkeypatch.setenv("TRAESIGN_OFFLINE_DAYS", "3")


def tw_auth() -> dict:
    """一份未过期的 TraeWork 凭证。"""
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    return {"token": "TOK", "refreshToken": "RT", "expiredAt": future,
            "account": {"username": "tw_user"}}


def wb_secret() -> dict:
    """一份 WorkBuddy 凭证（token 非 JWT，过期判定直接放行）。"""
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    return {"token": "WTOK", "uid": "wb_user", "expireAt": future,
            "expires_at": future}


def patch_tw(monkeypatch, status=None, claim=None, claim_exc=None):
    """打桩 TraeWork 的状态查询与领取（落点必须是 traework 命名空间）。"""
    if status is None:
        status = {"enable": True, "checked_in": False, "credits": None}

    def _fake_status(token, device_id, region):
        return status

    def _fake_claim(token, device_id, region):
        if claim_exc is not None:
            raise claim_exc
        return claim if claim is not None else {}

    monkeypatch.setattr(traework, "check_status",
                        mock.Mock(side_effect=_fake_status))
    monkeypatch.setattr(traework, "claim_checkin",
                        mock.Mock(side_effect=_fake_claim))


def patch_wb(monkeypatch, status_resp=None, claim_resp=None):
    """打桩 WorkBuddy：按调用次序依次返回 (状态响应, 领取响应)，回查一律未签。"""
    if status_resp is None:
        status_resp = (200, {"code": 0, "data": {
            "active": True, "today_checked_in": False,
            "total_credits": None}})
    if claim_resp is None:
        claim_resp = (200, {"code": 0, "data": {}})
    seq = [status_resp, claim_resp]

    def _fake_post(url, headers, payload):
        return seq.pop(0) if seq else (
            200, {"code": 0, "data": {"active": True,
                                      "today_checked_in": False}})

    monkeypatch.setattr(workbuddy, "http_post",
                        mock.Mock(side_effect=_fake_post))


def run_tw(**kw):
    return traework.run_checkin_with_credential(tw_auth(), "device-1", **kw)


def run_wb(**kw):
    return workbuddy.run_wb_checkin_with_credential(
        wb_secret(), display_name="wb_user", **kw)


# ────────────────────────── TraeWork 判定 ──────────────────────────

class TestTraeWorkSuspicious:
    def test_no_credits_no_signal_is_suspicious(self, data_home, monkeypatch):
        patch_tw(monkeypatch,
                 status={"enable": True, "checked_in": False},
                 claim={"code": 0})
        r = run_tw()
        assert r["ok"] is True
        assert r["suspicious"] is True
        assert "疑似客户端改版" in r["message"]
        assert "⚠" in r["message"]

    def test_claim_credits_not_suspicious(self, data_home, monkeypatch):
        patch_tw(monkeypatch,
                 claim={"code": 0, "credits": 10})
        r = run_tw()
        assert r["ok"] is True
        assert "suspicious" not in r
        assert "获得 10 积分" in r["message"]

    def test_status_credits_fallback_not_suspicious(self, data_home, monkeypatch):
        # 领取响应缺 credits 时回退状态积分；非 None 即视为有信号
        patch_tw(monkeypatch,
                 status={"enable": True, "checked_in": False, "credits": 42},
                 claim={"code": 0})
        r = run_tw()
        assert r["ok"] is True
        assert "suspicious" not in r
        assert r["credits"] == 42

    @pytest.mark.parametrize("field", ["checked_in", "success", "claimed"])
    def test_confirmation_fields_each_exempt(self, data_home, monkeypatch, field):
        patch_tw(monkeypatch,
                 status={"enable": True, "checked_in": False},
                 claim={"code": 0, field: True})
        r = run_tw()
        assert r["ok"] is True
        assert "suspicious" not in r

    def test_success_false_is_not_confirmation(self, data_home, monkeypatch):
        patch_tw(monkeypatch,
                 status={"enable": True, "checked_in": False},
                 claim={"code": 0, "success": False})
        r = run_tw()
        assert r["ok"] is True
        assert r["suspicious"] is True

    def test_already_checked_in_not_suspicious(self, data_home, monkeypatch):
        patch_tw(monkeypatch,
                 status={"enable": True, "checked_in": True, "credits": 100})
        r = run_tw()
        assert r["ok"] is True
        assert r["already"] is True
        assert "suspicious" not in r

    def test_extra_credits_still_shown_on_success(self, data_home, monkeypatch):
        patch_tw(monkeypatch,
                 claim={"code": 0, "credits": 5, "extra_credits": 2})
        r = run_tw()
        assert "suspicious" not in r
        assert "含额外奖励 2" in r["message"]


# ────────────────────────── WorkBuddy 判定 ──────────────────────────

class TestWorkBuddySuspicious:
    def test_no_credit_no_signal_is_suspicious(self, data_home, monkeypatch):
        patch_wb(monkeypatch, claim_resp=(200, {"code": 0, "data": {}}))
        r = run_wb()
        assert r["ok"] is True
        assert r["suspicious"] is True
        assert "疑似客户端改版" in r["message"]

    def test_today_credit_not_suspicious(self, data_home, monkeypatch):
        patch_wb(monkeypatch, claim_resp=(200, {"code": 0, "data": {
            "credit": 8, "streak_days": 5}}))
        r = run_wb()
        assert r["ok"] is True
        assert "suspicious" not in r
        assert "获得 8 积分" in r["message"]
        assert "连续签到 5 天" in r["message"]

    def test_total_credits_confirmation_not_suspicious(self, data_home, monkeypatch):
        # 状态没有积分，但领取 data.total_credits 有值 → 已到账确认
        patch_wb(monkeypatch,
                 claim_resp=(200, {"code": 0, "data": {"total_credits": 99}}))
        r = run_wb()
        assert r["ok"] is True
        assert "suspicious" not in r
        assert r["credits"] == 99

    @pytest.mark.parametrize("field",
                             ["today_checked_in", "checked_in", "success"])
    def test_confirmation_fields_each_exempt(self, data_home, monkeypatch, field):
        patch_wb(monkeypatch,
                 claim_resp=(200, {"code": 0, "data": {field: True}}))
        r = run_wb()
        assert r["ok"] is True
        assert "suspicious" not in r

    def test_success_false_is_not_confirmation(self, data_home, monkeypatch):
        patch_wb(monkeypatch,
                 claim_resp=(200, {"code": 0, "data": {"success": False}}))
        r = run_wb()
        assert r["ok"] is True
        assert r["suspicious"] is True

    def test_already_path_not_suspicious(self, data_home, monkeypatch):
        # 状态接口直接返回已签（业务 code 1001）
        patch_wb(
            monkeypatch,
            status_resp=(400, {"code": 1001, "msg": "今日已签到",
                               "data": None}),
            claim_resp=(200, {"code": 0, "data": {}}))
        r = run_wb()
        assert r["ok"] is True
        assert r["already"] is True
        assert "suspicious" not in r

    def test_no_streak_text_when_no_gained_credit(self, data_home, monkeypatch):
        # gained 为空时即便带了 streak_days 也不应拼连续文案（走可疑分支）
        patch_wb(monkeypatch,
                 claim_resp=(200, {"code": 0, "data": {"streak_days": 9}}))
        r = run_wb()
        assert r["suspicious"] is True
        assert "连续签到" not in r["message"]


# ────────────────────────── 历史持久化 ──────────────────────────

class TestHistoryPersist:
    def _today_item(self):
        data = jsonstore._load_json_file(history.HISTORY_FILE)
        bucket = data["days"][datetime.now().strftime("%Y-%m-%d")]
        return bucket["traework::tw_user"]

    def test_suspicious_flag_persisted(self, data_home, monkeypatch):
        patch_tw(monkeypatch, claim={"code": 0})
        r = run_tw()
        assert r.get("suspicious") is True
        history.record_checkin_history([r])
        item = self._today_item()
        assert item["suspicious"] is True
        assert item["ok"] is True

    def test_normal_success_persists_false_flag(self, data_home, monkeypatch):
        patch_tw(monkeypatch, claim={"code": 0, "credits": 3})
        r = run_tw()
        history.record_checkin_history([r])
        assert self._today_item()["suspicious"] is False

    def test_old_history_without_suspicious_key_reads_falsy(
            self, data_home, monkeypatch, tmp_path):
        # 旧版本写的历史没有 suspicious 键，bool(None) 为 False，不应炸
        patch_tw(monkeypatch, claim={"code": 0})
        r = run_tw()
        history.record_checkin_history([r])
        data = jsonstore._load_json_file(history.HISTORY_FILE)
        d = datetime.now().strftime("%Y-%m-%d")
        del data["days"][d]["traework::tw_user"]["suspicious"]
        jsonstore._save_json_file(history.HISTORY_FILE, data)
        csv_path = tmp_path / "h.csv"
        n = history.export_history_csv(str(csv_path))
        assert n == 1
        assert "成功" in csv_path.read_text(encoding="utf-8-sig")


# ────────────────────────── 日报渲染 ──────────────────────────

def _mk_report_item(platform="traework", username="u1", ok=True,
                    suspicious=False, already=False, message=None,
                    relogin=False):
    label = constants.PLATFORM_LABELS.get(platform, platform)
    if message is None:
        if not ok:
            message = ("WorkBuddy 登录态已失效，请重新登录后再次保存。"
                       if relogin else "签到失败：网络错误")
        elif suspicious:
            message = ("⚠ 签到流程已执行，但未抓到积分/成功信号，"
                       "疑似客户端改版，规则待更新，请人工确认")
        else:
            message = "签到成功，获得 5 积分"
    return {"platform": platform, "platform_label": label,
            "username": username, "ok": ok, "already": already,
            "suspicious": suspicious,
            "credits": None if suspicious else 5, "message": message}


class TestReport:
    def test_title_all_ok(self, data_home):
        title, _ = reports.build_checkin_report(
            [_mk_report_item(username="u1"), _mk_report_item(username="u2")])
        assert "✅" in title
        assert "2/2" in title

    def test_title_suspicious_only(self, data_home):
        title, content = reports.build_checkin_report(
            [_mk_report_item(username="u1", suspicious=True)])
        assert "⚠" in title
        assert "1 个账号需人工确认" in title
        assert "疑似客户端 / 接口改版" in content
        assert "u1" in content

    def test_title_mixed_fail_and_suspicious(self, data_home):
        title, _ = reports.build_checkin_report([
            _mk_report_item(username="ok1"),
            _mk_report_item(username="sus1", suspicious=True),
            _mk_report_item(username="fail1", ok=False),
        ])
        assert "❌" in title
        # suspicious 的 ok=True，故成功数按 2/3 计（设计如此，避免误报掉线）
        assert "成功 2/3" in title
        assert "另有 1 个需确认" in title

    def test_ordering_relogin_fail_suspicious_ok(self, data_home):
        results = [
            _mk_report_item(username="ok1"),
            _mk_report_item(username="sus1", suspicious=True),
            _mk_report_item(username="fail1", ok=False),
            _mk_report_item(username="relog1", ok=False, relogin=True),
        ]
        _, content = reports.build_checkin_report(results)
        # 仅按逐条账号标题（### 开头）排序，排除积分速览等汇总行
        heads = "\n".join(re.findall(r"^### .*$", content, flags=re.M))
        assert heads.find("relog1") < heads.find("fail1")
        assert heads.find("fail1") < heads.find("sus1")
        assert heads.find("sus1") < heads.find("ok1")
        assert "⚠️" in heads

    def test_summary_line_mentions_suspicious_count(self, data_home):
        _, content = reports.build_checkin_report([
            _mk_report_item(username="s1", suspicious=True),
            _mk_report_item(username="s2", suspicious=True),
            _mk_report_item(username="o1")])
        assert "2 个未抓到成功信号" in content


# ────────────────────────── 静默门禁 ──────────────────────────

def _enabled_settings(only_failures: bool) -> dict:
    return {"version": 2, "push_enabled": True,
            "channels": ["serverchan"],
            "creds": {"serverchan": {"key": "SCT123", "secret": ""}},
            "only_failures": only_failures}


def _suspicious_result() -> dict:
    return {"platform": "traework", "platform_label": "TraeWork",
            "username": "u1", "ok": True, "suspicious": True,
            "already": False, "credits": None,
            "message": "⚠ 疑似客户端改版，请人工确认"}


@pytest.fixture
def capture_push(monkeypatch):
    """打桩推送与掉线预警，返回捕获到的推送列表。"""
    pushes = []

    def _fake(settings, title, content, kind="签到结果"):
        pushes.append({"title": title, "kind": kind})
        return True, "ok"

    monkeypatch.setattr(silent, "push_wechat", mock.Mock(side_effect=_fake))
    monkeypatch.setattr(silent, "check_and_push_offline_alerts",
                        mock.Mock(return_value=0))
    return pushes


class TestSilentGate:
    def test_only_failures_still_pushes_suspicious(self, data_home, capture_push):
        settings_mod.save_settings_dict(_enabled_settings(True))
        silent._silent_record_and_push([_suspicious_result()])
        daily = [p for p in capture_push if p["kind"] == "签到结果"]
        assert len(daily) == 1
        assert "需人工确认" in daily[0]["title"]

    def test_only_failures_skips_clean_success(self, data_home, capture_push):
        settings_mod.save_settings_dict(_enabled_settings(True))
        r = _suspicious_result()
        r["suspicious"] = False
        r["message"] = "签到成功，获得 5 积分"
        r["credits"] = 5
        silent._silent_record_and_push([r])
        assert capture_push == []

    def test_offline_alert_still_runs(self, data_home, capture_push, monkeypatch):
        settings_mod.save_settings_dict(_enabled_settings(False))
        silent._silent_record_and_push([_suspicious_result()])
        silent.check_and_push_offline_alerts.assert_called_once()


# ────────────────────────── 掉线检测口径 ──────────────────────────

class TestOfflineSemantics:
    def _write_buckets(self, buckets: dict):
        jsonstore._save_json_file(
            history.HISTORY_FILE, {"days": buckets})

    def test_suspicious_counts_as_alive_success_today(self, data_home):
        # 今天可疑成功（ok=True）→ streak 中断，不触发掉线
        d = datetime.now().strftime("%Y-%m-%d")
        item = {"platform": "traework", "platform_label": "TraeWork",
                "username": "u1", "note": "", "ok": True, "already": False,
                "suspicious": True, "credits": None,
                "message": "⚠ 疑似改版", "time": "09:00"}
        self._write_buckets({d: {"traework::u1": item}})
        assert silent.detect_offline_accounts() == []

    def test_hard_failures_still_detected(self, data_home):
        # 对照组：连续 3 天硬失败仍命中
        today = datetime.now().date()
        buckets = {}
        for delta in range(3):
            d = (today - timedelta(days=delta)).strftime("%Y-%m-%d")
            buckets[d] = {"traework::u1": {
                "platform": "traework", "platform_label": "TraeWork",
                "username": "u1", "note": "", "ok": False,
                "already": False, "suspicious": False,
                "credits": None, "message": "登录态已失效", "time": "09:00"}}
        self._write_buckets(buckets)
        hit = silent.detect_offline_accounts()
        assert len(hit) == 1
        assert hit[0]["streak"] == 3


# ────────────────────────── CSV 导出 ──────────────────────────

class TestCsvExport:
    def test_csv_marks_suspicious_row(self, data_home, tmp_path):
        r = {"platform": "traework", "platform_label": "TraeWork",
             "username": "u1", "ok": True, "already": False,
             "suspicious": True, "credits": None,
             "message": "⚠ 疑似客户端改版，请人工确认"}
        history.record_checkin_history([r])
        path = tmp_path / "out.csv"
        assert history.export_history_csv(str(path)) == 1
        text = path.read_text(encoding="utf-8-sig")
        assert "成功（待人工确认）" in text
        assert "疑似改版" in text
