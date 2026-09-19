# -*- coding: utf-8 -*-
"""X(Twitter) 访问层：cookies → twscrape 账号池 → 取关注列表/最新推文。

只有本模块 import twscrape；其他模块想要推文数据都走这里。
"""
import sys
from datetime import datetime
from typing import Callable

from twscrape import API, AccountsPool, gather

from collect.env import (
    CHECK_PER_ACCOUNT,
    COOKIES_PATH,
    FOLLOW_LIMIT,
    NEWS_ACCOUNTS,
    PROXY_URL,
    WATCH_ACCOUNT,
    config_summary,
)
from collect.paths import ACCOUNTS_DB


def load_cookie_header(log: Callable[[str], None] = print) -> str:
    """把 Netscape 格式的 cookies 文件转成一行 Cookie 头；文件缺失直接退出。"""
    if not COOKIES_PATH.is_file():
        log(f"[错误] COOKIES_PATH 无效或文件不存在: {COOKIES_PATH}")
        log(f"[诊断] 采集配置: {config_summary()}")
        sys.exit(1)
    raw = COOKIES_PATH.read_text(encoding="utf-8")
    pairs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name = parts[-2].strip()
        value = parts[-1].strip()
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


async def build_api(cookies: str) -> API:
    """建一个临时账号池并返回 API（每轮重建 accounts.db，避免旧账号状态污染）。"""
    if ACCOUNTS_DB.exists():
        ACCOUNTS_DB.unlink()
    pool = AccountsPool(db_file=str(ACCOUNTS_DB))
    await pool.add_account(
        username="hx_probe", password="", email="",
        email_password="", cookies=cookies, proxy=PROXY_URL,
    )
    return API(pool=pool)


async def gather_accounts(api: API, log: Callable[[str], None] = print) -> list:
    """取全部关注账号。明确配置 NEWS_ACCOUNTS 时用其指定；否则从关注列表全量取。"""
    log(f"[配置] X采集模式={'指定账号' if NEWS_ACCOUNTS else '关注列表'} | WATCH_ACCOUNT=@{WATCH_ACCOUNT} | cookies={COOKIES_PATH}")
    if NEWS_ACCOUNTS:
        log(f"[配置] 指定账号数量={len(NEWS_ACCOUNTS)}")
        return NEWS_ACCOUNTS
    if not WATCH_ACCOUNT:
        log("[错误] WATCH_ACCOUNT 为空；请在根 .env 填写账号，或使用 NEWS_ACCOUNTS 指定账号")
        return []
    user = await api.user_by_login(WATCH_ACCOUNT)
    if not user:
        log(f"[错误] 找不到关注列表账号 @{WATCH_ACCOUNT}")
        return []
    follow = await gather(api.following(user.id, limit=FOLLOW_LIMIT))
    names = [f.username for f in follow if f]
    log(f"[信息] 从 @{WATCH_ACCOUNT} 关注列表拿到 {len(names)} 个账号")
    return names


async def get_latest_tweet(api: API, username: str):
    """取该账号最新一条推文（按时间戳最晚，避开置顶）。返回 (tweet, updated_ts)。"""
    user = await api.user_by_login(username)
    if not user:
        return None, 0
    tweets = await gather(api.user_tweets(user.id, limit=CHECK_PER_ACCOUNT))
    if not tweets:
        return None, 0
    latest = max(tweets, key=lambda t: (t.date or datetime.min))
    ts = int(latest.date.timestamp()) if latest.date else 0
    return latest, ts
