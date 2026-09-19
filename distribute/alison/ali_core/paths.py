# -*- coding: utf-8 -*-
"""路径与常量：艾莉森网关的"文件都在哪"唯一出处。"""
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # distribute/alison/
HX_ROOT = HERE.parent.parent                # repository root

ENV_FILE = HERE / ".env"                     # APPID / SECRET / SANDBOX
STATE_FILE = HERE / "state.json"             # 记录到的 group_openid / user_openid
LOG_FILE = HERE / "logs" / "推送.log"

PENDING_FILE = HX_ROOT / "data" / "queue" / "待推送.json"
SENT_EVER_FILE = HX_ROOT / "data" / "queue" / "已发记录.json"

# 目标群（群号只用于 sent_to 标记；真正的 group_openid 从 state.json 取）
TARGET_GROUP_NUMBER = "YOUR_GROUP_ID_2"
TARGET_KIND = f"group_{TARGET_GROUP_NUMBER}"

# ---- 网络与上传常量 ----
HTTP_TIMEOUT = 60.0        # 单次 HTTP 超时（秒）
MD5_10M_BYTES = 10002432   # QQ 分片上传要求的"前 10MB"md5 长度
MAX_SENT_RECORDS = 500     # 已发记录最多保留条数
