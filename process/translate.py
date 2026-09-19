# -*- coding: utf-8 -*-
"""P3 processing entry: translate raw items before judge."""
import json
import sys
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_COLLECT = _REPO / "collect"
_PROCESS = Path(__file__).resolve().parent
for p in (_REPO, _COLLECT, _PROCESS):
    if str(p) not in sys.path: sys.path.insert(0, str(p))
from ai import translate_to_chinese
from process.paths import RAW_DIR, WORK_DIR, LOG_DIR
from process.logging_util import make_log, setup_console_encoding

log=make_log(LOG_DIR/"translate.log")

def translate_file(path: Path) -> Path:
    data=json.loads(path.read_text(encoding='utf-8'))
    for item in data:
        if not item.get('zh'):
            item['zh']=translate_to_chinese(str(item.get('raw') or item.get('title') or ''))
        item['stage']='translated'
    WORK_DIR.mkdir(parents=True,exist_ok=True)
    out=WORK_DIR/f"translated_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return out

def main():
    source=Path(sys.argv[1]) if len(sys.argv)>1 else sorted(RAW_DIR.glob('new_news_*.json'))[-1]
    log(f"[输入] {source}")
    out=translate_file(source)
    log(f"[输出] {out}")
    return out

if __name__=='__main__':
    setup_console_encoding(); main()
