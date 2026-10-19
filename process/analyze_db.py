"""一次完整 AI 分析，逐条原子提交结果和事件确认。"""
import argparse
import json
import os
import sys
import time
import subprocess
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "process"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))
from ai import analyze_item, is_configured, config_summary
from config import ANALYSIS_MAX_ATTEMPTS, ANALYSIS_INLINE_RETRIES, ANALYSIS_PAUSE_SECONDS, ANALYSIS_RATE_LIMIT_WAIT, AI_REPEAT_CONTEXT
import config as runtime_config
from collect.db import open_db
from process.analysis_contract import processing_required
from collect.db import ensure_processing_event
from process import embedding
from process.logging_util import make_log, one_line, setup_console_encoding
log = make_log(ROOT / "logs" / "analysis.log")
PROGRESS_FILE = ROOT / "data" / "state" / "process_progress.json"
IS_WORKER = False

def write_progress(running, phase, item=None, index=0, total=0, active_items=None):
    if IS_WORKER: return
    def describe(row):
        return {"id": row["id"], "account": row.get("account", ""), "title": row.get("ai_title", ""),
                "raw": str(row.get("raw") or "")[:240], "started_at": row.get("_processing_started_at"),
                "generation": row.get("_processing_generation")}
    active = [describe(row) for row in (active_items if active_items is not None else ([item] if item else []))]
    current = active[0] if active else None
    payload = {"running": running, "phase": phase, "current": current, "active_items": active,
               "index": index, "total": total, "updated_at": datetime.now().isoformat(timespec="seconds")}
    try:
        PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PROGRESS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, PROGRESS_FILE)
    except OSError as exc: log(f"[进度] 写入失败：{exc}")

def status_code_of(error):
    return getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)

def is_transient(error):
    status = status_code_of(error)
    return status in (408,409,425,429) or isinstance(status,int) and status >= 500 or isinstance(error,(TimeoutError,ConnectionError,OSError)) or any(t in str(error).lower() for t in ("timeout","timed out","connection","rate limit","overloaded"))

def pending_rows(conn, item_ids=None):
    if item_ids == []: return []
    sql = "SELECT * FROM items WHERE raw IS NOT NULL AND TRIM(raw)<>''"
    args = []
    if item_ids is not None:
        sql += " AND id IN (" + ",".join("?" for _ in item_ids) + ")"
        args.extend(item_ids)
    result = [dict(r) for r in conn.execute(sql + " ORDER BY ts,id", args) if processing_required(dict(r))]
    if item_ids is not None:
        order = {ident: index for index, ident in enumerate(item_ids)}
        result.sort(key=lambda row: order[row['id']])
    return result

def known_titles(conn, exclude_id, limit=0):
    if limit <= 0: return []
    return [f"#{r[0]} {r[1]} | {r[2]}" for r in conn.execute("SELECT id,ai_title,ai_summary FROM items WHERE id<>? AND stage='processed' AND TRIM(COALESCE(ai_title,''))<>'' ORDER BY ts DESC,id DESC LIMIT ?",(exclude_id,limit))]

def _input_sql():
    return "id=? AND raw IS ? AND account IS ? AND quoted_raw IS ? AND quoted_account IS ?"

def _input_args(item):
    return (item["id"],item.get("raw"),item.get("account"),item.get("quoted_raw"),item.get("quoted_account"))

def save_ok(conn, item, analysis, attempts, generation, embedding_context=None):
    """事务内确认对应 generation；旧版本回复不能覆盖新采集。"""
    cursor = conn.execute("UPDATE items SET zh=?,quoted_zh=?,ai_title=?,ai_summary=?,importance=?,ai_score=?,ai_tags=?,repeat_of=?,decision=?,invalid_reason=?,stage='processed',analysis_attempts=?,analysis_error='',processed_at=?,rev=rev+1 WHERE " + _input_sql() + " AND EXISTS(SELECT 1 FROM processing_events WHERE item_id=items.id AND generation=?)",(analysis["zh"],analysis["quoted_zh"],analysis["title"],analysis["summary"],analysis["importance"],analysis["score"],json.dumps(analysis["tags"],ensure_ascii=False),analysis["repeat_of"],analysis["decision"],analysis["invalid_reason"],attempts,datetime.now().isoformat(timespec="seconds"),*_input_args(item),generation))
    if cursor.rowcount == 1:
        conn.execute("SAVEPOINT optional_embedding")
        try:
            embedding.save_embedding(conn, item, embedding_context)
            conn.execute("RELEASE SAVEPOINT optional_embedding")
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT optional_embedding")
            conn.execute("RELEASE SAVEPOINT optional_embedding")
            log(f"[embedding] item={item['id']} 向量未保存，分析结果仍提交：{one_line(str(exc))}")
        conn.execute("DELETE FROM processing_events WHERE item_id=? AND generation=?",(item["id"],generation))
        return True
    return False

def save_failed(conn, item, error, attempts, generation):
    conn.execute("UPDATE items SET analysis_error=?,analysis_attempts=?,rev=rev+1 WHERE " + _input_sql() + " AND EXISTS(SELECT 1 FROM processing_events WHERE item_id=items.id AND generation=?)",(one_line(str(error)),attempts,*_input_args(item),generation))

def defer_event(conn, item_id, generation):
    latest = conn.execute("SELECT MAX(created_at) FROM processing_events").fetchone()[0]
    tail = datetime.now()
    if latest: tail = max(tail,datetime.fromisoformat(latest)+timedelta(microseconds=1))
    conn.execute("UPDATE processing_events SET available_at=?,created_at=? WHERE item_id=? AND generation=?",(time.time()+max(1,ANALYSIS_RATE_LIMIT_WAIT),tail.isoformat(timespec='microseconds'),item_id,generation))

def cleanup_events(conn, item_ids=None):
    # 清掉已经完成或已删除条目的残留事件，避免空批次反复启动。
    events = conn.execute('SELECT item_id,generation FROM processing_events').fetchall()
    for event in events:
        if item_ids is not None and event['item_id'] not in item_ids: continue
        current = conn.execute('SELECT * FROM items WHERE id=?',(event['item_id'],)).fetchone()
        if current is None:
            conn.execute('DELETE FROM processing_events WHERE item_id=? AND generation=? AND NOT EXISTS(SELECT 1 FROM items WHERE id=?)',(event['item_id'],event['generation'],event['item_id']))
        elif not processing_required(dict(current)):
            item=dict(current)
            conn.execute("DELETE FROM processing_events WHERE item_id=? AND generation=? AND EXISTS(SELECT 1 FROM items WHERE " + _input_sql() + " AND stage='processed')",(event['item_id'],event['generation'],*_input_args(item)))
    conn.commit()


def analysis_pass(conn, item_ids=None, embedding_context_file=None):
    cleanup_events(conn, item_ids)
    todo = pending_rows(conn,item_ids)
    ok = failed = skipped = 0
    for index,snapshot in enumerate(todo,1):
        # 批次选定后，当前条目仍可能被修改、完成或删除。
        row = conn.execute("SELECT * FROM items WHERE id=?",(snapshot['id'],)).fetchone()
        if row is None or not processing_required(dict(row)):
            skipped += 1
            continue
        item = dict(row)
        ensure_processing_event(conn,item['id'])
        generation = conn.execute("SELECT generation FROM processing_events WHERE item_id=?",(item['id'],)).fetchone()[0]
        conn.commit()
        item['_processing_started_at'] = time.time()
        item['_processing_generation'] = generation
        write_progress(True,'正在完整分析',item,index,len(todo))
        context = embedding.read_context(embedding_context_file, item) if embedding_context_file else None
        if context is None and embedding_context_file:
            context = embedding.unavailable_context(item, '并行上下文与当前输入版本不匹配')
        elif context is None:
            try:
                context = embedding.prepare(item, conn)
            except Exception as exc:
                context = embedding.unavailable_context(item, exc)
        candidate_titles = context.get('candidates', []) if context.get('available') else []
        if not context.get('available') or context.get('indexed_count', 0) == 0:
            candidate_titles = known_titles(conn,item['id'],AI_REPEAT_CONTEXT)
        attempts = int(item['analysis_attempts'] or 0)
        analysis = None
        error = None
        hint = ''
        inline_left = max(0,ANALYSIS_INLINE_RETRIES)
        limit = max(1,ANALYSIS_MAX_ATTEMPTS)
        visit = 0
        while visit < limit:
            visit += 1
            attempts += 1
            try:
                analysis = analyze_item(item['raw'],item.get('account') or '',candidate_titles,item.get('quoted_account') or '',item.get('quoted_raw') or '',repair_hint=hint)
                break
            except Exception as exc:
                error = exc
                save_failed(conn,item,error,attempts,generation)
                conn.commit()
                if status_code_of(exc) in (401,403): break
                hint = one_line(str(exc)) if isinstance(exc,ValueError) else ''
                if is_transient(exc) and inline_left > 0:
                    inline_left -= 1
                    limit += 1
                    time.sleep(max(0,ANALYSIS_RATE_LIMIT_WAIT))
                elif visit < limit and ANALYSIS_PAUSE_SECONDS > 0: time.sleep(ANALYSIS_PAUSE_SECONDS)
        if analysis:
            saved = save_ok(conn,item,analysis,attempts,generation,context)
            conn.commit()
            if saved:
                if context.get('available') and not IS_WORKER:
                    embedding.sync_committed(conn,item['id'])
                ok += 1
                log(f"[分析完成 {index}/{len(todo)}] item={item['id']} {analysis['decision']} | {analysis['title']} | {analysis['invalid_reason']}")
            else:
                skipped += 1
                log(f"[版本变化] item={item['id']}，旧回复未提交，等待新事件")
        else:
            failed += 1
            defer_event(conn,item['id'],generation)
            conn.commit()
            log(f"[队尾重试] item={item['id']} 累计{attempts}次：{one_line(str(error))}")
        if not IS_WORKER and ANALYSIS_PAUSE_SECONDS > 0: time.sleep(ANALYSIS_PAUSE_SECONDS)
    return {'ok':ok,'failed':failed,'skipped':skipped}


def parallel_pass(conn, item_ids=None):
    """每个任务独立进程与连接；完成即提交，并按接入点节奏补充任务。"""
    cleanup_events(conn, item_ids)
    todo = deque(pending_rows(conn, item_ids))
    total = len(todo)
    running = {}
    context_files = {}
    available = [0.0] * runtime_config.ANALYSIS_CONCURRENCY
    capacity = runtime_config.ANALYSIS_CONCURRENCY
    result = {'ok': 0, 'failed': 0, 'skipped': 0}
    completed = 0
    try:
        while todo or running:
            runtime_config.refresh_model_config()
            limit = runtime_config.ANALYSIS_CONCURRENCY
            pause = runtime_config.ANALYSIS_PAUSE_SECONDS
            if limit > capacity:
                available.extend([0.0] * (limit - capacity))
            capacity = limit
            now = time.monotonic()
            for process, item in list(running.items()):
                if process.poll() is None: continue
                del running[process]
                context_file = context_files.pop(process, None)
                if context_file:
                    Path(context_file).unlink(missing_ok=True)
                available.append(now + pause)
                completed += 1
                row = conn.execute('SELECT * FROM items WHERE id=?', (item['id'],)).fetchone()
                key = 'skipped' if row is None else ('failed' if processing_required(dict(row)) else 'ok')
                result[key] += 1
                if key == 'ok':
                    embedding.sync_committed(conn,item['id'])
                if process.returncode:
                    event = conn.execute('SELECT generation FROM processing_events WHERE item_id=?', (item['id'],)).fetchone()
                    if event: defer_event(conn, item['id'], event['generation']); conn.commit()
                write_progress(True, f'已完成 {completed}/{total}，并行处理中 {len(running)}',
                               index=completed, total=total, active_items=list(running.values()))
            # 降低并行数时收回闲置槽位，保留任务完成后的冷却时间。
            available = sorted(available, reverse=True)[:max(0, limit - len(running))]
            available.sort()
            while todo and len(running) < limit and available and available[0] <= now:
                item = dict(todo[0])
                context_file = embedding.write_context(item, conn)
                item['_processing_started_at'] = time.time()
                event = conn.execute('SELECT generation FROM processing_events WHERE item_id=?', (item['id'],)).fetchone()
                item['_processing_generation'] = event['generation'] if event else None
                command = [sys.executable, str(Path(__file__).resolve()), '--worker']
                if context_file:
                    command += ['--embedding-context-file', str(context_file)]
                command += ['--item-ids', str(item['id'])]
                process = subprocess.Popen(command, cwd=ROOT)
                todo.popleft()
                available.pop(0)
                running[process] = item
                context_files[process] = context_file
                write_progress(True, f'并行处理中 {len(running)}，已完成 {completed}/{total}',
                               index=completed, total=total, active_items=list(running.values()))
            if todo or running: time.sleep(0.1)
    finally:
        for process in running:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            context_file = context_files.get(process)
            if context_file:
                Path(context_file).unlink(missing_ok=True)
    return result


def main():
    global IS_WORKER
    parser = argparse.ArgumentParser()
    parser.add_argument('--item-ids')
    parser.add_argument('--embedding-context-file')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    ids = None if args.item_ids is None else list(dict.fromkeys(int(v) for v in args.item_ids.split(',') if v.strip().isdigit()))
    if ids == []: parser.error('--item-ids 不能为空')
    IS_WORKER = args.worker
    if IS_WORKER and (ids is None or len(ids) != 1): parser.error('内部任务必须指定单条内容')
    from dashboard.service_runner import acquire_instance_lock
    worker_lock = acquire_instance_lock(f'analysis_item_{ids[0]}' if IS_WORKER else 'analysis_worker')
    if worker_lock is None:
        log('[等待] 已有完整分析程序运行，本次不重复启动')
        return 0
    conn = open_db()
    write_progress(True,'正在检查分析队列')
    try:
        if not is_configured():
            for item in pending_rows(conn,ids):
                ensure_processing_event(conn,item['id'])
                generation = conn.execute("SELECT generation FROM processing_events WHERE item_id=?",(item['id'],)).fetchone()[0]
                defer_event(conn,item['id'],generation)
            conn.commit()
            log('[等待] AI 配置不完整，待分析内容保留在队列')
            return 0
        summary = config_summary()
        if not IS_WORKER:
            log(f"[完整分析] 模型={summary['model']} 待分析={len(pending_rows(conn,ids))} 并行={runtime_config.ANALYSIS_CONCURRENCY} 间隔={runtime_config.ANALYSIS_PAUSE_SECONDS}秒")
        result = analysis_pass(conn,ids,args.embedding_context_file) if IS_WORKER else parallel_pass(conn,ids)
        if not IS_WORKER: log(f"[本轮结果] 完成{result['ok']}，失败待重试{result['failed']}，跳过{result['skipped']}")
        return 0
    finally:
        conn.close()
        write_progress(False,'等待下一轮')
        worker_lock.close()

if __name__ == '__main__':
    setup_console_encoding()
    sys.exit(main())
