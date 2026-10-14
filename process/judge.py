# -*- coding: utf-8 -*-
"""航小麦 · 打标打分器 (模块2 · judge.py)
=======================================
作用：读模块1输出的新闻(new_news_*.json)，混合打标(机械=规则 / 语义=AI)，
      按规则+偏好打分，判定"该不该发 / 发给谁 / 分数"。

输入：抓取脚本/output/new_news_*.json（模块1输出）
输出：抓取脚本/output/judged_*.json
判定：排除命中→丢；四盏灯有红灯→丢；四盏灯全绿→发（配了 [gate] AI分下限 时低于下限也丢）；
      白名单外→丢；其余（没点灯的老条目）看规则分：≥高阈值发 / ≤低阈值丢 / 中间 AI兜底

运行：
    <venv>/Scripts/python.exe judge.py [新闻json路径]
    （不传则自动找最新的 new_news_*.json）

依赖：openai（AI打标）；tomllib（读规则/偏好）
配置：配置/推送规则.toml（+ 推送规则_<QQ>.toml 单人覆盖）、配置/偏好_<QQ>.toml

注意：本文件只做"接线 + 主循环"，打标打分的算法在 process/rules.py，
      AI 语义打标在 process/ai.py。
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

from config import DEFAULT_TARGET_PRIVATE
from rules import (
    ai_tags as _ai_tags,
    decide,
    has_quote as _has_quote,
    hit_exclude as _hit_exclude,
    importance_label as _importance_label,
    in_allowlist as _in_allowlist,
    light_state as _light_state,
    mechanical_labels as _mechanical_labels,
    score_floor_ok as _score_floor_ok,
    score_with_rules as _score_with_rules,
)
from process.db import rows as db_rows, open_db
from collect.db import ensure_processing_event
from process.analysis_contract import missing_analysis_fields
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
    parser = argparse.ArgumentParser()
    parser.add_argument("news_file", nargs="?", help="旧 JSON 入口；默认读取 SQLite")
    parser.add_argument("--item-ids", help="仅处理订阅收到的逗号分隔条目 ID")
    args = parser.parse_args()
    item_ids = None
    if args.item_ids is not None:
        item_ids = [int(value) for value in args.item_ids.split(",") if value.strip().isdigit()]
        if not item_ids:
            log("[错误] --item-ids 没有有效条目 ID")
            return
        if args.news_file:
            parser.error("--item-ids 不能和 JSON 文件路径同时使用")
    log("=" * 55)
    log("航小麦 · 打标打分器 (模块2)")

    news_file = args.news_file or "数据库"
    if news_file == "数据库":
        translated = db_rows("translated", item_ids)
        # Do not repeatedly rejudge rows already finalized in the database.
        news_list = [row for row in translated
                     if not row.get("decision") and not missing_analysis_fields(row)]
        skipped = len(translated) - len(news_list) - sum(1 for row in translated if row.get("decision"))
        if skipped:
            log(f"[跳过] {skipped} 条 translated 记录缺少完整分析字段；等待处理池完整重做")
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
    gate = rules.get("gate", {})
    # 四盏灯（是新闻 / 不重复 / 非评价 / 非敏感政治）全绿就放行；[gate] 三灯放行 = false 可退回纯打分
    three_lights_pass = bool(gate.get("三灯放行", True))
    # AI 价值分下限：>0 时"四绿但分太低"也丢；0（默认）= 不看分，四绿就放行
    score_floor = int(gate.get("AI分下限", 0) or 0)

    judged = []
    for n in news_list:
        mech = _mechanical_labels(n)
        # 标签由完整分析阶段一次写齐；判定阶段直接消费 ai_tags。
        tags = _ai_tags(n)
        importance = _importance_label(n)          # "重要:高"（没判出来是空串）
        labels = mech + tags + ([importance] if importance else [])
        rules_score, reason = _score_with_rules(labels, n, rules, pref)
        # 分数：AI 打的价值分优先（0~100），模型没给才退回规则加权分
        ai_score = n.get("ai_score")
        try:
            ai_score = int(ai_score) if ai_score is not None else None
        except (TypeError, ValueError):
            ai_score = None
        score = ai_score if ai_score is not None else rules_score
        if ai_score is not None:
            reason = (reason + f",AI分:{ai_score}" if reason else f"AI分:{ai_score}")
        state, detail = _light_state(n)
        # 带引用的条目：第 3 盏灯（非评价）在 translate_db 写库时已被强制放过，
        # 这里再往 reason 里标一句，看板详情页能直接看到"这条为什么放行"。
        if _has_quote(n):
            reason = (reason + ",引用放行" if reason else "引用放行")
        if _hit_exclude(n, labels, pref):
            decision = "丢"
            reason = (reason + ",排除命中" if reason else "排除命中")
        elif state == "red":
            # 四盏灯里有一盏是红的（非新闻 / 重复 / 只是评价别人 / 敏感政治）→ 直接丢
            decision = "丢"
            reason = (reason + f",红灯:{detail}" if reason else f"红灯:{detail}")
        elif state == "green" and three_lights_pass:
            if _score_floor_ok(ai_score, score_floor):
                decision = "发"
                reason = (reason + ",四绿放行" if reason else "四绿放行")
            else:
                decision = "丢"
                reason = (reason + f",AI分低于下限{score_floor}" if reason
                          else f"AI分低于下限{score_floor}")
        elif not _in_allowlist(n, labels, allow):
            decision = "丢"
            reason = (reason + ",白名单外" if reason else "白名单外")
        else:
            # 没点灯的老条目，或关掉了四灯放行 → 回到权重 + 阈值 + AI兜底（用规则分，保持老口径）
            decision = decide(rules_score, high, low)
        item = dict(n)
        item["labels"] = labels
        item["score"] = score
        item["rules_score"] = rules_score
        item["ai_score"] = ai_score
        item["decision"] = decision
        item["reason"] = reason
        judged.append(item)
        title = str(n.get("ai_title") or "").strip()
        log(f"[判定] @{n.get('account','?')} 灯={state} AI分={ai_score} 规则分={rules_score} "
            f"决策={decision} | {reason} | {title} | {' '.join(tags)}")

    if news_file == "数据库":
        conn=open_db()
        for item in judged:
            ensure_processing_event(conn, item.get('id'))
            # 只写值得长期保存的：分数落在 ai_score/rules_score，标签落在 ai_tags+importance，
            # 所以这里不再写 score / labels（它们都能由上面几列算出来）。
            conn.execute("UPDATE items SET rules_score=?,decision=?,reason=?,stage='judged',processed_at=?,rev=COALESCE(rev,0)+1 "
                         "WHERE id=? AND stage='translated' AND raw IS ? AND quoted_raw IS ? "
                         "AND quoted_account IS ? AND account IS ?",
                         (item.get('rules_score'),item.get('decision'),item.get('reason'),
                          datetime.now().isoformat(timespec='seconds'),item.get('id'),item.get('raw'),
                          item.get('quoted_raw'),item.get('quoted_account'),item.get('account')))
        conn.commit(); conn.close()
    else:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        rf = WORK_DIR / f"judged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(rf, "w", encoding="utf-8") as f:
            json.dump(judged, f, ensure_ascii=False, indent=2)
        log(f"[结果] 判定 {len(judged)} 条，已存: {rf}")
    for j in judged:
        log(f"  - [{j.get('decision')}] @{j.get('account','?')} 分{j.get('score')} {j.get('zh','')}")
    log("=== 打标打分结束 ===")


if __name__ == "__main__":
    setup_console_encoding()
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
