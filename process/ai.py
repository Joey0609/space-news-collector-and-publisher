# -*- coding: utf-8 -*-
"""大模型调用：一个协议无关的 chat()，供翻译+新闻判定+标签 / 语义打标 / AI 兜底共用。

协议由 config.MODEL_PROTOCOL 决定：
    openai     POST {base}/chat/completions   走 openai SDK
    responses  POST {base}/responses          走 openai SDK 的 Responses API
    anthropic  POST {base}/messages           走 httpx（不额外引入 anthropic 依赖）

约定：
  - 完整分析（analyze_item）失败要抛（外层记录 translate_error 并累计重试次数）；
  - AI 兜底失败要吞掉并给安全值，不能中断整轮处理。

一次调用完成这些事（analyze_item）：出译文、点四盏灯（是新闻 / 不重复 / 非评价 / 非敏感政治）、
给新闻标题/要点/重要度、打标签。
标签沿用 judge 认识的写法："领域:<发射|重大任务|任务|商业|科普|故障|历史|其他>"、"对象:<星舰/猎鹰9/...>"、
"机构:<SpaceX/...>"；领域/对象的取值与 config_templates/推送规则.toml 的权重、白名单对齐。
"""
import json
import re

import httpx
import config as model_config

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


def _refresh_config():
    global _openai_client
    if model_config.refresh_model_config():
        for key in ("MODEL_API_BASE", "MODEL_API_KEY", "MODEL_NAME", "MODEL_PROTOCOL", "MODEL_TIMEOUT", "MODEL_MAX_TOKENS"):
            globals()[key] = getattr(model_config, key)
        if _openai_client is not None:
            _openai_client.close()
            _openai_client = None


class OutputTruncated(RuntimeError):
    """模型输出被输出上限截断：译文/JSON 都不完整，不能当成功入库。"""


def is_configured() -> bool:
    """必需项（base / name / key）是否都配好。没配就整段跳过，而不是给每条都记一次失败。"""
    _refresh_config()
    return not missing_config()


def config_summary() -> dict:
    """给日志用的安全摘要（不含 key）。"""
    _refresh_config()
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

# 新闻判定 + 四盏灯 + 新闻三件套：完整分析共用的判定规则。
_NEWS_RULES = """- "is_news"【第 1 盏灯】：这条本身是不是一条航天新闻？
  航天新闻、官方公告、任务进展、行业动态、故障通报 → true；
  闲聊、个人日常、情绪转发、广告、抽奖、招聘、无实质信息 → false。
- "not_repeat"【第 2 盏灯】：true = 不是重复内容。
  比较的是具体事实和信息增量，不是账号、关键词、标题相似度或是否属于同一事件。
  只要本条包含此前新闻没有的新事实、新进展或有实质意义的新细节，就判 true，repeat_of 给空串 ""；
  即使主体、任务或事件相同，大部分背景与此前新闻重合，也不能因而判重复。
  新信息包括：新的任务阶段或结果、发射时间/计划变更、新公布的数据或数量、故障原因、修复措施、
  新合同或参与方、新地点，以及原报道未披露的其他事实。无需是重大进展，有真实信息增量即可。
  例如：已报道发射预告，随后报道发射成功或入轨结果 → true；已报道试飞成功，随后披露新的试飞数据 → true。
  只有核心事实已被此前新闻完整覆盖，且没有任何新增事实/进展/实质细节时，才判 false；
  换一种说法、换账号转载、重复配图、感想、宣传口号、观看链接不构成新增事实。
  旧闻回顾（周年纪念、"X 年前的今天"、翻旧图）若只复述已知事实 → false；若披露此前没有的新事实，也判 true。
  【最近已入库清单】可能只有标题，不要凭标题推断旧报道已经包含本条的全部细节；无法确认无信息增量时判 true。
  只有确认重复时才在 "repeat_of" 写对应清单编号（形如 "#56"）；没有对应条目时给空串，不得编造编号。
- "not_comment"【第 3 盏灯】：true = 它自己就是内容（原发报道、官方发布、独立消息）。
  如果它是针对**另一条新闻**的评价、吐槽、转发跟评（"这公司又…"、"这事真离谱"）→ false。
  注意：带引用的推文由程序统一放行（见下文【被引用的推文】），你不用为它特意判什么。
- "not_sensitive"【第 4 盏灯，政治/外交红线】：true = 不涉及敏感政治与外交关系。
  只要沾上下面任何一条就是 false（哪怕它同时也在讲航天）：
  · 国家间的政治/外交冲突：互相谴责、抗议、制裁、断交、军事对峙、领土争端
  · 涉台 / 涉港 / 涉疆 / 涉藏等政治议题，或任何带政治立场的表态
  · 政客或政府发言、选举、政党、意识形态争论、民族宗教敏感话题
  · 战争、军演、导弹试射这类带军事对抗含义的话题
  注意：正常的商业/科研航天活动（SpaceX 发射、中国航天任务、NASA 公告）→ true，不要误杀。
- 每条内容都必须有非空的 "title"，最多 20 个汉字，不带账号名、书名号或引号。
  原文若含清楚、准确、适合直接作标题的标题，优先复用（英文标题译成中文）；没有合适标题、太长或只是口号/链接时，依据正文重写。
  标题只概括核心内容，优先写主体/任务和关键动作或结果；以清理后的 zh 为准并用原文核对，不能臆造。
  忽略问候、宣传口号、邀请提问、直播引导、重复解释、无关背景和纯链接；账号会由发送格式单独附在标题后。
- 仅当 is_news 是 true 时，生成下面两项；is_news 是 false 时 summary 和 importance 给空字符串：
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

# 标签规则：完整分析必须一次生成。
_TAG_RULES = """- "tags"：3~6 个简体中文标签，必须含一个领域标签，尽量含一个对象标签：
  · 领域标签只能取：发射 / 重大任务 / 任务 / 商业 / 科普 / 故障 / 历史 / 其他，写法 "领域:发射"
    （重大任务 = 星舰首次/载人/深空探测这类里程碑；不是航天内容的写 "领域:其他"）
  · 对象标签写具体型号/任务/机构，如 星舰、猎鹰9、长征五号、Artemis，写法 "对象:星舰"
  · 其余标签写事件、机构、型号等关键词，每个不超过 8 个字，写法 "机构:SpaceX"
"""

_BODY_RULES = """正文整合规则（适用于 zh / clean_zh）：
- 正文应把主帖和实际提供的所有引用内容融合成一篇连贯、完整的正文；引用可以在原文中，也可以在【被引用的推文】中。
  不要照搬“文本 + 引用1：（链接）【引用内容1】 + 引用2：（链接）【引用内容2】”的拼接结构，
  不输出“引用1/引用2/被引用内容/译文”等结构标签、包裹引用的括号或引用链接。
- 融合不是概括或摘要：保留各部分全部有效信息、事实、数字、条件、限定语和必要背景，不把多个具体句子压缩成一句要点。
  正文长度以保留信息为准，没有 summary 的字数限制；summary 单独生成，不能拿 summary 替代正文。
- 尽量保留原句的表述、语气和细节：中文通顺的句子直接沿用，英文逐句忠实翻译；只做必要的衔接、语序调整和重复事实合并。
  特殊情况才改写：原句生硬或不通顺、零散字段需成句、指代脱离引用后不明、相同事实重复，或需清除链接和结构噪声。
  不能为使文字简短而删掉有效信息；不能把不同主体的说法混成同一个人的陈述，必要时保留“某机构表示”等归属。
- 只使用本条主帖和实际提供的引用正文。不要从最近已入库清单借用事实，也不要猜测链接里没有提供的内容。
"""

ANALYSIS_PROMPT = (
    "你是航天内容处理助手。下面是一条来源内容原文（推文或完整文章），请一次完成下面这些事。\n"
    "只依据本条原文及实际提供的引用正文；来源账号、历史标题、格式说明都不是事实依据。信息不足时忠实翻译已有文字并判非新闻，不得补造任务、编号或结果。\n"
    "只输出一个 JSON 对象：不要 Markdown 代码块，不要任何解释、前后缀或多余文字。\n\n"
    "- \"zh\"：输出忠实、通顺的简体中文正文。合适的原文/译文表述直接保留；英文翻译成中文，专有名词可保留英文。\n"
    "  如果原文生硬、是直播状态或零散字段，整理成一条自然的新闻正文；保留主体、事实、数字和有意义的时间/地点，不得补造信息。\n"
    "  删除 t.co/短链/追踪链接、直播观看引导、空 location 字段、重复口号、无关背景及 \"Information as of\" 状态时间。\n"
    "  正文不留裸 URL；来源原链接由系统单独保存。不要加\"译文：\"前缀。\n"
    "- \"quoted_zh\"：如果给了【被引用的推文】，把它的正文另译成简体中文放这里供归档；\n"
    "  其中有效信息必须同时融入 zh，不能只放 quoted_zh 而使实际发送的 zh 缺失引用信息；没有引用就给空字符串 \"\"。\n"
    "- 判断时要把【被引用的推文】的内容一起算进去（被引用的那条往往才是新闻本体）。\n"
    + _BODY_RULES + "\n"
    + _NEWS_RULES + "\n"
    + _TAG_RULES + "\n"
    "输出格式（严格 JSON）：字符串仅说明字段含义，不是新闻示例；依据原文重新填写所有值，不得照抄占位内容。\n"
    '{"zh":"整理后的完整译文正文","quoted_zh":"被引用正文的译文（没有引用就给空串）",'
    '"is_news":false,"not_repeat":true,"repeat_of":"","not_comment":true,"not_sensitive":true,'
    '"title":"依据本条原文生成的标题","summary":"","importance":"","score":0,"tags":["领域:其他"]}\n\n'
    "{known}{source}{quoted}原文：\n{raw}"
)

def _fill(template: str, **fields) -> str:
    """把 {raw} / {zh} / {source} / {known} / {quoted} 这类占位符换成真实内容
    （不用 format，避免 JSON 花括号被吃掉）。"""
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", str(value or ""))
    return out


def _quoted_block(quoted_account: str, quoted_raw: str) -> str:
    """引用推文里【被引用的那条】→ 提示词里的一段。

    为什么要有：引用者常常只写一句感想，真正的新闻在被引用的那条上。之前这里的内容
    完全没进提示词，模型只看到引用者那句话，判定和翻译都会跑偏。

    没有引用（被引用正文为空）时返回空串，`{quoted}` 占位符随之整段消失。
    """
    text = str(quoted_raw or "").strip()
    if not text:
        return ""
    who = f"@{quoted_account} " if str(quoted_account or "").strip() else ""
    return f"【被引用的推文】{who}：\n{text}\n\n"


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
    return ("【最近已入库清单】（仅供事实对比：同一事件有新增事实/进展/实质细节就不重复；标题相似不足以判重复）\n"
            f"{body}\n\n")


def build_analysis_prompt(text: str, account: str = "", known_titles=None,
                          quoted_account: str = "", quoted_raw: str = "") -> str:
    return _fill(ANALYSIS_PROMPT, known=_known_block(known_titles),
                 source=_source_head(account),
                 quoted=_quoted_block(quoted_account, quoted_raw),
                 raw=str(text or ""))


def _find_json_object(text: str) -> dict:
    """从模型回复里抠出第一个合法 JSON 对象（容忍代码块围栏和前后废话）。"""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            # 有些模型会把个别 JSON 键写成裸键或多一个右引号（如
            # `quoted_zh:"",is_news":false`）。只在字符串外修复对象键，
            # 避免误改正文中的逗号、冒号或字段名。
            repaired = _quote_unquoted_json_keys(text[start:])
            try:
                obj, _ = decoder.raw_decode(repaired)
            except ValueError:
                start = text.find("{", start + 1)
                continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    snippet = re.sub(r"\s+", " ", str(text or "")).strip()
    raise ValueError(f"模型输出不是合法 JSON：{snippet or '(空)'}")


def _quote_unquoted_json_keys(text: str) -> str:
    """修复 JSON 对象里裸键或键尾多余引号，不触碰字符串值。"""
    out = []
    i = 0
    in_string = False
    escaped = False
    expect_key = False
    while i < len(text):
        char = text[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            expect_key = False
            out.append(char)
            i += 1
            continue
        if char in "{,":
            expect_key = True
            out.append(char)
            i += 1
            continue
        if expect_key and char.isspace():
            out.append(char)
            i += 1
            continue
        if expect_key and (char.isalpha() or char == "_"):
            end = i + 1
            while end < len(text) and (text[end].isalnum() or text[end] == "_"):
                end += 1
            after = end
            while after < len(text) and text[after].isspace():
                after += 1
            extra_quote = after < len(text) and text[after] == '"'
            colon_at = after + 1 if extra_quote else after
            while colon_at < len(text) and text[colon_at].isspace():
                colon_at += 1
            if colon_at < len(text) and text[colon_at] == ":":
                out.extend(('"', text[i:end], '"'))
                i = after + 1 if extra_quote else end
                expect_key = False
                continue
        if char == ":":
            expect_key = False
        out.append(char)
        i += 1
    return "".join(out)


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


def clean_zh_body(value) -> str:
    """清理正文里的短链、直播引导和状态字段，不改写其余事实内容。"""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)(?:https?://|www\.)\S+", "", text)
    text = re.sub(r"(?i)\bInformation\s+as\s+of\s+\d{4}-\d{1,2}-\d{1,2}"
                  r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*UTC)?\b", "", text)
    text = re.sub(r"(?i)(?:▶️\s*)?(?:直播观看|观看直播|收看直播|watch\s+live|live\s+stream)"
                  r"\s*[:：]?", "", text)
    text = re.sub(r"(?im)(^|[\s，,。；;])(?:location|位置)\s*[:：]\s*(?=$|[\s，,。；;])",
                  r"\1", text)
    text = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b(?=\s*(?:location\s*[:：]|$))",
                  "", text, flags=re.I)
    text = re.sub(r"(?:→|->)+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [re.sub(r"\s+", " ", line).strip(" \t,，;；→-") for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\s+([，。；：！？,.!?])", r"\1", text)
    return text.strip()


def _ensure_title(value, body: str) -> str:
    """Enforce a non-empty title no longer than 20 characters."""
    title = _clean_text(value)
    if not title:
        content = clean_zh_body(body)
        title = re.split(r"[。！？.!?\n]", content, maxsplit=1)[0].strip()
    title = re.sub(r"\s+", "", title).strip("，。；：、,.!?！？-— ")
    return title[:20] or "航天动态"


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


def _analysis_fields(data: dict) -> dict:
    """Normalize the required full-analysis metadata returned with the translation."""
    repeat_of = _clean_text(_pick(data, "repeat_of", "重复于"))
    not_repeat = _to_flag(_pick(data, "not_repeat", "不重复"))
    # 按提示词契约，只有重复时才填写 repeat_of；两者冲突时以明确的重复编号为准。
    if repeat_of:
        not_repeat = False
    return {
        "is_news": _to_flag(_pick(data, "is_news", "新闻")),
        "not_repeat": not_repeat,
        "repeat_of": repeat_of,
        "not_comment": _to_flag(_pick(data, "not_comment", "不是评价", "非评价")),
        "not_sensitive": _to_flag(_pick(data, "not_sensitive", "不敏感", "非敏感政治")),
        "title": _clean_text(_pick(data, "title", "标题")),
        "summary": _clean_text(_pick(data, "summary", "要点", "摘要")),
        "importance": _clean_importance(_pick(data, "importance", "重要度")),
        "score": _clean_score(_pick(data, "score", "分数", "价值分")),
        "tags": _clean_tags(_pick(data, "tags", "标签") or []),
        # 被引用推文正文的译文；没有引用时模型会给空串
        "quoted_zh": clean_zh_body(_pick(data, "quoted_zh", "引用译文", "被引用译文")),
    }


def parse_analysis(output: str) -> dict:
    """把模型回复解析成
    {"zh", "is_news", "not_repeat", "repeat_of", "not_comment", "not_sensitive",
     "title", "summary", "importance", "score", "tags"}。

    译文缺失直接抛：调用方会记 translate_error 并重试，绝不把空译文写进库。
    """
    data = _find_json_object(output)
    zh = clean_zh_body(data.get("zh") or data.get("译文") or "")
    if not zh:
        raise ValueError("模型没有返回译文（zh 为空）")
    if not _clean_text(_pick(data, "title", "标题")):
        raise ValueError("完整分析缺少 AI 标题")
    result = {"zh": zh}
    result.update(_analysis_fields(data))
    lights = ("is_news", "not_repeat", "not_comment", "not_sensitive")
    missing_lights = [key for key in lights if result[key] is None]
    if missing_lights:
        raise ValueError("完整分析缺少必需的灯: " + ", ".join(missing_lights))
    if not result["tags"]:
        raise ValueError("完整分析缺少标签")
    if result["score"] is None:
        raise ValueError("完整分析缺少 AI 价值分")
    if result["is_news"] and not result["summary"]:
        raise ValueError("完整分析缺少新闻要点")
    if result["is_news"] and result["importance"] not in {"高", "中", "低"}:
        raise ValueError("完整分析缺少有效重要度")
    result["title"] = _ensure_title(result.get("title"), zh)
    return result


def validate_analysis_grounding(result: dict, text: str, quoted_raw: str = "") -> None:
    """拦截已确认的示例污染；这是结果校验，不以关键词筛掉原文。"""
    source = re.sub(r"https?://\S+", "", str(text or "") + "\n" + str(quoted_raw or ""))
    output = "\n".join(str(result.get(key) or "") for key in ("zh", "quoted_zh", "title", "summary"))
    starship = r"星[舰艦]|\bstar\s*ship\b"
    if re.search(starship, output, re.I) and not re.search(starship + r"|\bship\s*\d{2,3}\b|スターシップ", source, re.I):
        raise ValueError("CONTENT_UNGROUNDED：分析补造星舰主体，原文及引用正文未提供；请忠实翻译，不借用格式示例或历史清单")
    flight11 = r"(?:第\s*(?:11|十一)\s*次\s*(?:试飞|飞行)|\bflight\s*[-#]?\s*11\b)"
    source11 = flight11 + r"|\b(?:11th|eleventh)\s+(?:test\s+)?flight\b|\bflight\s+eleven\b|\bIFT\s*[-#]?\s*11\b|11回目"
    if re.search(flight11, output, re.I) and not re.search(source11, source, re.I):
        raise ValueError("CONTENT_UNGROUNDED：原文未提供第11次试飞编号，分析不得补造")
    if result.get("title") == "依据本条原文生成的标题" or result.get("zh") == "整理后的完整译文正文":
        raise ValueError("CONTENT_UNGROUNDED：分析照抄格式占位内容")


def analyze_item(text: str, account: str = "", known_titles=None,
                 quoted_account: str = "", quoted_raw: str = "", repair_hint: str = "") -> dict:
    """一次调用拿齐：译文 + 四盏灯 + 标题/要点/重要度 + 标签。失败向上抛，由调用方记账重试。

    known_titles 是"最近已入库清单"（形如 ["#56 标题", ...]），用来判第 2 盏灯（不重复）。
    quoted_account / quoted_raw 是引用推文里【被引用的那条】，会拼成提示词里的
    【被引用的推文】一段，并让模型回 quoted_zh。
    """
    if not is_configured():
        raise RuntimeError("模型 API 未配置完整，缺少：" + "、".join(missing_config()))
    prompt = build_analysis_prompt(text, account, known_titles, quoted_account, quoted_raw)
    if repair_hint:
        prompt += (
            "\n\n【格式修复要求】上一次完整分析未通过校验：" + str(repair_hint) + "。"
            "请重新分析同一条原文并只返回一个语法正确的 JSON 对象，不要加 Markdown 代码围栏或解释。"
            "每个字段名和每个字符串值都必须使用英文双引号；布尔值只能写 true 或 false，不要给键名多加或漏掉引号。"
            "所有字段都必须出现，特别是 not_repeat 必须是 true 或 false，不能省略、不能填文字说明。"
            "字段至少包括 zh、is_news、not_repeat、repeat_of、not_comment、not_sensitive、title、"
            "summary、importance、score、tags；不要把重复编号填进 not_repeat，编号只写在 repeat_of。"
            '严格照此语法形状输出：{"zh":"...","quoted_zh":"","is_news":true,'
            '"not_repeat":true,"repeat_of":"","not_comment":true,"not_sensitive":true,'
            '"title":"...","summary":"...","importance":"中","score":50,"tags":["领域:其他"]}。'
        )
    result = parse_analysis(chat(prompt, temperature=0.3))
    validate_analysis_grounding(result, text, quoted_raw)
    if str(quoted_raw or "").strip() and not result["quoted_zh"]:
        raise ValueError("完整分析缺少引用译文")
    return result


# ---------------------------------------------------------------- AI 兜底（失败吞掉）


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
