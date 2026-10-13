# -*- coding: utf-8 -*-
"""X(Twitter) 访问层：文件账号名单 → cookies/twscrape → 最新推文。

只有本模块 import twscrape；其他模块想要推文数据都走这里。
"""
import sys
import re
import csv
from pathlib import Path
from datetime import datetime
from typing import Callable

from twscrape import API, AccountsPool, gather

from collect.env import (
    CHECK_PER_ACCOUNT,
    COOKIES_PATH,
    PROXY_URL,
    config_summary,
)
from collect.paths import ACCOUNTS_DB, X_ACCOUNTS_FILE


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


async def gather_accounts(api: API | None, log: Callable[[str], None] = print) -> list:
    """每轮重新读取本地账号名单；保留 api 参数以便现有调用方使用。"""
    log(f"[配置] X采集模式=文件名单 | 文件={X_ACCOUNTS_FILE}")
    try:
        names = load_account_names(X_ACCOUNTS_FILE)
    except (OSError, ValueError) as exc:
        log(f"[错误] X账号名单不可用：{exc}")
        raise
    log(f"[信息] 从账号名单读取 {len(names)} 个账号")
    return names


def load_account_names(path: Path) -> list[str]:
    """读取两列 CSV（名字、性质）；性质允许暂留空，按用户名原顺序去重。"""
    names = []
    seen = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["名字", "性质"]:
            raise ValueError(f"{path} 的 CSV 表头必须依次为：名字,性质")
        for row in reader:
            line_no = reader.line_num
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path} 第 {line_no} 行必须有两列：名字、性质")
            name = row["名字"].strip().removeprefix("@")
            kind = row["性质"].strip()
            if not name and not kind:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
                raise ValueError(f"{path} 第 {line_no} 行不是有效的 X 用户名：{name}")
            if kind not in ("", "official", "media", "photographer", "enthusiast", "others"):
                raise ValueError(f"{path} 第 {line_no} 行性质只能为空或 official/media/photographer/enthusiast/others：{kind}")
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    if not names:
        raise ValueError(f"{path} 没有可用账号，请在名字列填写 X 用户名")
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
