# -*- coding: utf-8 -*-
"""P3 normalization helper for raw collection outputs."""
import json
from pathlib import Path
from collect.contract import normalize_raw, validate_raw

def normalize_file(path: Path) -> list[dict]:
    data=json.loads(path.read_text(encoding='utf-8'))
    out=[]
    for item in data:
        item=normalize_raw(item)
        ok,reason=validate_raw(item)
        if ok: out.append(item)
    return out
