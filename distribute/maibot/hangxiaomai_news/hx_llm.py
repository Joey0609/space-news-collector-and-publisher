# -*- coding: utf-8 -*-
"""hx_llm.py · 直连 DeepSeek（写文案 / 翻译 / 解析偏好）
===================================================
为什么直连而不走麦麦的 cap.call：
    在 Hook 里等宿主 LLM 有 30s 限额，容易超时；本插件只需要"一小段文本"，
    所以直连 DeepSeek（deepseek-chat 纯文本模型）。

key 来源：航小麦/抓取脚本/.env 的 DEEPSEEK_KEY（与采集管线共用同一份 key）。
模型：deepseek-chat（不要换成 vision 模型来判断纯文本，会返空）。
"""
import os

from dotenv import load_dotenv
from openai import OpenAI

if __package__ in (None, ""):
    from hx_paths import HX_ROOT, MAIBOT_CONFIG
else:
    from .hx_paths import HX_ROOT, MAIBOT_CONFIG

DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

load_dotenv(HX_ROOT / ".env", encoding="utf-8")
load_dotenv(HX_ROOT / "collect" / ".env", encoding="utf-8")
load_dotenv(HX_ROOT / "抓取脚本" / ".env", encoding="utf-8")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "").strip()


def llm_text(prompt: str, temperature: float = 0.0, logger=None) -> str:
    """取一段话（失败返回 ''，绝不抛给调用方）。logger 用 self.ctx.logger。"""
    if not DEEPSEEK_KEY:
        return ""
    try:
        client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE, timeout=20)
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        if logger:
            logger.warning(f"DeepSeek 直连失败: {e}")
        return ""


def maisaka_persona() -> str:
    """读麦麦真实人设（bot_config.toml 的 [personality]）用于生成麦麦口吻。

    读麦麦自己的配置，避免我们手编导致风格割裂；读不到时给一个兜底人设。
    """
    try:
        import tomllib
        with open(MAIBOT_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
        p = cfg.get("personality", {}) or {}
        personality = str(p.get("personality", "") or "")
        reply_style = str(p.get("reply_style", "") or "")
    except Exception:
        personality, reply_style = "", ""
    parts = [x for x in (personality, reply_style) if x.strip()]
    return "\n".join(parts) if parts else "你是麦麦，一个刚开始入坑、爱探索的航天爱好者。"


def translate_short(text: str, logger=None) -> str:
    """把一句较短的英文（如转发者评论）翻成中文；失败返回原文。"""
    text = str(text or "").strip()
    if not text:
        return ""
    out = llm_text(
        f"把下面这句比较短的转发者评论翻译成简体中文，直接输出译文，不要加任何前缀或引号：\n{text}",
        temperature=0.3, logger=logger,
    )
    return str(out or "").strip() or text
