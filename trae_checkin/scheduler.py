# -*- coding: utf-8 -*-
"""Windows 计划任务的创建 / 查询 / 删除。

由单文件版机械拆分而来，函数实现保持不变；源码态入口改为
``python -m trae_checkin``，并显式切到仓库根作为工作目录。
"""

from __future__ import annotations
from typing import Any, Optional
from .constants import TASK_NAME
from .runtime import _run, entry_args, is_frozen, log, project_root


def task_exists() -> bool:
    try:
        return _run(["schtasks", "/Query", "/TN", TASK_NAME], timeout=15).returncode == 0
    except Exception:
        return False


def _ps_quote(s: str) -> str:
    """转义 PowerShell 单引号字符串中的单引号。"""
    return str(s).replace("'", "''")


def _action_parts() -> tuple[str, str, str]:
    """返回计划任务动作的 (execute, arguments, working_directory)。"""
    prefix = entry_args()
    execute = str(prefix[0])
    arguments = " ".join(list(prefix[1:]) + ["--silent"])
    if is_frozen():
        workdir = str(prefix[0].parent)
    else:
        workdir = str(project_root())
    return execute, arguments, workdir


def _task_command() -> str:
    """schtasks /TR 用的命令字符串。

    schtasks 无法单独设置工作目录，源码态用 cmd /c 先切到仓库根再运行。
    """
    execute, arguments, workdir = _action_parts()
    if is_frozen():
        return f'"{execute}" --silent'
    # 路径/参数中的双引号需在 cmd 层保留
    return (f'cmd /c cd /d "{workdir}" && "{execute}" {arguments}')


def create_scheduled_task(run_time: str = "09:00") -> tuple[bool, str]:
    try:
        h, m = run_time.split(":")
        int(h); int(m)
    except Exception:
        return False, f"时间格式无效：{run_time}（应为 HH:MM）"

    if task_exists():
        delete_scheduled_task()

    cmd = ["schtasks", "/Create", "/TN", TASK_NAME,
           "/TR", _task_command(), "/SC", "DAILY", "/ST", run_time,
           "/F", "/RL", "LIMITED"]
    r = _run(cmd, timeout=30)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "拒绝访问" in err or "denied" in err.lower():
            return False, "权限不足，请以管理员身份运行本程序后重试。"
        return False, f"创建失败：{err or '未知错误'}"

    # 高级设置：
    #   1) StartWhenAvailable：错过的每日签到在开机后自动补跑；
    #   2) 额外增加「用户登录后延迟 3 分钟」触发器，作为关机一整天的兜底补签；
    #   3) 失败重试 3 次（间隔 10 分钟），电池供电也允许运行。
    # 静默运行内部会先判断今日是否已全部签到，已签则立即退出、不重复推送，
    # 因此登录触发器不会造成打扰。
    execute, arguments, workdir = _action_parts()
    ps = (
        "$t = Get-ScheduledTask -TaskName '" + TASK_NAME + "' -ErrorAction SilentlyContinue; "
        "if ($t) { "
        "$daily = New-ScheduledTaskTrigger -Daily -At '" + run_time + "'; "
        "$logon = New-ScheduledTaskTrigger -AtLogOn; "
        "$logon.Delay = 'PT3M'; "
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 30) "
        "-MultipleInstances IgnoreNew; "
        "$a = New-ScheduledTaskAction -Execute '" + _ps_quote(execute) + "' "
        "-Argument '" + _ps_quote(arguments) + "' "
        "-WorkingDirectory '" + _ps_quote(workdir) + "'; "
        "Set-ScheduledTask -TaskName '" + TASK_NAME + "' -Action $a "
        "-Trigger @($daily, $logon) -Settings $s | Out-Null }"
    )
    try:
        r2 = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-Command", ps], timeout=25)
        if r2.returncode != 0:
            log.warning(f"登录触发器设置失败（每日定时仍有效）："
                        f"{(r2.stderr or '').strip()[:200]}")
    except Exception as e:
        log.warning(f"登录触发器设置异常（每日定时仍有效）：{e}")
    return True, f"已开启每日 {run_time} 自动签到（开机/登录后自动补签）"


def delete_scheduled_task() -> bool:
    try:
        return _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                    timeout=15).returncode == 0
    except Exception:
        return False
