# -*- coding: utf-8 -*-
"""真实 SQLite、离线模型替身验证失败循环、队尾顺序、旧条目恢复和版本隔离。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from dashboard import service_runner as runner
from collect.db import ensure_processing_event, enqueue_processing_event
from process import translate_db as tdb
from process.analysis_contract import missing_analysis_fields


def run_checks():
    with tempfile.TemporaryDirectory(prefix='hx-retry-') as directory:
        target = Path(directory)
        (target / '.env').write_text('TRANSLATE_RATE_LIMIT_WAIT=1\n', encoding='utf-8')
        with patch.object(runner, 'ROOT', target), patch.object(runner, 'DB_FILE', target / 'hx.sqlite3'):
            conn = runner._event_connection(initialize=True)
            for ident, text, attempts in [(30, 'failed', 100), (10, 'healthy', 0), (20, 'timeout', 0)]:
                conn.execute('INSERT INTO items(id,raw,account,stage,translate_attempts) VALUES(?,?,?, ?,?)',
                             (ident, text, 'offline', 'collected', attempts))
            # 先入队失败项，其后 ID 更小的正常项，验证不被 id 排序打乱。
            for ident in (30, 10, 20):
                ensure_processing_event(conn, ident)
            conn.commit()
            events = runner.read_processing_events()
            assert [event['item_id'] for event in events] == [30, 10, 20]
            seen = []
            repaired = False

            def offline(text, *args):
                seen.append(text)
                if text == 'timeout':
                    return None, TimeoutError('Request timed out.'), False
                if text == 'failed' and not repaired:
                    return None, ValueError('invalid output'), False
                return {'zh': '译文', 'is_news': True, 'not_repeat': True, 'not_comment': True,
                        'not_sensitive': True, 'score': 80, 'tags': ['领域:任务'], 'title': '标题',
                        'summary': '要点', 'importance': '高'}, None, False

            with patch.object(tdb, 'TRANSLATE_MAX_ATTEMPTS', 2), patch.object(tdb, 'TRANSLATE_PAUSE_SECONDS', 0), \
                 patch.object(tdb, 'PROGRESS_FILE', target / 'progress.json'), patch.object(tdb, 'analyze_with_retry', offline):
                for cycle in range(4):
                    events = runner.read_processing_events(due_only=True)
                    ids = [event['item_id'] for event in events]
                    tdb.translate_pass(conn, ids)
                    conn.execute("UPDATE items SET stage='processed' WHERE stage='translated'")
                    conn.commit()
                    runner.settle_processing_events(events, 0)
                    if cycle == 0:
                        assert seen == ['failed', 'failed', 'healthy', 'timeout', 'timeout'], seen
                        assert not missing_analysis_fields(dict(conn.execute('SELECT * FROM items WHERE id=10').fetchone()))
                        assert [event['item_id'] for event in runner.read_processing_events(False)] == [30, 20]
                        assert runner.read_processing_events(True) == [], '只有失败项时须等待冷却'
                    conn.execute('UPDATE processing_events SET available_at=0')
                    conn.commit()
                assert conn.execute('SELECT translate_attempts FROM items WHERE id=30').fetchone()[0] == 108
                assert len(runner.read_processing_events(False)) == 2, '旧上限超过后事件仍保留'
                repaired = True
                events = runner.read_processing_events(True)
                tdb.translate_pass(conn, [event['item_id'] for event in events])
                conn.execute("UPDATE items SET stage='processed' WHERE stage='translated'")
                conn.commit()
                runner.settle_processing_events(events, 0)
                assert [event['item_id'] for event in runner.read_processing_events(False)] == [20]

            # 处理中采集到了新版本：旧失败回执不能延迟或删除它。
            previous = runner.read_processing_events(False)
            enqueue_processing_event(conn, 20)
            conn.commit()
            runner.settle_processing_events(previous, 0)
            fresh = runner.read_processing_events(True)
            assert len(fresh) == 1 and fresh[0]['generation'] == 2 and fresh[0]['attempts'] == 0
            # 模拟旧版本达到上限时删除的事件，以及中途停止在 judge 前的完整项。
            conn.execute('DELETE FROM processing_events')
            conn.execute("UPDATE items SET stage='translated' WHERE id=10")
            conn.commit()
            runner.restore_processing_events()
            recovered = runner.read_processing_events(False)
            assert {event['item_id'] for event in recovered} == {10, 20}
            conn.close()
            print('通过：旧失败继续重试、每次轮到次数限制、正常项不被阻塞、重复轮转、成功出队、冷却、事件版本隔离、启动恢复。')


if __name__ == '__main__':
    run_checks()
