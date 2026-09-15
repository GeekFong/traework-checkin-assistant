# -*- coding: utf-8 -*-
"""健康自检：计划任务、客户端、网络、数据文件。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import json
import os
import socket
import time
from typing import Any, Optional
from .accounts import ACCOUNTS_FILE, list_accounts
from .constants import PLATFORM_LABELS, PLAT_TRAEWORK, PLAT_WORKBUDDY, TASK_NAME
from .history import HISTORY_FILE, load_history
from .launcher import find_platform_exes
from .runtime import _run
from .scheduler import task_exists
from .settings import SETTINGS_FILE


HEALTH_OK = "ok"


HEALTH_WARN = "warn"


HEALTH_ERROR = "error"


def _health_timeout() -> float:
    """网络探测超时（秒），默认 4 秒；TRAESIGN_HEALTH_TIMEOUT 可压缩以便测试。"""
    try:
        return max(0.05, float(os.environ.get("TRAESIGN_HEALTH_TIMEOUT", "4")))
    except (TypeError, ValueError):
        return 4.0


def _tcp_reachable(host: str, port: int = 443) -> tuple[bool, str]:
    """TCP 连通性探测，返回 (是否连通, 耗时或错误说明)。"""
    t0 = time.time()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_health_timeout())
    try:
        sock.connect((host, port))
        return True, f"{(time.time() - t0) * 1000:.0f} ms"
    except Exception as e:
        return False, str(e) or e.__class__.__name__
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _scheduled_task_info() -> Optional[dict]:
    """读取计划任务上次/下次运行信息；任务不存在或读取失败返回 None。"""
    ps = (
        "$t = Get-ScheduledTask -TaskName '" + TASK_NAME + "' -ErrorAction SilentlyContinue; "
        "if ($t) { "
        "$i = Get-ScheduledTaskInfo -TaskName '" + TASK_NAME + "' -ErrorAction SilentlyContinue; "
        "$o = [ordered]@{ State = [string]$t.State; "
        "LastRunTime = if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' }; "
        "LastTaskResult = if ($null -ne $i.LastTaskResult) { [int]$i.LastTaskResult } else { -1 }; "
        "NextRunTime = if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' } }; "
        "$o | ConvertTo-Json -Compress }"
    )
    try:
        r = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                  "-Command", ps], timeout=20)
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        data = json.loads(r.stdout.strip())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def run_health_checks() -> list[dict]:
    """执行全套健康自检，返回结构化结果列表。

    每项：{key, title, status(ok/warn/error), detail, hint}
    """
    results: list[dict] = []

    # 1) 每日自动签到计划任务
    try:
        if task_exists():
            info = _scheduled_task_info()
            last_result = None if not info else info.get("LastTaskResult")
            # 0/267009=任务尚未运行；267011=任务尚未到下次运行时间（均非错误）
            benign = (None, 0, 267009, 267011)
            last_run = (info or {}).get("LastRunTime") or ""
            nxt = (info or {}).get("NextRunTime") or ""
            state_txt = (info or {}).get("State") or ""
            detail_parts = ["计划任务已存在"]
            if state_txt:
                detail_parts.append(f"状态：{state_txt}")
            if last_run:
                detail_parts.append(f"上次运行：{last_run}")
            if last_result not in (None, -1):
                detail_parts.append(f"结果码：{last_result}")
            if nxt:
                detail_parts.append(f"下次运行：{nxt}")
            if last_result in benign:
                results.append({"key": "task", "title": "每日自动签到任务",
                                "status": HEALTH_OK,
                                "detail": "；".join(detail_parts), "hint": ""})
            else:
                results.append({"key": "task", "title": "每日自动签到任务",
                                "status": HEALTH_WARN,
                                "detail": "；".join(detail_parts),
                                "hint": ("计划任务上次执行返回了异常结果码，"
                                         "可手动签到一次或查看运行日志排查；"
                                         "267014 通常表示错过执行时间（开机后会自动补跑）。")})
        else:
            results.append({"key": "task", "title": "每日自动签到任务",
                            "status": HEALTH_WARN,
                            "detail": "未开启自动签到",
                            "hint": "在「每日自动签到」卡片设置时间并点「开启」后，"
                                    "将每天自动后台签到，无需手动操作。"})
    except Exception as e:
        results.append({"key": "task", "title": "每日自动签到任务",
                        "status": HEALTH_ERROR,
                        "detail": f"检查失败：{e}",
                        "hint": "可能是系统计划任务服务（Task Scheduler）异常，请重启电脑后重试。"})

    # 2) 两个平台的桌面客户端
    for plat, host in ((PLAT_TRAEWORK, "api.trae.cn"),
                       (PLAT_WORKBUDDY, "copilot.tencent.com")):
        label = PLATFORM_LABELS.get(plat, plat)
        try:
            exes = find_platform_exes(plat)
            if exes:
                results.append({
                    "key": f"client_{plat}", "title": f"{label} 桌面客户端",
                    "status": HEALTH_OK,
                    "detail": f"已安装：{exes[0]}", "hint": ""})
            else:
                accounts = [a for a in list_accounts() if a.get("platform") == plat]
                if accounts:
                    results.append({
                        "key": f"client_{plat}", "title": f"{label} 桌面客户端",
                        "status": HEALTH_ERROR,
                        "detail": f"未检测到客户端，但保存了 {len(accounts)} 个该平台账号",
                        "hint": (f"请重新安装 {label} 桌面端并重新登录，"
                                 "否则刷新凭据和启动引导功能无法使用。")})
                else:
                    results.append({
                        "key": f"client_{plat}", "title": f"{label} 桌面客户端",
                        "status": HEALTH_WARN,
                        "detail": "未检测到客户端（也未保存该平台账号）",
                        "hint": f"如不使用 {label}，可忽略此项。"})
        except Exception as e:
            results.append({"key": f"client_{plat}", "title": f"{label} 桌面客户端",
                            "status": HEALTH_ERROR,
                            "detail": f"检查失败：{e}", "hint": ""})

    # 3) 网络连通性（官方 API 域名 443）
    for host, plat in (("api.trae.cn", PLAT_TRAEWORK),
                       ("copilot.tencent.com", PLAT_WORKBUDDY)):
        label = PLATFORM_LABELS.get(plat, plat)
        try:
            ok, info_txt = _tcp_reachable(host, 443)
            if ok:
                results.append({"key": f"net_{plat}",
                                "title": f"{label} 服务器连通性",
                                "status": HEALTH_OK,
                                "detail": f"{host}:443 可达（{info_txt}）",
                                "hint": ""})
            else:
                results.append({"key": f"net_{plat}",
                                "title": f"{label} 服务器连通性",
                                "status": HEALTH_ERROR,
                                "detail": f"{host}:443 不可达（{info_txt}）",
                                "hint": "请检查网络连接、代理或防火墙设置；"
                                        "公司网络可能拦截了该域名。"})
        except Exception as e:
            results.append({"key": f"net_{plat}",
                            "title": f"{label} 服务器连通性",
                            "status": HEALTH_ERROR,
                            "detail": f"检查失败：{e}", "hint": ""})

    # 4) 本地数据文件完整性
    #    全新用户文件尚不存在属正常（首次保存时自动创建），只报告：
    #    JSON 结构损坏 / 无法读取 / 历史文件结构异常。
    accs = list_accounts()
    data_issues: list[str] = []
    for name, path in (("账号数据", ACCOUNTS_FILE),
                       ("设置", SETTINGS_FILE),
                       ("签到历史", HISTORY_FILE)):
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if not isinstance(parsed, dict):
                data_issues.append(f"{name}文件结构异常（顶层不是对象）")
            elif name == "签到历史" and "days" in parsed \
                    and not isinstance(parsed["days"], dict):
                data_issues.append("签到历史 days 结构异常")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            data_issues.append(f"{name}文件已损坏：{type(e).__name__}")
        except OSError as e:
            data_issues.append(f"{name}无法读取：{e}")
    today_key = datetime.now().date().strftime("%Y-%m-%d")
    today_bucket = (load_history().get("days") or {}).get(today_key) or {}
    today_ok_n = sum(1 for v in today_bucket.values()
                     if isinstance(v, dict) and v.get("success"))
    today_total = len(today_bucket)
    if data_issues:
        results.append({"key": "data", "title": "本地数据文件",
                        "status": HEALTH_WARN,
                        "detail": "；".join(data_issues),
                        "hint": "可用「备份 / 恢复」导出数据后联系排查；"
                                "损坏文件不会影响账号凭据本身。"})
    else:
        detail = f"共 {len(accs)} 个账号，数据文件正常"
        if today_total:
            detail += f"；今日已签到 {today_ok_n}/{today_total}"
        results.append({"key": "data", "title": "本地数据文件",
                        "status": HEALTH_OK,
                        "detail": detail,
                        "hint": ("今天还有账号未签到，点「全部账号签到」即可。"
                                 if today_total and today_ok_n < today_total else "")})

    return results
