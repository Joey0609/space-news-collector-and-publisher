# -*- coding: utf-8 -*-
"""collect public entry; X-specific implementation lives in sources/x/news_fetch.py."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from sources.x.news_fetch import *  # noqa: F401,F403
from sources.x.news_fetch import main
if __name__=='__main__':
    import asyncio
    # 父进程（main_flow / 看板）按 UTF-8 读我们的输出；非交互（管道/重定向）时必须也写 UTF-8，
    # 否则 Windows 会退回 GBK，父进程按 UTF-8 解码就变成乱码。
    if not sys.stdout.isatty():
        from collect.logging_util import setup_console_encoding
        setup_console_encoding()
    asyncio.run(main())
