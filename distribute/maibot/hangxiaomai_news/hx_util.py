# -*- coding: utf-8 -*-
"""hx_util.py · 纯工具函数（不碰文件、不碰 ctx，随便复用）
=====================================================
- 列表归一化：把"配置里可能写成 str / list / 逗号串"的字段统一成 list[str]

注意：私聊/各群"各发各的、互不占用"现在由 SQLite 的 `items.pushed` 负责
（每个目标一个 source，见 dashboard/channels.py），这里不再有 sent_to 标记。
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
