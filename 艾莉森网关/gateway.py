# -*- coding: utf-8 -*-
"""
艾莉森网关 · 阶段1（最小验证）
================================
目标：连上 QQ 官方机器人（botpy）→ 收群事件（拿 group_openid）→ 被动回复 → 主动发一条测试文本。

运行：
    venv\\Scripts\\python.exe gateway.py
前置：
    在 .env 填 APPID / SECRET（见 .env.example）
用法：
    启动后，在沙箱群里 @ 一下机器人 → 本程序会打印 group_openid、被动回复一条，并主动再发一条测试文本。
"""
import os
import sys
import json
import time
import asyncio
import datetime
from pathlib import Path

import botpy
from botpy.message import GroupMessage

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
LOG_FILE = HERE / "logs" / "gateway.log"
STATE_FILE = HERE / "state.json"

TEST_TEXT = "艾莉森已上线（阶段1测试）✅ 这是主动消息。"


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("APPID", "SECRET", "SANDBOX_GROUP", "SANDBOX"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    return env


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(d: dict) -> None:
    STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


class AlisonClient(botpy.Client):
    # 本轮是否已发过"主动测试"（避免 @ 多次重复刷屏）
    _active_tested = False

    async def on_ready(self):
        name = getattr(getattr(self, "robot", None), "name", "?")
        log(f"✅ 已连接（在线）：{name}")

    async def _handle_group(self, message: GroupMessage, tag: str):
        goid = str(getattr(message, "group_openid", "") or "")
        log(f"[{tag}] group_openid={goid} | content={getattr(message, 'content', '')!r}")
        if goid:
            st = load_state()
            st["group_openid"] = goid
            st["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            save_state(st)
            log(f"已记录 group_openid 到 state.json")

        # 1) 被动回复（验证"能收能回"）
        try:
            await self.api.post_group_message(
                group_openid=goid, msg_type=0,
                content="艾莉森已收到（被动回复测试）✅",
                msg_id=message.id, msg_seq=1,
            )
            log("被动回复已发送 ✅")
        except Exception as e:
            log(f"被动回复失败: {e}")

        # 2) 主动发一条（验证"能主动发"，本轮只发一次）
        if not AlisonClient._active_tested:
            AlisonClient._active_tested = True
            try:
                await self.api.post_group_message(
                    group_openid=goid, msg_type=0,
                    content=TEST_TEXT, msg_seq=1,
                )
                log("主动消息已发送 ✅")
            except Exception as e:
                log(f"主动消息失败: {e}")

    async def on_group_at_message_create(self, message: GroupMessage):
        await self._handle_group(message, "群@消息")

    async def on_group_message_create(self, message: GroupMessage):
        await self._handle_group(message, "群消息")

    async def on_c2c_message_create(self, message):
        a = getattr(message, "author", None)
        uid = ""
        for attr in ("id", "user_openid"):
            v = getattr(a, attr, None) if a is not None else None
            if v:
                uid = str(v)
                break
        if not uid and isinstance(a, dict):
            uid = str(a.get("id") or a.get("user_openid") or "")
        log(f"[私聊消息] author={a!r} -> user_openid={uid} | content={getattr(message, 'content', '')!r}")
        if uid:
            st = load_state()
            st["user_openid"] = uid
            st["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            save_state(st)
            log("已记录 user_openid 到 state.json")
        try:
            await self.api.post_c2c_message(
                openid=uid, msg_type=0,
                content="艾莉森已收到（私聊测试）✅",
                msg_id=message.id, msg_seq=1,
            )
            log("私聊被动回复已发送 ✅")
        except Exception as e:
            log(f"私聊被动回复失败: {e}")


def main():
    log("=" * 50)
    log("艾莉森网关 · 阶段1（最小验证）启动")
    env = load_env()
    appid = str(env.get("APPID", "")).strip()
    secret = str(env.get("SECRET", "")).strip()
    sandbox = str(env.get("SANDBOX", "0")).strip().lower() in ("1", "true", "yes", "on")
    if not appid or not secret:
        log("❌ 未配置 APPID/SECRET：请在 艾莉森网关\\.env 里填写（见 .env.example）")
        sys.exit(1)
    log(f"APPID={appid[:6]}...  环境={'沙箱' if sandbox else '正式'}")
    # Python 3.14 兼容：botpy 内部用 asyncio.get_event_loop()，需先显式建 loop
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass
    intents = botpy.Intents(public_messages=True)
    client = AlisonClient(intents=intents, is_sandbox=sandbox)
    try:
        client.run(appid=appid, secret=secret)
    except Exception as e:
        log(f"❌ 运行异常: {e}")
        raise


if __name__ == "__main__":
    main()
