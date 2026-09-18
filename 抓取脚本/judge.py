# -*- coding: utf-8 -*-
"""
航小麦 · 打标打分器 (模块2 · judge.py)
=======================================
作用：读模块1输出的新闻(news json)，混合打标(机械=规则/语义=AI)，
      按规则+偏好打分，判定"该不该发/发给谁/分数"。

输入：抓取脚本/output/new_news_*.json（模块1输出）
输出：输出判定结果到 output/judged_*.json，并打印摘要

运行：
    <venv>/Scripts/python.exe judge.py [新闻json路径]
    （不传则自动找最新的 new_news_*.json）

依赖：openai（AI打标）；tomllib（读规则/偏好）
配置：规则文件 配置/推送规则.toml；偏好文件 配置/偏好_<qq>.toml
"""

import asyncio
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
SEED_DIR = Path(__file__).parent
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = SEED_DIR / "output"
CONFIG_DIR = ROOT / "配置"
LOG_FILE = LOG_DIR / "judge.log"
RULE_FILE = CONFIG_DIR / "推送规则.toml"

load_dotenv(SEED_DIR / ".env", encoding="utf-8")
# 避免 Windows GBK 控制台打印中文崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "").strip()
VISION_MODEL = "deepseek-chat"  # 语义打标/AI兜底用纯文本模型（不用 vision，避免对纯文本返空）

# 默认目标（来自规则文件）
DEFAULT_TARGET_PRIVATE = "YOUR_QQ_ID"
DEFAULT_TARGET_GROUP = "YOUR_GROUP_ID_1"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import tomllib
        return tomllib.load(open(path, "rb"))
    except Exception:
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base 的同名键；嵌套 dict 逐层合并。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_latest_news() -> Path:
    files = sorted(glob.glob(str(OUTPUT_DIR / "new_news_*.json")))
    if not files:
        log("[错误] 找不到 new_news_*.json，先运行模块1抓取")
        sys.exit(1)
    return Path(files[-1])


def _mechanical_labels(item: dict) -> list:
    """机械标签（规则）：来源、有无图/视频、是否疑似重复。"""
    labels = []
    src_account = item.get("orig_account") or item.get("account") or ""
    if src_account:
        labels.append(f"来源:{src_account}")
    if item.get("images") or item.get("image"):
        labels.append("带图")
    return labels


def _ai_semantic_labels(text: str) -> list:
    """语义标签（AI）：领域、关键对象。用 flash-vision-exp 判断。"""
    if not DEEPSEEK_KEY or not text:
        return []
    try:
        client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
        prompt = (
            "判断下面的航天新闻属于哪个领域、关键对象是什么。只输出一行中文，格式："
            "领域=<发射/任务/商业/科普/故障/历史/其他>;关键对象=<星舰/猎鹰9/长征/X空 或具体任务>。\n"
            f"新闻：{text[:300]}"
        )
        resp = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        out = resp.choices[0].message.content.strip().replace("领域=", "").replace("关键对象=", "")
        parts = out.split(";")
        labels = []
        if len(parts) >= 1:
            labels.append(f"领域:{parts[0].strip()}")
        if len(parts) >= 2:
            labels.append(f"对象:{parts[1].strip()}")
        return labels
    except Exception as e:
        log(f"[警告] AI打标失败: {e}")
        return []


def _score_with_rules(labels: list, item: dict, rules: dict, pref: dict) -> tuple[int, str]:
    """按规则+偏好打分，返回 (总分, 说明)。"""
    weight = rules.get("weights", {})
    score = 0
    reasons = []
    txt = (item.get("raw", "") + item.get("zh", "")).lower()

    # 1) 标签加分（AI语义标签 与 机械标签 命中权重）
    cat_keywords = {
        "发射": ["发射"],
        "任务": ["任务"],
        "重大任务": ["重大任务"],
        "科普": ["科普"],
        "历史": ["历史"],
        "商业": ["商业"],
        "故障": ["故障"],
    }
    for lab in labels:
        for cat, kw in cat_keywords.items():
            if any(k in lab for k in kw):
                val = weight.get(cat, 0)
                score += val
                reasons.append(f"{cat}:{val:+d}")

    # 2) 带图加分
    if item.get("images") or item.get("image"):
        val = weight.get("带图视频", 0)
        score += val
        reasons.append(f"带图:{val:+d}")

    # 3) 关键词兜底（若AI没给语义标签，用关键词粗判领域）
    if not any("领域:" in l for l in labels):
        cn_map = {"发射关键词": "发射", "商业关键词": "商业", "故障关键词": "故障"}
        for cat, words in rules.get("keywords", {}).items():
            rule_cat = cn_map.get(cat)
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
            score += 15
            reasons.append(f"偏好+账号:{a}")
    for d in pref.get("like", {}).get("喜欢领域", []):
        if str(d).strip() and str(d) in lab_text:
            score += 15
            reasons.append(f"偏好+领域:{d}")
    for kw in pref.get("like", {}).get("喜欢关键词", []):
        if kw.lower() in txt:
            score += 15
            reasons.append(f"偏好+:{kw}")
    for d in pref.get("dislike", {}).get("不喜欢领域", []):
        if str(d).strip() and str(d) in lab_text:
            score -= 15
            reasons.append(f"偏好-领域:{d}")
    for kw in pref.get("dislike", {}).get("不喜欢关键词", []):
        if kw.lower() in txt:
            score -= 15
            reasons.append(f"偏好-:{kw}")

    return score, ",".join(reasons)


def decide(score: int, high: int, low: int) -> str:
    """判定：>=高 发；<=低 丢；之间 AI兜底。"""
    if score >= high:
        return "发"
    if score <= low:
        return "丢"
    return "AI兜底"


def _in_allowlist(item: dict, labels: list, allow: dict) -> bool:
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


def _hit_exclude(item: dict, labels: list, pref: dict) -> bool:
    """排除列表（聊天'别发/不要 X'）：命中账号/关键词/领域 → 硬丢。"""
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


async def main():
    import tomllib
    log("=" * 55)
    log("航小麦 · 打标打分器 (模块2)")

    news_file = sys.argv[1] if len(sys.argv) > 1 else str(find_latest_news())
    log(f"[输入] 新闻文件: {news_file}")
    news_list = json.loads(Path(news_file).read_text(encoding="utf-8"))
    log(f"[信息] 待判定 {len(news_list)} 条")

    rules = load_toml(RULE_FILE)
    # 叠加该用户的评分规则覆盖（一人一份：默认 推送规则.toml + 推送规则_<QQ>.toml）
    user_rules = load_toml(CONFIG_DIR / f"推送规则_{DEFAULT_TARGET_PRIVATE}.toml")
    rules = _deep_merge(rules, user_rules)
    pref = load_toml(CONFIG_DIR / f"偏好_{DEFAULT_TARGET_PRIVATE}.toml")
    threshold = rules.get("threshold", {"高阈值": 60, "低阈值": 20})
    high = threshold.get("高阈值", 60)
    low = threshold.get("低阈值", 20)
    allow = rules.get("allowlist", {})

    judged = []
    for n in news_list:
        text = n.get("raw", "") + " " + n.get("zh", "")
        mech = _mechanical_labels(n)
        sem = _ai_semantic_labels(n.get("zh", "") or n.get("raw", ""))
        labels = mech + sem
        score, reason = _score_with_rules(labels, n, rules, pref)
        if _hit_exclude(n, labels, pref):
            decision = "丢"
            reason = (reason + ",排除命中" if reason else "排除命中")
        elif not _in_allowlist(n, labels, allow):
            decision = "丢"
            reason = (reason + ",白名单外" if reason else "白名单外")
        else:
            decision = decide(score, high, low)
        item = dict(n)
        item["labels"] = labels
        item["score"] = score
        item["decision"] = decision
        item["reason"] = reason
        judged.append(item)
        log(f"[判定] @{n.get('account','?')} 分数={score} 决策={decision} | {reason}")

    # 输出
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rf = OUTPUT_DIR / f"judged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(rf, "w", encoding="utf-8") as f:
        json.dump(judged, f, ensure_ascii=False, indent=2)
    log(f"[结果] 判定 {len(judged)} 条，已存: {rf}")
    for j in judged:
        log(f"  - [{j.get('decision')}] @{j.get('account','?')} 分{j.get('score')} {j.get('zh','')[:40]}")
    log("=== 打标打分结束 ===")


if __name__ == "__main__":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
