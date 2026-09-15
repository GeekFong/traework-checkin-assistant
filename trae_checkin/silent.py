# -*- coding: utf-8 -*-
"""定时任务静默执行：重试、掉线预警、周期报告。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import os
import random
import time
from typing import Any, Optional
from .accounts import list_accounts
from .constants import PLATFORM_LABELS, PLAT_TRAEWORK, PLAT_WORKBUDDY
from .history import history_identity, history_recent_rows, load_history, record_checkin_history
from .jsonstore import _load_json_file, _save_json_file
from .push import push_wechat
from .reports import build_checkin_report, build_monthly_report, build_weekly_report
from .runtime import _log_dir, log
from .service import run_batch_checkin
from .settings import OFFLINE_ALERT_KEY, load_settings, push_configured, save_settings_dict
from .platforms.traework import is_logged_in, run_checkin
from .platforms.workbuddy import run_wb_checkin_live, wb_is_logged_in


PERIOD_MARKERS_FILE = _log_dir() / "period_markers.json"


def _today_done_identities() -> set:
    """今日历史桶中已成功签到的账号标识集合。"""
    today = datetime.now().strftime("%Y-%m-%d")
    bucket = load_history().get("days", {}).get(today) or {}
    return {ident for ident, item in bucket.items()
            if isinstance(item, dict) and item.get("ok")}


def _silent_expected_identities(accounts: list[dict]) -> set:
    """
    静默运行预期会签到的账号集合：
      - 所有启用的账号快照；
      - 近 7 天历史中出现过、但其平台未被快照覆盖的桌面端 live 兜底账号
        （例如只保存了 TraeWork 快照，但 WorkBuddy 也一直靠登录态自动签）。
    """
    enabled = [a for a in accounts if a.get("enabled", True)]
    expected = {history_identity(a.get("platform", ""), a.get("username", ""))
                for a in enabled}
    saved_platforms = {a.get("platform") for a in enabled}
    for row in history_recent_rows(7):
        for item in row.get("items", []):
            if item.get("platform") not in saved_platforms:
                expected.add(history_identity(
                    item.get("platform", ""), item.get("username", "")))
    return expected


def _silent_retry_config() -> tuple[int, int, int]:
    """
    失败分轮重试配置，返回 (重试轮数, 最小等待秒, 最大等待秒)。
    可用环境变量覆盖以便测试：
      TRAESIGN_RETRY_ROUNDS（默认 2，即首轮失败后再试 2 轮）
      TRAESIGN_RETRY_WAIT_MIN（默认 1200 秒 = 20 分钟）
      TRAESIGN_RETRY_WAIT_MAX（默认 2400 秒 = 40 分钟）
    """
    def _int_env(name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default
    rounds = _int_env("TRAESIGN_RETRY_ROUNDS", 2)
    wait_min = _int_env("TRAESIGN_RETRY_WAIT_MIN", 1200)
    wait_max = _int_env("TRAESIGN_RETRY_WAIT_MAX", 2400)
    if wait_max < wait_min:
        wait_max = wait_min
    return rounds, wait_min, wait_max


def _merge_results(old: list[dict], new: list[dict]) -> list[dict]:
    """
    按平台+用户名合并两轮结果：成功记录永不被后续失败覆盖；
    失败记录则以最新一轮结果为准（消息可能从「网络错误」变成成功或更具体原因）。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for r in old + new:
        ident = history_identity(r.get("platform", ""), r.get("username", ""))
        if ident not in merged:
            merged[ident] = r
            order.append(ident)
            continue
        if merged[ident].get("ok"):
            continue
        merged[ident] = r
    return [merged[i] for i in order]


def _silent_run_with_retry(accounts: list[dict]) -> list[dict]:
    """
    执行一轮批量签到（含桌面端 live 兜底），失败账号分轮重试。
    每轮仅重试上一轮仍失败的账号；成功结果不被覆盖；全部轮次结束后统一返回。
    """
    active = [a for a in accounts if a.get("enabled", True)]
    saved_platforms = {a.get("platform") for a in active}

    if active:
        log.info(f"开始批量签到，共 {len(active)} 个启用账号（双平台）")
        # run_batch_checkin 对每个启用账号恰好产出一条结果，顺序与 active 对齐
        batch_results = run_batch_checkin(active, status_only=False)
        for acct, r in zip(active, batch_results):
            r["_acct_key"] = acct.get("key", "")
        results = list(batch_results)
        # live 兜底：首轮尝试未覆盖平台；后续轮次只重试失败平台
        results = _silent_append_live(results, saved_platforms)
    else:
        log.info("未配置多账号，尝试当前客户端登录账号签到（TraeWork / WorkBuddy）")
        batch_results = []
        results = _silent_append_live([])

    for r in results:
        (log.info if r.get("ok") else log.error)(
            f"[{r.get('platform_label', '?')}/{r.get('username', '?')}] "
            f"{r.get('message', '')}")

    rounds, wait_min, wait_max = _silent_retry_config()
    for round_no in range(1, rounds + 1):
        # 快照账号按 key 对齐失败结果（_acct_key 为首轮内部标记）
        ok_keys = {r.get("_acct_key") for r in results
                   if r.get("ok") and r.get("_acct_key")}
        failed_keys = {r.get("_acct_key") for r in results
                       if not r.get("ok") and r.get("_acct_key")}
        failed_saved = [a for a in active
                        if a.get("key", "") in failed_keys
                        and a.get("key", "") not in ok_keys]
        # live 平台：首轮结果中失败的平台才重试
        live_failed_platforms = {
            r.get("platform") for r in results
            if not r.get("ok") and r.get("platform") not in saved_platforms}
        if not failed_saved and not live_failed_platforms:
            break
        wait_s = random.randint(wait_min, wait_max) if wait_max else wait_min
        log.info(f"第 {round_no}/{rounds} 轮重试：{len(failed_saved)} 个快照账号、"
                 f"{len(live_failed_platforms)} 个 live 平台仍失败，"
                 f"{wait_s} 秒后重试")
        if wait_s:
            time.sleep(wait_s)
        round_results: list[dict] = []
        if failed_saved:
            retry_batch = run_batch_checkin(failed_saved, status_only=False)
            for acct, r in zip(failed_saved, retry_batch):
                r["_acct_key"] = acct.get("key", "")
            round_results.extend(retry_batch)
        if live_failed_platforms:
            round_results = _silent_append_live(
                round_results,
                saved_platforms | ({PLAT_TRAEWORK, PLAT_WORKBUDDY}
                                   - live_failed_platforms))
        for r in round_results:
            (log.info if r.get("ok") else log.error)(
                f"[重试{round_no}][{r.get('platform_label', '?')}/"
                f"{r.get('username', '?')}] {r.get('message', '')}")
        results = _merge_results(results, round_results)
    # 剥离仅供本轮重试对齐使用的内部字段
    for r in results:
        r.pop("_acct_key", None)
    return results


def _silent_record_and_push(results: list[dict]) -> None:
    """统一写入历史并按设置推送日报（全部轮次重试结束后只推一次）。"""
    ok_n = sum(1 for r in results if r.get("ok"))
    suspicious_n = sum(1 for r in results
                       if r.get("ok") and r.get("suspicious"))
    record_checkin_history(results)
    settings = None
    try:
        settings = load_settings()
        if settings.get("push_enabled") and push_configured(settings):
            fail_n = len(results) - ok_n
            # 「仅失败时推送」下，假成功（suspicious）也必须推送，否则被静默吞掉
            if not (settings.get("only_failures")
                    and fail_n == 0 and suspicious_n == 0):
                title, content = build_checkin_report(results)
                pushed, pmsg = push_wechat(settings, title, content,
                                           kind="签到结果")
                (log.info if pushed else log.error)(f"推送结果：{pmsg}")
            else:
                log.info("全部成功且开启了「仅失败时推送」，本次不推送日报。")
    except Exception as e:
        log.error(f"推送流程异常：{e}")
    # 连续失败「疑似掉线」预警：独立保护，任何异常都不影响主流程与日报
    try:
        check_and_push_offline_alerts(settings)
    except Exception as e:
        log.error(f"掉线预警异常：{e}")


def _offline_streak_days() -> int:
    """连续失败多少天触发预警，默认 3；TRAESIGN_OFFLINE_DAYS 可压缩以便测试。"""
    try:
        return max(1, int(os.environ.get("TRAESIGN_OFFLINE_DAYS", "3")))
    except (TypeError, ValueError):
        return 3


def detect_offline_accounts(results: Optional[list[dict]] = None,
                            threshold: Optional[int] = None) -> list[dict]:
    """扫描历史，找出截至今天已连续失败 ≥ threshold 天的账号。

    判定规则（从今天含今天向过去逐天查看，最多回溯 30 天）：
      - 整天 bucket 缺失（当天机器根本没跑）：所有人的连续计数到此中断；
      - bucket 存在但缺该账号记录（机器跑了、该账号未参与/被移除）：
        该账号的连续计数中断，避免把"没签到"误算成"签到失败"；
      - 当天该账号成功（含「已签到」）：连续失败中断；
      - 连续失败天数达到阈值即命中。
    results：保留给调用方传当天内存结果；本函数统一读落盘历史，故未使用。
    """
    threshold = threshold or _offline_streak_days()
    days = load_history().get("days") or {}
    today = datetime.now().date()
    # 静默流程在调用前已 record_checkin_history，今天的结果已在历史里
    idents: dict[str, dict] = {}
    for i in range(30):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        bucket = days.get(d)
        if not isinstance(bucket, dict):
            break  # 当天机器没跑：所有人的连续计数到此为止
        seen: set[str] = set()
        for ident, item in bucket.items():
            if not isinstance(item, dict):
                continue
            seen.add(ident)
            info = idents.setdefault(ident, {
                "ident": ident,
                "platform": item.get("platform", ""),
                "platform_label": item.get("platform_label", "")
                                  or PLATFORM_LABELS.get(item.get("platform", ""), ""),
                "username": item.get("username", ""),
                "note": item.get("note", ""),
                "streak": 0, "last_message": "", "alive": True})
            info["note"] = info["note"] or item.get("note", "") or ""
            if info["alive"]:
                if item.get("ok"):
                    info["alive"] = False  # 遇到成功，连续失败中断
                else:
                    info["streak"] += 1
                    # 遍历从今天向过去，保留最近一次（第一次遇到）的报错
                    if not info["last_message"]:
                        info["last_message"] = item.get("message", "") or ""
        # 机器当天跑了但没有该账号的结果：不能算作连续失败的一天
        for ident, info in idents.items():
            if info["alive"] and ident not in seen:
                info["alive"] = False
    return [v for v in idents.values()
            if v["alive"] and v["streak"] >= threshold]


def _offline_alert_content(accs: list[dict]) -> tuple[str, str]:
    now = datetime.now()
    min_streak = min(int(a.get("streak", 0) or 0) for a in accs)
    title = (f"⚠️ 疑似掉线：{len(accs)} 个账号连续 {min_streak} 天签到失败 "
             f"{now.strftime('%m-%d')}")
    lines = ["**疑似账号掉线 / 登录态失效，请重新登录**", "",
             f"以下账号已连续 {min_streak} 天以上签到失败：", ""]
    for a in accs:
        name = a.get("note") or a.get("username") or a.get("ident")
        plat = a.get("platform_label") or PLATFORM_LABELS.get(
            a.get("platform", ""), "")
        lines.append(f"### 🔴 [{plat}] {name}")
        lines.append(f"连续失败 {a['streak']} 天"
                     + (f"，最近报错：{a['last_message']}"
                        if a.get("last_message") else ""))
        lines.append("")
    lines += [
        "---", "### 处理步骤", "",
        "1. 打开对应桌面客户端（TraeWork CN / WorkBuddy）；",
        "2. 检查登录状态，如已退出请重新登录；",
        "3. 登录成功后保持在线约 10 秒；",
        "4. 打开本助手点「保存当前登录账号」更新凭据，",
        "    再点「全部账号签到」验证；",
        "5. 若账号正常但仍失败，可点「健康自检」排查网络与客户端。", "",
        "在恢复成功签到前，本条提醒每天最多发送一次，不会重复打扰。",
        f"提醒时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return title, "\n".join(lines)


def check_and_push_offline_alerts(settings: Optional[dict] = None) -> int:
    """检测连续失败账号并按需推送预警，返回本次推送的账号数。

    去重：settings.offline_alerts[ident] = "YYYY-MM-DD"，
    同一掉线周期（未恢复成功前）每天最多推一次；账号恢复成功后
    历史连续中断，旧标记保留也不会再次命中（命中以 streak 为准）。
    """
    try:
        settings = settings if settings is not None else load_settings()
        if not (settings.get("push_enabled") and push_configured(settings)):
            return 0
        accs = detect_offline_accounts()
        if not accs:
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        alerts = settings.get(OFFLINE_ALERT_KEY)
        if not isinstance(alerts, dict):
            alerts = {}
        fresh = [a for a in accs if alerts.get(a["ident"]) != today]
        if not fresh:
            log.info(f"{len(accs)} 个账号连续失败但今日已预警过，跳过重复推送。")
            return 0
        title, content = _offline_alert_content(fresh)
        pushed, pmsg = push_wechat(settings, title, content,
                                   kind="掉线预警")
        if pushed:
            for a in fresh:
                alerts[a["ident"]] = today
            # 清理 60 天前的旧标记
            cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            alerts = {k: v for k, v in alerts.items()
                      if isinstance(v, str) and v >= cutoff}
            settings[OFFLINE_ALERT_KEY] = alerts
            save_settings_dict(settings)
            log.info(f"掉线预警已推送：{len(fresh)} 个账号（{pmsg}）")
            return len(fresh)
        log.error(f"掉线预警推送失败：{pmsg}")
        return 0
    except Exception as e:
        log.error(f"掉线预警流程异常：{e}")
        return 0


def _send_periodic_reports() -> None:
    """
    周一周报、每月 1~3 号月报（补上月初几天机器没开机的情况）。
    用标记文件记录当天已发送项，防止同一天开机/登录触发器重复推送。
    周报/月报属于汇总消息，不受「仅失败时推送」开关影响。
    """
    try:
        now = datetime.now()
        pending = []
        if now.weekday() == 0:
            pending.append(("weekly_" + now.strftime("%Y-%m-%d"),
                            build_weekly_report))
        if now.day <= 3:
            pending.append(("monthly_" + now.strftime("%Y-%m"),
                            build_monthly_report))
        if not pending:
            return
        markers = _load_json_file(PERIOD_MARKERS_FILE)
        sent = markers.get("sent")
        if not isinstance(sent, dict):
            sent = {}
        todo = [(key, builder) for key, builder in pending if not sent.get(key)]
        if not todo:
            return
        settings = load_settings()
        if not (settings.get("push_enabled") and push_configured(settings)):
            log.info("周报/月报已到期，但推送未开启或未配置凭据，本次跳过。")
            return
        for key, builder in todo:
            try:
                title, content = builder()
                kind = "签到周报" if key.startswith("weekly_") else "签到月报"
                pushed, pmsg = push_wechat(settings, title, content, kind=kind)
                if pushed:
                    sent[key] = now.strftime("%Y-%m-%d %H:%M")
                    log.info(f"周期报告已推送：{title}（{pmsg}）")
                else:
                    log.error(f"周期报告推送失败：{pmsg}")
            except Exception as e:
                log.error(f"周期报告生成/推送异常：{e}")
        # 仅清理 60 天前的旧标记
        cutoff = (now - timedelta(days=60)).strftime("%Y-%m-%d")
        sent = {k: v for k, v in sent.items()
                if k[-10:] >= cutoff or not isinstance(v, str)}
        _save_json_file(PERIOD_MARKERS_FILE, {"sent": sent})
    except Exception as e:
        log.warning(f"周期报告检查失败（不影响签到）：{e}")


def silent_run() -> int:
    """定时任务静默执行：周期报告 → 批量签到（失败分轮重试）→ 汇总推送。"""
    log.info("=" * 40)
    # 周报/月报在早退判断之前发送：即使今天账号都已签到，周一/月初也应收报告
    try:
        _send_periodic_reports()
    except Exception as e:
        log.warning(f"周期报告流程异常：{e}")

    accounts = list_accounts()
    # 今日已全部成功签到则立即退出：每日定时任务通常先跑，开机/登录触发器
    # 当天可能再次触发，早退可避免重复请求和重复推送。
    # 设置环境变量 TRAESIGN_FORCE=1 可强制重跑（排查用）。
    if os.environ.get("TRAESIGN_FORCE") != "1":
        try:
            expected = _silent_expected_identities(accounts)
            done = _today_done_identities()
            if expected and expected <= done:
                log.info(
                    f"今日 {len(expected)} 个账号均已成功签到，静默任务直接退出"
                    "（不重复签到、不重复推送）")
                return 0
        except Exception as e:
            log.warning(f"今日签到状态判断失败，按正常流程执行：{e}")
    # 随机抖动 0~5 分钟，避免每天固定整点请求（可通过环境变量关闭，便于测试）
    if os.environ.get("TRAESIGN_NO_JITTER") != "1":
        jitter = random.randint(0, 300)
        if jitter:
            log.info(f"随机延迟 {jitter} 秒后开始签到（避免整点并发）")
            time.sleep(jitter)

    results = _silent_run_with_retry(accounts)
    if not results:
        log.error("未检测到任何已登录平台（TraeWork / WorkBuddy）。")
        return 1
    _silent_record_and_push(results)
    ok_n = sum(1 for r in results if r.get("ok"))
    return 0 if ok_n == len(results) else 1


def _silent_append_live(results: list[dict],
                        saved_platforms: Optional[set] = None,
                        status_only: bool = False) -> list[dict]:
    """对尚未保存快照的平台，补签其当前桌面端登录账号（静默兜底）。"""
    saved_platforms = saved_platforms if saved_platforms is not None else set()
    if PLAT_TRAEWORK not in saved_platforms:
        try:
            logged, _ = is_logged_in()
        except Exception:
            logged = False
        if logged:
            results.append(run_checkin(status_only=status_only))
    if PLAT_WORKBUDDY not in saved_platforms:
        try:
            wb_logged, _ = wb_is_logged_in()
        except Exception:
            wb_logged = False
        if wb_logged:
            results.append(run_wb_checkin_live(status_only=status_only))
    return results
