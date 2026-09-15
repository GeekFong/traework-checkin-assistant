# -*- coding: utf-8 -*-
"""带退避重试的 HTTP POST 封装。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import json
import random
import socket
import ssl
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Optional
from .runtime import RETRYABLE_STATUS, log


def _friendly_net_error(err: BaseException) -> str:
    """把底层网络异常翻译成人话，方便用户自行排查（断网/DNS/超时/代理/证书）。"""
    reason = getattr(err, "reason", err)
    text = str(reason).lower()
    if isinstance(reason, socket.timeout) or "timed out" in text or "timeout" in text:
        return "请求超时：网络较慢或对方服务器暂时无响应，请稍后重试"
    if isinstance(reason, ssl.SSLError) or "certificate" in text:
        return "HTTPS 证书校验失败：请检查系统时间是否正确，或代理/杀毒软件是否在拦截加密连接"
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in text or "name or service" in text \
            or "gethostbyname" in text or "dns" in text:
        return "无法解析服务器域名（DNS 故障）：请检查网络连接或更换 DNS（如 223.5.5.5）"
    if isinstance(reason, ConnectionRefusedError) or "refused" in text:
        return "服务器拒绝连接：服务暂时不可用，请稍后重试"
    if isinstance(reason, ConnectionResetError) or "reset" in text or "forcibly closed" in text:
        return "连接被远端重置：网络不稳定或被代理/防火墙中断，请稍后重试"
    if "proxy" in text:
        return "代理连接失败：请检查系统代理设置，或关闭代理后重试"
    if (isinstance(reason, OSError) and getattr(reason, "winerror", None) == 12029) \
            or "winerror 12029" in text or "12029" in text:
        return "无法连接服务器：电脑当前似乎没有联网，请检查网络后重试"
    # WinError 100xx 系列统一提示检查网络
    if "winerror 100" in text or "network is unreachable" in text or "unreachable" in text:
        return "网络不可达：请检查电脑是否已联网、Wi-Fi/网线是否正常"
    return f"网络连接异常：{reason}"


def http_post(url: str, headers: dict, data: Optional[dict] = None,
              timeout: int = 30, retries: int = 3) -> tuple[int, dict]:
    body = json.dumps(data or {}).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
                return resp.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"raw": text}
            if e.code in RETRYABLE_STATUS and attempt < retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 0.8)
                hint = "服务器繁忙（限流）" if e.code == 429 else f"服务器临时错误 HTTP {e.code}"
                log.warning(f"请求 {url} {hint}，{delay:.1f}s 后重试"
                            f"（{attempt + 1}/{retries}）")
                time.sleep(delay)
                continue
            return e.code, parsed
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 0.8)
                log.warning(f"请求 {url} 网络异常（{_friendly_net_error(e)}），"
                            f"{delay:.1f}s 后重试（{attempt + 1}/{retries}）")
                time.sleep(delay)
                continue
    raise ConnectionError(f"{_friendly_net_error(last_err)}（已重试 {retries} 次仍失败）")
