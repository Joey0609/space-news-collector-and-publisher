"""离线验证真实队列排序、并行状态与开始时间，不启动模型或渠道。"""
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from collect.db import ensure_schema, ensure_processing_event, enqueue_processing_event
from dashboard import server
from process import analyze_db as adb


def test_queue_status():
    with tempfile.TemporaryDirectory(prefix='hx-queue-progress-') as directory:
        root = Path(directory)
        progress_file = root / 'progress.json'
        conn = sqlite3.connect(root / 'hx.sqlite3')
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        for ident in (50, 20, 30, 10, 60):
            conn.execute('INSERT INTO items(id,raw,ts,analysis_error) VALUES(?,?,?,?)',
                         (ident, 'offline', 1000-ident, 'previous timeout' if ident == 50 else ''))
        for index, ident in enumerate((50, 20, 30, 10), 1):
            ensure_processing_event(conn, ident)
            conn.execute('UPDATE processing_events SET created_at=? WHERE item_id=?',
                         (f'2026-10-14T00:00:0{index}', ident))
        conn.execute('UPDATE processing_events SET available_at=? WHERE item_id=30', (time.time()+60,))
        conn.commit()
        start = time.time()-65
        with patch.object(server, 'DATA', root), patch.object(server, 'PROCESS_PROGRESS_FILE', progress_file), \
             patch.object(server, 'services', return_value={'process': {'running': True}}), \
             patch.object(adb, 'PROGRESS_FILE', progress_file):
            adb.write_progress(True, '并行处理中 2', active_items=[
                {'id': 50, '_processing_started_at': start, '_processing_generation': 1},
                {'id': 20, '_processing_started_at': start+10, '_processing_generation': 1},
            ])
            result = server.process_backlog()
            rows = result['items']
            assert [row['id'] for row in rows] == [50, 20, 10, 30, 60], rows
            assert [row['queue_position'] for row in rows] == [1, 2, 3, 4, 5]
            assert rows[0]['processing'] and rows[1]['processing'] and not rows[2]['processing']
            assert rows[0]['phase'] == '正在完整分析' and rows[2]['phase'] == '待完整分析'
            assert rows[0]['error'] and 64 <= rows[0]['elapsed_seconds'] <= 67
            summary = server.process_progress()
            assert [row['id'] for row in summary['active_items']] == [50, 20]
            assert summary['current']['id'] == 50 and summary['current']['started_at'] == start
            # 新输入版本不能继承旧任务的正在处理标记。
            enqueue_processing_event(conn, 50)
            conn.commit()
            rows = server.process_backlog()['items']
            assert rows[0]['id'] == 20 and not next(row for row in rows if row['id']==50)['processing']
            with patch.object(server, 'services', return_value={'process': {'running': False}}):
                assert not any(row['processing'] for row in server.process_backlog()['items'])
                assert server.process_progress()['active_items'] == []
        conn.close()


def test_parallel_tracking():
    with tempfile.TemporaryDirectory(prefix='hx-parallel-progress-') as directory:
        root = Path(directory)
        conn = sqlite3.connect(root / 'hx.sqlite3')
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        for ident in (50, 20, 10):
            conn.execute('INSERT INTO items(id,raw) VALUES(?,?)', (ident, 'offline'))
            ensure_processing_event(conn, ident)
        conn.commit()
        snapshots, launches = [], []
        writer = adb.write_progress

        def record(*args, **kwargs):
            writer(*args, **kwargs)
            snapshots.append(json.loads((root / 'progress.json').read_text(encoding='utf-8')))

        class OfflineWorker:
            def __init__(self, command, **kwargs):
                launches.append(int(command[-1]))
                self.calls = 0
                self.returncode = 0

            def poll(self):
                self.calls += 1
                return None if self.calls < 3 else 0

        with patch.object(adb, 'PROGRESS_FILE', root / 'progress.json'), patch.object(adb, 'write_progress', side_effect=record), \
             patch.object(adb.subprocess, 'Popen', side_effect=OfflineWorker), patch.object(adb.time, 'sleep'), \
             patch.object(adb.runtime_config, 'refresh_model_config'), patch.object(adb.runtime_config, 'ANALYSIS_CONCURRENCY', 2), \
             patch.object(adb.runtime_config, 'ANALYSIS_PAUSE_SECONDS', 0):
            adb.parallel_pass(conn, [50, 20, 10])
        assert launches == [50, 20, 10]
        both = next(snapshot for snapshot in snapshots if len(snapshot['active_items']) == 2)
        assert [row['id'] for row in both['active_items']] == [50, 20] and both['current']['id'] == 50
        starts = {}
        for snapshot in snapshots:
            for row in snapshot['active_items']:
                assert row['started_at'] > 0
                assert starts.setdefault(row['id'], row['started_at']) == row['started_at'], '后续进度不能重置开始时间'
        assert snapshots[-1]['active_items'] == []
        conn.close()


if __name__ == '__main__':
    test_queue_status()
    test_parallel_tracking()
    print('通过：队头优先、队列顺序、冷却顺序、全部并行任务标记、稳定开始时间、处理时长、停止清理与版本隔离。')
