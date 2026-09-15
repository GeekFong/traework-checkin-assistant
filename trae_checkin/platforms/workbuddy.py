# -*- coding: utf-8 -*-
"""WorkBuddy 平台：Chromium 凭据解密、令牌刷新与签到流程。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from pathlib import Path
import base64
import ctypes
import json
import os
import time
from typing import Any, Optional
from ..constants import PLAT_WORKBUDDY, WB_CHECKIN_CLAIM_URL, WB_CHECKIN_STATUS_URL, WB_REFRESH_TOKEN_URL
from ..crypto import _DATA_BLOB
from ..httpclient import http_post
from ..runtime import log


_wb_ua_cache: Optional[str] = None


WB_AUTH_REL = Path("CodeBuddyExtension") / "Data" / "Public" / "auth" / "workbuddy-desktop.info"


WB_LOCAL_STATE_REL = Path("CodeBuddy CN") / "Local State"


WB_VSCDB_REL = Path("CodeBuddy CN") / "User" / "globalStorage" / "state.vscdb"


WB_SECRET_KEY = ('secret://{"extensionId":"tencent-cloud.coding-copilot",'
                 '"key":"planning-genie.new.accessTokencn"}')


def wb_auth_file_candidates() -> list[Path]:
    """返回 WorkBuddy 明文会话文件候选路径（Windows / macOS / Linux）。"""
    cands: list[Path] = []
    env_file = os.environ.get("WORKBUDDY_AUTH_FILE")
    if env_file:
        cands.append(Path(env_file))
    local = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    home = Path.home()
    if local:
        cands.append(Path(local) / WB_AUTH_REL)
    cands.append(home / "Library" / "Application Support" / WB_AUTH_REL)
    cands.append(home / ".config" / WB_AUTH_REL)
    cands.append(home / ".workbuddy" / "auth" / "workbuddy-desktop.info")
    return cands


def wb_find_auth_file() -> Optional[Path]:
    for p in wb_auth_file_candidates():
        try:
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def wb_load_session_file(path: Path, retries: int = 4) -> dict:
    """读取明文会话；桌面端刷新时可能短暂加锁或写入一半，需重试。"""
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data.get("auth", {}).get("accessToken"):
                raise ValueError("会话文件内容不完整")
            return data
        except (PermissionError, OSError, json.JSONDecodeError, ValueError) as e:
            last_err = e
            time.sleep(0.25 * (i + 1))
    raise FileNotFoundError(f"读取 WorkBuddy 会话失败：{last_err}")


def wb_local_state_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / WB_LOCAL_STATE_REL
    return p if p.exists() else None


def wb_vscdb_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / WB_VSCDB_REL
    return p if p.exists() else None


def wb_get_master_key() -> bytes:
    """从 CodeBuddy CN 的 Local State 解出 Chromium os_crypt 主密钥。"""
    ls_path = wb_local_state_path()
    if not ls_path:
        raise FileNotFoundError("未找到 WorkBuddy 的 Local State")
    with open(ls_path, "r", encoding="utf-8") as f:
        ls = json.load(f)
    enc = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise ValueError("Local State 主密钥格式异常")
    return dpapi_decrypt_blob(enc[5:])


def dpapi_decrypt_blob(raw: bytes) -> bytes:
    """对一段 DPAPI 密文执行 CryptUnprotectData（区别于 base64 版 dpapi_decrypt）。"""
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI 解密失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def wb_decrypt_v10(blob: bytes, key: bytes) -> bytes:
    """解密 Chromium v10/v20：3 字节前缀 + 12 字节 nonce + 密文 + 16 字节 GCM tag。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] not in (b"v10", b"v20"):
        raise ValueError("不是 v10/v20 密文")
    nonce, ct = blob[3:15], blob[15:]
    return AESGCM(key).decrypt(nonce, ct, None)


def wb_extract_blob(v: Any) -> bytes:
    """state.vscdb 中 secret 值可能是 Node Buffer JSON，也可能是裸 v10 字节/字符串。"""
    if isinstance(v, str):
        s = v.strip()
        try:
            o = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            o = None
        if isinstance(o, dict) and o.get("type") == "Buffer":
            return bytes(o.get("data", []))
        return s.encode("latin1", errors="ignore")
    if isinstance(v, (bytes, bytearray)):
        try:
            o = json.loads(bytes(v).decode("utf-8"))
            if isinstance(o, dict) and o.get("type") == "Buffer":
                return bytes(o.get("data", []))
        except Exception:
            pass
        return bytes(v)
    raise ValueError("无法识别的密文包装格式")


def wb_load_session_vscdb() -> dict:
    """DPAPI 降级路径：从 state.vscdb 解密当前会话。拷贝后读取以避免占用。"""
    import sqlite3
    import tempfile
    src = wb_vscdb_path()
    if not src:
        raise FileNotFoundError("未找到 WorkBuddy 的 state.vscdb")
    key = wb_get_master_key()
    tmp_dir = Path(tempfile.gettempdir())
    db_copy = tmp_dir / "wb_state_checkin.vscdb"
    try:
        shutil_copy_file(src, db_copy)
    except Exception:
        db_copy = src  # 拷贝失败则直接只读打开
    con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (WB_SECRET_KEY,)).fetchone()
    finally:
        con.close()
    if not row:
        raise KeyError("state.vscdb 中未找到 WorkBuddy 登录凭证")
    plain = wb_decrypt_v10(wb_extract_blob(row[0]), key)
    data = json.loads(plain.decode("utf-8"))
    if not data.get("auth", {}).get("accessToken"):
        raise ValueError("解密得到的会话不完整")
    return data


def shutil_copy_file(src: Path, dst: Path) -> None:
    import shutil
    shutil.copy2(src, dst)


def load_wb_session() -> dict:
    """优先明文会话文件，失败时降级到 DPAPI 解密 vscdb。"""
    path = wb_find_auth_file()
    if path:
        try:
            return wb_load_session_file(path)
        except Exception as e:
            log.warning(f"明文 WorkBuddy 会话读取失败，尝试 DPAPI 降级：{e}")
    return wb_load_session_vscdb()


def wb_is_logged_in() -> tuple[bool, str]:
    try:
        load_wb_session()
        return True, ""
    except Exception as e:
        return False, str(e)


def wb_session_secret_obj(sess: dict) -> dict:
    """从会话中抽取需要加密快照的最小字段。"""
    auth = sess.get("auth", {})
    acc = sess.get("account", {})
    return {
        "token": auth.get("accessToken", ""),
        "refreshToken": auth.get("refreshToken", ""),
        "expiresAt": auth.get("expiresAt", 0),
        "refreshExpiresAt": auth.get("refreshExpiresAt", 0),
        "uid": acc.get("uid", ""),
        "domain": auth.get("domain", ""),
        "enterpriseId": acc.get("enterpriseId", "") or acc.get("tenantId", ""),
        "tenantId": acc.get("tenantId", "") or acc.get("enterpriseId", ""),
    }


def wb_account_key(sess: dict) -> str:
    uid = sess.get("account", {}).get("uid", "")
    if not uid:
        raise ValueError("会话中缺少账号 uid")
    return f"wb:{uid}"


def wb_account_display(sess: dict) -> str:
    acc = sess.get("account", {})
    return acc.get("nickname") or acc.get("phoneNumber") or acc.get("uin") \
        or acc.get("uid", "WorkBuddy 账号")


def wb_detect_version() -> str:
    """从已安装的 WorkBuddy.exe 文件版本号动态获取版本，读不到时回退默认。"""
    global _wb_ua_cache
    if _wb_ua_cache is not None:
        return _wb_ua_cache
    ver = ""
    # 优先读 Windows 文件版本资源
    try:
        exes: list[Path] = []
        for env_name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if not base:
                continue
            b = Path(base)
            if env_name == "LOCALAPPDATA":
                exes.extend(b.glob("Programs/WorkBuddy*/WorkBuddy.exe"))
                exes.extend(b.glob("WorkBuddy*/WorkBuddy.exe"))
            else:
                exes.extend(b.glob("WorkBuddy*/WorkBuddy.exe"))
        for p in [Path("G:/workb/WorkBuddy/WorkBuddy.exe")] + exes:
            if p.exists():
                size = (ctypes.windll.version.GetFileVersionInfoSizeW(str(p), None)
                        if os.name == "nt" else 0)
                if not size:
                    continue
                buf = ctypes.create_string_buffer(size)
                h = ctypes.windll.kernel32
                if ctypes.windll.version.GetFileVersionInfoW(str(p), 0, size, buf):
                    trans = ctypes.c_void_p()
                    ulen = ctypes.c_uint()
                    if ctypes.windll.version.VerQueryValueW(
                            buf, "\\VarFileInfo\\Translation",
                            ctypes.byref(trans), ctypes.byref(ulen)) and ulen.value:
                        arr = ctypes.cast(trans,
                            ctypes.POINTER(ctypes.c_ushort * 2)).contents
                        sub = (f"\\StringFileInfo\\{arr[0]:04x}{arr[1]:04x}"
                               f"\\ProductVersion")
                        val = ctypes.c_wchar_p()
                        vlen = ctypes.c_uint()
                        if ctypes.windll.version.VerQueryValueW(
                                buf, sub, ctypes.byref(val), ctypes.byref(vlen)) \
                                and val.value:
                            ver = val.value
                            break
    except Exception:
        ver = ""
    # 兜底：resources/app/package.json
    if not ver:
        try:
            for base_env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
                base = os.environ.get(base_env)
                if not base:
                    continue
                b = Path(base)
                cands = list(b.glob("**/WorkBuddy/resources/app/package.json"))
                for pj in cands[:3]:
                    if pj.exists():
                        with open(pj, "r", encoding="utf-8") as f:
                            ver = json.load(f).get("version", "")
                        if ver:
                            break
                if ver:
                    break
        except Exception:
            ver = ""
    if ver:
        # 规范化为三段版本号（如 5.5.6.0 -> 5.5.6）
        parts = ver.strip().split(".")
        ver = ".".join(parts[:3]) if len(parts) >= 3 else ver.strip()
    _wb_ua_cache = ver or "5.5.6"
    return _wb_ua_cache


def wb_user_agent() -> str:
    return f"WorkBuddy/{wb_detect_version()}"


def wb_build_headers(secret: dict) -> dict:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {secret.get('token', '')}",
        "X-User-Id": secret.get("uid", ""),
        "User-Agent": wb_user_agent(),
    }
    if secret.get("domain"):
        h["X-Domain"] = secret["domain"]
    if secret.get("enterpriseId"):
        h["X-Enterprise-Id"] = str(secret["enterpriseId"])
    if secret.get("tenantId"):
        h["X-Tenant-Id"] = str(secret["tenantId"])
    return h


def wb_is_expired(secret: dict, skew: int = 300) -> bool:
    exp = secret.get("expiresAt")
    try:
        if exp:
            # 桌面端存的是毫秒时间戳
            exp_ms = int(exp)
            if exp_ms > 10_000_000_000:
                return time.time() * 1000 > exp_ms - skew * 1000
            return time.time() > exp_ms - skew
    except (TypeError, ValueError):
        pass
    token = secret.get("token", "")
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            data = json.loads(base64.b64decode(payload))
            return time.time() > data.get("exp", 0) - skew
        except Exception:
            return False
    return False


def wb_refresh_token(secret: dict) -> dict:
    """用 refreshToken 换新 accessToken；返回更新后的 secret 对象。"""
    log.info("WorkBuddy 凭证即将过期，正在自动刷新...")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Refresh-Token": secret.get("refreshToken", ""),
        "X-Auth-Refresh-Source": "plugin",
        "User-Agent": wb_user_agent(),
    }
    if secret.get("token"):
        headers["Authorization"] = f"Bearer {secret['token']}"
    if secret.get("domain"):
        headers["X-Domain"] = secret["domain"]
    status, resp = http_post(WB_REFRESH_TOKEN_URL, headers, {})
    if status in (401, 403):
        raise RuntimeError("WorkBuddy 登录态已失效，请重新打开桌面端登录后再次保存该账号。")
    if status != 200:
        raise RuntimeError(f"刷新 WorkBuddy 凭证失败 (HTTP {status})")
    new_auth = resp.get("data") if isinstance(resp.get("data"), dict) else None
    if not new_auth:
        new_auth = resp if isinstance(resp, dict) and resp.get("accessToken") else None
    if not new_auth or not new_auth.get("accessToken"):
        raise RuntimeError("刷新 WorkBuddy 凭证响应异常")
    now_ms = int(time.time() * 1000)
    expires_in = int(new_auth.get("expiresIn", 259200) or 259200)
    refresh_in = int(new_auth.get("refreshExpiresIn", 604799) or 604799)
    updated = dict(secret)
    updated["token"] = new_auth.get("accessToken")
    if new_auth.get("refreshToken"):
        updated["refreshToken"] = new_auth.get("refreshToken")
    updated["expiresAt"] = new_auth.get("expiresAt") or (now_ms + expires_in * 1000)
    updated["refreshExpiresAt"] = (new_auth.get("refreshExpiresAt")
                                   or (now_ms + refresh_in * 1000))
    if new_auth.get("domain"):
        updated["domain"] = new_auth["domain"]
    return updated


def wb_is_already_checked_in(code: Any, msg: str, data: Any) -> bool:
    """兼容三种已签形态：HTTP 400 code=10001、业务 code 1001、data=null。"""
    if code in (10001, 1001):
        return True
    if msg and ("已签到" in msg or "已领取" in msg or "明天再来" in msg):
        return True
    return False


def _wb_recheck(headers: dict) -> Optional[dict]:
    """领取后回查状态：已签返回结果片段，未签/回查失败返回 None。"""
    try:
        rc, rr = http_post(WB_CHECKIN_STATUS_URL, headers, {})
    except Exception:
        return None
    if rc in (401, 403):
        return None
    data = rr.get("data") if isinstance(rr, dict) else None
    if rc == 200 and isinstance(data, dict) and data.get("today_checked_in"):
        msg = "今日已签到（领取响应异常，回查确认已到账）"
        streak = data.get("streak_days")
        if streak:
            msg += f"，连续 {streak} 天"
        return {"ok": True, "already": True, "checked_in": True,
                "enabled": bool(data.get("active", True)),
                "credits": data.get("total_credits"), "message": msg}
    return None


def run_wb_checkin_with_credential(secret: dict, display_name: str = "",
                                   status_only: bool = False,
                                   on_token_refreshed=None) -> dict:
    """用快照凭证执行 WorkBuddy 签到，返回与 TraeWork 统一结构的结果。"""
    result: dict[str, Any] = {
        "ok": False, "message": "", "already": False,
        "platform": PLAT_WORKBUDDY, "platform_label": "WorkBuddy",
        "username": display_name or secret.get("uid") or "WorkBuddy 账号",
    }
    if not secret.get("token"):
        result["message"] = "凭证中缺少 Token，请重新保存该账号。"
        return result

    if wb_is_expired(secret):
        try:
            secret = wb_refresh_token(secret)
            if on_token_refreshed:
                try:
                    on_token_refreshed(secret)
                except Exception:
                    pass
        except Exception as e:
            result["message"] = str(e)
            log.error(result["message"])
            return result

    headers = wb_build_headers(secret)
    try:
        st_code, st_resp = http_post(WB_CHECKIN_STATUS_URL, headers, {})
    except Exception as e:
        result["message"] = f"查询签到状态失败：{e}"
        return result
    if st_code in (401, 403):
        result["message"] = "WorkBuddy 登录态已失效，请重新登录后再次保存。"
        return result

    st_data = st_resp.get("data") if isinstance(st_resp, dict) else None
    st_env_code = st_resp.get("code") if isinstance(st_resp, dict) else None
    if st_code != 200 or not isinstance(st_data, dict):
        # 某些情况下状态接口直接回已签
        msg = (st_resp.get("msg") or st_resp.get("message") or "") if isinstance(st_resp, dict) else ""
        if wb_is_already_checked_in(st_env_code, msg, st_data):
            result.update(ok=True, already=True, checked_in=True,
                          credits=None, message="今日已签到")
            return result
        result["message"] = f"查询签到状态失败 (HTTP {st_code} {st_env_code})"
        return result

    active = st_data.get("active", True)
    checked_in = bool(st_data.get("today_checked_in"))
    credits = st_data.get("total_credits")
    today_credit = st_data.get("today_credit")
    result.update({"enabled": bool(active), "checked_in": checked_in,
                   "credits": credits})

    if status_only:
        result["ok"] = True
        streak_days = st_data.get("streak_days")
        if not active:
            result["message"] = "签到活动暂未开启"
        elif checked_in:
            result["message"] = "今日已签到" + (
                f"，连续 {streak_days} 天" if streak_days else "")
            if today_credit is not None:
                result["message"] += f"，今日 {today_credit} 积分"
            result["already"] = True
        else:
            result["message"] = "今日尚未签到"
        return result

    if not active:
        result["message"] = "签到活动暂未开启或已结束"
        return result

    if checked_in:
        result.update(ok=True, already=True,
                      message="今日已签到" + (
                          f"，连续 {st_data.get('streak_days')} 天"
                          if st_data.get('streak_days') else ""))
        return result

    # 执行签到
    try:
        c_code, c_resp = http_post(WB_CHECKIN_CLAIM_URL, headers, {})
    except Exception as e:
        # 兜底：网络异常可能服务端已到账，回查状态确认真实结果
        recheck = _wb_recheck(headers)
        if recheck is not None:
            result.update(recheck)
            if result.get("ok"):
                log.warning(f"[WorkBuddy/{result['username']}] "
                            f"签到异常但回查已签：{e}")
                return result
        result["message"] = f"签到请求失败：{e}"
        return result
    if c_code in (401, 403):
        result["message"] = "WorkBuddy 登录态已失效，请重新登录后再次保存。"
        return result
    c_data = c_resp.get("data") if isinstance(c_resp, dict) else None
    c_env = c_resp.get("code") if isinstance(c_resp, dict) else None
    c_msg = (c_resp.get("msg") or c_resp.get("message") or "") if isinstance(c_resp, dict) else ""
    if wb_is_already_checked_in(c_env, c_msg, c_data):
        result.update(ok=True, already=True, checked_in=True, message="今日已签到")
        return result
    if c_code != 200 or not isinstance(c_data, dict):
        # 兜底：非预期响应也回查一次，避免漏报已到账
        recheck = _wb_recheck(headers)
        if recheck is not None and recheck.get("ok"):
            log.warning(f"[WorkBuddy/{result['username']}] "
                        f"签到响应异常(HTTP {c_code})但回查已签")
            result.update(recheck)
            return result
        result["message"] = f"签到失败（HTTP {c_code} {c_env} {c_msg}）".strip()
        return result

    gained = c_data.get("credit")
    streak = c_data.get("streak_days")
    result.update(ok=True, checked_in=True, credits=c_data.get("total_credits", credits))
    msg = "签到成功"
    if gained is not None:
        msg += f"，获得 {gained} 积分"
        if streak:
            msg += f"，连续签到 {streak} 天"
    else:
        # 防假成功（t23）：领取 200 但无积分，且无任何已到账/成功确认信号
        confirmed = bool(c_data.get("today_checked_in")
                         or c_data.get("checked_in")
                         or c_data.get("success") is True
                         or c_data.get("total_credits") is not None)
        if not confirmed:
            result["suspicious"] = True
            msg = ("⚠ 签到流程已执行，但未抓到积分/成功信号，"
                   "疑似客户端改版，规则待更新，请人工打开客户端确认")
            log.warning(f"[WorkBuddy/{result['username']}] {msg}；"
                        f"领取响应字段：{sorted(c_data.keys())}")
    result["message"] = msg
    log.info(f"[WorkBuddy/{result['username']}] {msg}")
    return result


def run_wb_checkin_live(status_only: bool = False) -> dict:
    """读取当前桌面端登录的 WorkBuddy 账号并签到（单账号即时流程）。"""
    try:
        sess = load_wb_session()
    except Exception as e:
        return {"ok": False, "message": str(e), "already": False,
                "platform": PLAT_WORKBUDDY, "platform_label": "WorkBuddy",
                "username": "WorkBuddy 账号"}
    secret = wb_session_secret_obj(sess)
    return run_wb_checkin_with_credential(
        secret, display_name=wb_account_display(sess), status_only=status_only)
