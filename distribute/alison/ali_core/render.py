# -*- coding: utf-8 -*-
"""排版与候选挑选：把"分发队列里的一条"变成"群里要发的那段话"。
============================================================
队列来自 SQLite（stage='processed'、decision='发' 且本通道 source 不是 true，见 push_queue.py），
所以这里**不再判断"发没发过"**——传进来的就是还没发过的。

格式：
    AI标题 - 原帖作者账号
    <中文译文正文>
    YYYY.MM.DD HH:MM（北京时间）

图片仍作为富媒体附件随消息发送，位于文字内容之后。
"""
import datetime


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


def fmt_beijing_short(date_str: str, ts) -> str:
    """北京时间短格式 YYYY.MM.DD HH:MM；无有效时间时返回空串。"""
    d = None
    if ts:
        try:
            d = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        except Exception:
            d = None
    if d is None and date_str:
        try:
            s = str(date_str).strip().replace("T", " ").replace("+00:00", "").strip()
            d = datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc)
        except Exception:
            d = None
    if d is None:
        return ""
    bj = d.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return bj.strftime("%Y.%m.%d %H:%M")


def quoted_text(item: dict) -> str:
    """引用推文里【被引用的那条】的展示块；没有引用返回空串。

    优先用被引用正文的译文（translated 阶段产出的 items.quoted_zh），
    模型没给译文时退回原文。
    """
    raw = str(item.get("quoted_raw") or "").strip()
    zh = str(item.get("quoted_zh") or "").strip()
    if not raw and not zh:
        return ""
    who = str(item.get("quoted_account") or "").strip()
    body = zh or raw
    return (f"【引用 @{who}】\n{body}" if who else f"【引用】\n{body}")


def build_text(item: dict) -> str:
    """艾莉森消息：AI 标题和作者、当前译文、末尾北京时间。"""
    title = str(item.get("ai_title") or "").strip()
    account = str(item.get("orig_account") or item.get("account") or "").strip()
    body = str(item.get("zh") or item.get("raw") or "").strip()
    date = fmt_beijing_short(str(item.get("date", "") or ""), item.get("ts"))
    # 老数据或非新闻没有 AI 标题时，以正文第一行兜底，避免出现空标题。
    if not title:
        title = body.splitlines()[0].strip()[:30] if body else "航天资讯"
    headline = f"{title} - {account}" if account else title
    parts = [headline]
    if body:
        parts.append(body)
    if date:
        parts.append(date)
    # QQ 富媒体消息会将 content 放在 media 图片之前；正文各段只用单换行。
    return "\n".join(parts)


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


def pick_item(items: list, index: int | None = None):
    """选一条待发条目：优先带图、分数高。返回 (下标, 条目)。

    传进来的 items 已经过滤成"本通道还没发过的"（见 push_queue.pending），
    所以这里只看 有没有图 / 分数。
    """
    if index is not None:
        if 0 <= index < len(items):
            return index, items[index]
        return None, None
    if not items:
        return None, None
    cands = []
    for i, it in enumerate(items):
        has_img = 1 if item_images(it) else 0
        cands.append((has_img, it.get("score", 0) or 0, i))
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


def pick_newest(items: list):
    """按时间倒序取最新的一条（传进来的已经是"本通道还没发过的"）。"""
    if not items:
        return None
    return sorted(items, key=ts_of, reverse=True)[0]
