# -*- coding: utf-8 -*-
"""排版与候选挑选：把"待推送.json 里的一条"变成"群里要发的那段话"。

格式（艾莉森只做纯转述，不写文案）：
    来源：@<原帖作者> · X · UTC+8-YYYY-MM-DD-HH:MM
    <空行>
    <中文译文正文>
    <空行>
    转发@<账号>：<转发者评论> （仅转推且原帖作者有加话时才有）
"""
import datetime

from .paths import MAX_SENT_RECORDS, TARGET_KIND  # noqa: F401  (供调用方引用)


def fmt_beijing(date_str: str, ts) -> str:
    """UTC 时间 → 北京时间 "UTC+8-YYYY-MM-DD-HH:MM"；失败返回 ''。"""
    d = None
    if ts:
        try:
            d = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        except Exception:
            d = None
    if d is None and date_str:
        try:
            s = str(date_str).strip().replace("T", " ").replace("+00:00", "").strip()
            d = datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            d = None
    if d is None:
        return ""
    bj = d.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return "UTC+8-" + bj.strftime("%Y-%m-%d-%H:%M")


def build_source_line(item: dict) -> str:
    """来源行：来源：@账户 · 平台 · 时间(北京时间)。转推显示原帖作者。"""
    account = str(item.get("orig_account", "") or item.get("account", "") or "")
    platform = str(item.get("platform", "") or "X")
    date = fmt_beijing(str(item.get("date", "") or ""), item.get("ts"))
    if not account and not date:
        return ""
    parts = [f"@{account}"] if account else []
    if platform:
        parts.append(platform)
    if date:
        parts.append(date)
    return "来源：" + " · ".join(parts)


def build_text(item: dict) -> str:
    """完整正文：来源行 + 译文 + （转推的）转发者评论。"""
    src = build_source_line(item)
    body = str(item.get("zh") or item.get("raw") or "").strip()
    text = (src + "\n\n" + body) if src else body
    fc = str(item.get("forward_comment", "") or "").strip()
    if fc:
        fa = str(item.get("account", "") or "")
        text += "\n\n" + (f"转发@{fa}：" + fc if fa else "转发者：" + fc)
    return text


def item_images(item: dict) -> list:
    """取条目图片路径列表（优先 images，回退 image）。"""
    imgs = item.get("images", [])
    if not isinstance(imgs, list):
        imgs = []
    imgs = [str(x) for x in imgs if str(x).strip()]
    if not imgs:
        single = str(item.get("image", "") or "")
        if single:
            imgs = [single]
    return imgs


def item_sent_to(item: dict) -> list:
    """该条目已经发给过哪些目标（kind 列表）。"""
    v = item.get("sent_to", [])
    return [str(x) for x in (v if isinstance(v, list) else [])]


def mark_sent_to(item: dict, kind: str) -> None:
    """标记该条目已发给某个目标。"""
    cur = item_sent_to(item)
    if kind and kind not in cur:
        cur.append(kind)
    item["sent_to"] = cur


def pick_item(items: list, kind: str = TARGET_KIND, index: int | None = None):
    """选一条还没发给 kind 的条目；优先带图、分数高。返回 (下标, 条目)。"""
    if index is not None:
        if 0 <= index < len(items):
            return index, items[index]
        return None, None
    cands = []
    for i, it in enumerate(items):
        if kind in item_sent_to(it):
            continue
        has_img = 1 if item_images(it) else 0
        cands.append((has_img, it.get("score", 0) or 0, i))
    if not cands:
        return None, None
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return cands[0][2], items[cands[0][2]]


def ts_of(item: dict) -> float:
    """取条目的时间戳：优先 ts，其次解析 date 字符串。"""
    v = item.get("ts")
    if v:
        try:
            return float(v)
        except Exception:
            pass
    d = str(item.get("date") or "").strip()
    if d:
        try:
            return datetime.datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return 0.0


def pick_newest_unsent(items: list, kind: str = TARGET_KIND):
    """按时间倒序，取第一条"还没发给本目标"的条目（临时定时推送用）。"""
    for it in sorted(items, key=ts_of, reverse=True):
        if kind not in item_sent_to(it):
            return it
    return None
