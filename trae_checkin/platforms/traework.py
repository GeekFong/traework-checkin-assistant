# -*- coding: utf-8 -*-
"""TraeWork 平台：本地会话读取、令牌刷新与签到流程。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import base64
from datetime import datetime, timezone, timedelta
import json
import platform
import re
import time
from typing import Any, Optional
from ..constants import CHECKIN_CLAIM_URL, CHECKIN_STATUS_URL, PLAT_TRAEWORK, REFRESH_TOKEN_URL, REQ_SOURCE
from ..crypto import decrypt_to_json
from ..httpclient import http_post
from ..launcher import get_storage_path, get_tiny_storage_path
from ..runtime import APP_VERSION, log


def load_auth_info() -> dict:
    sp = get_storage_path()
    if not sp:
        raise FileNotFoundError("未找到 TraeWork CN 的应用数据，请先安装并至少登录一次。")
    with open(sp, "r", encoding="utf-8") as f:
        storage = json.load(f)
    encrypted = storage.get("iCubeAuthInfo://icube.cloudide")
    if not encrypted:
        raise KeyError("未找到登录凭证，请先登录 TraeWork CN。")
    info = decrypt_to_json(encrypted)
    if not info.get("token"):
        raise ValueError("登录凭证中缺少 Token，请重新登录。")
    return info


def load_device_id() -> str:
    tp = get_tiny_storage_path()
    if not tp or not tp.exists():
        raise FileNotFoundError("找不到设备标识文件，请先打开一次 TraeWork CN。")
    with open(tp, "r", encoding="utf-8") as f:
        tiny = json.load(f)
    data = tiny.get("tiny_storage_data", tiny)
    if isinstance(data, str):
        data = json.loads(data)
    encrypted_did = data.get("aha.device.device_id")
    if not encrypted_did:
        raise KeyError("未找到设备 ID，请先打开一次 TraeWork CN。")
    info = decrypt_to_json(encrypted_did)
    did = info.get("device_id_str")
    if not did:
        raise ValueError("设备 ID 为空。")
    return did


def is_logged_in() -> tuple[bool, str]:
    """快速判断登录状态，返回 (是否登录, 原因说明)。"""
    try:
        load_auth_info()
        load_device_id()
        return True, ""
    except FileNotFoundError as e:
        return False, str(e)
    except KeyError as e:
        return False, str(e)
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"读取登录信息失败：{e}"


def build_headers(token: str, device_id: str, region: str = "") -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "x-device-id": device_id,
        "x-device-brand": "Windows",
        "x-device-type": "windows",
        "x-os-version": platform.version(),
        "x-app-version": APP_VERSION,
        "X-User-Region": region or "CN",
    }


def refresh_token(refresh_token_str: str) -> dict:
    log.info("登录凭证已过期，正在自动刷新...")
    body = {"refresh_token": refresh_token_str, "grant_type": "refresh_token"}
    status, resp = http_post(REFRESH_TOKEN_URL,
                             {"Content-Type": "application/json"}, body)
    if status != 200:
        raise RuntimeError("刷新凭证失败，请重新打开 TraeWork CN 登录。")
    data_obj = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    new_token = (resp.get("access_token") or resp.get("token")
                 or data_obj.get("access_token") or data_obj.get("token"))
    new_refresh = (resp.get("refresh_token") or resp.get("refreshToken")
                   or data_obj.get("refresh_token")
                   or data_obj.get("refreshToken"))
    if not new_token:
        raise RuntimeError("刷新凭证响应异常")
    out: dict[str, Any] = {"token": new_token,
                          "refreshToken": new_refresh or refresh_token_str}
    expired_at = (resp.get("expiredAt") or resp.get("expires_at")
                  or data_obj.get("expiredAt") or data_obj.get("expires_at"))
    if expired_at:
        out["expiredAt"] = expired_at
    ttl = (resp.get("expires_in") or resp.get("expiresIn")
           or data_obj.get("expires_in") or data_obj.get("expiresIn"))
    if ttl:
        out["expires_in"] = ttl
    return out


def is_token_expired(auth_info: dict) -> bool:
    expired_at = auth_info.get("expiredAt")
    if expired_at:
        try:
            dt = datetime.fromisoformat(expired_at.replace("Z", "+00:00"))
            return datetime.now(timezone.utc).timestamp() > dt.timestamp() - 300
        except Exception:
            pass
    token = auth_info.get("token", "")
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            data = json.loads(base64.b64decode(payload))
            return time.time() > data.get("exp", 0) - 300
        except Exception:
            return False
    return False


def _http_status_hint(status: int, action: str) -> str:
    """业务接口收到非 200 状态码时的中文说明。"""
    if status == 429:
        return f"{action}失败：请求过于频繁被限流，请稍后再试"
    if status in (401, 403):
        return f"{action}失败：登录已失效或无权限（HTTP {status}），请重新登录该账号后再保存一次"
    if status >= 500:
        return f"{action}失败：对方服务器暂时异常（HTTP {status}），请稍后再试"
    if status == 404:
        return f"{action}失败：接口地址不存在（HTTP 404），可能是客户端升级后接口变更"
    return f"{action}失败（HTTP {status}）"


def check_status(token: str, device_id: str, region: str = "") -> dict:
    status, resp = http_post(CHECKIN_STATUS_URL,
                             build_headers(token, device_id, region),
                             {"req_source": REQ_SOURCE})
    if status != 200:
        raise RuntimeError(_http_status_hint(status, "查询签到状态"))
    code = resp.get("code")
    if isinstance(code, (int, float)) and code != 0:
        raise RuntimeError(f"查询返回错误码 {code}：{resp.get('message', '')}")
    return resp


def claim_checkin(token: str, device_id: str, region: str = "") -> dict:
    status, resp = http_post(CHECKIN_CLAIM_URL,
                             build_headers(token, device_id, region),
                             {"req_source": REQ_SOURCE})
    if status != 200:
        raise RuntimeError(_http_status_hint(status, "领取积分"))
    code = resp.get("code")
    if isinstance(code, (int, float)) and code != 0:
        raise RuntimeError(f"领取失败（{code}）：{resp.get('message', '')}")
    return resp


def run_checkin_with_credential(auth_info: dict, device_id: str,
                                username: str = "",
                                status_only: bool = False,
                                on_token_refreshed=None) -> dict:
    """
    用显式传入的凭证执行签到（当前客户端账号与已保存快照共用此核心逻辑）。

    auth_info 至少含 token / refreshToken / expiredAt；
    on_token_refreshed(token, refreshToken, expiredAt) 在 token 自动续期后回调，
    供批量签到回写快照。
    返回结构化结果字典：
      ok, message, already, credits, extra_credits, username, checked_in, enabled
    """
    result: dict[str, Any] = {"ok": False, "message": "", "already": False}
    result["platform"] = PLAT_TRAEWORK
    result["platform_label"] = "TraeWork"
    result["username"] = username or auth_info.get("account", {}) \
        .get("username", "未知用户")
    region = auth_info.get("userRegion", {}).get("region", "") \
        or auth_info.get("region", "") or "CN"

    token = auth_info.get("token", "")
    if not token:
        result["message"] = "凭证中缺少 Token，请重新保存该账号。"
        log.error(result["message"])
        return result

    if is_token_expired(auth_info):
        try:
            refreshed = refresh_token(auth_info.get("refreshToken", ""))
            token = refreshed["token"]
            new_rt = refreshed.get("refreshToken", auth_info.get("refreshToken", ""))
            # 优先采用响应里的真实过期时间，没有再回退为 +1 小时
            new_exp = refreshed.get("expiredAt") or refreshed.get("expires_at", "")
            if not new_exp:
                ttl = refreshed.get("expires_in") or refreshed.get("expiresIn")
                try:
                    ttl_sec = int(ttl) if ttl else 3600
                except (TypeError, ValueError):
                    ttl_sec = 3600
                new_exp = datetime.fromtimestamp(
                    time.time() + ttl_sec, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            auth_info["token"] = token
            auth_info["refreshToken"] = new_rt
            auth_info["expiredAt"] = new_exp
            if on_token_refreshed:
                try:
                    on_token_refreshed(token, new_rt, new_exp)
                except Exception:
                    pass
        except Exception as e:
            result["message"] = str(e)
            log.error(result["message"])
            return result

    try:
        st = check_status(token, device_id, region)
    except Exception as e:
        result["message"] = f"查询签到状态失败：{e}"
        log.error(result["message"])
        return result

    enabled = st.get("enable", False)
    checked_in = st.get("checked_in", False)
    credits = st.get("credits")
    extra = st.get("extra_credits")
    result.update({"enabled": enabled, "checked_in": checked_in,
                   "credits": credits, "extra_credits": extra})

    if status_only:
        result["ok"] = True
        result["message"] = "已签到" if checked_in else "今日尚未签到"
        return result

    if checked_in:
        result["ok"] = True
        result["already"] = True
        result["message"] = "今日已签到"
        return result

    if not enabled:
        result["message"] = "签到功能未启用"
        return result

    try:
        claim = claim_checkin(token, device_id, region)
        result["ok"] = True
        gained = claim.get("credits", credits)
        g_extra = claim.get("extra_credits", extra)
        result["credits"] = gained
        result["extra_credits"] = g_extra
        msg = "签到成功"
        if gained is not None:
            msg += f"，获得 {gained} 积分"
            if g_extra:
                msg += f"（含额外奖励 {g_extra}）"
        else:
            # 防假成功（t23）：领取接口 200 但既无积分也无任何成功信号，
            # 极可能是官方接口/客户端改版导致旧解析规则失效
            claim_confirmed = bool(claim.get("checked_in")
                                   or claim.get("success") is True
                                   or claim.get("claimed") is True)
            if not claim_confirmed:
                result["suspicious"] = True
                msg = ("⚠ 签到流程已执行，但未抓到积分/成功信号，"
                       "疑似客户端改版，规则待更新，请人工打开客户端确认")
                log.warning(f"[{result['username']}] {msg}；"
                            f"领取响应字段：{sorted(claim.keys())}")
        result["message"] = msg
        log.info(f"[{result['username']}] {msg}")
        return result
    except Exception as e:
        # 兜底：领取请求可能已在服务端生效（超时等），回查状态确认真实结果
        try:
            recheck = check_status(token, device_id, region)
            if recheck.get("checked_in"):
                result["ok"] = True
                result["already"] = True
                result["checked_in"] = True
                result["credits"] = recheck.get("credits")
                result["extra_credits"] = recheck.get("extra_credits")
                result["message"] = "今日已签到（领取响应异常，回查确认已到账）"
                log.warning(f"[{result['username']}] 领取异常但回查已签：{e}")
                return result
        except Exception as re:
            log.error(f"[{result['username']}] 领取后回查也失败：{re}")
        result["message"] = f"领取积分失败：{e}"
        log.error(result["message"])
        return result


def run_checkin(status_only: bool = False) -> dict:
    """读取当前客户端登录凭证并签到（单账号传统流程）。"""
    result: dict[str, Any] = {"ok": False, "message": "", "already": False}
    try:
        auth_info = load_auth_info()
        device_id = load_device_id()
    except Exception as e:
        result["message"] = str(e)
        log.error(result["message"])
        return result
    username = auth_info.get("account", {}).get("username", "未知用户")
    return run_checkin_with_credential(auth_info, device_id,
                                       username=username, status_only=status_only)
