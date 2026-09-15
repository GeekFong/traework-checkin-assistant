# -*- coding: utf-8 -*-
"""签到日报 / 周报 / 月报构建。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from .constants import PLATFORM_LABELS, PLAT_TRAEWORK, PLAT_WORKBUDDY
from .history import history_identity, history_monthly_stats, history_recent_rows, history_summary


_RELOGIN_HINTS = ("登录已失效", "登录态已失效", "请重新登录", "无权限", "凭证")


def _is_relogin_failure(r: dict) -> bool:
    """识别因登录态失效导致的失败（用于在推送中给出重新登录指引）。"""
    if r.get("ok"):
        return False
    msg = str(r.get("message") or "")
    return any(h in msg for h in _RELOGIN_HINTS)


def _result_account_display(r: dict) -> str:
    """报告中单个账号的展示名：[平台] 备注（用户名）。"""
    label = r.get("platform_label") or PLATFORM_LABELS.get(
        r.get("platform", PLAT_TRAEWORK), "")
    user = r.get("username") or r.get("display_name") or "未知账号"
    note = r.get("note")
    if note:
        user = f"{note}（{user}）"
    return f"[{label}] {user}" if label else user


def _result_detail(r: dict) -> str:
    """单个账号签到结果的一句话说明。"""
    if r.get("ok"):
        if r.get("platform") == PLAT_WORKBUDDY:
            return r.get("message", "完成")
        credits = r.get("credits")
        if credits is not None and r.get("checked_in"):
            extra = r.get("extra_credits")
            detail = f"今日已签到，当前 {credits} 积分"
            if extra:
                detail += f"（本次额外 {extra}）"
            return detail
        return r.get("message", "完成")
    return r.get("message", "失败")


def _stats_lookup() -> dict:
    """{identity: {streak, rate30, month_rate, month_success, month_total}}。"""
    out: dict[str, dict] = {}
    try:
        for a in history_summary(30).get("accounts") or []:
            out[history_identity(a.get("platform", ""), a.get("username", ""))] = {
                "streak": a.get("streak", 0), "rate30": a.get("rate", 0)}
    except Exception:
        pass
    try:
        for a in (history_monthly_stats(1)[0].get("accounts") or []):
            cell = out.setdefault(a.get("identity", ""), {})
            cell.update({"month_rate": a.get("rate", 0),
                         "month_success": a.get("success", 0),
                         "month_total": a.get("total", 0)})
    except Exception:
        pass
    return out


def _relogin_guide(relogin_items: list[dict]) -> list[str]:
    """登录失效账号的分步操作指引（推送内置）。"""
    if not relogin_items:
        return []
    lines = ["---", "### ⚠️ 登录已失效，请按以下步骤处理", ""]
    for r in relogin_items:
        label = r.get("platform_label") or PLATFORM_LABELS.get(
            r.get("platform", PLAT_TRAEWORK), "客户端")
        lines.append(f"**{_result_account_display(r)}**")
        lines.append(f"1. 在电脑上打开 {label} 桌面客户端；")
        lines.append("2. 重新登录该账号（登录成功后保持客户端在线 10 秒）；")
        lines.append("3. 打开本签到助手 → 在账号卡片点「保存当前登录账号」刷新凭据；")
        lines.append("4. 点「立即签到全部」验证，或等待次日自动执行。")
        lines.append("")
    return lines


def build_checkin_start_report(accounts: list[dict]) -> tuple[str, str]:
    """定时任务触发时生成「开始签到」通知的 (标题, Markdown 正文)。

    每日到点与关机错过后的开机/登录补签共用同一静默入口，
    因此本消息统一覆盖两种场景，不区分触发来源。
    """
    now = datetime.now()
    enabled = [a for a in accounts if a.get("enabled", True)]
    n = len(enabled)
    title = (f"🔔 定时签到任务已启动（{n} 个账号） "
             f"{now.strftime('%m-%d %H:%M')}")
    lines = ["**定时签到任务已触发，即将开始签到。**", ""]
    for a in enabled:
        label = PLATFORM_LABELS.get(
            a.get("platform", PLAT_TRAEWORK), a.get("platform", ""))
        user = a.get("note") or a.get("display_name") or a.get(
            "username") or "未知账号"
        lines.append(f"- [{label}] {user}")
    lines += [
        "",
        "同时会检测桌面客户端登录态，为未保存快照的账号自动补签。",
        "",
        "> 若设定时间点本机处于关机状态，本条消息来自开机后的自动补签。",
        "> 全部签到完成后，将再推送一条当日签到结果日报。",
        "",
        f"触发时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return title, "\n".join(lines)


def build_checkin_report(results: list[dict], test: bool = False) -> tuple[str, str]:
    """根据批量签到结果生成 (标题, Markdown 正文)。失败账号置顶并附处理指引。"""
    if test:
        return "签到助手：推送测试成功", (
            "这是一条测试消息。\n\n收到此消息说明推送渠道已配置成功，"
            "今后每日自动签到（TraeWork / WorkBuddy）的结果会汇总推送到这里。\n\n"
            "可同时开启多个推送渠道；登录态失效时消息内会附重新登录操作步骤。")
    total = len(results)
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = total - ok_n
    suspicious_n = sum(1 for r in results if r.get("ok") and r.get("suspicious"))
    now = datetime.now()
    today = now.strftime("%m-%d")
    plats = {r.get("platform", PLAT_TRAEWORK) for r in results}
    scope = "多平台" if len(plats) > 1 else PLATFORM_LABELS.get(
        next(iter(plats)), "签到")
    if fail_n == 0 and suspicious_n == 0:
        title = f"✅ {scope}签到全部成功（{ok_n}/{total}）{today}"
    elif fail_n == 0:
        title = f"⚠ {scope}签到完成但有 {suspicious_n} 个账号需人工确认 {today}"
    else:
        title = f"❌ {scope}签到有失败（成功 {ok_n}/{total}）{today}"
        if suspicious_n:
            title += f"，另有 {suspicious_n} 个需确认"

    # 失败（尤其登录失效）置顶，可疑成功紧随其后
    relogin = [r for r in results if _is_relogin_failure(r)]
    other_fail = [r for r in results if not r.get("ok") and not _is_relogin_failure(r)]
    suspicious = [r for r in results if r.get("ok") and r.get("suspicious")]
    oks = [r for r in results if r.get("ok") and not r.get("suspicious")]
    ordered = relogin + other_fail + suspicious + oks
    stats = _stats_lookup()

    lines = [f"**{now.strftime('%Y年%m月%d日')} 签到报告**", ""]
    lines.append(f"共 {total} 个账号：✅ 成功 {ok_n}，❌ 失败 {fail_n}。"
                 + (f"其中 {suspicious_n} 个未抓到成功信号，需人工确认。"
                    if suspicious_n else ""))
    # 积分速览：列出本次返回积分的账号
    credit_parts = []
    for r in oks:
        c = r.get("credits")
        if c is not None:
            credit_parts.append(f"{_result_account_display(r)} {c}")
    if credit_parts:
        lines.append("当前积分：" + "；".join(credit_parts) + "。")
    lines.append("")

    for r in ordered:
        if r.get("ok") and r.get("suspicious"):
            mark = "⚠️"
        else:
            mark = "✅" if r.get("ok") else "🔴"
        tag = "（登录失效）" if _is_relogin_failure(r) else ""
        if r.get("ok") and r.get("suspicious"):
            tag = "（疑似客户端改版，需确认）"
        lines.append(f"### {mark} {_result_account_display(r)}{tag}")
        lines.append(_result_detail(r))
        ident = history_identity(r.get("platform", ""),
                                 r.get("username") or r.get("display_name") or "")
        st = stats.get(ident)
        if st:
            bits = []
            if st.get("streak"):
                bits.append(f"连续签到 {st['streak']} 天")
            if st.get("month_total"):
                bits.append(f"本月成功率 {st['month_rate']}%"
                            f"（{st['month_success']}/{st['month_total']} 天）")
            elif st.get("rate30") is not None:
                bits.append(f"近30天成功率 {st['rate30']}%")
            if bits:
                lines.append("　" + "，".join(bits))
        lines.append("")

    lines.extend(_relogin_guide(relogin))
    if suspicious:
        lines += ["---", "### ⚠️ 疑似客户端 / 接口改版，请人工确认", "",
                  "以下账号的签到流程虽返回成功，但未抓到积分等成功信号，"
                  "可能是官方更新了签到接口：", ""]
        for r in suspicious:
            lines.append(f"- {_result_account_display(r)}")
        lines += [
            "",
            "1. 打开对应桌面客户端，手动查看今天是否真的已签到、积分是否到账；",
            "2. 若实际未签到，可先在客户端内手动签到；",
            "3. 多个账号同时出现此提示，通常说明客户端已改版，"
            "请更新本签到助手到最新版。", ""]
    if other_fail and not relogin:
        lines += ["---", "### 失败排查建议", "",
                  "1. 检查电脑网络是否正常（公司网络可能拦截接口）；",
                  "2. 打开本工具「打开运行日志」查看具体报错；",
                  "3. 稍后在工具内手动点「立即签到全部」重试。", ""]
    lines.append(f"签到时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    return title, "\n".join(lines)


def build_weekly_report() -> tuple[str, str]:
    """周一周报：近 7 天每账号签到情况 + 连续天数。"""
    now = datetime.now()
    rows = history_recent_rows(7)
    per: dict[str, dict] = {}
    for row in rows:
        for it in row.get("items", []):
            ident = history_identity(it.get("platform", ""),
                                     it.get("username", ""))
            cell = per.setdefault(ident, {
                "label": it.get("platform_label", ""),
                "username": it.get("username", ""), "ok": 0, "fail": 0})
            cell["ok" if it.get("ok") else "fail"] += 1
    stats = _stats_lookup()
    lines = [f"**签到周报（{rows[-1]['date'][5:] if rows else ''} ~ "
             f"{rows[0]['date'][5:] if rows else ''}）**", ""]
    if not per:
        lines.append("近 7 天没有签到记录。请确认定时任务是否正常开启。")
    for ident, c in sorted(per.items(), key=lambda kv: (kv[1]["label"],
                                                        kv[1]["username"])):
        st = stats.get(ident, {})
        lines.append(
            f"- [{c['label']}] {c['username']}：成功 {c['ok']} 天、失败 {c['fail']} 天，"
            f"当前连续 {st.get('streak', 0)} 天")
    lines.append("")
    lines.append(f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}")
    return f"📊 签到周报 {now.strftime('%m-%d')}", "\n".join(lines)


def build_monthly_report() -> tuple[str, str]:
    """月初月报：上月每账号成功率。"""
    now = datetime.now()
    months = history_monthly_stats(2)
    last = months[-1] if len(months) >= 2 else (months[0] if months else None)
    lines = [f"**{last['month']} 签到月报**" if last else "**签到月报**", ""]
    if not last or not last.get("accounts"):
        lines.append("上个月没有签到记录。")
    else:
        for a in last["accounts"]:
            lines.append(
                f"- [{a['platform_label']}] {a['username']}："
                f"签到 {a['success']} 天、失败 {a['fail']} 天，"
                f"成功率 {a['rate']}%")
    lines.append("")
    lines.append(f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}")
    mlabel = last["month"] if last else ""
    return f"📅 {mlabel} 签到月报", "\n".join(lines)
