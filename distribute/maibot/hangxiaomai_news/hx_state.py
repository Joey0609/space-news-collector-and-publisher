# -*- coding: utf-8 -*-
"""hx_state.py · 推送节奏状态（配置/推送状态.json）
================================================
调度需要的"记忆"都在这一个文件里，键名如下（都按目标 kind 分开存）：

    last_push_ts            上次成功推送时间戳（全局，兼容旧字段）
    last_push_ts_<kind>     该目标上次推送时间戳
    next_gap_<kind>         该目标下次间隔秒数（随机出来的）
    empty_cycles_<kind>     连续几轮"无内容"（连续 3 次才闲聊兜底）
    prev_active_<kind>      上次该目标是否在活跃时段（1/0，用于识别"刚进入活跃"）
    prev_active_sig_<kind>  上次的活跃时段配置（配置变了就重置前态，避免误触发首推）
    daily_summary_done_<kind>  该目标"每日首推"已完成的日期
    last_push_date / daily_push_count   今日推送计数（跨天自动清零）
    boot_wall_base          开机墙钟基准（用于检测系统休眠唤醒）

术语：kind = "private"（私聊 target_qq）或 "group_<群号>"。
"""
import json
import time
from pathlib import Path
from typing import Any


def _read(state_file: Path) -> dict:
    try:
        if state_file.exists():
            d = json.loads(state_file.read_text(encoding="utf-8-sig"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _write(state_file: Path, d: dict) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_field(state_file: Path, key: str, default: Any = None):
    return _read(state_file).get(key, default)


def save_field(state_file: Path, key: str, value: Any) -> None:
    """写一个字段（合并写入，不清空其它字段）。"""
    d = _read(state_file)
    d[key] = value
    _write(state_file, d)


def save_fields(state_file: Path, fields: dict) -> None:
    """一次写多个字段。"""
    d = _read(state_file)
    d.update(fields)
    _write(state_file, d)


def load_last_push(state_file: Path) -> float:
    """读上次推送时间戳（秒）；没有返回 0.0。"""
    try:
        return float(load_field(state_file, "last_push_ts", 0) or 0)
    except Exception:
        return 0.0


def save_last_push(state_file: Path, ts: float) -> None:
    save_field(state_file, "last_push_ts", ts)


# ────────────────────────── 每日上限 ──────────────────────────

def daily_reached(state_file: Path, max_count: int) -> bool:
    """今日推送数是否已达上限（max_count<=0 表示不限）；跨天自动清零。"""
    if max_count <= 0:
        return False
    day_key = time.strftime("%Y-%m-%d")
    cur_day = load_field(state_file, "last_push_date", "")
    if cur_day != day_key:
        save_fields(state_file, {"last_push_date": day_key, "daily_push_count": 0})
        return False
    count = int(load_field(state_file, "daily_push_count", 0) or 0)
    return count >= max_count


def record_daily_push(state_file: Path) -> None:
    """记一次成功推送（累加今日次数）。"""
    day_key = time.strftime("%Y-%m-%d")
    if load_field(state_file, "last_push_date", "") == day_key:
        count = int(load_field(state_file, "daily_push_count", 0) or 0)
        save_field(state_file, "daily_push_count", count + 1)
    else:
        save_fields(state_file, {"last_push_date": day_key, "daily_push_count": 1})


# ────────────────────────── 休眠唤醒检测 ──────────────────────────

def resume_occurred(state_file: Path) -> bool:
    """判断系统是否刚从休眠/睡眠唤醒。

    原理：GetTickCount64(不含睡眠时间) 与 time.time()(墙钟) 的差值在正常运行时恒定；
    休眠会让墙钟往前走而 tick 不动 → 差值相对基线上跳 >60s 即判定"睡过"。
    返回 True 时已把基准更新为当前值。
    """
    try:
        import ctypes
        tick = ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return False
    wall = time.time()
    est = wall - tick  # 恒定的"开机墙钟偏移"，睡眠时会增大
    base = load_field(state_file, "boot_wall_base", 0.0)
    if not base:
        save_field(state_file, "boot_wall_base", est)
        return False
    if est - base > 60:
        save_field(state_file, "boot_wall_base", est)
        return True
    return False
