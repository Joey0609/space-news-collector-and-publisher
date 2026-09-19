# -*- coding: utf-8 -*-
"""航小麦 · 新闻抓取模块 (模块1 · 增量检测版)
==========================================
作用：读取关注列表账号（默认 @Space_Alioth 的 following），检测各账号"最新内容"，
      只处理新增内容 → output/new_news_<时间>.json，交给模块2 打标打分。

运行：
    <venv>/Scripts/python.exe news_fetch.py
    （venv 需装 twscrape / python-dotenv / httpx）

配置：根 .env（PROXY_URL、COOKIES_PATH、WATCH_ACCOUNT、NEWS_ACCOUNTS、SCAN_BATCH）
数据：配置/抓取基线.json（每账号已知最新）、配置/扫描游标.json（分批游标）

注意：本文件是 X 源适配器入口。X 专用逻辑在同目录的 scan.py/x_client.py/images.py/tweet_utils.py；
      通用采集基础在 collect/{paths,env,logging_util,jsonio,store}.py。
"""
import asyncio
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_COLLECT = _REPO / "collect"
for _path in (_REPO, _COLLECT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from collect.env import (  # noqa: F401
    COOKIES_PATH,
    NEWS_ACCOUNTS,
    PROXY_URL,
    WATCH_ACCOUNT,
)
from sources.x.images import download_image  # noqa: F401
from collect.logging_util import make_log, setup_console_encoding
from collect.paths import CONFIG_DIR, LOG_DIR, OUTPUT_DIR, ROOT, SEED_DIR  # noqa: F401
from sources.x.scan import run_scan  # noqa: F401
from collect.store import load_baseline, load_cursor, save_baseline, save_cursor  # noqa: F401
from sources.x.tweet_utils import (  # noqa: F401
    extract_forward_comment,
    get_content_type,
    get_media_urls,
    has_video,
    is_real_media_url,
)
from sources.x.x_client import build_api, gather_accounts, get_latest_tweet, load_cookie_header  # noqa: F401

LOG_FILE = LOG_DIR / "news_fetch.log"
log = make_log(LOG_FILE)


async def main():
    log("=" * 55)
    log("航小麦 · 新闻抓取 (模块1 · 增量检测 · 全量分批)")
    await run_scan(log)
    log("=== 检测结束 ===")


if __name__ == "__main__":
    setup_console_encoding()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
