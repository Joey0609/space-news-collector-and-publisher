# -*- coding: utf-8 -*-
"""采集入口：遍历 `collect/sources/` 下注册的所有源各跑一轮。

新增一个源不需要改这里 —— 照 `collect/sources/_template.py` 建 `<名字>/adapter.py`
并写好 `ADAPTER = XxxAdapter()`，本入口会自动把它跑起来。
"""
import asyncio
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "collect"
for _path in (ROOT, COLLECT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import sources  # noqa: E402
from collect.env import COOKIES_PATH, PROXY_URL  # noqa: E402,F401
from collect.logging_util import make_log, setup_console_encoding  # noqa: E402
from collect.paths import LOG_DIR, OUTPUT_DIR, SEED_DIR  # noqa: E402,F401
from sources.x.images import download_image  # noqa: E402,F401
from sources.x.tweet_utils import get_media_urls  # noqa: E402,F401
from sources.x.x_client import load_cookie_header  # noqa: E402,F401

LOG_FILE = LOG_DIR / "news_fetch.log"
log = make_log(LOG_FILE)


async def main(names=None):
    log("=" * 55)
    log("航小麦 · 采集（遍历所有已注册的源）")
    from sources._web.locking import acquire
    selected = sources.enabled_sources(names)
    if not selected:
        log("[采集] 没有启用的采集源")
        return
    groups = {"x" if adapter.name == "x" else "web" for adapter in selected}
    locks = []
    try:
        for group in sorted(groups):
            handle = acquire(group)
            if handle is None:
                log(f"[采集锁] {group} 已有一轮在运行，本次退出")
                return
            locks.append(handle)
        await sources.collect_all(log, [adapter.name for adapter in selected])
    finally:
        for handle in locks:
            handle.close()
    log("=== 检测结束 ===")


if __name__ == "__main__":
    setup_console_encoding()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", help="本轮采集源，逗号分隔：x,rss,snapi,sina")
    args = parser.parse_args()
    names = None
    if args.sources is not None:
        names = [value.strip() for value in args.sources.split(",") if value.strip()]
        registered = {adapter.name for adapter in sources.all_sources()}
        if not names or set(names) - registered:
            parser.error("--sources 必须是已注册的非空采集源列表")
    asyncio.run(main(names))
