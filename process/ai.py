# -*- coding: utf-8 -*-
"""大模型调用（DeepSeek）：客户端、通用取文、翻译、语义打标、AI 兜底判定。

约定：
  - 只走 deepseek-chat（纯文本）。不用 vision 模型来判断纯文本（会返空）。
  - 兜底策略：翻译失败要抛（外层会记"检测失败"）；打标/兜底失败要吞掉并给安全值。
"""
from typing import Callable

from openai import OpenAI

from config import DEEPSEEK_BASE, DEEPSEEK_KEY, DEEPSEEK_MODEL

_client: OpenAI | None = None


def get_client() -> OpenAI | None:
    """懒加载的 DeepSeek 客户端；没配 key 返回 None。"""
    global _client
    if not DEEPSEEK_KEY:
        return None
    if _client is None:
        _client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE)
    return _client


def chat(prompt: str, temperature: float = 0.0, model: str = "") -> str:
    """取一段模型回复（异常向上抛，由调用方决定怎么处理）。"""
    client = get_client()
    if client is None:
        return ""
    resp = client.chat.completions.create(
        model=model or DEEPSEEK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()


def translate_to_chinese(text: str) -> str:
    """英文推文 → 简体中文。没配 key 时返回 "[未配置 key]"（保持旧行为）。"""
    if not DEEPSEEK_KEY:
        return "[未配置 key]"
    prompt = (
        "把下面的英文航天推文翻译成简体中文，直接输出译文，不要加任何解释、前缀或引号。"
        "保留专有名词（Falcon 9、Starship 等）原文。\n\n原文：\n" + text
    )
    return chat(prompt, temperature=0.3)


def semantic_labels(text: str, log: Callable[[str], None] | None = None) -> list:
    """语义标签（AI）：领域、关键对象。失败返回 []（不中断打分）。"""
    if not DEEPSEEK_KEY or not text:
        return []
    try:
        prompt = (
            "判断下面的航天新闻属于哪个领域、关键对象是什么。只输出一行中文，格式："
            "领域=<发射/任务/商业/科普/故障/历史/其他>;关键对象=<星舰/猎鹰9/长征/X空 或具体任务>。\n"
            f"新闻：{text[:300]}"
        )
        out = chat(prompt, temperature=0.0)
        out = out.replace("领域=", "").replace("关键对象=", "")
        parts = out.split(";")
        labels = []
        if len(parts) >= 1:
            labels.append(f"领域:{parts[0].strip()}")
        if len(parts) >= 2:
            labels.append(f"对象:{parts[1].strip()}")
        return labels
    except Exception as e:
        if log:
            log(f"[警告] AI打标失败: {e}")
        return []


def build_fallback_prompt(news: dict, user_pref: dict, group_pref: dict) -> str:
    """构造 AI 兜底判定 prompt：通用转发价值 + 用户偏好 + 群偏好。"""
    base = (
        "你是一位航天爱好者群友。下面是一条航天新闻，请判断它有没有意思、值不值得分享。"
        "只输出一行：有意思 或 没意思。\n"
    )
    dims = ["标准1(通用)：普通航天爱好者看到这条会不会想转发给群友"]
    like_kw = user_pref.get("like", {}).get("喜欢关键词", [])
    if like_kw:
        dims.append(f"标准2(用户偏好)：用户对 {like_kw} 这类内容感兴趣，命中则更倾向发")
    group_kw = group_pref.get("like", {}).get("喜欢关键词", [])
    if group_kw:
        dims.append(f"标准3(群偏好)：该群对 {group_kw} 这类话题兴趣高，命中则更倾向发")
    base += "判断维度：\n" + "\n".join(dims) + "\n\n"
    base += f"新闻标题/译文：{news.get('zh','')[:200]}\n"
    base += f"原文：{news.get('raw','')[:200]}\n"
    base += "请综合以上维度判断，只输出：有意思 或 没意思。"
    return base


def fallback_decide(news: dict, user_pref: dict, group_pref: dict,
                    log: Callable[[str], None] | None = None) -> str:
    """AI 判断边缘新闻："发"（有意思）或 "丢"（没意思）。出错保守判"发"。"""
    if not DEEPSEEK_KEY:
        return "发"
    try:
        out = chat(build_fallback_prompt(news, user_pref, group_pref), temperature=0.0)
        return "发" if "有意思" in out else "丢"
    except Exception as e:
        if log:
            log(f"[警告] AI兜底判定失败: {e}")
        return "发"
