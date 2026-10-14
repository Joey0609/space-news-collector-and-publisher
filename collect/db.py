# -*- coding: utf-8 -*-
"""Collect-owned SQLite schema and raw-item writes."""
import json, sqlite3
from datetime import datetime
from collect.paths import DB_FILE
SCHEMA='''CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY,platform TEXT DEFAULT '',source TEXT DEFAULT '',origin_id TEXT,dedup_key TEXT UNIQUE,account TEXT,orig_account TEXT,forward_comment TEXT,quoted_account TEXT DEFAULT '',quoted_raw TEXT DEFAULT '',quoted_zh TEXT DEFAULT '',quoted_url TEXT DEFAULT '',ts INTEGER,date TEXT,content_type TEXT,raw TEXT,zh TEXT DEFAULT '',ai_tags TEXT DEFAULT '',is_news INTEGER,not_repeat INTEGER,not_comment INTEGER,not_sensitive INTEGER,repeat_of TEXT DEFAULT '',ai_title TEXT DEFAULT '',ai_summary TEXT DEFAULT '',importance TEXT DEFAULT '',translate_attempts INTEGER DEFAULT 0,translate_error TEXT DEFAULT '',translated_at TEXT DEFAULT '',ori_url TEXT,images TEXT DEFAULT '[]',decision TEXT,reason TEXT,stage TEXT DEFAULT 'collected',collected_at TEXT,processed_at TEXT,rev INTEGER DEFAULT 1,ai_score INTEGER,rules_score INTEGER,pushed TEXT DEFAULT '{}');CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE IF NOT EXISTS pushes(id INTEGER PRIMARY KEY,item_id INTEGER,channel TEXT DEFAULT '',kind TEXT DEFAULT '',source TEXT,sent_at TEXT,UNIQUE(source,item_id));CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY,label TEXT DEFAULT '',adapter TEXT DEFAULT 'plugin',command TEXT DEFAULT '',enabled INTEGER DEFAULT 1,targets TEXT DEFAULT '[]',last_seen TEXT DEFAULT '',note TEXT DEFAULT '');CREATE TABLE IF NOT EXISTS alison_groups(group_openid TEXT PRIMARY KEY,group_name TEXT DEFAULT '',group_finger_memo TEXT DEFAULT '',group_class_text TEXT DEFAULT '',group_tags TEXT DEFAULT '[]',group_member_num INTEGER,joined_at TEXT DEFAULT '',is_member INTEGER DEFAULT 1,push_enabled INTEGER DEFAULT 0,last_checked TEXT DEFAULT '',last_error TEXT DEFAULT '',updated_at TEXT DEFAULT '');CREATE TABLE IF NOT EXISTS processing_events(item_id INTEGER PRIMARY KEY,generation INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,available_at REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);'''

# 已删除的旧列（这里列出来是为了"别再被加回来"）：tweet_id / score / labels / final_decision，
# 以及旧版遗留的 published_ts / published_date / title / url / has_media / meta_attempts / meta_error。
# 现在的口径：tweet_id→origin_id、score→ai_score|rules_score、labels→ai_tags+importance、
#            final_decision→decision。
def _ensure_columns(c):
 existing={row[1] for row in c.execute('PRAGMA table_info(items)').fetchall()}
 additions={'ts':'INTEGER','date':'TEXT','ori_url':'TEXT','source':'TEXT','origin_id':'TEXT',
            'stage':"TEXT DEFAULT 'collected'",'decision':'TEXT',
            'translate_attempts':'INTEGER DEFAULT 0','translate_error':"TEXT DEFAULT ''",'translated_at':"TEXT DEFAULT ''",
            'ai_tags':"TEXT DEFAULT ''",'is_news':'INTEGER',
            'not_repeat':'INTEGER','not_comment':'INTEGER','not_sensitive':'INTEGER',"repeat_of":"TEXT DEFAULT ''",
            'ai_title':"TEXT DEFAULT ''",'ai_summary':"TEXT DEFAULT ''",'importance':"TEXT DEFAULT ''",
            # rev：每次改这一行就 +1，看板靠它判断"哪些老条目被更新了"（新条目看 id）
            'rev':'INTEGER DEFAULT 1',
            # ai_score：AI 打的价值分 0~100；rules_score：规则加权分（judge 老路阈值用）
            'ai_score':'INTEGER','rules_score':'INTEGER',
            # 引用（quote）推文：被引用的那条也随条目一起存。
            #   quoted_account 被引用作者 / quoted_raw 被引用原文 /
            #   quoted_zh 被引用原文的译文（翻译阶段填）/ quoted_url 被引用推文链接
            # 有引用的条目在 translate_db 里会把 not_comment 强制判绿，见那里的注释。
            'quoted_account':"TEXT DEFAULT ''",'quoted_raw':"TEXT DEFAULT ''",
            'quoted_zh':"TEXT DEFAULT ''",'quoted_url':"TEXT DEFAULT ''",
            # pushed：分发状态。{"通道:目标": true} —— 谁发过就置 true，见 distribute/push_db.py。
            # 待推送池只收 processed 且 decision='发' 的行；source 未发送状态记在 pushed，
            # 就是"这条该由我发"。新 source（新群/新通道）注册时回填到已有行上。
            'pushed':"TEXT DEFAULT '{}'"}
 for name,kind in additions.items():
  if name not in existing: c.execute(f'ALTER TABLE items ADD COLUMN {name} {kind}')
def ensure_schema(c):
 """建表 + 补齐后加的列；任何入口（采集/处理）先连库都应该调用它。"""
 c.executescript(SCHEMA)
 _ensure_columns(c)
 c.execute('CREATE INDEX IF NOT EXISTS items_distribution_pool_idx ON items(stage,decision,id)')
 c.execute('CREATE INDEX IF NOT EXISTS processing_events_due_idx ON processing_events(available_at,item_id)')
 c.execute('CREATE INDEX IF NOT EXISTS processing_events_queue_idx ON processing_events(created_at,item_id)')
 c.commit()

def open_db():
 c=sqlite3.connect(DB_FILE,timeout=10); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000'); ensure_schema(c); return c
# 冲突时**故意不覆盖 quoted_zh / zh / ai_***：那些是翻译与判定阶段的成果，
# 重复采集同一条推文只刷新原帖内容与引用信息，不能把已有译文冲掉。
def ensure_processing_event(c, item_id: int) -> None:
 """Create a retryable processing event without replacing a newer collector event."""
 c.execute(
  "INSERT OR IGNORE INTO processing_events(item_id,generation,created_at,available_at,attempts) "
  "VALUES(?,1,?,0,0)",
  (int(item_id), datetime.now().isoformat(timespec='microseconds')),
 )


def enqueue_processing_event(c, item_id: int) -> None:
 """Publish a fresh input revision to the durable processing subscription."""
 c.execute(
  "INSERT INTO processing_events(item_id,generation,created_at,available_at,attempts) "
  "VALUES(?,1,?,0,0) ON CONFLICT(item_id) DO UPDATE SET "
  "generation=processing_events.generation+1,created_at=excluded.created_at,available_at=0,attempts=0",
  (int(item_id), datetime.now().isoformat(timespec='microseconds')),
 )


def upsert_raw(item):
 key=f"{item.get('platform','x')}:{item.get('origin_id') or item.get('tweet_id') or item.get('account','')}"
 c=open_db()
 now=datetime.now().isoformat(timespec='seconds')
 values={
  'platform':item.get('platform','x'),'source':item.get('source','x'),
  'origin_id':str(item.get('origin_id') or item.get('tweet_id') or ''),'account':item.get('account',''),
  'orig_account':item.get('orig_account',''),'forward_comment':item.get('forward_comment',''),
  'quoted_account':item.get('quoted_account',''),'quoted_raw':item.get('quoted_raw',''),
  'quoted_url':item.get('quoted_url',''),'ts':item.get('ts',0),'date':item.get('date',''),
  'content_type':item.get('content_type',''),'raw':str(item.get('raw') or ''),
  'ori_url':item.get('ori_url') or item.get('url',''),
  'images':json.dumps(item.get('images',[]),ensure_ascii=False),
 }
 old=c.execute("SELECT id,raw,account,quoted_account,quoted_raw FROM items WHERE dedup_key=?",(key,)).fetchone()
 is_new=old is None
 text_changed=is_new or any(
  str(old[field] or '') != str(values[field] or '')
  for field in ('raw','account','quoted_account','quoted_raw')
 )
 if is_new:
  c.execute('''INSERT INTO items(platform,source,origin_id,dedup_key,account,orig_account,forward_comment,
   quoted_account,quoted_raw,quoted_zh,quoted_url,ts,date,content_type,raw,zh,ori_url,images,stage,collected_at)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
   (values['platform'],values['source'],values['origin_id'],key,values['account'],values['orig_account'],
    values['forward_comment'],values['quoted_account'],values['quoted_raw'],'',values['quoted_url'],
    values['ts'],values['date'],values['content_type'],values['raw'],'',values['ori_url'],values['images'],
    'collected',now))
  item_id=int(c.execute('SELECT id FROM items WHERE dedup_key=?',(key,)).fetchone()[0])
 else:
  item_id=int(old['id'])
  if text_changed:
   c.execute('''UPDATE items SET raw=?,account=?,images=?,content_type=?,quoted_account=?,quoted_raw=?,quoted_url=?,quoted_zh='',
    zh='',ai_tags='',is_news=NULL,not_repeat=NULL,not_comment=NULL,not_sensitive=NULL,repeat_of='',
    ai_title='',ai_summary='',importance='',ai_score=NULL,rules_score=NULL,stage='collected',
    decision=NULL,reason='',translate_attempts=0,translate_error='',translated_at='',processed_at='',
    rev=COALESCE(rev,0)+1 WHERE id=?''',
    (values['raw'],values['account'],values['images'],values['content_type'],values['quoted_account'],
     values['quoted_raw'],values['quoted_url'],item_id))
  else:
   c.execute('''UPDATE items SET images=?,content_type=?,quoted_url=?,rev=COALESCE(rev,0)+1 WHERE id=?''',
    (values['images'],values['content_type'],values['quoted_url'],item_id))
 if text_changed and values['raw'].strip():
  enqueue_processing_event(c,item_id)
 c.commit()
 c.close()
 return item_id
