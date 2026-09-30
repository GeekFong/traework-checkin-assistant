# -*- coding: utf-8 -*-
"""登录状态检测与自动恢复。

当检测到桌面客户端的登录状态失效时，自动结束客户端进程并重新启动，
等待客户端完成静默重新登录后再刷新凭证快照，无需人工干预。

仅依赖标准库（ctypes 调用 Windows API），不引入第三方包。
"""

from __future__ import annotations
import ctypes
import time
from ctypes import wintypes
from typing import Callable, Optional

from .constants import EXE_NAMES, PLATFORM_LABELS, PLAT_TRAEWORK, PLAT_WORKBUDDY
from .launcher import find_platform_exes, launch_platform_app
from .runtime import log


# ── Windows API 声明 ──
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
WM_CLOSE = 0x0010
STILL_ACTIVE = 259

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(_ProcessEntry32W)]
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(_ProcessEntry32W)]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                  wintypes.DWORD]
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(wintypes.DWORD)]
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]


INVALID_HANDLE = wintypes.HANDLE(-1).value


def _snapshot_pids() -> set[int]:
    """当前所有进程 PID 集合。"""
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE:
        return set()
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        pids: set[int] = set()
        if not _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return pids
        pids.add(int(entry.th32ProcessID))
        while _kernel32.Process32NextW(snap, ctypes.byref(entry)):
            pids.add(int(entry.th32ProcessID))
        return pids
    finally:
        _kernel32.CloseHandle(snap)


def _snapshot_names() -> list[tuple[int, str]]:
    """当前所有 (PID, 进程名小写) 列表。"""
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE:
        return []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        out: list[tuple[int, str]] = []
        if not _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return out
        out.append((int(entry.th32ProcessID),
                    (entry.szExeFile or "").lower()))
        while _kernel32.Process32NextW(snap, ctypes.byref(entry)):
            out.append((int(entry.th32ProcessID),
                        (entry.szExeFile or "").lower()))
        return out
    finally:
        _kernel32.CloseHandle(snap)


def _pid_alive(pid: int) -> bool:
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        if not _kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(h)


def _terminate_pid(pid: int) -> bool:
    h = _kernel32.OpenProcess(PROCESS_TERMINATE
                              | PROCESS_QUERY_LIMITED_INFORMATION,
                              False, pid)
    if not h:
        return False
    try:
        return bool(_kernel32.TerminateProcess(h, 0))
    finally:
        _kernel32.CloseHandle(h)


def client_process_names(platform: str) -> list[str]:
    """该平台桌面端可能的进程名（小写）。

    优先取安装探测到的 exe 文件名，再补齐已知名称与 Electron 子进程名，
    以便一并结束主进程与渲染/GPU 等辅助进程。
    """
    names: list[str] = []
    for exe in find_platform_exes(platform):
        nm = exe.name.lower()
        if nm not in names:
            names.append(nm)
    if platform == PLAT_WORKBUDDY:
        for nm in ("workbuddy.exe",):
            if nm not in names:
                names.append(nm)
    else:
        for nm in EXE_NAMES:
            low = nm.lower()
            if low not in names:
                names.append(low)
    return names


def client_pids(platform: str) -> list[int]:
    """正在运行的桌面端进程 PID（含 Electron 辅助进程）。"""
    wanted = set(client_process_names(platform))
    if not wanted:
        return []
    return [pid for pid, nm in _snapshot_names() if nm in wanted]


def _post_close_to_windows(pids: set[int]) -> int:
    """向这些进程的可见顶层窗口发送正常关闭请求，返回送达窗口数。"""
    sent = {"n": 0}

    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and _user32.IsWindowVisible(hwnd):
            if _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                sent["n"] += 1
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(_cb), 0)
    except Exception as e:
        log.warning(f"发送窗口关闭请求失败：{e}")
    return sent["n"]


def stop_client(platform: str, *, grace_seconds: int = 15,
                on_progress: Optional[Callable[[str], None]] = None) -> dict:
    """结束该平台桌面端进程：先优雅关闭，超时未退出的再强制结束。

    返回 {closed, graceful, forced, leftover}。
    """
    label = PLATFORM_LABELS.get(platform, "客户端")
    pids = client_pids(platform)
    info = {"closed": len(pids), "graceful": 0, "forced": 0, "leftover": 0}
    if not pids:
        return info

    if on_progress:
        on_progress(f"正在退出 {label}（共 {len(pids)} 个进程）…")
    n_win = _post_close_to_windows(set(pids))
    if n_win == 0:
        # 没有可见窗口（后台驻留/托盘），直接进入强制结束，避免白等
        log.info(f"{label} 无可见窗口，直接进入强制结束")
    else:
        log.info(f"已向 {label} 的 {n_win} 个窗口发送关闭请求")

    deadline = time.time() + max(0, grace_seconds)
    while time.time() < deadline:
        if not any(_pid_alive(p) for p in pids):
            break
        time.sleep(1)

    leftover = [p for p in pids if _pid_alive(p)]
    info["graceful"] = len(pids) - len(leftover)
    if leftover:
        log.info(f"{label} 有 {len(leftover)} 个进程未响应关闭请求，强制结束")
        for p in leftover:
            _terminate_pid(p)
        info["forced"] = len(leftover)
        time.sleep(2)
        info["leftover"] = sum(1 for p in leftover if _pid_alive(p))
    return info


def restart_client(platform: str, *, grace_seconds: int = 15,
                   on_progress: Optional[Callable[[str], None]] = None) -> dict:
    """退出平台客户端后立即重新启动它。"""
    label = PLATFORM_LABELS.get(platform, "客户端")
    stop_info = stop_client(platform, grace_seconds=grace_seconds,
                            on_progress=on_progress)
    if on_progress:
        on_progress(f"正在重新启动 {label}…")
    ok, detail = launch_platform_app(platform)
    if ok:
        log.info(f"{label} 已重新启动：{detail}")
    else:
        log.error(f"{label} 重新启动失败：{detail}")
    return {"stopped": stop_info, "relaunched": ok, "launch_detail": detail}


def login_state(platform: str) -> tuple[bool, str]:
    """本地登录凭证状态（文件层面）。返回 (是否有效, 原因)。"""
    if platform == PLAT_WORKBUDDY:
        from .platforms.workbuddy import wb_is_logged_in
        return wb_is_logged_in()
    from .platforms.traework import is_logged_in
    return is_logged_in()


def live_login_probe(platform: str) -> tuple[Optional[bool], str]:
    """向平台接口做一次仅状态查询，确认登录态是否真的被服务端接受。

    返回 (结论, 说明)：True 有效、False 确认失效、None 无法判定
    （网络异常等，调用方不应据此触发重启客户端）。
    """
    try:
        if platform == PLAT_WORKBUDDY:
            from .platforms.workbuddy import run_wb_checkin_live
            r = run_wb_checkin_live(status_only=True)
        else:
            from .platforms.traework import run_checkin
            r = run_checkin(status_only=True)
    except Exception as e:
        return None, f"探测异常：{e}"
    if r.get("ok"):
        return True, r.get("message", "")
    msg = r.get("message", "") or "未知错误"
    net_hints = ("网络", "timeout", "timed out", "connection",
                 "getaddrinfo", "ssl", "proxy", "无法连接", "dns")
    if any(h in msg.lower() or h in msg for h in net_hints):
        return None, f"网络异常，暂不判定：{msg}"
    return False, msg


def check_login_health(platform: str, *, probe_live: bool = True) -> dict:
    """综合判断某平台登录健康度。

    返回 {"platform","label","valid","need_relogin","reason"}：
      - valid：本地凭证是否可用；
      - need_relogin：是否需要重启客户端重新登录（本地缺失或服务端确认失效）。
    """
    logged, reason = login_state(platform)
    out = {"platform": platform,
           "label": PLATFORM_LABELS.get(platform, platform),
           "valid": logged, "need_relogin": not logged,
           "reason": reason or ("登录态正常" if logged else "本地无有效登录凭证")}
    if not logged or not probe_live:
        return out
    verdict, detail = live_login_probe(platform)
    if verdict is False:
        out["need_relogin"] = True
        out["reason"] = f"服务端拒绝当前登录态：{detail}"
    elif verdict is True:
        out["reason"] = detail or out["reason"]
    else:
        out["reason"] = detail
    return out


def recover_login(platform: str, *, on_progress: Optional[Callable[[str], None]] = None,
                  close_grace: int = 15, login_timeout: int = 150,
                  probe_live: bool = True) -> dict:
    """登录失效自动恢复：重启客户端 → 等待重新登录 → 刷新凭证快照。

    返回 {"ok","platform","label","stage","message"}。
    """
    label = PLATFORM_LABELS.get(platform, platform)
    result = {"ok": False, "platform": platform, "label": label,
              "stage": "restart", "message": ""}

    health = check_login_health(platform, probe_live=probe_live)
    if not health["need_relogin"]:
        result["ok"] = True
        result["stage"] = "healthy"
        result["message"] = f"{label} 登录态正常，无需处理"
        return result

    if not find_platform_exes(platform):
        result["stage"] = "not_installed"
        result["message"] = (f"未能找到 {label} 客户端的安装位置，无法自动重启；"
                             "请安装并登录客户端后重试。")
        log.warning(result["message"])
        return result

    log.info(f"{label} 登录状态失效（{health['reason']}），开始自动重启客户端恢复")
    if on_progress:
        on_progress(f"检测到 {label} 登录状态失效：{health['reason']}\n正在退出客户端并重新启动…")

    restart = restart_client(platform, grace_seconds=close_grace,
                             on_progress=on_progress)
    result["stage"] = "wait_login"
    if not restart["relaunched"]:
        result["message"] = (f"{label} 已退出，但无法自动重新启动："
                             f"{restart['launch_detail']}")
        return result

    # 等待客户端进程起来
    deadline = time.time() + 20
    while time.time() < deadline and not client_pids(platform):
        time.sleep(1)

    if on_progress:
        on_progress(f"{label} 已重新启动，正在等待登录恢复（最长 {login_timeout} 秒）…")
    logged = False
    reason = ""
    waited = 0.0
    interval = 3
    since_probe = 0.0
    while waited < login_timeout:
        ok_local, reason = login_state(platform)
        if ok_local:
            if not probe_live:
                logged = True
                reason = reason or "登录态已恢复"
                break
            # 本地凭证就绪后再向服务端确认，并节流到约 15 秒一次，避免高频请求
            if since_probe >= 15:
                verdict, detail = live_login_probe(platform)
                since_probe = 0.0
                if verdict is not False:
                    logged = True
                    reason = detail or "登录态已恢复"
                    break
                reason = detail or reason
        time.sleep(interval)
        waited += interval
        since_probe += interval

    if not logged:
        result["stage"] = "needs_manual_login"
        tail = f"（{reason}）" if reason else ""
        result["message"] = (f"{label} 已重启，但 {login_timeout} 秒内未恢复登录态"
                             f"{tail}，需要你手动完成一次登录。")
        log.warning(result["message"])
        return result

    # 登录恢复：刷新凭证快照，让定时签到立刻用上新凭证
    from .accounts import save_current_account
    saved, save_msg = save_current_account(platform)
    result["stage"] = "recovered"
    result["ok"] = True
    result["message"] = (f"{label} 登录态已自动恢复"
                         + (f"，凭证快照已更新（{save_msg}）" if saved
                            else f"，凭证快照更新失败：{save_msg}"))
    log.info(result["message"])
    return result
