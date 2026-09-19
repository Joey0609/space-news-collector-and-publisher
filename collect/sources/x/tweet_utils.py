# -*- coding: utf-8 -*-
"""推文对象工具：内容类型 / 视频判定 / 真图 URL / 转发者评论提取。

这里全部是"鸭子类型"的纯函数（只 getattr 推文对象），不 import twscrape，
所以 judge.py 这类不需要网络的模块也能安全复用。
"""


def has_video(tweet) -> bool:
    """该推文自身是否含视频/动图(animated_gif)。"""
    media = getattr(tweet, "media", None)
    if not media:
        return False
    return bool(getattr(media, "videos", None) or getattr(media, "animated", None))


def get_content_type(tweet) -> str:
    """尽力判断内容类型：视频/图片/转推/引用/文本。视频含 video 与 animated(GIF)。"""
    try:
        media = getattr(tweet, "media", None)
        photos = getattr(media, "photos", None) or []
        if has_video(tweet):
            return "视频"
        if photos:
            return "图片"
        if getattr(tweet, "retweetedTweet", None) is not None:
            return "转推"
        if getattr(tweet, "quotedTweet", None) is not None:
            return "引用"
    except Exception:
        pass
    return "文本"


def is_real_media_url(url: str) -> bool:
    """只认真正的推文媒体图：pbs.twimg.com/media/ 开头。头像/非推文图不算。"""
    if not url:
        return False
    return "pbs.twimg.com/media/" in url


def get_media_urls(tweet) -> list:
    """取推文全部真图 URL 列表。"""
    media = getattr(tweet, "media", None)
    photos = getattr(media, "photos", None) or []
    urls = []
    for p in photos:
        url = getattr(p, "url", "")
        if is_real_media_url(url):
            urls.append(url)
    return urls


def extract_forward_comment(latest, eff) -> str:
    """从转推(latest)的 rawContent 里尽力提取"转发者自己加的话"（纯转推一般没有，返回空）。

    启发式：去掉 "RT @xxx: " 前缀/开头 @账号: 后，若剩余基本等于原帖文本 → 说明只是转发（无评论）；
    若有额外文本 → 视为转发者加的评论。
    """
    raw_l = str(getattr(latest, "rawContent", "") or "").strip()
    if not raw_l:
        return ""
    s = raw_l
    if s.startswith("RT "):
        s = s[3:].strip()
    parts = s.split(":", 1)
    if parts and parts[0].strip().startswith("@"):
        s = parts[1].strip() if len(parts) > 1 else ""
    orig = str(getattr(eff, "rawContent", "") or "").strip()
    if not s:
        return ""
    if s == orig:
        return ""  # 只是转发原文，无评论
    if orig and orig in s:
        pre = s.split(orig, 1)[0].strip()  # 原帖文本之前的部分=转发者的话
        return pre[:200] if pre else ""
    return s[:200]
