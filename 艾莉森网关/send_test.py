# -*- coding: utf-8 -*-
"""
一次性"主动消息"测试（走 OpenAPI HTTP，不动常驻网关）
用法：
    发群：venv\\Scripts\\python.exe send_test.py "文本"
    发私聊：venv\\Scripts\\python.exe send_test.py --user "文本"
"""
import sys
import json
from pathlib import Path
import httpx

HERE = Path(__file__).resolve().parent


def load_env():
    env = {}
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    env = load_env()
    appid = env.get("APPID", "").strip()
    secret = env.get("SECRET", "").strip()
    sandbox = str(env.get("SANDBOX", "0")).strip().lower() in ("1", "true", "yes", "on")
    host = "https://sandbox.api.sgroup.qq.com" if sandbox else "https://api.sgroup.qq.com"
    state = json.loads((HERE / "state.json").read_text(encoding="utf-8"))

    args = sys.argv[1:]
    to_user = False
    if args and args[0] == "--user":
        to_user = True
        args = args[1:]
    text = args[0] if args else "艾莉森测试：这是一条主动消息 ✅"

    if to_user:
        target = state.get("user_openid", "")
        url = f"{host}/v2/users/{target}/messages"
        print(f"[target] user_openid={target}")
    else:
        target = state.get("group_openid", "")
        url = f"{host}/v2/groups/{target}/messages"
        print(f"[target] group_openid={target}")

    print(f"[env] sandbox={sandbox} host={host}")

    # 1) 取 access token
    r = httpx.post("https://bots.qq.com/app/getAppAccessToken",
                   json={"appId": appid, "clientSecret": secret}, timeout=30)
    print("[token]", r.status_code, r.text[:120])
    token = r.json().get("access_token", "")

    # 2) 主动发消息（不带 msg_id = 主动）
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json",
               "X-Union-Appid": appid}
    body = {"msg_type": 0, "content": text, "msg_seq": 1}
    r2 = httpx.post(url, headers=headers, json=body, timeout=30)
    print("[send]", r2.status_code, r2.text)


if __name__ == "__main__":
    main()
