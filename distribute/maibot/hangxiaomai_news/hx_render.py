# -*- coding: utf-8 -*-
"""hx_render.py · 文本与图片排版（"分发队列里的一条" → "要发的消息"）
====================================================================
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


def body_text(item):
    """直接排版已经整合引用和转发正文的完整译文。"""
    return "\n\n".join(str(p).strip() for p in (item.get('ai_title'),build_source_line(item),item.get('zh')) if p)


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
