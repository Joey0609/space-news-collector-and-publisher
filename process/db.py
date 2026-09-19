# -*- coding: utf-8 -*-
"""Process-owned SQLite access; no JSON handoff is required between stages."""
import json, sqlite3
from pathlib import Path
from collect.paths import DB_FILE

def open_db():
 c=sqlite3.connect(DB_FILE,timeout=10); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000'); return c

def rows(stage=None):
 c=open_db(); q='SELECT * FROM items'; args=()
 if stage: q+=' WHERE stage=?'; args=(stage,)
 result=[dict(r) for r in c.execute(q,args).fetchall()]; c.close(); return result

def update(item_id, **fields):
 c=open_db(); keys=[]; vals=[]
 for k,v in fields.items():
  if k=='images': v=json.dumps(v,ensure_ascii=False)
  keys.append(f'{k}=?'); vals.append(v)
 vals.append(item_id); c.execute('UPDATE items SET '+','.join(keys)+' WHERE id=?',vals); c.commit(); c.close()
