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
    """避免 Windows GBK 控制台打印中文崩溃（失败不影响运行）。

    同时打开行缓冲：输出被父进程（main_flow / 看板）接成管道时不 flush，
    日志会卡在 8KB 缓冲区里，表现就是"看板终端看不到实时日志"。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def one_line(text: str) -> str:
    """把消息压成一行。

    日志是按行消费的（看板会给每一行加 [处理] 前缀），消息里带换行的话，
    一条记录会被劈成两半：前半截带时间戳和标签，后半截变成没有时间戳的裸行。
    模型返回的译文、异常信息里都可能有换行，所以统一在这里压平。
    """
    return " ".join(str(text).splitlines())


def make_log(log_file: Path, echo: bool = True) -> Callable[[str], None]:
    """造一个 log(msg)：打印 + 追加写 log_file。目录不存在会自动建。"""

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {one_line(msg)}"
        if echo:
            print(line, flush=True)
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    return log
