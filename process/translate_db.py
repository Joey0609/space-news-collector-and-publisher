"""航小麦 · 完整内容分析（模块 2-A）
=====================================
每条内容只有一个处理队列。缺译文、四灯、AI分、标题、标签或正文清理项时，都重新调用同一个完整分析：
译文 zh / 四盏灯 / AI 价值分 / ai_title / 摘要 / 重要度 / 标签一次生成并校验齐全后才落库。

分析失败不会把灰灯当成功；逐条记录错误与尝试次数，达到上限的记录仍留在看板列表中。
临时网络错误与限流不消耗永久失败次数。
judge.py 只处理完整分析成功写入 translated 的记录。"""
import json
import os
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

_PROCESS = Path(__file__).resolve().parent
_ROOT = _PROCESS.parent
for _path in (_ROOT, _PROCESS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai import analyze_item, config_summary, is_configured  # noqa: E402
from config import (  # noqa: E402
    AI_REPEAT_CONTEXT,
    TRANSLATE_INLINE_RETRIES,
    TRANSLATE_MAX_ATTEMPTS,
    TRANSLATE_PAUSE_SECONDS,
    TRANSLATE_RATE_LIMIT_WAIT,
    missing_config,
)
from process.db import open_db  # noqa: E402
from process.analysis_contract import missing_analysis_fields  # noqa: E402
from collect.db import ensure_processing_event  # noqa: E402
from process.logging_util import make_log, one_line, setup_console_encoding  # noqa: E402
from process.paths import LOG_DIR  # noqa: E402
from rules import has_quote  # noqa: E402  （"有引用"判定，见 _not_comment_flag）

log = make_log(LOG_DIR / "translate.log")

PROGRESS_FILE = _ROOT / "data" / "state" / "process_progress.json"


def write_progress(running: bool, phase: str, item: dict | None = None,
                   index: int = 0, total: int = 0) -> None:
    """给看板记录处理阶段和当前条目；写状态失败不影响处理主流程。"""
    current = None
    if item:
        current = {
            "id": item.get("id"),
            "account": str(item.get("account") or ""),
            "title": str(item.get("ai_title") or "").strip(),
            "raw": str(item.get("raw") or "")[:240],
        }
    payload = {
        "running": bool(running),
        "phase": str(phase or ""),
        "current": current,
        "index": int(index or 0),
        "total": int(total or 0),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PROGRESS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, PROGRESS_FILE)
    except OSError as exc:
        log(f"[进度] 看板进度写入失败: {one_line(str(exc))}")

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def status_code_of(exc: Exception):
    """尽力从 openai SDK / httpx 的异常里取出 HTTP 状态码。"""
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


TRANSIENT_HINTS = ("timeout", "timed out", "connection", "reset", "temporarily", "overloaded", "rate limit")


def is_transient(exc: Exception) -> bool:
    """临时故障（限流/超时/网络/服务端 5xx）：等一会儿重试即可，不该把条目判死。"""
    status = status_code_of(exc)
    if status in (408, 409, 425, 429):
        return True
    if isinstance(status, int) and status >= 500:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        # 例如 Windows 上的 [Errno 22] Invalid argument、连接被重置
        return True
    text = str(exc).lower()
    return any(hint in text for hint in TRANSIENT_HINTS)


def _normalize_item_ids(item_ids):
    if item_ids is None:
        return None
    result = []
    for value in item_ids:
        if isinstance(value, bool):
            continue
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id not in result:
            result.append(item_id)
    return result


def pending_rows(conn, max_attempts: int, item_ids=None) -> list[dict]:
    """选出指定增量或全库中缺少完整分析字段且未达重试上限的记录。"""
    ids = _normalize_item_ids(item_ids)
    if ids == []:
        return []
    sql = "SELECT * FROM items WHERE raw IS NOT NULL AND TRIM(raw) <> ''"
    args = []
    if ids is not None:
        sql += " AND id IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
    rows = conn.execute(sql + " ORDER BY COALESCE(ts, 0) DESC, id DESC", args).fetchall()
    picked = []
    for row in rows:
        item = dict(row)
        item["attempts"] = int(item.get("translate_attempts") or 0)
        if missing_analysis_fields(item) and item["attempts"] < max_attempts:
            picked.append(item)
    return picked


def stuck_count(conn, max_attempts: int, item_ids=None) -> int:
    """失败次数已用完、被放弃的条目数（用来提示用户改配置或手工处理）。"""
    ids = _normalize_item_ids(item_ids)
    if ids == []:
        return 0
    sql = "SELECT * FROM items WHERE raw IS NOT NULL AND TRIM(raw) <> ''"
    args = []
    if ids is not None:
        sql += " AND id IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
    rows = conn.execute(sql, args).fetchall()
    return sum(1 for row in rows
               if missing_analysis_fields(dict(row))
               and int(row["translate_attempts"] or 0) >= max_attempts)


def promote_complete_collected(conn, item_ids=None) -> int:
    """Advance imported, already-complete collected rows into the normal judge stage."""
    ids = _normalize_item_ids(item_ids)
    if ids == []:
        return 0
    sql = ("SELECT * FROM items WHERE COALESCE(stage, 'collected')='collected' "
           "AND raw IS NOT NULL AND TRIM(raw) <> ''")
    args = []
    if ids is not None:
        sql += " AND id IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
    rows = conn.execute(sql, args).fetchall()
    ids = [int(row["id"]) for row in rows if not missing_analysis_fields(dict(row))]
    if not ids:
        return 0
    for item_id in ids:
        ensure_processing_event(conn, item_id)
    stamp = now()
    conn.executemany(
        "UPDATE items SET stage='translated', decision=NULL, reason='', "
        "translated_at=COALESCE(NULLIF(translated_at,''), ?), "
        "rev=COALESCE(rev,0)+1 WHERE id=? AND COALESCE(stage,'collected')='collected'",
        [(stamp, item_id) for item_id in ids],
    )
    conn.commit()
    return len(ids)



def _news_flag(analysis: dict):
    news = analysis.get("is_news")
    return None if news is None else (1 if news else 0)


def _flag(value):
    """True/False/None → 1/0/NULL（三盏灯都用这个存）。"""
    return None if value is None else (1 if value else 0)


def _not_comment_flag(item: dict, ai_value):
    """第 3 盏灯（非评价 / not_comment）写库前的最终取值。

    ★★ 有引用的条目一律放行：不管 AI 判成什么，这里都强制给绿灯（1）。★★

    理由：带引用（quote）的推文，内容本体往往在被引用的那条上（quoted_raw 已随条目
    一起入库并进了提示词），引用者那句话只是个引子。AI 很容易把这种"引用+一句感想"
    判成「针对另一条新闻的评价」，从而让整条被丢掉。所以这里统一放行。

    注意：只放行这一盏灯 —— 「是不是新闻 / 有没有重复 / 敏不敏感」仍然照常由 AI 判。
    """
    if has_quote(item):
        return 1
    return _flag(ai_value)


def _quoted_zh(analysis: dict) -> str:
    """被引用正文的译文（没有引用时模型会给空串）。"""
    return str(analysis.get("quoted_zh") or "").strip()


def _tags_json(analysis: dict) -> str:
    return json.dumps(analysis.get("tags") or [], ensure_ascii=False)


def known_titles(conn, exclude_id, limit: int = 0) -> list:
    """给"第 2 盏灯（不重复）"用的最近已入库清单：["#56 标题", ...]。

    limit <= 0（AI_REPEAT_CONTEXT=0）时返回空列表 = 不给模型清单，只看文本里的旧闻线索。
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT id, ai_title FROM items "
        " WHERE id <> ? AND TRIM(COALESCE(ai_title, '')) <> '' "
        " ORDER BY COALESCE(ts, 0) DESC, id DESC LIMIT ?",
        (exclude_id, int(limit)),
    ).fetchall()
    return [f"#{r[0]} {r[1]}" for r in rows]


def save_ok(conn, item: dict, analysis: dict, attempts: int) -> bool:
    """完整分析成功：写译文 + 四盏灯 + 标题/要点/重要度 + 标签 + 被引用正文的译文。"""
    # 完整分析（包括修复缺灯）成功后必须回到 translated，让 judge 基于新灯重新判定。
    new_stage = "translated"
    stamp = now()
    cursor = conn.execute(
        "UPDATE items SET zh=?, quoted_zh=?, ai_tags=?, is_news=?, not_repeat=?, not_comment=?, not_sensitive=?, "
        "repeat_of=?, ai_title=?, ai_summary=?, importance=?, ai_score=?, "
        "stage=?, decision=NULL, reason='', translate_error='', translate_attempts=?, "
        "translated_at=?, processed_at=?, "
        "rev=COALESCE(rev, 0) + 1 WHERE id=? AND raw IS ? AND quoted_raw IS ? "
        "AND quoted_account IS ? AND account IS ?",
        (
            analysis["zh"],
            _quoted_zh(analysis),
            _tags_json(analysis),
            _news_flag(analysis),
            _flag(analysis.get("not_repeat")),
            # 有用引用的条目这里会被强制成 1（绿灯），见 _not_comment_flag 的注释
            _not_comment_flag(item, analysis.get("not_comment")),
            _flag(analysis.get("not_sensitive")),
            analysis.get("repeat_of") or "",
            analysis.get("title") or "",
            analysis.get("summary") or "",
            analysis.get("importance") or "",
            analysis.get("score"),
            new_stage,
            attempts,
            stamp,
            stamp,
            item["id"],
            item.get("raw"),
            item.get("quoted_raw"),
            item.get("quoted_account"),
            item.get("account"),
        ),
    )
    return getattr(cursor, "rowcount", 1) == 1



def save_failed(conn, item: dict, error: Exception, attempts: int) -> None:
    """翻译的永久性失败：记错误并消耗一次重试机会。"""
    conn.execute(
        "UPDATE items SET translate_error=?, translate_attempts=? "
        "WHERE id=? AND raw IS ? AND quoted_raw IS ? AND quoted_account IS ? AND account IS ?",
        (one_line(str(error)), attempts, item["id"], item.get("raw"),
         item.get("quoted_raw"), item.get("quoted_account"), item.get("account")),
    )


def save_transient(conn, item: dict, error: Exception) -> None:
    """翻译的临时失败（限流/超时）：只记错误，**不消耗**重试次数，下一轮继续试。"""
    conn.execute(
        "UPDATE items SET translate_error=? WHERE id=? AND raw IS ? AND quoted_raw IS ? "
        "AND quoted_account IS ? AND account IS ?",
        (one_line(str(error)), item["id"], item.get("raw"), item.get("quoted_raw"),
         item.get("quoted_account"), item.get("account")),
    )



def run_with_retry(call, tag: str):
    """跑一次模型调用：临时故障当场等待重试。

    返回 (结果, 永久性异常, 是否认证失败)。三者互斥，正常时后两个都是 None/False。
    """
    for round_index in range(TRANSLATE_INLINE_RETRIES + 1):
        try:
            return call(), None, False
        except Exception as exc:  # noqa: BLE001
            status = status_code_of(exc)
            if status in (401, 403):
                return None, exc, True
            if is_transient(exc) and round_index < TRANSLATE_INLINE_RETRIES:
                log(f"[等待] {tag} 临时失败({one_line(str(exc))})，"
                    f"{TRANSLATE_RATE_LIMIT_WAIT:.0f}s 后第 {round_index + 2} 次尝试")
                time.sleep(TRANSLATE_RATE_LIMIT_WAIT)
                continue
            return None, exc, False
    return None, RuntimeError("重试次数用尽"), False


def analyze_with_retry(text: str, tag: str, account: str = "", known=None,
                       quoted_account: str = "", quoted_raw: str = ""):
    """完整分析（翻译 + 四盏灯 + 标题/要点/重要度 + 标签 + 被引用正文译文）。"""
    result, error, auth_failed = run_with_retry(
        lambda: analyze_item(text, account, known, quoted_account, quoted_raw), tag)
    # 模型偶尔漏字段或输出坏 JSON 时，立即用明确校验反馈再做一次完整分析；
    # 只有全部必需字段齐全才会写库。
    if result is None and isinstance(error, ValueError):
        log(f"[格式重试] {tag} 完整分析结果未通过校验：{one_line(str(error))}")
        result, error, auth_failed = run_with_retry(
            lambda: analyze_item(text, account, known, quoted_account, quoted_raw,
                                 repair_hint=one_line(str(error))), tag)
    return result, error, auth_failed



def translate_pass(conn, item_ids=None) -> tuple[int, int, int, bool]:
    """① 完整分析一遍：返回 (成功, 永久失败, 限流待重试, 是否认证中止)。"""
    promoted = promote_complete_collected(conn, item_ids)
    if promoted:
        log(f"[扫描] {promoted} 条已有完整分析的采集记录进入判定阶段")
    todo = pending_rows(conn, TRANSLATE_MAX_ATTEMPTS, item_ids)
    stuck = stuck_count(conn, TRANSLATE_MAX_ATTEMPTS, item_ids)
    for item in todo:
        ensure_processing_event(conn, item["id"])
    conn.commit()
    if not todo:
        log("[检查] 没有待翻译/待重试的条目")
        if stuck:
            log(f"[提示] 另有 {stuck} 条翻译连续失败已达上限 {TRANSLATE_MAX_ATTEMPTS} 次，已跳过")
        return 0, 0, 0, False

    log(f"[检查] 待翻译 {len(todo)} 条（含失败重试；单条上限 {TRANSLATE_MAX_ATTEMPTS} 次）")

    ok = failed = transient_left = 0
    for index, item in enumerate(todo, 1):
        write_progress(True, "正在翻译并判灯", item, index, len(todo))
        attempts = int(item["attempts"]) + 1
        tag = f"{index}/{len(todo)}"

        analysis, error, auth_failed = analyze_with_retry(
            str(item["raw"]), tag, str(item.get("account") or ""),
            known_titles(conn, item["id"], AI_REPEAT_CONTEXT),
            str(item.get("quoted_account") or ""), str(item.get("quoted_raw") or ""))

        if auth_failed:
            # key 不对是配置问题：整轮停下，绝不把 71 条的重试次数一次烧光
            log(f"[中止] 认证失败 HTTP {status_code_of(error)}：请检查 MODEL_API_KEY 是否正确")
            return ok, failed, transient_left, True

        if analysis:
            saved = save_ok(conn, item, analysis, attempts)
            conn.commit()          # 逐条提交：中断也不丢已完成的部分
            if not saved:
                log(f"[增量过期 {tag}] @{item['account']} 分析期间原文已变化，丢弃旧回复等待新事件")
                continue
            ok += 1
            log(f"[翻译OK {tag}] @{item['account']} 第{attempts}次 | {describe(analysis)} "
                f"| {one_line(analysis['zh'])}")
            # 有引用的条目：第 3 盏灯被强制放过，日志里标注一下（存的是绿，AI 可能判的是红）
            if has_quote(item) and analysis.get("not_comment") is not True:
                log(f"[引用放行] {tag} @{item['account']} 有引用，第 3 盏灯（非评价）强制绿灯"
                    f"（AI 原判 {analysis.get('not_comment')}）")
            quoted_zh = _quoted_zh(analysis)
            if quoted_zh:
                log(f"[引用OK {tag}] @{item['account']} 引用 @{item.get('quoted_account') or '?'} "
                    f"的译文 | {one_line(quoted_zh)}")
        elif is_transient(error):
            # 限流/超时：记下原因但不消耗次数，下一轮继续
            save_transient(conn, item, error)
            conn.commit()
            transient_left += 1
            log(f"[限流] {tag} @{item['account']} {one_line(str(error))}")
        else:
            save_failed(conn, item, error, attempts)
            conn.commit()
            failed += 1
            log(f"[翻译X  {tag}] @{item['account']} 第{attempts}次失败: {one_line(str(error))}")

        if TRANSLATE_PAUSE_SECONDS > 0:
            time.sleep(TRANSLATE_PAUSE_SECONDS)

    return ok, failed, transient_left, False



def describe(analysis: dict) -> str:
    """日志用的一句话：四盏灯 + 重要度 + 标题或标签。"""
    lights = "".join(
        {True: "绿", False: "红", None: "灰"}[value]
        for value in (analysis.get("is_news"), analysis.get("not_repeat"),
                      analysis.get("not_comment"), analysis.get("not_sensitive"))
    )
    parts = [f"灯:{lights}"]
    repeat_of = str(analysis.get("repeat_of") or "").strip()
    if repeat_of:
        parts.append(f"重复于{repeat_of}")
    importance = str(analysis.get("importance") or "").strip()
    if importance:
        parts.append(f"重要度{importance}")
    title = str(analysis.get("title") or "").strip()
    if title:
        parts.append(title)
    tags_txt = " ".join(analysis.get("tags") or [])
    if tags_txt:
        parts.append(tags_txt)
    return " | ".join(parts)


def _run_processing(item_ids=None) -> int:
    log("=" * 55)
    log("航小麦 · 完整分析（翻译 + 四盏灯 + 标题/摘要/分数/标签）")

    summary = config_summary()
    log(f"[配置] 协议={summary['protocol']} 模型={summary['model']} 地址={summary['base']} "
        f"输出上限={summary['max_tokens'] or '服务端默认'}")

    if not is_configured():
        conn = open_db()
        try:
            promoted = promote_complete_collected(conn, item_ids)
            todo = pending_rows(conn, TRANSLATE_MAX_ATTEMPTS, item_ids)
            stuck = stuck_count(conn, TRANSLATE_MAX_ATTEMPTS, item_ids)
            for item in todo:
                ensure_processing_event(conn, item["id"])
            conn.commit()
            scope = "指定增量" if item_ids is not None else "全库"
            log(f"[扫描] {scope}检查完成：待完整分析 {len(todo)} 条，完整采集转入判定 {promoted} 条，"
                f"达到重试上限 {stuck} 条")
        finally:
            conn.close()
        log(f"[等待] 模型 API 未配置完整，缺少：{'、'.join(missing_config())}；不完整条目保留在订阅队列")
        log("       配置入口：看板 → 处理服务 → 配置（MODEL_API_KEY / MODEL_API_BASE / MODEL_NAME）")
        # Keep already-complete rows moving through judge/fallback. Incomplete rows
        # remain on the durable subscription and will retry after their backoff.
        return 0

    conn = open_db()
    try:
        ok, failed, transient_left, aborted = translate_pass(conn, item_ids)
        if aborted:
            log(f"[结果] 已中止：成功 {ok} 条，失败 {failed} 条（认证问题不消耗重试次数）")
            log("       填好 key 后重新启动处理服务即可继续")
            return 1
        log(f"[结果] 完整分析：成功 {ok} 条，永久失败 {failed} 条，限流待重试 {transient_left} 条")
        if failed:
            log("[提示] 不完整回复或翻译失败已记 translate_error，下一轮会自动重试")
        if transient_left:
            log(f"[提示] 限流/超时的条目没有消耗重试次数，下一轮会继续（本次已等待重试 "
                f"{TRANSLATE_INLINE_RETRIES} 次）")
        left = stuck_count(conn, TRANSLATE_MAX_ATTEMPTS)
        if left:
            log(f"[提示] {left} 条完整分析达到重试上限；看板队列会保留并显示错误")
    finally:
        conn.close()

    log("=== 完整分析结束 ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--item-ids", help="仅处理订阅收到的逗号分隔条目 ID")
    args = parser.parse_args()
    item_ids = None
    if args.item_ids is not None:
        item_ids = _normalize_item_ids(args.item_ids.split(","))
        if not item_ids:
            log("[错误] --item-ids 没有有效条目 ID")
            return 2
    write_progress(True, "正在检查处理队列")
    try:
        return _run_processing(item_ids)
    finally:
        write_progress(False, "本轮处理结束")


if __name__ == "__main__":
    setup_console_encoding()
    sys.exit(main())
