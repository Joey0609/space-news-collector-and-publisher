# -*- coding: utf-8 -*-
"""JSON 读写：读失败一律给默认值（state.json 半截写坏也不该让程序崩）。"""
import json
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any = None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def save_json(path: Path, data: Any) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False
