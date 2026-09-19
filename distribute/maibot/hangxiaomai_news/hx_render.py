# -*- coding: utf-8 -*-
"""hx_render.py · 文本与图片排版（"待推送条目" → "要发的消息"）
============================================================
统一格式（麦麦/艾莉森两条通道都用同一套字段，见 AGENTS.md「数据契约」）：

    来源：@<原帖作者> · X · UTC+8-YYYY-MM-DD-HH:MM
    <空行>
    <中文译文>
    <空行>                        （仅转推且转发者加了话时）
    转发@<转发账号>：<评论译文>

图片：本地路径列表；发给模型/QQ 前要转 base64（发原图，不压缩）。
"""
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    from hx_llm import translate_short
else:
    from .hx_llm import translate_short


def fmt_beijing(date_str: str, ts) -> str:
    """UTC 时间 → "UTC+8-YYYY-MM-DD-HH:MM"；解析失败返回 ''。"""
    d = None
    if ts:
        try:
            d = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception:
            d = None
    if d is None and date_str:
        try:
            s = str(date_str).strip().replace("T", " ").replace("+00:00", "").strip()
            d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            d = None
    if d is None:
        return ""
    bj = d.astimezone(timezone(timedelta(hours=8)))
    return "UTC+8-" + bj.strftime("%Y-%m-%d-%H:%M")


def build_source_line(item: dict) -> str:
    """来源行：来源：@账户 · 平台 · 时间(北京时间)。转推时显示原帖作者。"""
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


def forward_line(item: dict, logger=None) -> str:
    """转推且转发者加了评论 → "转发@<账号>：<译文>"；否则返回 ''。"""
    fc = str(item.get("forward_comment", "") or "").strip()
    if not fc:
        return ""
    fa = str(item.get("account", "") or "")
    tr = translate_short(fc, logger)
    return (f"转发@{fa}：" + tr) if fa else ("转发者：" + tr)


def quoted_text(item: dict) -> str:
    """引用推文里【被引用的那条】的展示块；没有引用返回 ''。

    优先用被引用正文的译文（items.quoted_zh），模型没给译文时退回原文。
    """
    raw = str(item.get("quoted_raw") or "").strip()
    zh = str(item.get("quoted_zh") or "").strip()
    if not raw and not zh:
        return ""
    who = str(item.get("quoted_account") or "").strip()
    body = zh or raw
    return (f"【引用 @{who}】\n{body}" if who else f"【引用】\n{body}")


def body_text(item: dict) -> str:
    """正文（来源行 + 译文 + 转发评论 + 被引用内容），即"纯转述"模式下的一条消息文本。"""
    src = build_source_line(item)
    body = str(item.get("zh") or item.get("raw") or "").strip()
    text = (src + "\n\n" + body) if src else body
    fl = forward_line(item)
    if fl:
        text += "\n\n" + fl
    qt = quoted_text(item)
    if qt:
        text += "\n\n" + qt
    return text


def item_images(item: dict) -> list:
    """取条目图片本地路径列表（优先 images，回退 image）。"""
    imgs = item.get("images", [])
    if not isinstance(imgs, list):
        imgs = []
    imgs = [str(x) for x in imgs if str(x).strip()]
    if not imgs:
        single = str(item.get("image", "") or "")
        if single:
            imgs = [single]
    return imgs


def img_to_base64(path: str) -> str:
    """读本地图片转 base64（发原图，不压缩）；失败返回 ''。"""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        return base64.b64encode(p.read_bytes()).decode("utf-8")
    except Exception:
        return ""
