# -*- coding: utf-8 -*-
"""用 botpy 官方 BotAPI 发群主动消息（排除 raw 请求问题）"""
import asyncio
import json
import sys
from pathlib import Path

from botpy.http import BotHttp
from botpy.api import BotAPI

HERE = Path(__file__).resolve().parent


def load_env():
    env = {}
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


async def main():
    env = load_env()
    appid = env["APPID"].strip()
    secret = env["SECRET"].strip()
    sandbox = str(env.get("SANDBOX", "0")).strip().lower() in ("1", "true", "yes", "on")
    state = json.loads((HERE / "state.json").read_text(encoding="utf-8"))
    goid = state["group_openid"]
    text = sys.argv[1] if len(sys.argv) > 1 else "botpy官方方法-群主动测试"

    http = BotHttp(timeout=30, is_sandbox=sandbox, app_id=appid, secret=secret)
    api = BotAPI(http)
    print(f"[botpy] sandbox={sandbox} group_openid={goid}")
    try:
        r = await api.post_group_message(group_openid=goid, msg_type=0, content=text)
        print("[botpy OK]", r)
    except Exception as e:
        print("[botpy FAIL]", type(e).__name__, e)
    try:
        await http.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
