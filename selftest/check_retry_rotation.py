"""离线验证失败轮转、逐条确认、无累计上限及事件版本隔离。"""
import sys
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'process')]
sys.stdout.reconfigure(encoding='utf-8',errors='replace')
from collect.db import ensure_schema,ensure_processing_event,enqueue_processing_event
from process import analyze_db as adb
from dashboard import service_runner as runner

with tempfile.TemporaryDirectory() as directory:
    db=Path(directory)/'hx.sqlite3'
    conn=sqlite3.connect(db);conn.row_factory=sqlite3.Row;ensure_schema(conn)
    for ident,text,attempts in ((30,'failed',100),(10,'healthy',0),(20,'timeout',0)):
        conn.execute("INSERT INTO items(id,raw,analysis_attempts) VALUES(?,?,?)",(ident,text,attempts))
        ensure_processing_event(conn,ident)
    conn.commit()
    output=dict(zh='任务完成',quoted_zh='',title='任务完成',summary='任务完成。',importance='中',score=70,tags=['领域:任务'],repeat_of='',decision='发',invalid_reason='')
    seen=[]
    def model(text,*args,**kwargs):
        seen.append(text)
        if text=='failed':raise ValueError('bad format')
        if text=='timeout':raise TimeoutError('offline timeout')
        return output
    with patch.object(adb,'analyze_item',side_effect=model),patch.object(adb,'ANALYSIS_MAX_ATTEMPTS',1),patch.object(adb,'ANALYSIS_INLINE_RETRIES',0),patch.object(adb,'ANALYSIS_PAUSE_SECONDS',0),patch.object(adb,'write_progress'):
        result=adb.analysis_pass(conn,[30,10,20])
        assert seen==['failed','healthy','timeout'],seen
        assert result['ok']==1 and result['failed']==2,result
        assert conn.execute('SELECT COUNT(*) FROM processing_events WHERE item_id=10').fetchone()[0]==0
        assert conn.execute('SELECT analysis_attempts FROM items WHERE id=30').fetchone()[0]==101
        assert conn.execute('SELECT MIN(available_at) FROM processing_events').fetchone()[0]>time.time()
        conn.execute('UPDATE processing_events SET available_at=0');conn.commit()
        seen.clear();adb.analysis_pass(conn,[30,20])
        assert conn.execute('SELECT analysis_attempts FROM items WHERE id=30').fetchone()[0]==102
    with patch.object(runner,'DB_FILE',db):
        assert runner.restore_processing_events()==2
        assert not runner.read_processing_events(), '启动核对不得跳过失败冷却'
    stale=dict(conn.execute('SELECT * FROM items WHERE id=30').fetchone())
    generation=conn.execute('SELECT generation FROM processing_events WHERE item_id=30').fetchone()[0]
    conn.execute("UPDATE items SET raw='fresh' WHERE id=30");enqueue_processing_event(conn,30);conn.commit()
    assert not adb.save_ok(conn,stale,output,103,generation)
    conn.commit()
    assert conn.execute('SELECT generation FROM processing_events WHERE item_id=30').fetchone()[0]==generation+1
    conn.close()
print('通过：单次分析、失败轮转、冷却、逐条确认、累计失败不限、启动不空跑及版本隔离。')
