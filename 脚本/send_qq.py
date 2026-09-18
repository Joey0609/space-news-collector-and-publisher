# -*- coding: utf-8 -*-
"""
航小麦 · 发射端脚本（通过 NapCat OneBot11 WebSocket 发 QQ 消息）
=================================================================
作用：连接 NapCat 的正向 WebSocket，调用 OneBot11 的 send 动作，
      把消息发到指定 QQ（私聊）。这是"航小麦发消息"的通道。

运行：
    E:\AI程序员\03_实用程序\航小Q\航小麦\脚本\venv\Scripts\python.exe send_qq.py
    或直接用能 import websockets 的 python 跑。

依赖：websockets（用来连 WebSocket）
配置：WS 地址、token、目标 QQ 见下面常量 / 可读配置文件
说明：NapCat 必须已启动且机器人号在线（端口 3001 监听）。
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import websockets

# ---------------- 配置 ----------------
WS_URL = "ws://127.0.0.1:3001"          # NapCat OneBot11 正向 WebSocket 地址
WS_TOKEN = "YOUR_WS_TOKEN"  # 与 onebot11 配置一致
TARGET_QQ = "YOUR_QQ_ID"                  # 接收方：你的常用 QQ（私聊）

# 项目根 = 航小麦目录（本脚本在 航小麦\脚本\ 下）
ROOT = Path(__file__).parent.parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "send_qq.log"

# 测试消息内容
TEST_MSG = "【航小麦测试】这是航小麦发QQ通道的测试消息。收到说明链路通了！"


def log(msg: str) -> None:
    """带时间戳日志：打印 + 写文件（满足"所有程序有日志"规范）。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def send_private_msg(ws, user_id: int, message: str) -> dict:
    """调用 OneBot11 的 send_private_msg 动作发私聊，返回响应。"""
    action = {
        "action": "send_private_msg",
        "params": {"user_id": user_id, "message": message},
        "echo": f"hx_send_{int(time.time()*1000)}",
    }
    log(f"[发送] 私聊 {user_id}: {message}")
    await ws.send(json.dumps(action, ensure_ascii=False))
    # 等 NapCat 返回响应（含 echo）
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
        except asyncio.TimeoutError:
            log("[超时] 未收到 NapCat 响应")
            return {}
        data = json.loads(raw)
        # 匹配我们的 echo
        if data.get("echo") == action["echo"]:
            return data
        # 忽略其它事件（可能是群消息回显）
        log(f"    <- 事件 {data.get('post_type') or data.get('type')}（忽略）")


async def main():
    log("=" * 50)
    log("航小麦 发射端 · 发QQ消息测试")
    log("=" * 50)
    log(f"连接 NapCat WS: {WS_URL}")

    # 连接时带上 token（OneBot11 用 Authorization 头或 query token）
    try:
        # websockets 16: 用 additional_headers 传 Authorization: Bearer <token>
        import websockets as wsmod
        headers = {"Authorization": f"Bearer {WS_TOKEN}"} if WS_TOKEN else {}
        async with wsmod.connect(WS_URL, additional_headers=headers, open_timeout=15) as ws:
            log("[连接] 成功，开始发送测试消息...")
            resp = await send_private_msg(ws, TARGET_QQ, TEST_MSG)
            status = resp.get("status", "?")
            log(f"[结果] send_private_msg 返回 status={status}")
            if resp:
                log(f"[完成] 已尝试发送，响应：{json.dumps(resp, ensure_ascii=False)}")
            else:
                log("[完成] 无响应（可能发送失败，见上方日志）")
    except Exception as e:
        log(f"[错误] 连接/发送异常: {e}")

    log("=== 发射端测试结束 ===")


if __name__ == "__main__":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
