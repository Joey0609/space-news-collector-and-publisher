# -*- coding: utf-8 -*-
"""环境变量与扫描节奏参数（.env 只在这里读一次）。

.env 位置：抓取脚本/.env（模板见 .env.example），不入库。
本模块在 import 时就把 .env 读进环境并取好值 —— 各入口脚本 import 即生效，
与旧版"每个脚本自己 load_dotenv"的行为一致。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

from .paths import ROOT, SEED_DIR

# New canonical location is the repository root; collect/.env is accepted during P1.
load_dotenv(ROOT / ".env", encoding="utf-8")
load_dotenv(SEED_DIR / ".env", encoding="utf-8")

# ---- collection environment ----
PROXY_URL = os.getenv("PROXY_URL", "").strip()
_cookie_path = os.getenv("COOKIES_PATH", "").strip()
COOKIES_PATH = Path(_cookie_path) if _cookie_path else (ROOT / "data" / "state" / "cookies.txt")
_watch_account = os.getenv("WATCH_ACCOUNT", "").strip()
WATCH_ACCOUNT = _watch_account or "Space_Alioth"
NEWS_ACCOUNTS = [x.strip() for x in os.getenv("NEWS_ACCOUNTS", "").split(",") if x.strip()]


def config_summary() -> dict:
    """Return safe collection configuration diagnostics without exposing secrets."""
    return {
        "proxy_configured": bool(PROXY_URL),
        "cookies_path": str(COOKIES_PATH),
        "cookies_exists": COOKIES_PATH.is_file(),
        "watch_account": WATCH_ACCOUNT,
        "watch_account_from_default": not bool(_watch_account),
        "explicit_accounts": len(NEWS_ACCOUNTS),
    }

# ---- 抓取节奏（防 X 限流；也可用 .env 覆盖后两项）----
CHECK_PER_ACCOUNT = 3        # 每账号取最近几条判定"最新"（少取省调用，避开置顶）
SCAN_BATCH = int(os.getenv("SCAN_BATCH", "40") or 40)   # 每轮扫描账号数
PAUSE_SECONDS = float(os.getenv("PAUSE_SECONDS", "1.0") or 1.0)  # 每账号间暂停秒数
MAX_CONSECUTIVE_FAIL = 8     # 连续失败超过此值视为被限流，停下轮续扫
FOLLOW_LIMIT = 500           # 关注列表拉取上限（覆盖 170+）

# ---- 目标（推送默认对象，可被 配置/*.toml 覆盖）----
DEFAULT_TARGET_PRIVATE = "YOUR_QQ_ID"
DEFAULT_TARGET_GROUP = "YOUR_GROUP_ID_1"
