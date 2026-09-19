# -*- coding: utf-8 -*-
"""hx_llm.py · 直连模型 API（写文案 / 翻译 / 解析偏好）
===================================================
为什么直连而不走麦麦的 cap.call：
    在 Hook 里等宿主 LLM 有 30s 限额，容易超时；本插件只需要"一小段文本"，
    所以直连模型 API，用同一个模型做纯文本任务。

配置来源：航小麦根目录 `.env` 的 MODEL_API_BASE / MODEL_NAME / MODEL_API_KEY
（与采集、处理管线共用一份配置；协议看 MODEL_PROTOCOL，支持 openai / responses / anthropic）。
超时固定 20s：插件跑在麦麦 Hook 里，等不起更久。
"""
import os

from dotenv import load_dotenv

if __package__ in (None, ""):
    from hx_paths import HX_ROOT, MAIBOT_CONFIG
else:
    from .hx_paths import HX_ROOT, MAIBOT_CONFIG

load_dotenv(HX_ROOT / ".env", encoding="utf-8")
load_dotenv(HX_ROOT / "collect" / ".env", encoding="utf-8")

MODEL_API_BASE = os.getenv("MODEL_API_BASE", "").strip().rstrip("/")
MODEL_NAME = os.getenv("MODEL_NAME", "").strip()
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "").strip()
MODEL_PROTOCOL = (os.getenv("MODEL_PROTOCOL", "").strip().lower() or "openai")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "").strip() or "2023-06-01"
MAX_TOKENS = int(os.getenv("MODEL_MAX_TOKENS", "").strip() or 0)
LLM_TIMEOUT = 20


def is_configured() -> bool:
    return bool(MODEL_API_BASE and MODEL_NAME and MODEL_API_KEY)


def _chat(messages_text: str, temperature: float) -> str:
    """一次纯文本调用；按 MODEL_PROTOCOL 选通道。"""
    if MODEL_PROTOCOL == "anthropic":
        import httpx
        body = {"model": MODEL_NAME, "max_tokens": MAX_TOKENS or 1024,
                "temperature": temperature, "messages": [{"role": "user", "content": messages_text}]}
        response = httpx.post(
            f"{MODEL_API_BASE}/messages",
            headers={"x-api-key": MODEL_API_KEY, "anthropic-version": ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            json=body, timeout=LLM_TIMEOUT,
        )
        response.raise_for_status()
        blocks = response.json().get("content") or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict)).strip()

    from openai import OpenAI
    client = OpenAI(api_key=MODEL_API_KEY, base_url=MODEL_API_BASE, timeout=LLM_TIMEOUT)
    if MODEL_PROTOCOL == "responses":
        resp = client.responses.create(model=MODEL_NAME, input=messages_text, temperature=temperature)
        return (getattr(resp, "output_text", "") or "").strip()
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": messages_text}],
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()


def llm_text(prompt: str, temperature: float = 0.0, logger=None) -> str:
    """取一段话（失败返回 ''，绝不抛给调用方）。logger 用 self.ctx.logger。"""
    if not is_configured():
        return ""
    try:
        return _chat(prompt, temperature)
    except Exception as e:
        if logger:
            logger.warning(f"模型 API 直连失败: {e}")
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
