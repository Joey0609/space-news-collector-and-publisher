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
    asyncio.run(main())
