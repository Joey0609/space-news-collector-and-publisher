# -*- coding: utf-8 -*-
"""航小麦 · AI兜底判定 (模块3 · ai_fallback.py)
=============================================
作用：对"边缘分数"的新闻（judge 判定为 AI兜底 的）做最后判断：
      AI 综合多维度（通用转发价值 + 用户偏好 + 群偏好），判断"这条有没有意思"，
      有意思则硬发，没意思则丢。

输入：output/judged_*.json（模块2输出，只处理 decision="AI兜底" 的条目）
输出：output/fallback_*.json（每条都带 final_decision，模块5 只挑 "发" 的）

运行：
    <venv>/Scripts/python.exe ai_fallback.py [judged.json路径]

依赖：openai（AI判定）；tomllib（读偏好）
配置：配置/偏好_<QQ>.toml（用户偏好）、配置/偏好_群<群号>.toml（群偏好）

注意：prompt 与判定在 process/ai.py，本文件只做"接线 + 主循环"。
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_COLLECT = _REPO / "collect"
_PROCESS = _REPO / "process"
for _path in (_REPO, _COLLECT, _PROCESS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from config import DEFAULT_TARGET_GROUP, DEFAULT_TARGET_PRIVATE
from ai import build_fallback_prompt as build_ai_prompt
from ai import fallback_decide as ai_decide
from process.db import open_db, rows as db_rows
from process.logging_util import make_log, setup_console_encoding
from process.paths import CONFIG_DIR, LOG_DIR, WORK_DIR

OUTPUT_DIR = WORK_DIR


LOG_FILE = LOG_DIR / "ai_fallback.log"
log = make_log(LOG_FILE)


def find_latest_judged() -> Path:
    f = latest_file(OUTPUT_DIR, "judged_*.json")
    if not f:
        log("[错误] 找不到 judged_*.json，先运行模块2")
        sys.exit(1)
    return f


async def main():
    log("=" * 55)
    log("航小麦 · AI兜底判定 (模块3)")

    judged = db_rows("judged")
    log(f"[输入] 数据库 judged 条目，共 {len(judged)} 条")

    user_pref = load_toml(CONFIG_DIR / f"偏好_{DEFAULT_TARGET_PRIVATE}.toml")
    group_pref = load_toml(CONFIG_DIR / f"偏好_群{DEFAULT_TARGET_GROUP}.toml")

    conn = open_db()
    for n in judged:
        verdict = ai_decide(n, user_pref, group_pref, log) if n.get("decision") == "AI兜底" else n.get("decision", "丢")
        conn.execute("UPDATE items SET final_decision=?, stage='processed' WHERE id=?", (verdict, n.get("id")))
        log(f"[AI兜底] @{n.get('account','?')} 分{n.get('score')} -> {verdict} | {n.get('zh','')[:40]}")
    conn.commit()
    conn.close()
    log(f"[结果] 数据库最终处理 {len(judged)} 条")
    log("=== AI兜底判定结束 ===")


if __name__ == "__main__":
    setup_console_encoding()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
