# -*- coding: utf-8 -*-
"""群主动消息 变体测试：1) 无 msg_seq  3) is_wakeup"""
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


env = load_env()
appid, secret = env["APPID"].strip(), env["SECRET"].strip()
state = json.loads((HERE / "state.json").read_text(encoding="utf-8"))
goid = state["group_openid"]
url = f"https://api.sgroup.qq.com/v2/groups/{goid}/messages"

r = httpx.post("https://bots.qq.com/app/getAppAccessToken",
               json={"appId": appid, "clientSecret": secret}, timeout=30)
token = r.json()["access_token"]
h = {"Authorization": f"QQBot {token}", "Content-Type": "application/json", "X-Union-Appid": appid}

variants = {
    "1-无msg_seq": {"msg_type": 0, "content": "变体1：群主动·不带msg_seq"},
    "3-is_wakeup": {"msg_type": 0, "content": "变体3：群主动·带is_wakeup", "is_wakeup": True},
}
for name, body in variants.items():
    try:
        rr = httpx.post(url, headers=h, json=body, timeout=30)
        print(f"[{name}] {rr.status_code} {rr.text[:160]}")
    except Exception as e:
        print(f"[{name}] EXC {e}")
