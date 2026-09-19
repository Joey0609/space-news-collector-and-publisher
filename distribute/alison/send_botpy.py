# -*- coding: utf-8 -*-
"""诊断脚本 · 用 botpy 官方 BotAPI 发群主动消息（用来排除 raw 请求问题）

用法（在 艾莉森网关 目录下）：
    venv\\Scripts\\python.exe send_botpy.py "文本"
"""
import asyncio
import sys

from botpy.api import BotAPI
from botpy.http import BotHttp

from ali_core.env import credentials, is_sandbox, load_env, load_state
from ali_core.log import log, setup_console_encoding


async def main():
    env = load_env()
    appid, secret = credentials(env)
    sandbox = is_sandbox(env)
    goid = str(load_state().get("group_openid", "") or "")
    text = sys.argv[1] if len(sys.argv) > 1 else "botpy官方方法-群主动测试"

    http = BotHttp(timeout=30, is_sandbox=sandbox, app_id=appid, secret=secret)
    api = BotAPI(http)
    log(f"[botpy] sandbox={sandbox} group_openid={goid}")
    try:
        r = await api.post_group_message(group_openid=goid, msg_type=0, content=text)
        log(f"[botpy OK] {r}")
    except Exception as e:
        log(f"[botpy FAIL] {type(e).__name__} {e}")
    try:
        await http.close()
    except Exception:
        pass


if __name__ == "__main__":
    setup_console_encoding()
    asyncio.run(main())
