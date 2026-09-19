# -*- coding: utf-8 -*-
"""X 源单独入口（调试单个源用）。

全量入口是 `collect/news_fetch.py`（会遍历所有已注册的源）；
这个文件只跑 X，方便单独调试。
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_COLLECT = _ROOT / "collect"
for _path in (_ROOT, _COLLECT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from collect.env import COOKIES_PATH, NEWS_ACCOUNTS, PROXY_URL, WATCH_ACCOUNT  # noqa: E402,F401
from collect.logging_util import make_log, setup_console_encoding  # noqa: E402
from collect.paths import CONFIG_DIR, LOG_DIR, OUTPUT_DIR, ROOT, SEED_DIR  # noqa: E402,F401
from collect.store import load_baseline, load_cursor, save_baseline, save_cursor  # noqa: E402,F401
from sources.x import ADAPTER  # noqa: E402
from sources.x.images import download_image  # noqa: E402,F401
from sources.x.scan import run_scan  # noqa: E402,F401
from sources.x.tweet_utils import (  # noqa: E402,F401
    extract_forward_comment,
    get_content_type,
    get_media_urls,
    has_video,
    is_real_media_url,
)
from sources.x.x_client import build_api, gather_accounts, get_latest_tweet, load_cookie_header  # noqa: E402,F401

LOG_FILE = LOG_DIR / "news_fetch.log"
log = make_log(LOG_FILE)


async def main():
    log("=" * 55)
    log("航小麦 · 新闻抓取 (X 源 · 增量检测 · 全量分批)")
    result = await ADAPTER.collect(log)
    log(f"[源] x 结束：新增 {len(result.items)} 条"
        + (f"，错误：{result.error}" if result.error else ""))
    log("=== 检测结束 ===")


if __name__ == "__main__":
    setup_console_encoding()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
