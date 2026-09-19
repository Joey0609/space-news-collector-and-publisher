# -*- coding: utf-8 -*-
"""大模型调用：一个协议无关的 chat()，供翻译+新闻判定+标签 / 语义打标 / AI 兜底共用。

协议由 config.MODEL_PROTOCOL 决定：
    openai     POST {base}/chat/completions   走 openai SDK
    responses  POST {base}/responses          走 openai SDK 的 Responses API
    anthropic  POST {base}/messages           走 httpx（不额外引入 anthropic 依赖）

约定：
  - 翻译（analyze_item）失败要抛（外层记录 translate_error 并累计重试次数）；
  - 打标 / 兜底失败要吞掉并给安全值，不能中断整轮处理。

一次调用完成这些事（analyze_item）：出译文、点四盏灯（是新闻 / 不重复 / 非评价 / 非敏感政治）、
给新闻标题/要点/重要度、打标签。
另有一条 analyze_meta()：给**已经有译文**的存量条目补四盏灯和标签，不重新翻译、不覆盖人工改过的译文。
标签沿用 judge 认识的写法："领域:<发射|重大任务|任务|商业|科普|故障|历史|其他>"、"对象:<星舰/猎鹰9/...>"、
"机构:<SpaceX/...>"；领域/对象的取值与 config_templates/推送规则.toml 的权重、白名单对齐。
"""
import json
import re

import httpx

from config import (
    ANTHROPIC_VERSION,
    MODEL_API_BASE,
    MODEL_API_KEY,
    MODEL_MAX_TOKENS,
    MODEL_NAME,
    MODEL_PROTOCOL,
    MODEL_TIMEOUT,
    missing_config,
)

_openai_client = None


class OutputTruncated(RuntimeError):
    """模型输出被输出上限截断：译文/JSON 都不完整，不能当成功入库。"""


def is_configured() -> bool:
    """必需项（base / name / key）是否都配好。没配就整段跳过，而不是给每条都记一次失败。"""
    return not missing_config()


def config_summary() -> dict:
    """给日志用的安全摘要（不含 key）。"""
    return {
        "protocol": MODEL_PROTOCOL,
        "model": MODEL_NAME,
        "base": MODEL_API_BASE,
        "max_tokens": MODEL_MAX_TOKENS,
        "key_configured": bool(MODEL_API_KEY),
        "missing": missing_config(),
    }


def get_client():
    """OpenAI 协议族（openai / responses）用的 SDK 客户端，懒加载。"""
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(
            api_key=MODEL_API_KEY, base_url=MODEL_API_BASE, timeout=MODEL_TIMEOUT
        )
    return _openai_client


def _endpoint(path: str) -> str:
    return f"{MODEL_API_BASE}{path}"


def _headers() -> dict:
    if MODEL_PROTOCOL == "anthropic":
        return {
            "x-api-key": MODEL_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    return {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "content-type": "application/json",
    }


def _chat_openai(prompt: str, temperature: float, model: str) -> str:
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if MODEL_MAX_TOKENS > 0:      # 0 = 不显式传，交给服务端默认
        kwargs["max_tokens"] = MODEL_MAX_TOKENS
    resp = get_client().chat.completions.create(**kwargs)
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", "") == "length":
        raise OutputTruncated(
            f"模型输出被截断（finish_reason=length）；调大 MODEL_MAX_TOKENS 或换更长输出的模型"
        )
    return (choice.message.content or "").strip()


def _chat_responses(prompt: str, temperature: float, model: str) -> str:
    resp = get_client().responses.create(model=model, input=prompt, temperature=temperature)
    text = ""
    try:
        text = (resp.output_text or "").strip()
    except Exception:
        text = ""
    if text:
        return text
    # 个别网关不填 output_text，从 output 结构里兜一遍
    parts = []
    for item in getattr(resp, "output", None) or []:
        for chunk in getattr(item, "content", None) or []:
            parts.append(getattr(chunk, "text", "") or "")
    return "".join(parts).strip()


def _chat_anthropic(prompt: str, temperature: float, model: str) -> str:
    body = {
        "model": model,
        "max_tokens": MODEL_MAX_TOKENS or 4096,   # Anthropic 必填
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    with httpx.Client(timeout=MODEL_TIMEOUT) as client:
        response = client.post(_endpoint("/messages"), headers=_headers(), json=body)
        response.raise_for_status()
        data = response.json()
    if str(data.get("stop_reason") or "") == "max_tokens":
        raise OutputTruncated("模型输出被 max_tokens 截断；调大 MODEL_MAX_TOKENS")
    blocks = data.get("content") or []
    return "".join(
        block.get("text", "") for block in blocks if isinstance(block, dict)
    ).strip()


_HANDLERS = {
    "openai": _chat_openai,
    "responses": _chat_responses,
    "anthropic": _chat_anthropic,
}


def chat(prompt: str, temperature: float = 0.0, model: str = "") -> str:
    """取一段模型回复（异常向上抛，由调用方决定怎么处理）。"""
    if not is_configured():
        return ""
    handler = _HANDLERS.get(MODEL_PROTOCOL, _chat_openai)
    return handler(prompt, temperature, model or MODEL_NAME)


# ---------------------------------------------------------------- 翻译 + 新闻 + 标签

# 新闻判定 + 四盏灯 + 新闻三件套：完整分析 与 "只补标" 两条 prompt 共用，改这里就够
_NEWS_RULES = """- "is_news"【第 1 盏灯】：这条本身是不是一条航天新闻？
  航天新闻、官方公告、任务进展、行业动态、故障通报 → true；
  闲聊、个人日常、情绪转发、广告、抽奖、招聘、无实质信息 → false。
- "not_repeat"【第 2 盏灯】：true = 不是重复内容。
  如果它是旧闻回顾（周年纪念、"X 年前的今天"、翻旧图）、
  或者【最近已入库清单】里已经有同一条/同一事件的 → false，
  并在 "repeat_of" 里写出清单里那条的编号（形如 "#56"；不是重复就给空串 ""）。
- "not_comment"【第 3 盏灯】：true = 它自己就是内容（原发报道、官方发布、独立消息）。
  如果它是针对**另一条新闻**的评价、吐槽、转发跟评（"这公司又…"、"这事真离谱"、
  引用别人消息加一句感想）→ false。
- "not_sensitive"【第 4 盏灯，政治/外交红线】：true = 不涉及敏感政治与外交关系。
  只要沾上下面任何一条就是 false（哪怕它同时也在讲航天）：
  · 国家间的政治/外交冲突：互相谴责、抗议、制裁、断交、军事对峙、领土争端
  · 涉台 / 涉港 / 涉疆 / 涉藏等政治议题，或任何带政治立场的表态
  · 政客或政府发言、选举、政党、意识形态争论、民族宗教敏感话题
  · 战争、军演、导弹试射这类带军事对抗含义的话题
  注意：正常的商业/科研航天活动（SpaceX 发射、中国航天任务、NASA 公告）→ true，不要误杀。
- 如果 is_news 是 true，再给下面三项；is_news 是 false 时这三项都给空字符串 ""：
  · "title"：一句话中文标题，不超过 20 个字，不要带账号名、不要引号
  · "summary"：1~2 句中文要点，不超过 60 个字，说清"谁做了什么、结果/时间/数字"
  · "importance"：重要度，只取 高 / 中 / 低 三个字之一
    高 = 首飞/载人/深空/里程碑/重大故障；中 = 常规发射、合同订单、型号进展；低 = 科普、花絮、旧闻回顾
- "score"【价值分】：0~100 的整数，表示"这条对航天爱好者值不值得看"。评分标准：
  90~100 = 必看（首飞、载人、深空里程碑、重大故障）
  70~89  = 很值得看（重要任务、大额合同、关键型号进展）
  40~69  = 一般（常规发射、科普、行业动态）
  10~39  = 信息量低（花絮、闲聊、转发的感想）
  0~9    = 垃圾/广告/纯噪声
  只按内容本身打分，**不要**考虑任何用户偏好；不是新闻也照打（给对应的低分）。"""

# 标签规则：同样两条 prompt 共用
_TAG_RULES = """- "tags"：3~6 个简体中文标签，必须含一个领域标签，尽量含一个对象标签：
  · 领域标签只能取：发射 / 重大任务 / 任务 / 商业 / 科普 / 故障 / 历史 / 其他，写法 "领域:发射"
    （重大任务 = 星舰首次/载人/深空探测这类里程碑；不是航天内容的写 "领域:其他"）
  · 对象标签写具体型号/任务/机构，如 星舰、猎鹰9、长征五号、Artemis，写法 "对象:星舰"
  · 其余标签写事件、机构、型号等关键词，每个不超过 8 个字，写法 "机构:SpaceX"
"""

ANALYSIS_PROMPT = (
    "你是航天内容处理助手。下面是一条 X（推特）推文原文，请一次完成下面这些事。\n"
    "只输出一个 JSON 对象：不要 Markdown 代码块，不要任何解释、前后缀或多余文字。\n\n"
    "- \"zh\"：把原文翻译成简体中文。保留 Falcon 9、Starship 等专有名词原文；链接、@、# 原样保留；\n"
    "  不要加\"译文：\"之类的前缀，也不要加引号。\n"
    + _NEWS_RULES + "\n"
    + _TAG_RULES + "\n"
    "输出格式（严格 JSON）：\n"
    '{"zh":"译文","is_news":true,"not_repeat":true,"repeat_of":"","not_comment":true,"not_sensitive":true,'
    '"title":"星舰第 11 次试飞成功","summary":"SpaceX 完成星舰第 11 次试飞，飞船再入后在海上溅落。",'
    '"importance":"高","score":92,"tags":["领域:发射","对象:星舰","机构:SpaceX"]}\n\n'
    "{known}{source}原文：\n{raw}"
)

# 存量条目补标用：已经有译文了，只补判断 + 四盏灯 + 三件套 + 价值分 + 标签，不重新翻译（也不覆盖人工改过的译文）
META_PROMPT = (
    "你是航天内容处理助手。下面是一条**已经翻译过**的航天推文（原文和译文都给你）。\n"
    "不要翻译，只做判断和打标。只输出一个 JSON 对象：不要 Markdown 代码块，不要任何解释或前后缀。\n\n"
    + _NEWS_RULES + "\n"
    + _TAG_RULES + "\n"
    "输出格式（严格 JSON）：\n"
    '{"is_news":true,"not_repeat":true,"repeat_of":"","not_comment":true,"not_sensitive":true,'
    '"title":"星舰第 11 次试飞成功","summary":"……","importance":"高","score":92,'
    '"tags":["领域:发射","对象:星舰"]}\n\n'
    "{known}{source}原文：\n{raw}\n\n译文：\n{zh}"
)


def _fill(template: str, **fields) -> str:
    """把 {raw} / {zh} / {source} / {known} 这类占位符换成真实内容（不用 format，避免 JSON 花括号被吃掉）。"""
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", str(value or ""))
    return out


def _source_head(account: str) -> str:
    """有来源账号时插一行"来源账号：@xx"（官方号的简短通报更容易被判成新闻）。"""
    source = str(account or "").strip()
    return f"来源账号：@{source}\n" if source else ""


def _known_block(known_titles) -> str:
    """把"最近已入库清单"拼成提示词里的一段，用来判 not_repeat（第 2 盏灯）。

    没有清单（关闭时）返回空串，提示词里那一段就消失。
    """
    lines = [str(x).strip() for x in (known_titles or []) if str(x).strip()]
    if not lines:
        return ""
    body = "\n".join(lines)
    return ("【最近已入库清单】（判断 not_repeat 用：和这里某条是同一件事就算重复）\n"
            f"{body}\n\n")


def build_analysis_prompt(text: str, account: str = "", known_titles=None) -> str:
    return _fill(ANALYSIS_PROMPT, known=_known_block(known_titles),
                 source=_source_head(account), raw=str(text or ""))


def build_meta_prompt(text: str, zh: str = "", account: str = "", known_titles=None) -> str:
    return _fill(META_PROMPT, known=_known_block(known_titles),
                 source=_source_head(account), raw=str(text or ""), zh=str(zh or ""))


def _find_json_object(text: str) -> dict:
    """从模型回复里抠出第一个合法 JSON 对象（容忍代码块围栏和前后废话）。"""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    snippet = re.sub(r"\s+", " ", str(text or "")).strip()
    raise ValueError(f"模型输出不是合法 JSON：{snippet or '(空)'}")


def _to_flag(value):
    """把 is_news 归一成 True / False / None（None = 模型没判出来，判分时当未知）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "y", "1", "是", "新闻", "有"):
            return True
        if text in ("false", "no", "n", "0", "否", "不是", "非新闻", "无"):
            return False
    return None


def _clean_tags(value) -> list:
    """标签归一：字符串列表/逗号串都能吃，去空、去重、限长限量。"""
    if isinstance(value, str):
        value = re.split(r"[,，、;；\s]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    tags = []
    for raw in value:
        tag = str(raw or "").strip().strip("\"'").strip()
        if not tag:
            continue
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= 8:
            break
    return tags


def _clean_text(value) -> str:
    """标题/要点这类短文本：压空白、去引号。不截断——长度由提示词约束，不由代码砍。"""
    return re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'“”").strip()


_IMPORTANCE_MAP = {
    "高": "高", "中": "中", "低": "低",
    "high": "高", "medium": "中", "mid": "中", "low": "低",
}


def _clean_importance(value) -> str:
    """重要度归一成 高 / 中 / 低；判不出给空串（judge 不拦）。"""
    key = str(value or "").strip().lower()
    if not key:
        return ""
    if key in _IMPORTANCE_MAP:
        return _IMPORTANCE_MAP[key]
    if "高" in key:
        return "高"
    if "低" in key:
        return "低"
    if "中" in key:
        return "中"
    return ""


def _clean_score(value):
    """AI 价值分归一成 0~100 整数；判不出给 None（看板显示"—"，不参与门控）。"""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().rstrip("分").rstrip("%").strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if number != number:          # NaN
        return None
    return max(0, min(100, int(round(number))))


def _pick(data: dict, *keys):
    """按顺序取第一个"存在"的键（不能用 or 链：模型回 false 会被 or 当成缺失丢掉）。"""
    for key in keys:
        if key in data:
            return data[key]
    return None


def _meta_fields(data: dict) -> dict:
    """四盏灯 + 标题/要点/重要度/价值分 + 标签，完整分析和只补标共用。"""
    return {
        "is_news": _to_flag(_pick(data, "is_news", "新闻")),
        "not_repeat": _to_flag(_pick(data, "not_repeat", "不重复")),
        "repeat_of": _clean_text(_pick(data, "repeat_of", "重复于")),
        "not_comment": _to_flag(_pick(data, "not_comment", "不是评价", "非评价")),
        "not_sensitive": _to_flag(_pick(data, "not_sensitive", "不敏感", "非敏感政治")),
        "title": _clean_text(_pick(data, "title", "标题")),
        "summary": _clean_text(_pick(data, "summary", "要点", "摘要")),
        "importance": _clean_importance(_pick(data, "importance", "重要度")),
        "score": _clean_score(_pick(data, "score", "分数", "价值分")),
        "tags": _clean_tags(_pick(data, "tags", "标签") or []),
    }


def parse_analysis(output: str) -> dict:
    """把模型回复解析成
    {"zh", "is_news", "not_repeat", "repeat_of", "not_comment", "not_sensitive",
     "title", "summary", "importance", "score", "tags"}。

    译文缺失直接抛：调用方会记 translate_error 并重试，绝不把空译文写进库。
    """
    data = _find_json_object(output)
    zh = str(data.get("zh") or data.get("译文") or "").strip()
    if not zh:
        raise ValueError("模型没有返回译文（zh 为空）")
    result = {"zh": zh}
    result.update(_meta_fields(data))
    return result


def parse_meta(output: str) -> dict:
    """解析"只判断不翻译"的回复；四盏灯和标签都拿不到就算失败（宁可重试）。"""
    meta = _meta_fields(_find_json_object(output))
    lights = ("is_news", "not_repeat", "not_comment", "not_sensitive")
    if all(meta[key] is None for key in lights) and not meta["tags"]:
        raise ValueError("模型既没给四盏灯也没给标签")
    return meta


def analyze_item(text: str, account: str = "", known_titles=None) -> dict:
    """一次调用拿齐：译文 + 四盏灯 + 标题/要点/重要度 + 标签。失败向上抛，由调用方记账重试。

    known_titles 是"最近已入库清单"（形如 ["#56 标题", ...]），用来判第 2 盏灯（不重复）。
    """
    if not is_configured():
        raise RuntimeError("模型 API 未配置完整，缺少：" + "、".join(missing_config()))
    return parse_analysis(chat(build_analysis_prompt(text, account, known_titles), temperature=0.3))


def analyze_meta(text: str, zh: str = "", account: str = "", known_titles=None) -> dict:
    """存量条目补标：只判断四盏灯 + 打标，不翻译（也不动已有译文）。失败向上抛。"""
    if not is_configured():
        raise RuntimeError("模型 API 未配置完整，缺少：" + "、".join(missing_config()))
    return parse_meta(chat(build_meta_prompt(text, zh, account, known_titles), temperature=0.0))


# ---------------------------------------------------------------- 打标 / 兜底（失败吞掉）

def build_semantic_prompt(text: str) -> str:
    """老条目兜底打标的提示词（放进函数是为了能被 tools/dump_prompts.py 原样导出）。"""
    return (
        "判断下面的航天新闻属于哪个领域、关键对象是什么。只输出一行中文，格式："
        "领域=<发射/任务/商业/科普/故障/历史/其他>;关键对象=<星舰/猎鹰9/长征/X空 或具体任务>。\n"
        f"新闻：{text}"
    )


def semantic_labels(text: str, log=None) -> list:
    """语义标签（AI）：领域、关键对象。失败返回 []（不中断打分）。

    翻译阶段已经把标签存进 items.ai_tags，这里是**老条目兜底**：
    这些条目是本功能上线前翻译的，库里只有译文没有标签。
    """
    if not is_configured() or not text:
        return []
    try:
        out = chat(build_semantic_prompt(text), temperature=0.0)
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
    title = str(news.get("ai_title") or "").strip()
    summary = str(news.get("ai_summary") or "").strip()
    importance = str(news.get("importance") or "").strip()
    if title:
        base += f"标题：{title}\n"
    base += f"要点：{summary or str(news.get('zh',''))}\n"
    if importance:
        base += f"AI判定的重要度：{importance}（高/中/低）\n"
    base += f"原文：{str(news.get('raw',''))}\n"
    base += "请综合以上维度判断，只输出：有意思 或 没意思。"
    return base


def fallback_decide(news: dict, user_pref: dict, group_pref: dict, log=None) -> str:
    """AI 判断边缘新闻："发"（有意思）或 "丢"（没意思）。出错保守判"发"。"""
    if not is_configured():
        return "发"
    try:
        out = chat(build_fallback_prompt(news, user_pref, group_pref), temperature=0.0)
        return "发" if "有意思" in out else "丢"
    except Exception as e:
        if log:
            log(f"[警告] AI兜底判定失败: {e}")
        return "发"
