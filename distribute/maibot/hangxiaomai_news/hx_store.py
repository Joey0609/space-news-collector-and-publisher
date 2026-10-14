# -*- coding: utf-8 -*-
"""功能开关与一次性提醒。新闻内容仅由看板 WebSocket 提供。"""
if __package__ in (None, ""):
    from hx_paths import REMINDER_FILE, SWITCH_FILE
else:
    from .hx_paths import REMINDER_FILE, SWITCH_FILE

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
