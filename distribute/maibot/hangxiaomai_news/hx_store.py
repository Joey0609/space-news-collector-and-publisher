# -*- coding: utf-8 -*-
"""hx_store.py · 航小麦数据文件的读写（JSON/文本，一律容错）
=======================================================
管这几份文件（都在 航小麦/配置/ 下）：
    待推送.json     队列：模块5 写、本插件读+改写（移除已发完的）
    功能开关.toml   全局总闸：新闻推送 = true/false
    内容留档.json   队列空时的兜底内容
    已发记录.json   已推送过的（去重，永不重推）
    提醒待发.txt    存在则启动时发一条并删除
"""
import json
from pathlib import Path
from typing import Any

if __package__ in (None, ""):   # 作为顶层模块加载（插件目录在 sys.path 上）
    from hx_paths import ARCHIVE_FILE, PENDING_FILE, REMINDER_FILE, SENT_EVER_FILE, SWITCH_FILE
    from hx_util import dedup_key
else:                            # 正常包内导入
    from .hx_paths import ARCHIVE_FILE, PENDING_FILE, REMINDER_FILE, SENT_EVER_FILE, SWITCH_FILE
    from .hx_util import dedup_key

MAX_SENT_RECORDS = 500   # 已发记录最多保留条数


# ────────────────────────── 通用 JSON ──────────────────────────

def load_json_file(path: Path, default: Any = None):
    """读一个 json（容忍 BOM）；失败/不存在返回 default。"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def save_json_file(path: Path, data: Any) -> None:
    """写一个 json（UTF-8、不转义中文）；失败静默（不动主流程）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ────────────────────────── 功能开关（全局总闸）──────────────────────────

def load_switch_state() -> bool:
    """读 功能开关.toml 的"新闻推送"，默认 true。"""
    try:
        if not SWITCH_FILE.exists():
            return True
        for line in SWITCH_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("新闻推送") and "=" in line:
                return line.split("=", 1)[1].strip().lower() == "true"
        return True
    except Exception:
        return True


# ────────────────────────── 待推送队列 ──────────────────────────

def load_pending() -> list:
    """读 待推送.json（模块5 写的候选队列）。"""
    data = load_json_file(PENDING_FILE, [])
    return data if isinstance(data, list) else []


def save_pending(items: list) -> None:
    """把剩余待推送写回。"""
    save_json_file(PENDING_FILE, items)


# ────────────────────────── 内容留档 / 已发记录 ──────────────────────────

def load_archive() -> list:
    """读 内容留档.json（旧闻兜底用）。"""
    data = load_json_file(ARCHIVE_FILE, [])
    return data if isinstance(data, list) else []


def save_archive(items: list) -> None:
    save_json_file(ARCHIVE_FILE, items)


def archive_to_store(items: list) -> None:
    """把条目追加进 内容留档.json（按 账号+内容 去重）。"""
    if not items:
        return
    archive = load_archive()
    seen = {dedup_key(x) for x in archive}
    for it in items:
        k = dedup_key(it)
        if k not in seen:
            archive.append(it)
            seen.add(k)
    save_archive(archive)


def record_sent(items: list) -> None:
    """把已推送的条目记入 已发记录.json（按 账号+内容 去重，保留最近 MAX_SENT_RECORDS 条）。"""
    if not items:
        return
    records = load_json_file(SENT_EVER_FILE, []) or []
    if not isinstance(records, list):
        records = []
    seen = {dedup_key(x) for x in records}
    for it in items:
        k = dedup_key(it)
        if k not in seen:
            records.append(it)
            seen.add(k)
    save_json_file(SENT_EVER_FILE, records[-MAX_SENT_RECORDS:])


# ────────────────────────── 一次性提醒 ──────────────────────────

def take_reminder() -> str:
    """取"提醒待发.txt"内容并删除文件（一次性提醒）；没有则返回 ''。"""
    try:
        if not REMINDER_FILE.exists():
            return ""
        text = REMINDER_FILE.read_text(encoding="utf-8").strip()
        REMINDER_FILE.unlink(missing_ok=True)
        return text
    except Exception:
        return ""
