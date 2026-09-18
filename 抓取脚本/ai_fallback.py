# -*- coding: utf-8 -*-
"""
航小麦 · AI兜底判定 (模块3 · ai_fallback.py)
=============================================
作用：对"边缘分数"的新闻（judge 判定为 AI兜底 的）做最后判断：
      AI 综合多维度（通用转发价值 + 用户偏好 + 群偏好 + 其他规则），
      判断"这条有没有意思"，有意思则硬发，没意思则丢。

输入：judged_*.json（模块2输出，decision="AI兜底" 的条目）
输出：输出最终决定到 output/fallback_*.json，并打印摘要

运行：
    <venv>/Scripts/python.exe ai_fallback.py [judged.json路径]

依赖：openai（AI判定）；tomllib（读偏好）
配置：偏好文件 配置/偏好_<qq>.toml；群偏好 配置/偏好_群<group>.toml
"""

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
LOG_FILE = LOG_DIR / "ai_fallback.log"
RULE_FILE = CONFIG_DIR / "推送规则.toml"

load_dotenv(SEED_DIR / ".env", encoding="utf-8")
# 避免 Windows GBK 控制台打印中文崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "").strip()
VISION_MODEL = "deepseek-chat"  # AI兜底判"有意思"用纯文本模型

DEFAULT_PRIVATE = "YOUR_QQ_ID"
DEFAULT_GROUP = "YOUR_GROUP_ID_1"


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


def find_latest_judged() -> Path:
    files = sorted(glob.glob(str(OUTPUT_DIR / "judged_*.json")))
    if not files:
        log("[错误] 找不到 judged_*.json，先运行模块2")
        sys.exit(1)
    return Path(files[-1])


def build_ai_prompt(news: dict, user_pref: dict, group_pref: dict) -> str:
    """构造 AI 判定 prompt：结合新闻 + 多维度标准。"""
    base = (
        "你是一位航天爱好者群友。下面是一条航天新闻，请判断它有没有意思、值不值得分享。"
        "只输出一行：有意思 或 没意思。\n"
    )
    dims = []
    dims.append("标准1(通用)：普通航天爱好者看到这条会不会想转发给群友")
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


def ai_decide(news: dict, user_pref: dict, group_pref: dict) -> str:
    """AI 判断，返回 "发"（有意思）或 "丢"（没意思）。"""
    if not DEEPSEEK_KEY:
        return "发"  # 没key时保守发（但通常不会到这）
    try:
        client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
        prompt = build_ai_prompt(news, user_pref, group_pref)
        resp = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        out = resp.choices[0].message.content.strip()
        return "发" if "有意思" in out else "丢"
    except Exception as e:
        log(f"[警告] AI兜底判定失败: {e}")
        return "发"


async def main():
    import sys as _s
    log("=" * 55)
    log("航小麦 · AI兜底判定 (模块3)")

    judged_file = _s.argv[1] if len(_s.argv) > 1 else str(find_latest_judged())
    judged = json.loads(Path(judged_file).read_text(encoding="utf-8"))
    log(f"[输入] 判定文件: {judged_file}，共 {len(judged)} 条")

    user_pref = load_toml(CONFIG_DIR / f"偏好_{DEFAULT_PRIVATE}.toml")
    group_pref = load_toml(CONFIG_DIR / f"偏好_群{DEFAULT_GROUP}.toml")

    final = []
    for n in judged:
        if n.get("decision") == "AI兜底":
            verdict = ai_decide(n, user_pref, group_pref)
            n["final_decision"] = verdict
            log(f"[AI兜底] @{n.get('account','?')} 分{n.get('score')} -> {verdict} | {n.get('zh','')[:40]}")
        else:
            n["final_decision"] = n.get("decision", "丢")
        final.append(n)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rf = OUTPUT_DIR / f"fallback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(rf, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    log(f"[结果] 最终判定 {len(final)} 条，已存: {rf}")
    for j in final:
        log(f"  - [{j.get('final_decision')}] @{j.get('account','?')} 分{j.get('score')} {j.get('zh','')[:40]}")
    log("=== AI兜底判定结束 ===")


if __name__ == "__main__":
    try:
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except Exception:
        pass
