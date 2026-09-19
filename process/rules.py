# -*- coding: utf-8 -*-
"""打标打分（模块2 的规则部分）：机械标签、加权打分、判定、白名单/排除。

判定链路（judge.py 调用）：
    排除列表命中     -> 丢（硬丢）
    白名单不允许     -> 丢
    分数 >= 高阈值   -> 发
    分数 <= 低阈值   -> 丢
    中间             -> AI兜底（交给模块3）
"""
import json

# 标签类别 → 参与打分的关键词（AI 语义标签与关键词兜底共用）
CAT_KEYWORDS = {
    "发射": ["发射"],
    "任务": ["任务"],
    "重大任务": ["重大任务"],
    "科普": ["科普"],
    "历史": ["历史"],
    "商业": ["商业"],
    "故障": ["故障"],
}

# 关键词兜底：规则文件里的关键词表名 → 打分类别
KEYWORD_CATEGORY_MAP = {"发射关键词": "发射", "商业关键词": "商业", "故障关键词": "故障"}

PREF_BONUS = 15   # 用户偏好(喜欢/不喜欢)每条加减的分值
PREF_PENALTY = -15


def item_images(item: dict) -> list:
    images = item.get("images")
    if isinstance(images, list):
        return [str(x) for x in images if str(x).strip()]
    old = str(item.get("image", "") or "")
    return [old] if old else []


def ai_tags(item: dict) -> list:
    """取翻译阶段存下的 AI 标签（items.ai_tags 是 JSON 数组，也接受已经是 list 的情况）。"""
    raw = item.get("ai_tags")
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


def news_flag(value):
    """把 is_news 归一成 True / False / None（None = 未知，不拦）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "是", "新闻"):
            return True
        if text in ("0", "false", "no", "否", "不是", "非新闻"):
            return False
    return None


def is_non_news(item: dict) -> bool:
    """模型明确判为"不是新闻"（闲聊/广告/无实质信息）→ 该条直接丢。

    未知（None，例如老条目或模型没返回）一律不拦，交给后面的打分与白名单。
    """
    return news_flag(item.get("is_news")) is False


# 四盏灯：(items 列, 红灯时的说法)
LIGHT_FIELDS = (
    ("is_news", "非新闻"),
    ("not_repeat", "重复"),
    ("not_comment", "只是评价"),
    ("not_sensitive", "敏感政治/外交"),
)


def has_quote(item: dict) -> bool:
    """这条推文是不是"带引用（quote）"。

    以 quoted_url 为准：采集时只要拿到了被引用推文对象就一定写了它，
    所以「纯图引用」（被引用那条没有文字）也算引用。

    ⚠️ 这个判断被 process/translate_db.py 用来给第 3 盏灯（not_comment）强制放行，
    改字段名要同步那边。
    """
    return bool(str(item.get("quoted_url") or "").strip())


def lights(item: dict) -> list:
    """四盏灯：是新闻 / 不重复 / 非评价 / 非敏感政治。每盏 True(绿) / False(红) / None(灰)。"""
    return [news_flag(item.get(field)) for field, _ in LIGHT_FIELDS]


def light_state(item: dict) -> tuple:
    """返回 ("green"|"red"|"unknown", 说明)。

    任一红 → ("red", "重复、只是评价" 这种说明)；四盏全绿 → ("green", "")；有灰没红 → ("unknown", "")。
    """
    states = lights(item)
    reds = [label for value, (_, label) in zip(states, LIGHT_FIELDS) if value is False]
    if reds:
        return "red", "、".join(reds)
    if all(value is True for value in states):
        return "green", ""
    return "unknown", ""


def importance_label(item: dict) -> str:
    """AI 重要度 → 标签 "重要:高|中|低"；没判出来给空串（不参与打分）。"""
    value = str(item.get("importance") or "").strip()
    return f"重要:{value}" if value in ("高", "中", "低") else ""


def score_floor_ok(ai_score, floor) -> bool:
    """AI 价值分下限门控（[gate] AI分下限）：

    floor <= 0（默认）→ 不看分，一律通过；模型没给分也不拦；否则要求 ai_score >= floor。
    """
    try:
        floor = int(floor or 0)
    except (TypeError, ValueError):
        floor = 0
    if floor <= 0 or ai_score is None:
        return True
    try:
        return int(ai_score) >= floor
    except (TypeError, ValueError):
        return True


def mechanical_labels(item: dict) -> list:
    """机械标签（规则）：来源、有无图。"""
    labels = []
    src_account = item.get("orig_account") or item.get("account") or ""
    if src_account:
        labels.append(f"来源:{src_account}")
    if item_images(item):
        labels.append("带图")
    return labels


def score_with_rules(labels: list, item: dict, rules: dict, pref: dict) -> tuple[int, str]:
    """按规则+偏好打分，返回 (总分, 说明)。"""
    weight = rules.get("weights", {})
    score = 0
    reasons = []
    txt = (item.get("raw", "") + item.get("zh", "")).lower()

    # 1) 标签加分（AI语义标签 与 机械标签 命中权重）
    for lab in labels:
        for cat, kw in CAT_KEYWORDS.items():
            if any(k in lab for k in kw):
                val = weight.get(cat, 0)
                score += val
                reasons.append(f"{cat}:{val:+d}")

    # 2) 带图加分
    if item_images(item):
        val = weight.get("带图视频", 0)
        score += val
        reasons.append(f"带图:{val:+d}")

    # 2b) AI 重要度加分（可选）：只有 [weights] 里配了 "重要高"/"重要中"/"重要低" 才生效，
    #     没配就是 0，不改变原有打分行为。
    importance = str(item.get("importance") or "").strip()
    if importance in ("高", "中", "低"):
        key = f"重要{importance}"
        val = weight.get(key, 0)
        if val:
            score += val
            reasons.append(f"{key}:{val:+d}")

    # 3) 关键词兜底（若AI没给语义标签，用关键词粗判领域）
    if not any("领域:" in l for l in labels):
        for cat, words in rules.get("keywords", {}).items():
            rule_cat = KEYWORD_CATEGORY_MAP.get(cat)
            if rule_cat and any(w.lower() in txt for w in words):
                val = weight.get(rule_cat, 0)
                if val:
                    score += val
                    reasons.append(f"键:{rule_cat}:{val:+d}")

    # 4) 用户偏好加减分（喜欢/不喜欢 账号、领域、关键词）
    account = str(item.get("account", "") or "")
    lab_text = " ".join(labels)
    for a in pref.get("like", {}).get("喜欢账号", []):
        if str(a).strip() and str(a).lower() in account.lower():
            score += PREF_BONUS
            reasons.append(f"偏好+账号:{a}")
    for d in pref.get("like", {}).get("喜欢领域", []):
        if str(d).strip() and str(d) in lab_text:
            score += PREF_BONUS
            reasons.append(f"偏好+领域:{d}")
    for kw in pref.get("like", {}).get("喜欢关键词", []):
        if kw.lower() in txt:
            score += PREF_BONUS
            reasons.append(f"偏好+:{kw}")
    for d in pref.get("dislike", {}).get("不喜欢领域", []):
        if str(d).strip() and str(d) in lab_text:
            score += PREF_PENALTY
            reasons.append(f"偏好-领域:{d}")
    for kw in pref.get("dislike", {}).get("不喜欢关键词", []):
        if kw.lower() in txt:
            score += PREF_PENALTY
            reasons.append(f"偏好-:{kw}")

    return score, ",".join(reasons)


def decide(score: int, high: int, low: int) -> str:
    """判定：>=高 发；<=低 丢；之间 AI兜底。"""
    if score >= high:
        return "发"
    if score <= low:
        return "丢"
    return "AI兜底"


def in_allowlist(item: dict, labels: list, allow: dict) -> bool:
    """可发送内容白名单：命中 领域/账号/关键词 任一即允许；三者都空 = 不限。

    领域不在列表（如动漫/政经/军演等被判'其他'）→ False（丢）；AI 没判出领域则宽松放行。
    """
    domains = [str(x) for x in (allow.get("可发送领域", []) or [])]
    accounts = [str(x).lower() for x in (allow.get("可发送账号", []) or [])]
    kws = [str(x).lower() for x in (allow.get("可发送关键词", []) or [])]
    account = str(item.get("account", "") or "").lower()
    txt = (str(item.get("raw", "") or "") + " " + str(item.get("zh", "") or "")).lower()

    if not domains and not accounts and not kws:
        return True  # 白名单为空 = 不限
    if accounts and account in accounts:
        return True
    if kws and any(k in txt for k in kws):
        return True
    if not domains:
        return True

    found = ""
    for l in labels:
        if l.startswith("领域:"):
            found = l.split(":", 1)[1].strip()
            break
    if not found:
        return True  # AI 没能判领域 → 宽松放行（兜底交给打分）
    return found in domains


def hit_exclude(item: dict, labels: list, pref: dict) -> bool:
    """排除列表（聊天"别发/不要 X"）：命中账号/关键词/领域 → 硬丢。"""
    exc = pref.get("exclude", {}) or {}
    kws = [str(x).lower() for x in (exc.get("排除关键词", []) or [])]
    accts = [str(x).lower() for x in (exc.get("排除账号", []) or [])]
    doms = [str(x) for x in (exc.get("排除领域", []) or [])]
    account = str(item.get("account", "") or "").lower()
    txt = (str(item.get("raw", "") or "") + " " + str(item.get("zh", "") or "")).lower()
    if accts and account in accts:
        return True
    if kws and any(k in txt for k in kws):
        return True
    if doms:
        lab = " ".join(labels)
        if any(d in lab for d in doms):
            return True
    return False
