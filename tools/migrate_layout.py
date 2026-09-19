# -*- coding: utf-8 -*-
"""Migrate legacy runtime files into data/ and initialize SQLite.
Usage: python tools/migrate_layout.py --dry-run | --apply
"""
from __future__ import annotations
import argparse, json, shutil, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
MOVES=[
 ('配置/待推送.json','data/queue/待推送.json'),('配置/已发记录.json','data/queue/已发记录.json'),
 ('配置/内容留档.json','data/queue/内容留档.json'),('配置/队列档案.json','data/queue/队列档案.json'),
 ('配置/抓取基线.json','data/state/抓取基线.json'),('配置/扫描游标.json','data/state/扫描游标.json'),
 ('配置/推送状态.json','data/state/推送状态.json'),('抓取脚本/accounts.db','data/state/accounts.db')]

def main():
 p=argparse.ArgumentParser(); p.add_argument('--apply',action='store_true'); p.add_argument('--dry-run',action='store_true'); a=p.parse_args()
 for d in ('data/raw','data/work','data/images','data/queue','data/state','data/markdown','config'):
  print('[mkdir]',d)
  if a.apply:(ROOT/d).mkdir(parents=True,exist_ok=True)
 for src,dst in MOVES:
  s=ROOT/src; d=ROOT/dst
  if not s.exists(): continue
  print('[move]',src,'->',dst)
  if a.apply:
   d.parent.mkdir(parents=True,exist_ok=True)
   if d.exists(): print('[skip conflict]',dst)
   else: shutil.move(str(s),str(d))
 legacy_output=ROOT/'抓取脚本'/'output'
 for f in list(legacy_output.glob('*')) if legacy_output.exists() else []:
  dst=ROOT/'data'/'images'/f.name if f.suffix.lower() in ('.jpg','.jpeg','.png','.webp') else ROOT/'data'/'raw'/f.name
  print('[move]',f.relative_to(ROOT),'->',dst.relative_to(ROOT))
  if a.apply:
   dst.parent.mkdir(parents=True,exist_ok=True)
   if not dst.exists(): shutil.move(str(f),str(dst))
 if a.apply:
  try:
   from process.db import open_db, upsert_item
  except ImportError:
   print('[warn] process.db is not installed; files moved but DB backfill skipped')
  else:
   conn=open_db(ROOT/'data'/'hx.sqlite3')
   for folder in ('raw','work','queue'):
    for f in (ROOT/'data'/folder).glob('*.json'):
     try:
      data=json.loads(f.read_text(encoding='utf-8-sig'))
      if isinstance(data,list):
       for item in data: upsert_item(conn,item)
     except Exception as e: print('[warn]',f,e)
   conn.close()
  print('[done] migration applied')
 else: print('[dry-run] no files changed')
if __name__=='__main__': main()
