# -*- coding: utf-8 -*-
"""
航小麦 · 新闻抓取模块 (模块1 · 增量检测版)
==========================================
作用：读取 @Space_Alioth 的关注列表，检测各账号"最新内容"，只处理新增内容。

本次改造（阶段1）：
  - 全量监控关注列表（不再截取前 N 个）
  - 分批扫描 + 游标 + 账号间隔，防 X 限流/封号；限流则优雅跳过下轮续扫
  - 每条保存元信息：时间 / 用户 / 内容类型(文本/图/视频/转推/引用) / 原图 / 译文
  - 去重：基线按 tweet id + 时间戳（已抓过的旧内容这次再见到不算最新）
  - 每轮都写一个 new_news_*.json（0 新增也写空），避免 judge 重复判上一轮的旧文件

运行：
    <venv>/Scripts/python.exe news_fetch.py
    （venv 需装 twscrape / python-dotenv / openai / httpx）

配置：见 .env（PROXY_URL、DEEPSEEK_KEY、COOKIES_PATH、WATCH_ACCOUNT、NEWS_ACCOUNTS）
    扫描节奏见本文件 SCAN_BATCH / PAUSE_SECONDS；游标存 配置/扫描游标.json
基线：存 配置/抓取基线.json（记录每个账号已知最新 tweet id/时间）
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from openai import OpenAI
from twscrape import API, AccountsPool, gather
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
SEED_DIR = Path(__file__).parent
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = SEED_DIR / "output"
CONFIG_DIR = ROOT / "配置"
LOG_FILE = LOG_DIR / "news_fetch.log"
BASELINE_FILE = CONFIG_DIR / "抓取基线.json"
CURSOR_FILE = CONFIG_DIR / "扫描游标.json"

load_dotenv(SEED_DIR / ".env", encoding="utf-8")
# 避免 Windows GBK 控制台打印中文崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROXY_URL = os.getenv("PROXY_URL", "").strip()
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
COOKIES_PATH = Path(os.getenv("COOKIES_PATH", "").strip())
WATCH_ACCOUNT = os.getenv("WATCH_ACCOUNT", "Space_Alioth").strip()
NEWS_ACCOUNTS = [x.strip() for x in os.getenv("NEWS_ACCOUNTS", "").split(",") if x.strip()]

CHECK_PER_ACCOUNT = 3        # 每账号取最近几条来判定"最新"（少取省调用，避开置顶）
SCAN_BATCH = int(os.getenv("SCAN_BATCH", "40") or 40)   # 每轮扫描账号数（分批防限流）
PAUSE_SECONDS = float(os.getenv("PAUSE_SECONDS", "1.0") or 1.0)  # 每账号间暂停
MAX_CONSECUTIVE_FAIL = 8     # 连续失败超过此值视为被限流，停下轮续扫
FOLLOW_LIMIT = 500           # 关注列表拉取上限（覆盖170+）


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cookie_header() -> str:
    if not COOKIES_PATH.exists():
        log(f"[错误] 找不到 cookies: {COOKIES_PATH}")
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


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, data) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_baseline() -> dict:
    return load_json(BASELINE_FILE, {})


def save_baseline(base: dict) -> None:
    save_json(BASELINE_FILE, base)


def load_cursor() -> int:
    return int(load_json(CURSOR_FILE, {"cursor": 0}).get("cursor", 0))


def save_cursor(cursor: int) -> None:
    save_json(CURSOR_FILE, {"cursor": int(cursor)})


def _has_video(tweet) -> bool:
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
        if _has_video(tweet):
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


def translate_to_chinese(text: str) -> str:
    if not DEEPSEEK_KEY:
        return "[未配置 key]"
    client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
    prompt = (
        "把下面的英文航天推文翻译成简体中文，直接输出译文，不要加任何解释、前缀或引号。"
        "保留专有名词（Falcon 9、Starship 等）原文。\n\n原文：\n" + text
    )
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()


def is_real_media_url(url: str) -> bool:
    """只认真正的推文媒体图：pbs.twimg.com/media/ 开头。头像/非推文图不算。"""
    if not url:
        return False
    return "pbs.twimg.com/media/" in url


def download_image(tweet_id, url, idx=0) -> str:
    """下载推文媒体图（仅真图），返回本地路径；非真图/失败返回 ''. 成功/失败都写日志。"""
    if not is_real_media_url(url):
        return ""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = url.split("?")[0]
    ok = False
    for suffix in ["?name=orig", "?name=large", ""]:
        try:
            cand = base + suffix
            with httpx.Client(proxy=PROXY_URL, timeout=60) as client:
                r = client.get(cand)
                r.raise_for_status()
            fname = OUTPUT_DIR / f"{tweet_id}_{idx}.jpg"
            fname.write_bytes(r.content)
            log(f"[图片OK] {tweet_id} 第{idx+1}张 下载成功 -> {fname.name}")
            return str(fname)
        except Exception:
            continue
    log(f"[图片X ] {tweet_id} 第{idx+1}张 下载失败(重试全部后缀均失败)")
    return ""


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
    """从转推(latest)的 rawContent 里尽力提取'转发者自己加的话'（纯转推一般没有，返回空）。

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


async def gather_accounts(api) -> list:
    """取全部关注账号。明确配置 NEWS_ACCOUNTS 时用其指定；否则从关注列表全量取。"""
    if NEWS_ACCOUNTS:
        return NEWS_ACCOUNTS
    user = await api.user_by_login(WATCH_ACCOUNT)
    if not user:
        log(f"[错误] 找不到关注列表账号 @{WATCH_ACCOUNT}")
        return []
    follow = await gather(api.following(user.id, limit=FOLLOW_LIMIT))
    names = [f.username for f in follow if f]
    log(f"[信息] 从 @{WATCH_ACCOUNT} 关注列表拿到 {len(names)} 个账号")
    return names


async def get_latest_tweet(api, username: str):
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


async def main():
    log("=" * 55)
    log("航小麦 · 新闻抓取 (模块1 · 增量检测 · 全量分批)")
    cookie = load_cookie_header()

    pid = SEED_DIR / "accounts.db"
    if pid.exists():
        pid.unlink()
    pool = AccountsPool(db_file=str(pid))
    await pool.add_account(
        username="hx_probe", password="", email="",
        email_password="", cookies=cookie, proxy=PROXY_URL,
    )
    api = API(pool=pool)

    accounts = await gather_accounts(api)
    if not accounts:
        log("[结果] 没有可抓取的账号")
        return

    # ---- 分批扫描 ----
    total = len(accounts)
    cursor = load_cursor()
    start = cursor % total if total else 0
    batch = accounts[start:start + SCAN_BATCH]
    # 下一批游标：本轮走完了整批就推进；若限流中断则保持原位
    next_cursor = (start + SCAN_BATCH) % total if total else 0
    log(f"[信息] 账号总数 {total}，本轮扫描 {start}~{start + len(batch)}，共 {len(batch)} 个")

    baseline = load_baseline()
    new_items = []
    consecutive_fail = 0
    rate_limited = False

    for idx, name in enumerate(batch):
        if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
            rate_limited = True
            log(f"[提示] 连续失败 {consecutive_fail} 次，疑似被限流，本轮提前结束，游标停在 {start}")
            break
        try:
            latest, ts = await get_latest_tweet(api, name)
            if not latest or ts == 0:
                log(f"[跳过] @{name} 拿不到最新")
            else:
                prev_ts = baseline.get(name, {}).get("ts", 0)
                prev_id = baseline.get(name, {}).get("tweet_id", "")
                if ts > prev_ts and str(latest.id) != str(prev_id):
                    # 转推 → 取原帖(retweetedTweet)的媒体/原文翻译；转发者评论从 latest 提取
                    rt = getattr(latest, "retweetedTweet", None)
                    raw_latest = str(getattr(latest, "rawContent", "") or "")
                    # 转推但原帖对象缺失(媒体无从判断) → 保守跳过，避免漏视频
                    if rt is None and raw_latest.startswith("RT @"):
                        e = baseline.setdefault(name, {})
                        e["ts"] = ts
                        e["tweet_id"] = latest.id
                        log(f"[跳视频] @{name} 更新 {latest.id} 为转推(原帖缺失)，保守跳过")
                        continue
                    is_rt = rt is not None and get_content_type(latest) == "转推"
                    eff = rt if is_rt else latest
                    content_type = get_content_type(eff)
                    # 视频/GIF → 先不推（只更新基线），视频下载/发送以后再做
                    if content_type == "视频" or _has_video(eff):
                        e = baseline.setdefault(name, {})
                        e["ts"] = ts
                        e["tweet_id"] = latest.id
                        log(f"[跳视频] @{name} 更新 {latest.id} 为视频，仅更新基线")
                        continue
                    # 引用对象里是视频 → 也跳过
                    qt = getattr(eff, "quotedTweet", None)
                    if qt is not None and _has_video(qt):
                        e = baseline.setdefault(name, {})
                        e["ts"] = ts
                        e["tweet_id"] = latest.id
                        log(f"[跳视频] @{name} 更新 {latest.id} 为引用视频，仅更新基线")
                        continue
                    raw = eff.rawContent if hasattr(eff, "rawContent") else ""
                    if raw:
                        zh = translate_to_chinese(raw)
                        media_urls = get_media_urls(eff)
                        images = []
                        for i, mu in enumerate(media_urls):
                            p = download_image(latest.id, mu, i)
                            if p:
                                images.append(p)
                        orig_account = ""
                        forward_comment = ""
                        if is_rt:
                            auth = getattr(rt, "author", None)
                            orig_account = str(getattr(auth, "username", "") or "")
                            forward_comment = extract_forward_comment(latest, eff)
                        item = {
                            "account": name,
                            "orig_account": orig_account,
                            "forward_comment": forward_comment,
                            "tweet_id": latest.id,
                            "ts": ts,
                            "date": str(latest.date) if latest.date else "",
                            "content_type": content_type,
                            "raw": raw[:600],
                            "zh": zh,
                            "images": images,
                            "image": images[0] if images else "",
                            "has_media": bool(images),
                        }
                        new_items.append(item)
                        log(f"[新增] @{name} 更新于 {item['date']} ({content_type})，已抓取")
                    else:
                        log(f"[新增] @{name} 更新但无文本内容，仅更新基线")
                    # 更新基线
                    e = baseline.setdefault(name, {})
                    e["ts"] = ts
                    e["tweet_id"] = latest.id
                else:
                    log(f"[无新] @{name} 最新 ts={ts} 基线 {prev_ts}")
            consecutive_fail = 0
        except Exception as e:
            consecutive_fail += 1
            log(f"[跳过] @{name} 检测失败: {e}")

    # 保存游标（未限流才推进）
    if not rate_limited:
        save_cursor(next_cursor)
        log(f"[信息] 游标推进到 {next_cursor}")
    else:
        log(f"[信息] 本批限流，游标保持 {start}，下轮续扫")

    save_baseline(baseline)
    log(f"[结果] 本轮新增 {len(new_items)} 条")

    # 每轮都写文件（0 新增也写空），避免 judge 读到上一轮的旧文件重复判
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rf = OUTPUT_DIR / f"new_news_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(rf, "w", encoding="utf-8") as f:
        json.dump(new_items, f, ensure_ascii=False, indent=2)
    log(f"[存档] 新增新闻已存: {rf}（本轮 {len(new_items)} 条）")
    for n in new_items[:10]:
        log(f"  - @{n['account']} [{n['date']}] {n.get('content_type','')} {n['zh'][:50]}")
    if not new_items:
        log("[存档] 无新增新闻")

    log("=== 检测结束 ===")


if __name__ == "__main__":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
