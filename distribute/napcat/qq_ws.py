# -*- coding: utf-8 -*-
"""qq_ws.py · NapCat OneBot11 WebSocket 公共发送逻辑
==================================================
send_qq.py（本机 NapCat）和 send_qq_nb.py（经笔记本 NapCat）用同一套发送代码，
区别只有 WS 地址/ token / 目标 QQ，所以发送逻辑集中在这里，两个脚本只留配置。

依赖：websockets
"""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[2]   # repository root


def make_log(name: str):
    """造一个 log(msg)：打印 + 写 logs/<name>.log（项目规范：所有程序带日志）。"""
    log_file = ROOT / "logs" / name

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line)
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    return log


def connect(url: str, token: str = "", open_timeout: float = 15):
    """连 NapCat 正向 WS（带 token 时用 Authorization: Bearer）。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return websockets.connect(url, additional_headers=headers, open_timeout=open_timeout)


async def call_action(ws, action: str, params: dict, log=print,
                      echo_prefix: str = "hx", attempts: int = 4,
                      per_try_timeout: float = 15.0) -> dict:
    """发一个 OneBot11 动作并等它的响应（按 echo 匹配）；超时/失败返回 {}。

    期间收到的其它事件（群消息回显等）会记一条"忽略"日志。
    """
    echo = f"{echo_prefix}_{int(time.time() * 1000)}"
    payload = {"action": action, "params": params, "echo": echo}
    await ws.send(json.dumps(payload, ensure_ascii=False))
    for _ in range(attempts):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=per_try_timeout)
        except asyncio.TimeoutError:
            log("[超时] 未收到 NapCat 响应")
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("echo") == echo:
            return data
        if isinstance(data, dict):
            log(f"    <- 事件 {data.get('post_type') or data.get('type')}（忽略）")
    return {}


async def send_private_msg(ws, user_id, message, log=print,
                           echo_prefix: str = "hx", attempts: int = 4,
                           per_try_timeout: float = 15.0) -> dict:
    """发私聊消息，返回 NapCat 响应（含 status/data）；超时返回 {}。"""
    log(f"[发送] 私聊 {user_id}: {message}")
    return await call_action(ws, "send_private_msg",
                             {"user_id": user_id, "message": message},
                             log=log, echo_prefix=echo_prefix, attempts=attempts,
                             per_try_timeout=per_try_timeout)
