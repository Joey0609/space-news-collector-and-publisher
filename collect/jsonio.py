# -*- coding: utf-8 -*-
"""JSON / TOML 读写小工具（读失败一律给默认值，绝不抛给调用方）。"""
import json
import tomllib
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any = None):
    """读 JSON；不存在/解析失败返回 default。"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def save_json(path: Path, data: Any) -> bool:
    """写 JSON（UTF-8、不转义中文、缩进 2）；成功 True。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_toml(path: Path) -> dict:
    """读 TOML；不存在/解析失败返回 {}。"""
    try:
        if path.exists():
            with open(path, "rb") as f:
                return tomllib.load(f)
    except Exception:
        pass
    return {}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base 的同名键；嵌套 dict 逐层合并。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def latest_file(directory: Path, pattern: str) -> Path | None:
    """取目录里按文件名排序（即时间序）的最后一个匹配文件。"""
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None
