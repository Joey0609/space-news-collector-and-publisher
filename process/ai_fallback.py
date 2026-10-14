# -*- coding: utf-8 -*-
"""航小麦 · AI兜底判定 (模块3 · ai_fallback.py)
=============================================
作用：对"边缘分数"的新闻（judge 判定为 AI兜底 的）做最后判断：
      AI 综合多维度（通用转发价值 + 用户偏好 + 群偏好），判断"这条有没有意思"，
      有意思则硬发，没意思则丢。

输入：output/judged_*.json（模块2输出，只处理 decision="AI兜底" 的条目）
输出：数据库 stage='processed' + items.decision（最终判定直接写回；分发池只读处理完成且 decision='发'）

运行：
    <venv>/Scripts/python.exe ai_fallback.py [judged.json路径]

依赖：openai（AI判定）；tomllib（读偏好）
配置：配置/偏好_<QQ>.toml（用户偏好）、配置/偏好_群<群号>.toml（群偏好）

注意：prompt 与判定在 process/ai.py，本文件只做"接线 + 主循环"。
"""
import asyncio
import argparse
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
from collect.db import ensure_processing_event
from process.jsonio import load_toml
from process.logging_util import make_log, setup_console_encoding
from process.paths import CONFIG_DIR, LOG_DIR, WORK_DIR

OUTPUT_DIR = WORK_DIR


LOG_FILE = LOG_DIR / "ai_fallback.log"
log = make_log(LOG_FILE)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item-ids", help="仅处理订阅收到的逗号分隔条目 ID")
    args = parser.parse_args()
    item_ids = None
    if args.item_ids is not None:
        item_ids = [int(value) for value in args.item_ids.split(",") if value.strip().isdigit()]
        if not item_ids:
            log("[错误] --item-ids 没有有效条目 ID")
            return
    log("=" * 55)
    log("航小麦 · AI兜底判定 (模块3)")

    judged = db_rows("judged", item_ids)
    log(f"[输入] 数据库 judged 条目，共 {len(judged)} 条")

    user_pref = load_toml(CONFIG_DIR / f"偏好_{DEFAULT_TARGET_PRIVATE}.toml")
    group_pref = load_toml(CONFIG_DIR / f"偏好_群{DEFAULT_TARGET_GROUP}.toml")

    conn = open_db()
    for n in judged:
        ensure_processing_event(conn, n.get("id"))
        verdict = ai_decide(n, user_pref, group_pref, log) if n.get("decision") == "AI兜底" else n.get("decision", "丢")
        # 最终判定直接写回 decision（以前另存 final_decision，两列 100% 相同，已合并）
        conn.execute("UPDATE items SET decision=?, stage='processed', rev=COALESCE(rev,0)+1 "
                     "WHERE id=? AND stage='judged' AND raw IS ? AND quoted_raw IS ? "
                     "AND quoted_account IS ? AND account IS ?",
                     (verdict, n.get("id"), n.get("raw"), n.get("quoted_raw"),
                      n.get("quoted_account"), n.get("account")))
        score = n.get("ai_score")
        if score is None:
            score = n.get("rules_score")
        log(f"[AI兜底] @{n.get('account','?')} 分{score} -> {verdict} | {n.get('zh','')}")
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
