# -*- coding: utf-8 -*-
"""hx_prefs.py · 用自然语言改推送设置/内容偏好（私聊里对麦麦说即可）
================================================================
用户说："以后 12 点到 2 点推" / "一次发 2 条" / "少发猎鹰" / "别发某账号" → 解析 → 落盘。

落盘位置（都由 cfg 提供路径，cfg = 插件实例）：
    schedule  → 配置/推送偏好_<QQ>.toml   [push]        （推送方式）
    scoring   → 配置/推送规则_<QQ>.toml   [weights]/[threshold]/[allowlist]
    preference→ 配置/偏好_<QQ>.toml       [like]/[dislike]/[exclude]

设计要点：
  - 先做"像不像在说设置"的粗判（SETTING_TOKENS），避免每句话都调 LLM、也避免劫持日常聊天
  - 解析交给 LLM（任意词都认：二次元/DDLC/广告/某账号都行），返回 JSON
  - 落盘前逐字段校验（时段格式、数值范围），非法值直接丢弃，不让脏配置进文件
"""
import json
import re

if __package__ in (None, ""):
    from hx_llm import llm_text
    from hx_paths import BUILTIN_PUSH_DEFAULTS
    from hx_toml import read_toml, write_toml
    from hx_util import add_words, as_bool, to_list
else:
    from .hx_llm import llm_text
    from .hx_paths import BUILTIN_PUSH_DEFAULTS
    from .hx_toml import read_toml, write_toml
    from .hx_util import add_words, as_bool, to_list

# 粗判关键词：句子命中任一个才可能是在设置推送（宁可漏判一点，也不劫持聊天）
SETTING_TOKENS = [
    "推送", "发新闻", "别推", "不发", "少发", "多发", "时段", "几点",
    "每次", "每隔", "频率", "带图", "带不带图", "几条", "一次", "每间隔", "每天", "推送设置",
    "判定", "阈值", "权重", "发射", "任务", "科普", "历史", "商业", "故障", "爱看", "少来", "多来", "更喜欢",
]

SCHEDULE_KEYS = set(BUILTIN_PUSH_DEFAULTS.keys())          # 推送方式（[push]）
SCORE_WEIGHT_KEYS = {"发射", "任务", "重大任务", "科普", "历史", "商业", "故障", "带图视频", "重复旧闻",
                     "重要高", "重要中", "重要低"}
SCORE_THRESHOLD_KEYS = {"高阈值", "低阈值"}
SCORE_ALLOW_KEYS = {"可发送领域", "可发送账号", "可发送关键词"}
PREF_LIKE_KEYS = {"喜欢账号", "喜欢领域", "喜欢关键词"}
PREF_DISLIKE_KEYS = {"不喜欢领域", "不喜欢关键词"}


def looks_like_push_pref(text: str) -> bool:
    """粗判是否是"设置推送相关配置"的指令（避免每次都调 LLM）。"""
    return any(tok in text for tok in SETTING_TOKENS)


async def parse_push_pref(text: str, context: str = "", logger=None) -> dict:
    """用 LLM 理解自然语言里的"推送/内容偏好"意图，返回

        {"action": "set"|"none"|"ask", "schedule": {...}, "scoring": {...}, "preference": {...}}
    """
    prompt = (
        "你是航小麦配置解析器。用自然语言理解用户这句话里的'推送/内容偏好'，任意词都认（二次元、DDLC、广告、某账号都行）。\n"
        "结合上下文最近几句聊天，判断这句是不是在针对'推送/内容'做设置。\n"
        "输出JSON：{\"action\":\"set\",\"schedule\":{},\"scoring\":{},\"preference\":{}}。\n"
        "- schedule 推送方式：新闻推送(bool)、活跃时段(\"HH:MM-HH:MM\")、间隔最小分钟(int)、间隔最大分钟(int)、"
        "一次发几条(int)、带图(bool)、一天最多发几次(int)\n"
        "- scoring 评分/可发送内容：高阈值(int)、低阈值(int)、发射/任务/重大任务/科普/历史/商业/故障/带图视频/重复旧闻/"
        "重要高/重要中/重要低(int权重，重要度是 AI 给这条新闻判的高/中/低)、"
        "可发送领域/可发送账号/可发送关键词(字符串数组，'可以发送的内容=X,y,z'即这些)\n"
        "- preference 内容偏好（任意词列表）：\n"
        "   多发(list)：爱看/喜欢/多给我发 X\n"
        "   少发(list)：少发/别太多/少来点 X\n"
        "   排除(list)：别发/不要/不要再发 X\n"
        "每段只放用户提到的字段，没提到的别出现。只有明确'与配置/内容偏好完全无关'才输出 {\"action\":\"none\"}；"
        "若用户明显在说内容/推送偏好但你不确定具体改什么，输出 {\"action\":\"ask\",\"hint\":\"你想改推送偏好吗？比如'少发二次元'/\"别发某某账号\"\"}。\n"
        "只输出一个JSON对象，不要任何其他文字、不要代码块。\n"
    )
    if context.strip():
        prompt += f"\n最近聊天上下文：\n{context.strip()[-1200:]}\n"
    prompt += f"\n用户当前的这句话：{text}"

    raw = llm_text(prompt, temperature=0.0, logger=logger).strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"action": "none"}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"action": "none"}
    if not isinstance(data, dict):
        return {"action": "none"}

    action = data.get("action", "none")
    out = {"action": action}
    if action == "ask":
        out["hint"] = str(data.get("hint") or "")
    for sec in ("schedule", "scoring", "preference"):
        v = data.get(sec)
        out[sec] = v if isinstance(v, dict) else {}
    return out


# ────────────────────────── 落盘（都带校验）──────────────────────────

def apply_setting(cfg, user_id: str, parsed: dict) -> dict:
    """把解析出的三段分别写进对应的按用户配置，返回改了哪些。"""
    changed = {}
    sched = parsed.get("schedule") or {}
    if sched:
        apply_schedule(cfg, user_id, sched)
        changed["推送方式"] = list(sched)
    scoring = parsed.get("scoring") or {}
    if scoring:
        apply_scoring(cfg, user_id, scoring)
        changed["评分"] = list(scoring)
    pref = parsed.get("preference") or {}
    if pref:
        apply_preference(cfg, user_id, pref)
        changed["偏好"] = list(pref)
    return changed


def apply_schedule(cfg, user_id: str, fields: dict) -> None:
    """写 推送偏好_<QQ>.toml 的 [push]（带校验，防非法值落盘）。"""
    path = cfg._user_pref_path(user_id)
    new = dict(cfg._read_toml(path).get("push", {}))
    for k, v in fields.items():
        if k not in SCHEDULE_KEYS or v is None or v == "":
            continue
        if k == "活跃时段":
            w = str(v).strip()
            if not re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", w):
                continue  # 格式不对，不写
            s_, e_ = w.split("-")
            if s_ == e_:
                continue  # 零点/畸形窗，跳过
            new[k] = w
        elif k in ("间隔最小分钟", "间隔最大分钟", "一次发几条", "一天最多发几次"):
            try:
                n = int(v)
                new[k] = max(0, n)
            except Exception:
                continue
        elif k in ("新闻推送", "带图"):
            new[k] = as_bool(v)
    write_toml(path, {"push": new})


def apply_scoring(cfg, user_id: str, fields: dict) -> None:
    """写 推送规则_<QQ>.toml 的 [weights]/[threshold]/[allowlist]（带校验/钳制）。"""
    path = cfg._user_scoring_path(user_id)
    existing = cfg._read_toml(path)
    weights = dict(existing.get("weights", {}))
    threshold = dict(existing.get("threshold", {}))
    allow = dict(existing.get("allowlist", {}))
    for k, v in fields.items():
        if v is None or v == "":
            continue
        if k in SCORE_WEIGHT_KEYS:
            try:
                weights[k] = int(v)
            except Exception:
                continue
        elif k in SCORE_THRESHOLD_KEYS:
            try:
                threshold[k] = max(0, min(200, int(v)))
            except Exception:
                continue
        elif k in SCORE_ALLOW_KEYS:
            if isinstance(v, list):
                allow[k] = [str(x) for x in v if str(x).strip()]
            elif isinstance(v, str):
                allow[k] = [x.strip() for x in v.split(",") if x.strip()]
    tables = {}
    if weights:
        tables["weights"] = weights
    if threshold:
        tables["threshold"] = threshold
    if allow:
        tables["allowlist"] = allow
    if tables:
        write_toml(path, tables)


def apply_preference(cfg, user_id: str, fields: dict) -> None:
    """写 偏好_<QQ>.toml：多发→[like]喜欢关键词(+15)、少发→[dislike]不喜欢关键词(-15)、
    排除→[exclude]排除(硬丢)。任意词都支持（不限定航天）。"""
    path = cfg._pref_path(user_id)
    existing = cfg._read_toml(path)
    like = dict(existing.get("like", {}))
    dislike = dict(existing.get("dislike", {}))
    exclude = dict(existing.get("exclude", {}))
    if to_list(fields.get("多发")):
        add_words(like, "喜欢关键词", to_list(fields.get("多发")))
    if to_list(fields.get("少发")):
        add_words(dislike, "不喜欢关键词", to_list(fields.get("少发")))
    if to_list(fields.get("排除")):
        add_words(exclude, "排除关键词", to_list(fields.get("排除")))
    # 兼容模型直接给 喜欢/不喜欢 列表
    for k in PREF_LIKE_KEYS:
        if fields.get(k):
            like[k] = to_list(fields[k])
    for k in PREF_DISLIKE_KEYS:
        if fields.get(k):
            dislike[k] = to_list(fields[k])
    tables = {}
    if like:
        tables["like"] = like
    if dislike:
        tables["dislike"] = dislike
    if exclude:
        tables["exclude"] = exclude
    if tables:
        write_toml(path, tables)


async def build_setting_reply(parsed: dict, logger=None) -> str:
    """生成一句自然、具体的确认话（失败退回模板话）。"""
    desc = []
    naming = {"schedule": "推送方式", "scoring": "评分", "preference": "偏好"}
    for sec in ("schedule", "scoring", "preference"):
        fields = parsed.get(sec) or {}
        if fields:
            desc.append(f"{naming[sec]}=" + "、".join(f"{k}={v}" for k, v in fields.items()))
    if not desc:
        return ""
    combined = "；".join(desc)
    reply = llm_text(
        "你是麦麦，刚接收了用户对\u2018航天内容推送\u2019的修改。"
        "请用一句简洁自然、有人味的话确认，让用户知道以后会照办，"
        "要提到具体改动点，不要说套话、不要长篇。\n"
        f"改动：{combined}\n只输出你确认的那一句话。",
        temperature=0.7, logger=logger,
    )
    return reply or f"好嘞，我记下了：{combined}，之后推送就这么来。"
