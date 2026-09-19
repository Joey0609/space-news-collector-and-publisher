# -*- coding: utf-8 -*-
"""航小麦 · 翻译+判定+价值分+标签 (模块2-A · translate_db.py)
==========================================================
处理服务每一轮开始时都会跑一次本入口，它干两件事（都在库里自动补齐）：

  ① 完整分析：把"没译文 / 译文还是旧占位符 / 上次翻译失败"的条目一次模型调用做完
       译文 zh / 四盏灯 is_news·not_repeat·not_comment·not_sensitive / AI 价值分 ai_score /
       标题 ai_title / 要点 ai_summary / 重要度 importance / 标签 ai_tags
  ② 存量补标：已经有译文、但"缺灯 / 缺分 / 缺标签"的条目
       只补四盏灯 + AI 分 + 标题/要点/重要度 + 标签（**不重新翻译、不覆盖人工改过的译文**），
       用完 meta_attempts 记账

四盏灯：① 是新闻 ② 不重复（判第 2 盏灯时会带上"最近已入库标题清单"给模型对比）
③ 非评价（不是对另一条新闻的吐槽/跟评）④ 非敏感政治（政治/外交红线）。
judge 里四绿直接放行，任一红直接丢。

**缺什么就重做什么**（两路合起来覆盖全部三种情况）：
  - 缺译文（zh 空 / 旧占位符 / 翻译失败）→ 第 ① 遍完整重做；
  - 缺灯、缺 AI 分、缺标签（但有译文）→ 第 ② 遍重做判断，译文一个字不动；
  - 判定（丢/发）不在这里做：judge 看四盏灯，再看 AI 分/规则分。

失败不会中断整轮：逐条写错误与尝试次数，并立即提交，所以进程被中断也不会丢进度。

"这条要不要重新完整翻译"只看三件事（PENDING_SQL）：
    ① raw 非空；
    ② translate_attempts < TRANSLATE_MAX_ATTEMPTS（默认 3，只有永久失败才 +1；429/超时不算）；
    ③ zh 还是空的（NULL / 空串），或者是旧版本留下的 "[未配置 key]" 占位符。
**有译文的条目不会重新翻译**——包括人工在看板改过的、以及"翻译成功过但还留着旧错误信息"的，
所以再跑多少轮也不会覆盖已经写好的译文；缺灯/缺分由第 ② 遍补，同样不碰 zh。
补标那一路（META_PENDING_SQL）另有自己的计数 meta_attempts，条件同样独立。

stage 只在 stage='collected' 时推进到 'translated'；已经进入后续阶段的条目只补字段、不回退 stage，
避免被 judge 重复判定。补标这一路完全不改 stage。

用法：
    python process/translate_db.py              # 新条目分析 + 存量补标
    python process/translate_db.py --meta-only  # 只补标（不碰新条目）
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

_PROCESS = Path(__file__).resolve().parent
_ROOT = _PROCESS.parent
for _path in (_ROOT, _PROCESS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai import analyze_item, analyze_meta, config_summary, is_configured  # noqa: E402
from config import (  # noqa: E402
    AI_REPEAT_CONTEXT,
    TRANSLATE_INLINE_RETRIES,
    TRANSLATE_MAX_ATTEMPTS,
    TRANSLATE_PAUSE_SECONDS,
    TRANSLATE_RATE_LIMIT_WAIT,
    missing_config,
)
from process.db import open_db  # noqa: E402
from process.logging_util import make_log, one_line, setup_console_encoding  # noqa: E402
from process.paths import LOG_DIR  # noqa: E402
from rules import has_quote  # noqa: E402  （"有引用"判定，见 _not_comment_flag）

log = make_log(LOG_DIR / "translate.log")

# 旧版本在没配 key 时会把这句话写进 zh，当成"没翻译"看待，本轮补翻。
PLACEHOLDER = "[未配置 key]"

PENDING_SQL = """
    SELECT id, account, raw, stage, quoted_account, quoted_raw,
           COALESCE(translate_attempts, 0) AS attempts
      FROM items
     WHERE raw IS NOT NULL AND TRIM(raw) <> ''
       AND COALESCE(translate_attempts, 0) < ?
       AND (
             zh IS NULL OR TRIM(zh) = '' OR zh = ?
           )
     ORDER BY COALESCE(ts, 0) DESC, id DESC
"""

STUCK_SQL = """
    SELECT COUNT(*)
      FROM items
     WHERE raw IS NOT NULL AND TRIM(raw) <> ''
       AND COALESCE(translate_attempts, 0) >= ?
       AND (zh IS NULL OR TRIM(zh) = '' OR TRIM(COALESCE(translate_error, '')) <> '')
"""

# 存量补标：有译文，但"四盏灯没点齐 / 没有 AI 分 / 没有标签"——这三样缺一个就重做一遍判断
_META_NEEDED = ("(COALESCE(is_news, -1) = -1 OR COALESCE(not_repeat, -1) = -1 "
                "OR COALESCE(not_comment, -1) = -1 OR COALESCE(not_sensitive, -1) = -1 "
                "OR COALESCE(ai_score, -1) = -1 "
                "OR TRIM(COALESCE(ai_tags, '')) = '')")

META_PENDING_SQL = f"""
    SELECT id, account, raw, zh, quoted_account, quoted_raw,
           COALESCE(meta_attempts, 0) AS attempts
      FROM items
     WHERE raw IS NOT NULL AND TRIM(raw) <> ''
       AND zh IS NOT NULL AND TRIM(zh) <> '' AND zh <> ?
       AND COALESCE(meta_attempts, 0) < ?
       AND {_META_NEEDED}
     ORDER BY COALESCE(ts, 0) DESC, id DESC
"""

META_STUCK_SQL = f"""
    SELECT COUNT(*)
      FROM items
     WHERE raw IS NOT NULL AND TRIM(raw) <> ''
       AND zh IS NOT NULL AND TRIM(zh) <> ''
       AND COALESCE(meta_attempts, 0) >= ?
       AND {_META_NEEDED}
"""


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


def pending_rows(conn, max_attempts: int) -> list[dict]:
    """本轮要做完整分析（翻译+判断+标签）的条目：新条目 + 失败的条目。"""
    return [dict(r) for r in conn.execute(PENDING_SQL, (max_attempts, PLACEHOLDER)).fetchall()]


def stuck_count(conn, max_attempts: int) -> int:
    """失败次数已用完、被放弃的条目数（用来提示用户改配置或手工处理）。"""
    return int(conn.execute(STUCK_SQL, (max_attempts,)).fetchone()[0] or 0)


def meta_pending_rows(conn, max_attempts: int) -> list[dict]:
    """本轮要补标的条目：已有译文，但缺 is_news / ai_tags。"""
    return [dict(r) for r in conn.execute(META_PENDING_SQL, (PLACEHOLDER, max_attempts)).fetchall()]


def meta_stuck_count(conn, max_attempts: int) -> int:
    return int(conn.execute(META_STUCK_SQL, (max_attempts,)).fetchone()[0] or 0)


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


def save_ok(conn, item: dict, analysis: dict, attempts: int) -> None:
    """完整分析成功：写译文 + 四盏灯 + 标题/要点/重要度 + 标签 + 被引用正文的译文。"""
    # 只在还停在 collected 时推进 stage；后续阶段只补字段，避免被重复判定
    stage = item["stage"] or "collected"
    new_stage = "translated" if stage == "collected" else stage
    stamp = now()
    conn.execute(
        "UPDATE items SET zh=?, quoted_zh=?, ai_tags=?, is_news=?, not_repeat=?, not_comment=?, not_sensitive=?, "
        "repeat_of=?, ai_title=?, ai_summary=?, importance=?, ai_score=?, "
        "stage=?, translate_error='', translate_attempts=?, translated_at=?, processed_at=?, "
        "rev=COALESCE(rev, 0) + 1 WHERE id=?",
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
        ),
    )


def save_meta_ok(conn, item: dict, meta: dict, attempts: int) -> None:
    """补标成功：只动四盏灯和标签这几列；zh / stage / quoted_zh 一律不碰。"""
    conn.execute(
        "UPDATE items SET ai_tags=?, is_news=?, not_repeat=?, not_comment=?, not_sensitive=?, "
        "repeat_of=?, ai_title=?, ai_summary=?, importance=?, ai_score=?, "
        "meta_error='', meta_attempts=?, processed_at=?, rev=COALESCE(rev, 0) + 1 WHERE id=?",
        (
            _tags_json(meta),
            _news_flag(meta),
            _flag(meta.get("not_repeat")),
            # 同 save_ok：有引用的条目第 3 盏灯强制绿灯
            _not_comment_flag(item, meta.get("not_comment")),
            _flag(meta.get("not_sensitive")),
            meta.get("repeat_of") or "",
            meta.get("title") or "",
            meta.get("summary") or "",
            meta.get("importance") or "",
            meta.get("score"),
            attempts,
            now(),
            item["id"],
        ),
    )


def save_failed(conn, item: dict, error: Exception, attempts: int) -> None:
    """翻译的永久性失败：记错误并消耗一次重试机会。"""
    conn.execute(
        "UPDATE items SET translate_error=?, translate_attempts=? WHERE id=?",
        (one_line(str(error)), attempts, item["id"]),
    )


def save_transient(conn, item: dict, error: Exception) -> None:
    """翻译的临时失败（限流/超时）：只记错误，**不消耗**重试次数，下一轮继续试。"""
    conn.execute(
        "UPDATE items SET translate_error=? WHERE id=?",
        (one_line(str(error)), item["id"]),
    )


def save_meta_failed(conn, item: dict, error: Exception, attempts: int) -> None:
    conn.execute(
        "UPDATE items SET meta_error=?, meta_attempts=? WHERE id=?",
        (one_line(str(error)), attempts, item["id"]),
    )


def save_meta_transient(conn, item: dict, error: Exception) -> None:
    conn.execute(
        "UPDATE items SET meta_error=? WHERE id=?",
        (one_line(str(error)), item["id"]),
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
    return run_with_retry(
        lambda: analyze_item(text, account, known, quoted_account, quoted_raw), tag)


def analyze_meta_with_retry(text: str, zh: str, tag: str, account: str = "", known=None,
                            quoted_account: str = "", quoted_raw: str = ""):
    """存量补标（只点灯 + 打标，不翻译）。"""
    return run_with_retry(
        lambda: analyze_meta(text, zh, account, known, quoted_account, quoted_raw), tag)


def translate_pass(conn) -> tuple[int, int, int, bool]:
    """① 完整分析一遍：返回 (成功, 永久失败, 限流待重试, 是否认证中止)。"""
    todo = pending_rows(conn, TRANSLATE_MAX_ATTEMPTS)
    stuck = stuck_count(conn, TRANSLATE_MAX_ATTEMPTS)
    if not todo:
        log("[检查] 没有待翻译/待重试的条目")
        if stuck:
            log(f"[提示] 另有 {stuck} 条翻译连续失败已达上限 {TRANSLATE_MAX_ATTEMPTS} 次，已跳过")
        return 0, 0, 0, False

    log(f"[检查] 待翻译 {len(todo)} 条（含失败重试；单条上限 {TRANSLATE_MAX_ATTEMPTS} 次）")

    ok = failed = transient_left = 0
    for index, item in enumerate(todo, 1):
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
            save_ok(conn, item, analysis, attempts)
            conn.commit()          # 逐条提交：中断也不丢已完成的部分
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


def meta_pass(conn) -> tuple[int, int, int, bool]:
    """② 存量补标一遍：返回 (成功, 永久失败, 限流待重试, 是否认证中止)。"""
    todo = meta_pending_rows(conn, TRANSLATE_MAX_ATTEMPTS)
    stuck = meta_stuck_count(conn, TRANSLATE_MAX_ATTEMPTS)
    if not todo:
        log("[检查] 没有需要补标的存量条目")
        if stuck:
            log(f"[提示] 另有 {stuck} 条补标连续失败已达上限 {TRANSLATE_MAX_ATTEMPTS} 次，已跳过")
        return 0, 0, 0, False

    log(f"[检查] 待补标 {len(todo)} 条（已有译文但缺灯/缺 AI 分/缺标签；不重新翻译）")

    ok = failed = transient_left = 0
    for index, item in enumerate(todo, 1):
        attempts = int(item["attempts"]) + 1
        tag = f"{index}/{len(todo)}"

        meta, error, auth_failed = analyze_meta_with_retry(
            str(item["raw"]), str(item["zh"]), tag, str(item.get("account") or ""),
            known_titles(conn, item["id"], AI_REPEAT_CONTEXT),
            str(item.get("quoted_account") or ""), str(item.get("quoted_raw") or ""))

        if auth_failed:
            log(f"[中止] 认证失败 HTTP {status_code_of(error)}：请检查 MODEL_API_KEY 是否正确")
            return ok, failed, transient_left, True

        if meta:
            save_meta_ok(conn, item, meta, attempts)
            conn.commit()
            ok += 1
            log(f"[补标OK {tag}] @{item['account']} 第{attempts}次 | {describe(meta)}")
            # 有引用的条目同样会被强制放过第 3 盏灯，标注一下
            if has_quote(item) and meta.get("not_comment") is not True:
                log(f"[引用放行] {tag} @{item['account']} 有引用，第 3 盏灯（非评价）强制绿灯"
                    f"（AI 原判 {meta.get('not_comment')}）")
        elif is_transient(error):
            save_meta_transient(conn, item, error)
            conn.commit()
            transient_left += 1
            log(f"[限流] {tag} @{item['account']} 补标 {one_line(str(error))}")
        else:
            save_meta_failed(conn, item, error, attempts)
            conn.commit()
            failed += 1
            log(f"[补标X  {tag}] @{item['account']} 第{attempts}次失败: {one_line(str(error))}")

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


def main() -> int:
    meta_only = "--meta-only" in sys.argv[1:]
    log("=" * 55)
    log("航小麦 · 翻译+新闻判定+标签 (模块2-A)" + ("（只补标）" if meta_only else ""))

    summary = config_summary()
    log(f"[配置] 协议={summary['protocol']} 模型={summary['model']} 地址={summary['base']} "
        f"输出上限={summary['max_tokens'] or '服务端默认'}")

    if not is_configured():
        log(f"[错误] 模型 API 未配置完整，缺少：{'、'.join(missing_config())}，本轮跳过")
        log("       配置入口：看板 → 处理服务 → 配置（MODEL_API_KEY / MODEL_API_BASE / MODEL_NAME）")
        return 1

    if meta_only:
        log("[跳过] --meta-only：本轮不做新条目的翻译")

    conn = open_db()
    try:
        ok = failed = transient_left = 0
        if not meta_only:
            ok, failed, transient_left, aborted = translate_pass(conn)
            if aborted:
                log(f"[结果] 已中止：成功 {ok} 条，失败 {failed} 条（认证问题不消耗重试次数）")
                log("       填好 key 后重新启动处理服务即可继续")
                return 1
            log(f"[结果] 完整分析：成功 {ok} 条，永久失败 {failed} 条，限流待重试 {transient_left} 条")

        meta_ok, meta_failed, meta_transient, meta_aborted = meta_pass(conn)
        if meta_aborted:
            log(f"[结果] 补标已中止：成功 {meta_ok} 条，失败 {meta_failed} 条（认证问题不消耗重试次数）")
            return 1
        log(f"[结果] 存量补标：成功 {meta_ok} 条，永久失败 {meta_failed} 条，限流待重试 {meta_transient} 条")

        if failed:
            log("[提示] 翻译失败的条目已记 translate_error，下一轮会自动重试")
        if transient_left or meta_transient:
            log(f"[提示] 限流/超时的条目没有消耗重试次数，下一轮会继续（本次已等待重试 "
                f"{TRANSLATE_INLINE_RETRIES} 次）")
        left = stuck_count(conn, TRANSLATE_MAX_ATTEMPTS)
        meta_left = meta_stuck_count(conn, TRANSLATE_MAX_ATTEMPTS)
        if left:
            log(f"[提示] {left} 条翻译已达重试上限被跳过；可调大 TRANSLATE_MAX_ATTEMPTS 再试")
        if meta_left:
            log(f"[提示] {meta_left} 条补标已达重试上限被跳过；可调大 TRANSLATE_MAX_ATTEMPTS 再试")
    finally:
        conn.close()

    log("=== 翻译+判定+标签结束 ===")
    return 0


if __name__ == "__main__":
    setup_console_encoding()
    sys.exit(main())
