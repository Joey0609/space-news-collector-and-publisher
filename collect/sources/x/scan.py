# -*- coding: utf-8 -*-
"""模块1 的抓取逻辑：全量分批扫描关注列表，只处理"新增内容"。

设计要点（改动前先读）：
  - 分批 + 游标：每轮只扫 SCAN_BATCH 个账号，游标存 配置/扫描游标.json；
    连续失败达 MAX_CONSECUTIVE_FAIL 视为被限流 → 本轮提前结束且游标不推进，下轮续扫。
  - 增量：以 配置/抓取基线.json 里每账号的 (ts, tweet_id) 判定"是否新增"。
  - 跳过视频/动图与"原帖缺失的转推"，只更新基线（视频下载/发送是后续阶段）。
  - 每轮都写一个 new_news_*.json（0 新增也写空文件），避免 judge 读到上一轮旧文件重复判。
"""
import asyncio
import json
from datetime import datetime
from typing import Callable

from collect.env import MAX_CONSECUTIVE_FAIL, PAUSE_SECONDS, SCAN_BATCH  # noqa: F401
from collect.logging_util import one_line
from .images import download_image
from collect.db import upsert_raw
from collect.store import load_baseline, load_cursor, save_baseline, save_cursor
from .tweet_utils import extract_forward_comment, get_content_type, get_media_urls, has_video
from .x_client import build_api, gather_accounts, get_latest_tweet, load_cookie_header


async def run_scan(log: Callable[[str], None] = print) -> list:
    """跑一轮增量抓取，返回本轮新增条目列表（同时写好 new_news_*.json）。"""
    cookie = load_cookie_header(log)
    api = await build_api(cookie)

    accounts = await gather_accounts(api, log)
    if not accounts:
        log("[结果] 没有可抓取的账号")
        return []

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

    for name in batch:
        if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
            rate_limited = True
            log(f"[提示] 连续失败 {consecutive_fail} 次，疑似被限流，本轮提前结束，游标停在 {start}")
            break
        try:
            latest, ts = await get_latest_tweet(api, name)
            if not latest or ts == 0:
                log(f"[跳过] @{name} 拿不到最新")
                consecutive_fail = 0
                continue

            prev_ts = baseline.get(name, {}).get("ts", 0)
            prev_id = baseline.get(name, {}).get("tweet_id", "")
            if ts > prev_ts and str(latest.id) != str(prev_id):
                item = _build_new_item(name, latest, ts, baseline, log)
                if item:
                    new_items.append(item)
                    log(f"[新增] @{name} 更新于 {item['date']} ({item['content_type']})，已抓取")
                # 无论是否产出条目都要更新基线（跳过/无文本也算"已见过"）
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
    for item in new_items:
        try:
            item["id"] = upsert_raw(item)
            log(f"[数据库] 已写入 item_id={item['id']} @{item.get('account')}")
        except Exception as exc:
            log(f"[数据库失败] @{item.get('account')}: {exc}")
    return new_items


def _build_new_item(name: str, latest, ts: int, baseline: dict,
                    log: Callable[[str], None]) -> dict | None:
    """把一条"新增推文"整理成标准条目；不需要发的（视频/原帖缺失等）返回 None 并只更新基线。"""
    # 转推 → 取原帖(retweetedTweet)的媒体/原文翻译；转发者评论从 latest 提取
    rt = getattr(latest, "retweetedTweet", None)
    raw_latest = str(getattr(latest, "rawContent", "") or "")
    # 转推但原帖对象缺失(媒体无从判断) → 保守跳过，避免漏视频
    if rt is None and raw_latest.startswith("RT @"):
        log(f"[跳视频] @{name} 更新 {latest.id} 为转推(原帖缺失)，保守跳过")
        return None
    is_rt = rt is not None and get_content_type(latest) == "转推"
    eff = rt if is_rt else latest
    content_type = get_content_type(eff)
    # 视频/GIF → 先不推（只更新基线），视频下载/发送以后再做
    if content_type == "视频" or has_video(eff):
        log(f"[跳视频] @{name} 更新 {latest.id} 为视频，仅更新基线")
        return None
    # 引用对象里是视频 → 也跳过
    qt = getattr(eff, "quotedTweet", None)
    if qt is not None and has_video(qt):
        log(f"[跳视频] @{name} 更新 {latest.id} 为引用视频，仅更新基线")
        return None

    # ---- 引用（quote）：被引用推文的内容随条目一起存 ----
    # 为什么要存：被引用那条往往才是"新闻本体"（引用者只加一句感想），
    # 之前这些信息全被丢掉，模型只看到引用者那句话。
    # 存了之后：① 进 AI 提示词参与判定 ② 翻译阶段产出 quoted_zh
    # ③ 有引用的条目第 3 盏灯（not_comment）会被强制判绿，见 process/translate_db.py。
    quoted_account = quoted_raw = quoted_url = ""
    if qt is not None:
        quoted_author = getattr(qt, "author", None)
        quoted_account = str(getattr(quoted_author, "username", "") or "")
        quoted_raw = str(getattr(qt, "rawContent", "") or "")
        quoted_id = getattr(qt, "id", "")
        if quoted_id:
            quoted_url = f"https://x.com/{quoted_account or 'i'}/status/{quoted_id}"
        # 不截断：整段被引用正文都进日志（one_line 只把换行压平，保证一条记录一行）
        log(f"[引用] @{name} 更新 {latest.id} 引用了 @{quoted_account}：{one_line(quoted_raw)}")

    raw = eff.rawContent if hasattr(eff, "rawContent") else ""
    if not raw:
        if qt is not None:
            log(f"[新增] @{name} 更新但自身无文本（引用了 @{quoted_account}，"
                f"被引用正文 {len(quoted_raw)} 字），仅更新基线")
        else:
            log(f"[新增] @{name} 更新但无文本内容，仅更新基线")
        return None

    # 图片：先下引用者自己的图，再下被引用推文的图（接在后面、合并进同一个列表），
    # 这样分发端和看板都不用改就能用上引用图。
    day = str(latest.date)[:10].replace("-", "") if getattr(latest, "date", None) else ""
    images = []
    for i, mu in enumerate(get_media_urls(eff)):
        p = download_image(latest.id, mu, i, log, day=day)
        if p:
            images.append(p)
    quoted_media = get_media_urls(qt) if qt is not None else []
    if quoted_media:
        log(f"[引用图] @{name} 更新 {latest.id} 被引用推文带 {len(quoted_media)} 张图，一并下载")
    for j, mu in enumerate(quoted_media):
        p = download_image(latest.id, mu, len(images) + j, log, day=day)
        if p:
            images.append(p)

    orig_account = ""
    forward_comment = ""
    if is_rt:
        auth = getattr(rt, "author", None)
        orig_account = str(getattr(auth, "username", "") or "")
        forward_comment = extract_forward_comment(latest, eff)
    return {
        "platform": "x",
        "source": "x",
        "origin_id": str(latest.id),
        "account": name,
        "orig_account": orig_account,
        "forward_comment": forward_comment,
        "quoted_account": quoted_account,
        "quoted_raw": quoted_raw,
        "quoted_zh": "",
        "quoted_url": quoted_url,
        "tweet_id": latest.id,
        "ts": ts,
        "date": str(latest.date) if latest.date else "",
        "content_type": content_type,
        "raw": raw,
        "zh": "",
        "ori_url": f"https://x.com/{name}/status/{latest.id}",
        "images": images,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "stage": "collected",
    }

