# -*- coding: utf-8 -*-
"""日志：统一"[时间] 消息"格式，同时打印到控制台和各自的日志文件。

用法：
    from collect.logging_util import make_log
    from collect.paths import LOG_DIR
    log = make_log(LOG_DIR / "news_fetch.log")
"""
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable


def setup_console_encoding() -> None:
    """避免 Windows GBK 控制台打印中文崩溃（失败不影响运行）。

    同时打开行缓冲：输出被父进程（main_flow / 看板）接成管道时不 flush，
    日志会卡在 8KB 缓冲区里，表现就是"只看得到 loguru、看不到自己的日志"。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def make_log(log_file: Path, echo: bool = True) -> Callable[[str], None]:
    """造一个 log(msg)：打印 + 追加写 log_file。目录不存在会自动建。"""

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        if echo:
            print(line, flush=True)
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    return log
