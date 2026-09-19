# -*- coding: utf-8 -*-
"""诊断脚本 · 一次性"主动消息"测试（走 OpenAPI HTTP，不动常驻网关）

用法（在 艾莉森网关 目录下）：
    发群：venv\\Scripts\\python.exe send_test.py "文本"
    发私聊：venv\\Scripts\\python.exe send_test.py --user "文本"
"""
import sys

import httpx

from ali_core.env import api_host, credentials, group_openid, is_sandbox, load_env, load_state
from ali_core.log import log, setup_console_encoding
from ali_core.qq_api import TOKEN_URL


def main():
    env = load_env()
    appid, secret = credentials(env)
    sandbox = is_sandbox(env)
    host = api_host(env)
    state = load_state()

    args = sys.argv[1:]
    to_user = False
    if args and args[0] == "--user":
        to_user = True
        args = args[1:]
    text = args[0] if args else "艾莉森测试：这是一条主动消息 ✅"

    if to_user:
        target = str(state.get("user_openid", "") or "")
        url = f"{host}/v2/users/{target}/messages"
        log(f"[target] user_openid={target}")
    else:
        target = group_openid(log)
        url = f"{host}/v2/groups/{target}/messages"
        log(f"[target] group_openid={target}")

    log(f"[env] sandbox={sandbox} host={host}")

    # 1) 取 access token
    r = httpx.post(TOKEN_URL, json={"appId": appid, "clientSecret": secret}, timeout=30)
    log(f"[token] {r.status_code} {r.text[:120]}")
    token = r.json().get("access_token", "")

    # 2) 主动发消息（不带 msg_id = 主动）
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json",
               "X-Union-Appid": appid}
    body = {"msg_type": 0, "content": text, "msg_seq": 1}
    r2 = httpx.post(url, headers=headers, json=body, timeout=30)
    log(f"[send] {r2.status_code} {r2.text}")


if __name__ == "__main__":
    setup_console_encoding()
    main()
