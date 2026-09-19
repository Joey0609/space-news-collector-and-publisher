# -*- coding: utf-8 -*-
"""下载单条 X(Twitter) 推文的全部图片
==================================
复用本目录的 x_client / images / tweet_utils 逻辑。

用法（在 抓取脚本 目录下）：
    venv\\Scripts\\python.exe 下载图片.py https://x.com/SpaceX/status/2099237998294454546?s=20
    venv\\Scripts\\python.exe 下载图片.py 2099237998294454546

图片存到 抓取脚本\\output\\<tweet_id>_<i>.jpg
"""
import asyncio
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_COLLECT = _REPO / "collect"
for _path in (_REPO, _COLLECT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sources.x.images import download_image
from collect.logging_util import setup_console_encoding
from collect.paths import OUTPUT_DIR, SEED_DIR  # noqa: F401
from sources.x.tweet_utils import get_media_urls
from sources.x.x_client import build_api, load_cookie_header


def extract_id(s: str) -> str:
    m = re.search(r"/status/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(\d{10,})", s)
    return m.group(1) if m else s.strip()


async def main():
    if len(sys.argv) < 2:
        print("用法: 下载图片.py <推文URL或ID>")
        return
    tid = extract_id(sys.argv[1])
    print(f"[目标] tweet_id={tid}")

    api = await build_api(load_cookie_header())
    tweet = await api.tweet_details(int(tid))
    if not tweet:
        print("[失败] 拿不到推文（可能被删/受限/需登录）")
        return
    author = getattr(getattr(tweet, "user", None), "username", "?")
    raw = str(getattr(tweet, "rawContent", "") or "")
    print(f"[作者] @{author}")
    print(f"[内容] {raw[:120]}")

    # 若是转推，媒体在原帖里
    eff = getattr(tweet, "retweetedTweet", None) or tweet
    urls = get_media_urls(eff)
    print(f"[图片] 共 {len(urls)} 张")
    if not urls:
        print("[结果] 这条推文没有图片")
        return
    ok = 0
    for i, u in enumerate(urls):
        p = download_image(tid, u, i)
        if p:
            ok += 1
            print(f"  [{i+1}/{len(urls)}] OK -> {p}")
        else:
            print(f"  [{i+1}/{len(urls)}] 失败")
    print(f"[结果] 成功 {ok}/{len(urls)}，目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    setup_console_encoding()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
