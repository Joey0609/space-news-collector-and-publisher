# -*- coding: utf-8 -*-
"""数据存档层：抓取基线 / 扫描游标 / 待推送队列 / 已发记录。

这里是"文件格式的唯一出处"——想加字段先看这里。所有写入都容错（失败只记日志，
不抛异常），保证定时任务不会因为一次写盘失败整体中断。
"""
from typing import Callable

from .jsonio import load_json, save_json
from .paths import (
    BASELINE_FILE,
    CONFIG_DIR,
    CURSOR_FILE,
    MAX_PENDING,
    MAX_SENT,
    PENDING_FILE,
    QUEUE_ARCHIVE_FILE,
    SENT_FILE,
)


# ────────────────────────── 抓取基线与游标（模块1）──────────────────────────

def load_baseline() -> dict:
    return load_json(BASELINE_FILE, {})


def save_baseline(base: dict) -> None:
    save_json(BASELINE_FILE, base)


def load_cursor() -> int:
    return int(load_json(CURSOR_FILE, {"cursor": 0}).get("cursor", 0))


def save_cursor(cursor: int) -> None:
    save_json(CURSOR_FILE, {"cursor": int(cursor)})


# ────────────────────────── 待推送队列（模块5 写 / 插件·推送读）──────────────────────────

def load_pending() -> list:
    """读 待推送.json。"""
    data = load_json(PENDING_FILE, [])
    return data if isinstance(data, list) else []


def save_pending(items: list) -> None:
    save_json(PENDING_FILE, items)


def load_sent_keys() -> set:
    """读已发记录，返回 {(account, zh)} 集合；失败返回空。"""
    data = load_json(SENT_FILE, []) or []
    try:
        return {(str(x.get("account", "")), str(x.get("zh", ""))) for x in data}
    except Exception:
        return set()


def load_sent_records() -> list:
    data = load_json(SENT_FILE, []) or []
    return data if isinstance(data, list) else []


def save_sent_records(records: list) -> None:
    """写已发记录（保留最近 MAX_SENT 条）。"""
    save_json(SENT_FILE, records[-MAX_SENT:])


def append_sent_records(items: list, log: Callable[[str], None] = print) -> None:
    """把已发条目追加进 已发记录.json（按 账号+内容 去重，保留最近 MAX_SENT 条）。"""
    if not items:
        return
    records = load_sent_records()
    seen = {(str(x.get("account", "")), str(x.get("zh", ""))) for x in records}
    for it in items:
        key = (str(it.get("account", "")), str(it.get("zh", "")))
        if key not in seen:
            records.append(it)
            seen.add(key)
    save_sent_records(records)


def append_pending(items: list, log: Callable[[str], None] = print) -> None:
    """把应发的新闻追加到 待推送.json。

    - 队列上限 MAX_PENDING：超出部分存到"队列档案"防丢失。
    - 去重：按 (account, zh) 去掉队列内重复；跳过已在"已发记录"里的（永不重推）。
    """
    old = load_pending()
    seen = {(str(x.get("account", "")), str(x.get("zh", ""))) for x in old}
    sent = load_sent_keys()
    add = []
    for it in items:
        key = (str(it.get("account", "")), str(it.get("zh", "")))
        if key in seen or key in sent:
            continue
        seen.add(key)
        add.append(it)
    merged = old + add
    if len(merged) > MAX_PENDING:
        overflow = merged[:MAX_PENDING]
        archived = merged[MAX_PENDING:]
        exist = load_json(QUEUE_ARCHIVE_FILE, []) or []
        save_json(QUEUE_ARCHIVE_FILE, exist + archived)
        merged = overflow
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_pending(merged)
    log(f"[待推送] 新增 {len(add)} 条，当前队列 {len(merged)}（上限{MAX_PENDING}）")
