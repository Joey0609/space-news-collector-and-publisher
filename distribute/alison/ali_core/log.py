# -*- coding: utf-8 -*-
"""日志：统一 "[时间] 消息" 格式，打印 + 追加写 艾莉森网关/logs/推送.log。"""
import datetime
import sys

from .paths import LOG_FILE


def setup_console_encoding() -> None:
    """避免 Windows GBK 控制台打印中文/表情崩溃。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    """带时间戳的日志（控制台 + 文件；任一步失败都不影响主流程）。"""
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
