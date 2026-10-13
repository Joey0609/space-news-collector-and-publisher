# -*- coding: utf-8 -*-
"""路径与常量：艾莉森网关的"文件都在哪"唯一出处。"""
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # distribute/alison/
HX_ROOT = HERE.parent.parent                # repository root

ENV_FILE = HERE / ".env"                     # APPID / SECRET / SANDBOX
STATE_FILE = HERE / "state.json"             # 记录到的 group_openid / user_openid
LOG_FILE = HERE / "logs" / "推送.log"

# 分发队列在 SQLite 里（items.decision='发' + items.pushed），见 push_queue.py。
DB_FILE = HX_ROOT / "data" / "hx.sqlite3"
# 下面两个只在"首次迁移"时被读一次（把旧队列的 sent_to / 已发记录搬进 items.pushed），不再写。
PENDING_FILE = HX_ROOT / "data" / "queue" / "待推送.json"
SENT_EVER_FILE = HX_ROOT / "data" / "queue" / "已发记录.json"

# 已启用群由 alison_groups 表决定；群 OpenID 就是分发 source 的目标部分。
# ---- 网络与上传常量 ----
HTTP_TIMEOUT = 60.0        # 单次 HTTP 超时（秒）
MD5_10M_BYTES = 10002432   # QQ 分片上传要求的"前 10MB"md5 长度
MAX_SENT_RECORDS = 500     # 已发记录最多保留条数
