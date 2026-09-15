# -*- coding: utf-8 -*-
"""多渠道消息推送（Server酱/PushPlus/企业微信/钉钉）与推送历史。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import re
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Optional
from .httpclient import _friendly_net_error
from .jsonstore import _load_json_file, _save_json_file
from .runtime import _log_dir, log
from .settings import PUSH_CHANNELS, PUSH_CHANNEL_LABELS, SETTINGS_VERSION


PUSH_LOG_FILE = _log_dir() / "push_history.json"


PUSH_LOG_KEEP_DAYS = 90


PUSH_LOG_KEEP_ITEMS = 300


def _http_post_raw(url: str, body: bytes, headers: dict,
                   timeout: int = 20) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, {"raw": text}
    except urllib.error.URLError as e:
        raise ConnectionError(_friendly_net_error(e)) from e


def _extract_webhook_param(raw: str, name: str) -> str:
    """从机器人完整 webhook URL 中提取参数（如 key= / access_token=），否则原样返回。"""
    raw = raw.strip()
    marker = f"{name}="
    if marker in raw:
        tail = raw.split(marker, 1)[1]
        return tail.split("&", 1)[0].strip()
    return raw


def _md_to_plain(content_md: str) -> str:
    """Markdown 极简转纯文本，供企业微信/钉钉 text 消息使用。"""
    txt = re.sub(r"^#{1,6}\s*", "", content_md, flags=re.MULTILINE)
    txt = re.sub(r"\*\*(.+?)\*\*", r"\1", txt)
    txt = txt.replace("---\n", "").replace("\n\n\n+", "\n\n")
    return txt.strip()


def _robot_sign(secret: str) -> tuple[str, str]:
    """群机器人加签：返回 (timestamp毫秒串, URL编码后的sign)，钉钉/企微算法一致。"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    return ts, sign


def _push_one_channel(channel: str, key: str, secret: str,
                      title: str, content_md: str) -> tuple[bool, str]:
    """向单个渠道推送一条消息，返回 (是否成功, 说明)。"""
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key:
        return False, "未填写凭据"
    try:
        if channel == "wecom":
            webhook_key = _extract_webhook_param(key, "key")
            url = (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                   f"?key={webhook_key}")
            if secret:
                ts, sign = _robot_sign(secret)
                url += f"&timestamp={ts}&sign={sign}"
            text = title + "\n\n" + _md_to_plain(content_md)
            body = json.dumps(
                {"msgtype": "text", "text": {"content": text}},
                ensure_ascii=False).encode("utf-8")
            status, resp = _http_post_raw(
                url, body, {"Content-Type": "application/json"})
            if status == 200 and resp.get("errcode") == 0:
                return True, "企业微信机器人推送成功"
            return False, f"企业微信返回：{resp.get('errmsg') or status}"

        if channel == "dingtalk":
            token = _extract_webhook_param(key, "access_token")
            url = ("https://oapi.dingtalk.com/robot/send"
                   f"?access_token={token}")
            if secret:
                ts, sign = _robot_sign(secret)
                url += f"&timestamp={ts}&sign={sign}"
            text = title + "\n\n" + _md_to_plain(content_md)
            body = json.dumps(
                {"msgtype": "text", "text": {"content": text}},
                ensure_ascii=False).encode("utf-8")
            status, resp = _http_post_raw(
                url, body, {"Content-Type": "application/json"})
            if status == 200 and resp.get("errcode") == 0:
                return True, "钉钉机器人推送成功"
            return False, f"钉钉返回：{resp.get('errmsg') or status}"

        if channel == "pushplus":
            # Markdown 极简转 HTML，保证微信端可读
            html = content_md.replace("&", "&amp;").replace("<", "&lt;") \
                .replace(">", "&gt;")
            html = html.replace("\n", "<br/>")
            body = json.dumps({
                "token": key, "title": title,
                "content": html, "template": "html",
            }, ensure_ascii=False).encode("utf-8")
            status, resp = _http_post_raw(
                "https://www.pushplus.plus/send", body,
                {"Content-Type": "application/json"})
            code = resp.get("code")
            if status == 200 and code == 200:
                return True, "PushPlus 推送成功"
            return False, f"PushPlus 返回：{resp.get('msg') or resp.get('message') or code}"

        if channel == "serverchan":
            form = urllib.parse.urlencode(
                {"title": title, "desp": content_md}).encode("utf-8")
            status, resp = _http_post_raw(
                f"https://sctapi.ftqq.com/{key}.send", form,
                {"Content-Type": "application/x-www-form-urlencoded"})
            if status == 200 and resp.get("code") == 0:
                return True, "Server酱推送成功"
            msg = resp.get("message") or resp.get("error") or f"HTTP {status}"
            return False, f"Server酱返回：{msg}"

        return False, f"未知渠道：{channel}"
    except Exception as e:
        return False, f"{PUSH_CHANNEL_LABELS.get(channel, channel)}推送失败：{e}"


def load_push_history(limit: int = 100) -> list[dict]:
    """读取推送历史（按时间倒序），最多返回 limit 条。只含标题/渠道/结果，不含正文。"""
    data = _load_json_file(PUSH_LOG_FILE)
    items = data.get("items")
    if not isinstance(items, list):
        return []
    out = [it for it in items if isinstance(it, dict)]
    out.sort(key=lambda it: str(it.get("ts") or ""), reverse=True)
    return out[:limit]


def clear_push_history() -> int:
    """清空推送历史，返回被清除的条数（文件不存在或异常时返回 0）。"""
    try:
        n = len(load_push_history(limit=100000))
        _save_json_file(PUSH_LOG_FILE, {"items": []})
        return n
    except Exception as e:
        log.warning(f"清空推送历史失败：{e}")
        return 0


def record_push_history(title: str, targets: list[str], success: bool,
                        detail: str, kind: str = "签到结果") -> None:
    """追加一条推送历史（90 天 / 300 条上限，异常静默，绝不影响主流程）。"""
    try:
        data = _load_json_file(PUSH_LOG_FILE)
        items = data.get("items")
        if not isinstance(items, list):
            items = []
        now = datetime.now()
        items.append({
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "kind": str(kind or "签到结果"),
            "title": str(title or "")[:120],
            "channels": [PUSH_CHANNEL_LABELS.get(c, c) for c in targets if c],
            "ok": bool(success),
            "detail": str(detail or "")[:300],
        })
        cutoff = time.time() - PUSH_LOG_KEEP_DAYS * 86400
        kept = []
        for it in items:
            try:
                if datetime.strptime(it.get("ts", "")[:19],
                                     "%Y-%m-%d %H:%M:%S").timestamp() < cutoff:
                    continue
            except Exception:
                continue
            kept.append(it)
        if len(kept) > PUSH_LOG_KEEP_ITEMS:
            kept = kept[-PUSH_LOG_KEEP_ITEMS:]
        _save_json_file(PUSH_LOG_FILE, {"items": kept})
    except Exception as e:
        log.warning(f"写入推送历史失败（不影响推送）：{e}")


def push_wechat(settings: dict, title: str, content_md: str,
                kind: str = "签到结果") -> tuple[bool, str]:
    """
    向所有已勾选且已配置凭据的渠道顺序推送（任一渠道失败不影响其他渠道）。
    返回 (是否至少一个渠道成功, 汇总说明)。兼容旧版单渠道设置结构。
    每次调用都会在本地推送历史中追加一条（不含正文与密钥）。
    """
    if settings.get("version") == SETTINGS_VERSION or "channels" in settings:
        channels = [c for c in settings.get("channels", [])
                    if c in PUSH_CHANNELS]
        creds = settings.get("creds") or {}
    else:
        # 旧版内存结构（如 GUI 中手工构造的 dict）
        channels = [settings.get("channel", "serverchan")]
        creds = {channels[0]: {"key": settings.get("key", ""), "secret": ""}}
    targets = [c for c in channels
               if (creds.get(c) or {}).get("key")]
    if not targets:
        record_push_history(title, channels, False, "未填写推送凭据", kind)
        return False, "未填写推送凭据"
    oks, msgs = [], []
    for ch in targets:
        ok, msg = _push_one_channel(
            ch, creds[ch].get("key", ""), creds[ch].get("secret", ""),
            title, content_md)
        oks.append(ok)
        msgs.append(("✓ " if ok else "✗ ") + msg)
    success = any(oks)
    detail = "；".join(msgs)
    if success and all(oks):
        summary = f"已推送 {len(oks)} 个渠道。"
    elif success:
        summary = f"部分渠道成功（{sum(oks)}/{len(oks)}）：{detail}"
    else:
        summary = detail
    record_push_history(title, targets, success, summary, kind)
    if success and all(oks):
        return True, summary
    return success, summary
