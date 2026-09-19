# -*- coding: utf-8 -*-
"""hx_util.py · 纯工具函数（不碰文件、不碰 ctx，随便复用）
=====================================================
- 列表归一化：把"配置里可能写成 str / list / 逗号串"的字段统一成 list[str]
- 条目去重键与 sent_to 标记：私聊/各群"各发各的、互不占用"靠它
"""
from typing import Any


def to_list(v: Any) -> list:
    """把值转成字符串列表（兼容 list/tuple 与逗号/中文逗号分隔的字符串）。"""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    return []


def add_words(dct: dict, key: str, words: list) -> None:
    """往 dct[key] 列表追加去重后的词（就地改 dct）。"""
    cur = [str(x) for x in (dct.get(key, []) or [])]
    for w in words:
        w = str(w).strip()
        if w and w not in cur:
            cur.append(w)
    dct[key] = cur


def as_bool(v: Any) -> bool:
    """宽松布尔：true/1/yes/on（不区分大小写）为真，数字非零为真。

    注意：无法识别的字符串一律判 False（与旧实现一致，别改成"给默认值"）。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def dedup_key(item: dict) -> tuple:
    """条目去重键：账号 + 中文译文（与原实现一致，永不重推靠它）。"""
    return str(item.get("account", "")), str(item.get("zh", ""))


def item_sent_to(item: dict) -> list:
    """条目已发给哪些目标 kind（私聊/群各自独立、不漏发靠它）。"""
    v = item.get("sent_to", [])
    return [str(x) for x in (v if isinstance(v, list) else [])]


def mark_item_sent(item: dict, kind: str) -> None:
    """记录"该条目已发给某目标 kind"。"""
    cur = item_sent_to(item)
    if kind and kind not in cur:
        cur.append(kind)
    item["sent_to"] = cur
