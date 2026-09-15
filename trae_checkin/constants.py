# -*- coding: utf-8 -*-
"""应用级常量：产品信息、平台标识、接口地址与安装目录候选。

由单文件版机械拆分而来，函数实现保持不变。
"""

from __future__ import annotations
from typing import Any, Optional


APP_NAME = "每日签到助手"


APP_DISPLAY_NAME = "TraeWork CN / WorkBuddy 每日签到助手"


APP_COPYRIGHT = "个人开源工具 · 免费使用 · 数据仅存本机"


TASK_NAME = "TraeWorkDailyCheckin"


REQ_SOURCE = 2


PLAT_TRAEWORK = "traework"


PLAT_WORKBUDDY = "workbuddy"


PLATFORM_LABELS = {
    PLAT_TRAEWORK: "TraeWork",
    PLAT_WORKBUDDY: "WorkBuddy",
}


API_BASE = "https://api.trae.cn"


CHECKIN_STATUS_URL = f"{API_BASE}/trae/api/v2/ug/checkin_credits/status"


CHECKIN_CLAIM_URL = f"{API_BASE}/trae/api/v2/ug/checkin_credits/claim"


REFRESH_TOKEN_URL = f"{API_BASE}/trae/api/v3/oauth/ExchangeToken"


WB_API_BASE = "https://copilot.tencent.com"


WB_CHECKIN_STATUS_URL = f"{WB_API_BASE}/v2/billing/meter/checkin-activity-status"


WB_CHECKIN_CLAIM_URL = f"{WB_API_BASE}/v2/billing/meter/daily-checkin"


WB_REFRESH_TOKEN_URL = f"{WB_API_BASE}/v2/auth/token/refresh"


WB_USER_AGENT_FALLBACK = "WorkBuddy/5.5.6"


APPDATA_DIR_NAMES = ["TRAE SOLO CN", "TRAE SOLO", "Trae CN", "Trae", "TRAE"]


INSTALL_DIR_NAMES = ["TRAE SOLO CN", "TRAE SOLO", "Trae CN", "Trae"]


EXE_NAMES = ["Trae SOLO CN.exe", "TRAE SOLO CN.exe", "Trae.exe", "TRAE.exe"]
