# -*- coding: utf-8 -*-
"""Collect-owned SQLite schema and raw-item writes."""
import json, sqlite3
from datetime import datetime
from collect.paths import DB_FILE
SCHEMA='''CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY,platform TEXT DEFAULT '',source TEXT DEFAULT '',origin_id TEXT,dedup_key TEXT UNIQUE,account TEXT,orig_account TEXT,forward_comment TEXT,tweet_id TEXT,ts INTEGER,date TEXT,content_type TEXT,raw TEXT,zh TEXT DEFAULT '',ori_url TEXT,images TEXT DEFAULT '[]',score INTEGER,labels TEXT,decision TEXT,reason TEXT,final_decision TEXT,stage TEXT DEFAULT 'collected',collected_at TEXT,processed_at TEXT);CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);'''

def _ensure_columns(c):
 existing={row[1] for row in c.execute('PRAGMA table_info(items)').fetchall()}
 additions={'tweet_id':'TEXT','ts':'INTEGER','date':'TEXT','ori_url':'TEXT','source':'TEXT','origin_id':'TEXT'}
 for name,kind in additions.items():
  if name not in existing: c.execute(f'ALTER TABLE items ADD COLUMN {name} {kind}')
def open_db():
 c=sqlite3.connect(DB_FILE,timeout=10); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000'); c.executescript(SCHEMA); _ensure_columns(c); c.commit(); return c
def upsert_raw(item):
 key=f"{item.get('platform','x')}:{item.get('origin_id') or item.get('tweet_id') or item.get('account','')}"; c=open_db(); now=datetime.now().isoformat(timespec='seconds'); c.execute('''INSERT INTO items(platform,source,origin_id,dedup_key,account,orig_account,forward_comment,tweet_id,ts,date,content_type,raw,zh,ori_url,images,stage,collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dedup_key) DO UPDATE SET raw=excluded.raw,images=excluded.images,content_type=excluded.content_type''',(item.get('platform','x'),item.get('source','x'),str(item.get('origin_id') or item.get('tweet_id') or ''),key,item.get('account',''),item.get('orig_account',''),item.get('forward_comment',''),str(item.get('tweet_id','')),item.get('ts',0),item.get('date',''),item.get('content_type',''),item.get('raw',''),item.get('zh',''),item.get('ori_url') or item.get('url',''),json.dumps(item.get('images',[]),ensure_ascii=False),'collected',now)); row=c.execute('SELECT id FROM items WHERE dedup_key=?',(key,)).fetchone(); c.commit(); c.close(); return int(row[0])
