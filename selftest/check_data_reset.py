"""离线验收30条保留名单、新结构、发送记录清空与附件清理。"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8',errors='replace')
from tools import reset_analysis_data as resetter
from collect.db import ensure_schema
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory);(root/'data/images').mkdir(parents=True)
    conn=sqlite3.connect(root/'data/hx.sqlite3');ensure_schema(conn)
    for ident in range(1,41):
        image=root/'data/images'/f'{ident}.jpg';image.write_bytes(b'image')
        conn.execute("INSERT INTO items(id,raw,collected_at,ts,images,pushed) VALUES(?,?,?,?,?,?)",(ident,'raw',f'2026-10-{(41-ident)//2:02d}T12:00:00',ident,json.dumps([str(image)]),'{"maibot:private":true}'))
    conn.execute("INSERT INTO channels(id,enabled) VALUES('alison',1)")
    conn.execute("INSERT INTO alison_groups(group_openid,push_enabled) VALUES('GROUP',1)")
    conn.execute("INSERT INTO meta(key,value) VALUES('push_sources','[\"maibot:private\"]')")
    conn.execute("INSERT INTO meta(key,value) VALUES('push_seeded','1')")
    conn.execute("INSERT INTO pushes(item_id,source) VALUES(1,'maibot:private')");conn.commit();conn.close()
    preview=resetter.reset(root,seed=42)
    again=resetter.reset(root,seed=42)
    assert preview==again and len(preview['newest_ids'])==15 and len(preview['random_ids'])==15
    assert not set(preview['newest_ids']) & set(preview['random_ids'])
    with patch.object(resetter,'require_stopped'):
        result=resetter.reset(root,seed=42,apply=True)
    conn=sqlite3.connect(root/'data/hx.sqlite3')
    ids={r[0] for r in conn.execute('SELECT id FROM items')}
    assert ids==set(preview['newest_ids']+preview['random_ids'])
    assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]==30
    assert conn.execute("SELECT COUNT(*) FROM items WHERE pushed<>'{}' OR stage<>'collected' OR decision IS NOT NULL").fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM processing_events').fetchone()[0]==30
    assert conn.execute('SELECT COUNT(*) FROM pushes').fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM push_attempts').fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM meta WHERE key=\'push_seeded\'').fetchone()[0]==0
    assert conn.execute('SELECT enabled FROM channels').fetchone()[0]==1
    assert conn.execute('SELECT push_enabled FROM alison_groups').fetchone()[0]==1
    assert len(conn.execute('PRAGMA table_info(items)').fetchall())==34
    assert len(conn.execute('PRAGMA table_info(pushes)').fetchall())==6
    assert len(conn.execute('PRAGMA table_info(processing_events)').fetchall())==4
    assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert len(list((root/'data/images').glob('*.jpg')))==30
    conn.close()
print('通过：入库时间最新15+固定随机15、附件清理、新键结构、发送记录清空和渠道配置保留。')
