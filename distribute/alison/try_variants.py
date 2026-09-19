# -*- coding: utf-8 -*-
"""诊断脚本 · 群主动消息"参数变体"试验：1) 不带 msg_seq  3) 带 is_wakeup

用法（在 艾莉森网关 目录下）：
    venv\\Scripts\\python.exe try_variants.py
"""
import httpx

from ali_core.env import api_host, credentials, is_sandbox, load_env, load_state
from ali_core.log import log, setup_console_encoding
from ali_core.qq_api import TOKEN_URL


def main():
    env = load_env()
    appid, secret = credentials(env)
    host = api_host(env)
    log(f"[env] sandbox={is_sandbox(env)} host={host}")
    goid = str(load_state().get("group_openid", "") or "")
    url = f"{host}/v2/groups/{goid}/messages"

    r = httpx.post(TOKEN_URL, json={"appId": appid, "clientSecret": secret}, timeout=30)
    token = r.json()["access_token"]
    h = {"Authorization": f"QQBot {token}", "Content-Type": "application/json", "X-Union-Appid": appid}

    variants = {
        "1-无msg_seq": {"msg_type": 0, "content": "变体1：群主动·不带msg_seq"},
        "3-is_wakeup": {"msg_type": 0, "content": "变体3：群主动·带is_wakeup", "is_wakeup": True},
    }
    for name, body in variants.items():
        try:
            rr = httpx.post(url, headers=h, json=body, timeout=30)
            log(f"[{name}] {rr.status_code} {rr.text[:160]}")
        except Exception as e:
            log(f"[{name}] EXC {e}")


if __name__ == "__main__":
    setup_console_encoding()
    main()
