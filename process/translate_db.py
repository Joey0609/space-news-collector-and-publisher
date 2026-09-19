# -*- coding: utf-8 -*-
"""Translate collected database items directly into database rows."""
import json, sys
from datetime import datetime
from pathlib import Path
_PROCESS=Path(__file__).resolve().parent; _ROOT=_PROCESS.parent
for p in (_ROOT,_PROCESS):
 if str(p) not in sys.path: sys.path.insert(0,str(p))
from ai import translate_to_chinese
from db import open_db, rows

def main():
 c=open_db()
 for item in rows('collected'):
  if not item.get('zh'):
   zh=translate_to_chinese(str(item.get('raw') or ''))
   c.execute("UPDATE items SET zh=?,stage='translated',processed_at=? WHERE id=?",(zh,datetime.now().isoformat(timespec='seconds'),item['id']))
 c.commit(); c.close()
if __name__=='__main__': main()
