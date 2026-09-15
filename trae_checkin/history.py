# -*- coding: utf-8 -*-
"""签到历史记录、统计、日历、趋势与 CSV 导出。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import platform
import time
from typing import Any, Optional
from .constants import PLATFORM_LABELS, PLAT_TRAEWORK
from .jsonstore import _load_json_file, _save_json_file
from .runtime import _log_dir, log


HISTORY_FILE = _log_dir() / "checkin_history.json"


HISTORY_KEEP_DAYS = 90


def history_identity(platform: str, username: str) -> str:
    """历史记录中账号的稳定标识。"""
    return f"{platform or '?'}::{username or '?'}"


def record_checkin_history(results: list[dict]) -> None:
    """把一次批量/单账号签到结果按账号、按天写入历史（每天每账号仅一条，覆盖当天）。"""
    try:
        data = _load_json_file(HISTORY_FILE)
    except Exception:
        data = {}
    days = data.get("days")
    if not isinstance(days, dict):
        days = {}
    today = datetime.now().strftime("%Y-%m-%d")
    now_hm = datetime.now().strftime("%H:%M")
    bucket = days.setdefault(today, {})
    for r in results:
        if not isinstance(r, dict):
            continue
        plat = r.get("platform") or PLAT_TRAEWORK
        label = r.get("platform_label") or PLATFORM_LABELS.get(plat, plat)
        uname = r.get("username") or "未知账号"
        ident = history_identity(plat, uname)
        existing = bucket.get(ident)
        # 已成功记录不被后续失败覆盖（静默兜底可能对同一平台跑两次）
        if existing and existing.get("ok") and not r.get("ok"):
            continue
        # 新结果未带备注时沿用当天已有备注
        note = r.get("note") or (existing.get("note") if existing else "") or ""
        bucket[ident] = {
            "platform": plat, "platform_label": label, "username": uname,
            "note": note,
            "ok": bool(r.get("ok")), "already": bool(r.get("already")),
            "suspicious": bool(r.get("suspicious")),
            "credits": r.get("credits"), "message": r.get("message", ""),
            "time": now_hm,
        }
    # 仅保留最近 HISTORY_KEEP_DAYS 天
    cutoff = time.time() - HISTORY_KEEP_DAYS * 86400
    for d in list(days.keys()):
        try:
            if datetime.strptime(d, "%Y-%m-%d").timestamp() < cutoff:
                days.pop(d, None)
        except ValueError:
            days.pop(d, None)
    data["days"] = days
    try:
        _save_json_file(HISTORY_FILE, data)
    except Exception as e:
        log.error(f"写入签到历史失败：{e}")


def load_history() -> dict:
    try:
        data = _load_json_file(HISTORY_FILE)
        if isinstance(data.get("days"), dict):
            return data
    except Exception:
        pass
    return {"days": {}}


def history_summary(days_back: int = 30) -> dict:
    """汇总近 N 天统计：每账号连续签到天数、窗口内成功率、今日概况。"""
    data = load_history()
    days: dict = data.get("days", {})
    today = datetime.now().date()
    window = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(days_back)]
    accounts: dict[str, dict] = {}
    today_ok = today_total = 0
    for d in window:
        bucket = days.get(d) or {}
        for ident, item in bucket.items():
            acc = accounts.setdefault(ident, {
                "platform": item.get("platform", ""),
                "platform_label": item.get("platform_label", ""),
                "username": item.get("username", ""),
                "note": item.get("note", ""),
                "last_date": "", "last_time": "",
                "success": 0, "fail": 0, "checked_days": set()})
            # window 从今天向过去排列，首次遇到即最近一条记录
            if not acc["last_date"]:
                acc["last_date"] = d
                acc["last_time"] = item.get("time", "")
            if item.get("note") and not acc["note"]:
                acc["note"] = item.get("note")
            if item.get("ok"):
                acc["success"] += 1
                acc["checked_days"].add(d)
            else:
                acc["fail"] += 1
            if d == window[0]:
                today_total += 1
                if item.get("ok"):
                    today_ok += 1
    out_accounts = []
    for ident, acc in accounts.items():
        total = acc["success"] + acc["fail"]
        rate = round(acc["success"] * 100.0 / total) if total else 0
        # 连续天数：从今天（若今天还没记录则从昨天起算）往回数
        streak = 0
        cursor = today
        checked = acc["checked_days"]
        if cursor.strftime("%Y-%m-%d") not in checked:
            cursor = cursor - timedelta(days=1)
        while cursor.strftime("%Y-%m-%d") in checked:
            streak += 1
            cursor = cursor - timedelta(days=1)
        out_accounts.append({
            "identity": ident,
            "platform_label": acc["platform_label"], "username": acc["username"],
            "note": acc["note"], "last_date": acc["last_date"],
            "last_time": acc["last_time"],
            "streak": streak, "rate": rate, "success": acc["success"],
            "fail": acc["fail"], "total": total})
    out_accounts.sort(key=lambda a: (a["platform_label"], a["username"]))
    return {"accounts": out_accounts, "today_ok": today_ok,
            "today_total": today_total, "days_back": days_back}


def account_status_lookup(days_back: int = 90) -> dict:
    """{history_identity: {note,last_date,last_time,streak}}，供 GUI 账号行展示。"""
    out: dict[str, dict] = {}
    try:
        for a in history_summary(days_back).get("accounts") or []:
            out[a["identity"]] = {
                "note": a.get("note", ""),
                "last_date": a.get("last_date", ""),
                "last_time": a.get("last_time", ""),
                "streak": a.get("streak", 0),
            }
    except Exception:
        pass
    return out


def history_calendar(days_back: int = 90) -> dict:
    """近 N 天日历聚合（供热力图）。

    返回 {date: {state: none|all_ok|partial|all_fail,
                 success, fail, total, items}}，并含起止日期。
    """
    days: dict = load_history().get("days", {})
    today = datetime.now().date()
    out: dict = {}
    for i in range(days_back):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        bucket = days.get(d) or {}
        items = list(bucket.values())
        success = sum(1 for it in items if it.get("ok"))
        fail = len(items) - success
        if not items:
            state = "none"
        elif fail == 0:
            state = "all_ok"
        elif success == 0:
            state = "all_fail"
        else:
            state = "partial"
        out[d] = {"state": state, "success": success, "fail": fail,
                  "total": len(items), "items": items}
    return out


def history_recent_rows(limit: int = 14) -> list[dict]:
    """返回最近 limit 天（按日期倒序）的行：{date, items:[...]}。"""
    days: dict = load_history().get("days", {})
    dates = sorted(days.keys(), reverse=True)[:limit]
    rows = []
    for d in dates:
        items = sorted(days[d].values(),
                       key=lambda x: (x.get("platform_label", ""), x.get("username", "")))
        rows.append({"date": d, "items": items})
    return rows


def history_monthly_stats(months: int = 3) -> list[dict]:
    """最近 months 个自然月的每月每账号统计：成功/失败/成功率。"""
    days: dict = load_history().get("days", {})
    today = datetime.now().date()
    # 生成最近 months 个月份键（YYYY-MM），倒序
    keys: list[str] = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    # {月份: {ident: [success, fail, label, uname]}}
    bucket: dict[str, dict] = {k: {} for k in keys}
    for d, items in days.items():
        mkey = d[:7]
        if mkey not in bucket or not isinstance(items, dict):
            continue
        for ident, item in items.items():
            cell = bucket[mkey].setdefault(ident, [0, 0,
                                                    item.get("platform_label", ""),
                                                    item.get("username", "")])
            cell[0 if item.get("ok") else 1] += 1
    out: list[dict] = []
    for mkey in keys:
        accounts = []
        for ident, (succ, fail, label, uname) in bucket[mkey].items():
            total = succ + fail
            accounts.append({
                "identity": ident, "platform_label": label, "username": uname,
                "success": succ, "fail": fail, "total": total,
                "rate": round(succ * 100.0 / total) if total else 0,
            })
        accounts.sort(key=lambda a: (a["platform_label"], a["username"]))
        out.append({"month": mkey, "accounts": accounts})
    return out


def export_history_csv(path: str) -> int:
    """
    把全部本地签到历史导出为 Excel 友好的 CSV（UTF-8 BOM）。
    返回导出的记录条数。
    """
    days: dict = load_history().get("days", {})
    rows = ["日期,星期,平台,账号,结果,类型,积分,时间,说明"]
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    count = 0

    def _csv_escape(v: Any) -> str:
        s = "" if v is None else str(v)
        return '"' + s.replace('"', '""') + '"'

    for d in sorted(days.keys()):
        try:
            wd = weekday_cn[datetime.strptime(d, "%Y-%m-%d").weekday()]
        except Exception:
            wd = ""
        for item in sorted(days[d].values(),
                           key=lambda x: (x.get("platform_label", ""), x.get("username", ""))):
            ok = bool(item.get("ok"))
            susp = bool(item.get("suspicious"))
            if not ok:
                kind = "失败"
            elif item.get("already"):
                kind = "已签到（幂等）"
            elif susp:
                kind = "成功但无成功信号（疑似改版）"
            else:
                kind = "签到成功"
            result_txt = "失败" if not ok else ("成功（待人工确认）" if susp else "成功")
            rows.append(",".join([
                _csv_escape(d), _csv_escape(wd),
                _csv_escape(item.get("platform_label", "")),
                _csv_escape(item.get("username", "")),
                _csv_escape(result_txt),
                _csv_escape(kind),
                _csv_escape(item.get("credits")),
                _csv_escape(item.get("time", "")),
                _csv_escape(item.get("message", "")),
            ]))
            count += 1
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(rows))
    return count


TREND_MAX_LINES = 6


TREND_LINE_COLORS = ("#2f6bff", "#16a34a", "#e8870e", "#9b59b6",
                     "#0ea5b7", "#e0457b")


def history_credit_trend(days_back: int = 30) -> dict:
    """近 N 天每个账号的积分走势（历史里的 credits 是签到后积分余额）。

    返回 {dates:[升序日期],
          series:[{identity,label,color,points:[None|int]}],
          max_credit, has_data}。缺失日期补 None（断线处理由绘制端决定）。
    """
    days: dict = load_history().get("days", {})
    today = datetime.now().date()
    dates = [(today - timedelta(days=days_back - 1 - i)).strftime("%Y-%m-%d")
             for i in range(days_back)]
    # identity -> {label, {date: credit}}
    raw: dict[str, dict] = {}
    max_credit = 0
    for d in dates:
        for ident, item in (days.get(d) or {}).items():
            c = item.get("credits")
            if not isinstance(c, (int, float)):
                continue
            c = int(c)
            cell = raw.setdefault(ident, {"label": "", "pts": {}})
            cell["label"] = _trend_label(item)
            cell["pts"][d] = c
            if c > max_credit:
                max_credit = c
    # 账号顺序：按窗口内最后一次出现的积分余额倒序（积分高的靠前）
    def _last_val(cell: dict) -> int:
        for d in reversed(dates):
            v = cell["pts"].get(d)
            if v is not None:
                return v
        return -1
    ordered = sorted(raw.items(), key=lambda kv: _last_val(kv[1]), reverse=True)
    series = []
    for i, (ident, cell) in enumerate(ordered[:TREND_MAX_LINES]):
        series.append({
            "identity": ident,
            "label": cell["label"],
            "color": TREND_LINE_COLORS[i % len(TREND_LINE_COLORS)],
            "points": [cell["pts"].get(d) for d in dates],
        })
    return {"dates": dates, "series": series,
            "max_credit": max_credit, "has_data": bool(series),
            "truncated": len(raw) > TREND_MAX_LINES}


def _trend_label(item: dict) -> str:
    """趋势线图例名：备注 或 用户名（带平台前缀）。"""
    label = item.get("platform_label") or ""
    who = item.get("note") or item.get("username") or "未知账号"
    return f"{label}·{who}" if label else str(who)
