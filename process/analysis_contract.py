"""Single completeness contract for the processing pool.

Every incomplete row is re-run through the same full analysis.  This module is
shared by the processor and dashboard so their backlog counts cannot drift.
"""
from __future__ import annotations

import json
import math


PLACEHOLDER = "[未配置 key]"
_IMPORTANCE = {"高", "中", "低"}
_DIRTY_BODY_MARKERS = ("http://", "https://", "www.", "information as of", "location:")


def missing_analysis_fields(item: dict) -> list[str]:
    """Return the required full-analysis fields that are missing or invalid."""
    missing: list[str] = []
    zh = str(item.get("zh") or "").strip()
    if not zh or zh == PLACEHOLDER:
        missing.append("译文")

    flag_fields = (("is_news", "新闻"), ("not_repeat", "重复"),
                   ("not_comment", "评价"), ("not_sensitive", "敏感"))
    flags: dict[str, int | None] = {}
    for field, label in flag_fields:
        value = item.get(field)
        if value is True or value == 1 and type(value) is int:
            parsed = 1
        elif value is False or value == 0 and type(value) is int:
            parsed = 0
        elif isinstance(value, str) and value.strip() in {"0", "1"}:
            parsed = int(value.strip())
        else:
            parsed = None
        if parsed not in (0, 1):
            missing.append(label)
            flags[field] = None
        else:
            flags[field] = parsed

    try:
        value = item.get("ai_score")
        if isinstance(value, bool):
            raise ValueError
        score_number = float(value)
        if not math.isfinite(score_number) or not score_number.is_integer() or not 0 <= score_number <= 100:
            raise ValueError
    except (TypeError, ValueError):
        missing.append("AI分")

    tags = item.get("ai_tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (TypeError, ValueError):
            tags = None
    if not isinstance(tags, list) or not any(isinstance(tag, str) and tag.strip() for tag in tags):
        missing.append("标签")

    if not str(item.get("ai_title") or "").strip():
        missing.append("标题")

    if flags.get("is_news") == 1:
        if not str(item.get("ai_summary") or "").strip():
            missing.append("要点")
        if str(item.get("importance") or "").strip() not in _IMPORTANCE:
            missing.append("重要度")

    if str(item.get("quoted_raw") or "").strip() and not str(item.get("quoted_zh") or "").strip():
        missing.append("引用译文")

    body = zh.lower()
    if any(marker in body for marker in _DIRTY_BODY_MARKERS) or "直播观看" in zh:
        missing.append("正文清理")
    return missing


def processing_required(item: dict) -> bool:
    """Whether a row still belongs to the processing pool.

    A collected row with complete imported analysis still needs the normal
    judge/fallback stages, so it must be promoted into the processing chain.
    """
    return bool(missing_analysis_fields(item)) or str(item.get("stage") or "collected") != "processed"
