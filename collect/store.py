# -*- coding: utf-8 -*-
"""数据存档层：抓取基线 / 扫描游标 / 已发记录。

这里是"文件格式的唯一出处"——想加字段先看这里。所有写入都容错（失败只记日志，
不抛异常），保证定时任务不会因为一次写盘失败整体中断。

注意：**待推送队列已经不在 JSON 里了**——最终池是 items 表里 stage='processed' 且 decision='发' 的行，
"这条发给过谁"记在 items.pushed（见 distribute/push_db.py）。这里不再有
load_pending / append_pending。
"""
from typing import Callable

from .jsonio import load_json, save_json
from .paths import (
    BASELINE_FILE,
    CURSOR_FILE,
    MAX_SENT,
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


# ────────────────────────── 已发记录（模块5/分发端的历史留档）──────────────────────────

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
