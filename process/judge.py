# -*- coding: utf-8 -*-
"""航小麦 · 打标打分器 (模块2 · judge.py)
=======================================
作用：读模块1输出的新闻(new_news_*.json)，混合打标(机械=规则 / 语义=AI)，
      按规则+偏好打分，判定"该不该发 / 发给谁 / 分数"。

输入：抓取脚本/output/new_news_*.json（模块1输出）
输出：抓取脚本/output/judged_*.json
判定：排除命中→丢；白名单外→丢；分数≥高阈值→发；≤低阈值→丢；中间→AI兜底（模块3）

运行：
    <venv>/Scripts/python.exe judge.py [新闻json路径]
    （不传则自动找最新的 new_news_*.json）

依赖：openai（AI打标）；tomllib（读规则/偏好）
配置：配置/推送规则.toml（+ 推送规则_<QQ>.toml 单人覆盖）、配置/偏好_<QQ>.toml

注意：本文件只做"接线 + 主循环"，打标打分的算法在 process/rules.py，
      AI 语义打标在 process/ai.py。
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

from config import DEFAULT_TARGET_PRIVATE
from ai import semantic_labels as _ai_semantic_labels
from rules import (
    decide,
    hit_exclude as _hit_exclude,
    in_allowlist as _in_allowlist,
    mechanical_labels as _mechanical_labels,
    score_with_rules as _score_with_rules,
)
from process.db import rows as db_rows, open_db
from process.jsonio import deep_merge as _deep_merge, latest_file, load_toml
from process.logging_util import make_log, setup_console_encoding
from process.paths import CONFIG_DIR, LOG_DIR, RAW_DIR, WORK_DIR

LOG_FILE = LOG_DIR / "judge.log"
RULE_FILE = CONFIG_DIR / "推送规则.toml"
log = make_log(LOG_FILE)


def find_latest_news() -> Path:
    f = latest_file(WORK_DIR, "translated_*.json") or latest_file(RAW_DIR, "new_news_*.json")
    if not f:
        log("[错误] 找不到 new_news_*.json，先运行模块1抓取")
        sys.exit(1)
    return f


async def main():
    log("=" * 55)
    log("航小麦 · 打标打分器 (模块2)")

    news_file = sys.argv[1] if len(sys.argv) > 1 else "数据库"
    if news_file == "数据库":
        news_list = db_rows("translated")
        # Do not repeatedly rejudge rows already finalized in the database.
        news_list = [row for row in news_list if not row.get("decision")]
    else:
        news_list = json.loads(Path(news_file).read_text(encoding="utf-8"))
    log(f"[输入] {news_file}，共 {len(news_list)} 条")

    # 规则 = 推送规则.toml 叠加该用户的单人覆盖（一人一份）
    rules = load_toml(RULE_FILE)
    rules = _deep_merge(rules, load_toml(CONFIG_DIR / f"推送规则_{DEFAULT_TARGET_PRIVATE}.toml"))
    pref = load_toml(CONFIG_DIR / f"偏好_{DEFAULT_TARGET_PRIVATE}.toml")
    threshold = rules.get("threshold", {"高阈值": 60, "低阈值": 20})
    high = threshold.get("高阈值", 60)
    low = threshold.get("低阈值", 20)
    allow = rules.get("allowlist", {})

    judged = []
    for n in news_list:
        mech = _mechanical_labels(n)
        sem = _ai_semantic_labels(n.get("zh", "") or n.get("raw", ""), log)
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

    if news_file == "数据库":
        conn=open_db()
        for item in judged:
            conn.execute("UPDATE items SET labels=?,score=?,decision=?,reason=?,stage='judged',processed_at=? WHERE id=?",(json.dumps(item.get('labels',[]),ensure_ascii=False),item.get('score'),item.get('decision'),item.get('reason'),datetime.now().isoformat(timespec='seconds'),item.get('id')))
        conn.commit(); conn.close()
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        rf = OUTPUT_DIR / f"judged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(rf, "w", encoding="utf-8") as f:
            json.dump(judged, f, ensure_ascii=False, indent=2)
        log(f"[结果] 判定 {len(judged)} 条，已存: {rf}")
    for j in judged:
        log(f"  - [{j.get('decision')}] @{j.get('account','?')} 分{j.get('score')} {j.get('zh','')[:40]}")
    log("=== 打标打分结束 ===")


if __name__ == "__main__":
    setup_console_encoding()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
