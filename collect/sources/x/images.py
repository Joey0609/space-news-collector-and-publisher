# -*- coding: utf-8 -*-
"""图片下载：把推文媒体图存到 抓取脚本/output/<tweet_id>_<i>.jpg。"""
from typing import Callable

import httpx

from datetime import datetime
from collect.env import PROXY_URL
from collect.paths import image_path
from .tweet_utils import is_real_media_url


def download_image(tweet_id, url: str, idx: int = 0,
                   log: Callable[[str], None] = print, day: str = "") -> str:
    """下载推文媒体图（仅真图），返回本地路径；非真图/失败返回 ''. 成功失败都写日志。"""
    if not is_real_media_url(url):
        return ""
    base = url.split("?")[0]
    for suffix in ["?name=orig", "?name=large", ""]:
        try:
            cand = base + suffix
            with httpx.Client(proxy=PROXY_URL, timeout=60) as client:
                r = client.get(cand)
                r.raise_for_status()
            fname = image_path(f"{tweet_id}_{idx}.jpg", day or datetime.now().strftime("%Y%m%d"))
            from collect.image_store import store_image_bytes
            stored = store_image_bytes(r.content, fname)
            if not stored: return ''
            log(f"[图片OK] {tweet_id} 第{idx+1}张 下载成功 -> {fname.name}")
            return stored
        except Exception:
            continue
    log(f"[图片X ] {tweet_id} 第{idx+1}张 下载失败(重试全部后缀均失败)")
    return ""
