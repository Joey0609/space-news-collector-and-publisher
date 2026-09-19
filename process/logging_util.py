# -*- coding: utf-8 -*-
"""日志：统一"[时间] 消息"格式，同时打印到控制台和各自的日志文件。

用法：
    from process.logging_util import make_log
    from process.paths import LOG_DIR
    log = make_log(LOG_DIR / "news_fetch.log")
"""
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable


def setup_console_encoding() -> None:
    """避免 Windows GBK 控制台打印中文崩溃（失败不影响运行）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def make_log(log_file: Path, echo: bool = True) -> Callable[[str], None]:
    """造一个 log(msg)：打印 + 追加写 log_file。目录不存在会自动建。"""

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        if echo:
            print(line)
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    return log
