# -*- coding: utf-8 -*-
"""航小麦 · 发射端脚本（通过本机 NapCat OneBot11 WebSocket 发 QQ 消息）
=================================================================
作用：连接本机 NapCat 的正向 WebSocket，调用 OneBot11 的 send 动作，
      把消息发到指定 QQ（私聊）。这是"航小麦发消息"的通道。

运行（在 脚本 目录下）：
    ..\\MaiBot-main\\.venv\\Scripts\\python.exe send_qq.py
    或直接用能 import websockets 的 python 跑。

依赖：websockets
配置：见下面常量（WS 地址 / token / 目标 QQ）
说明：NapCat 必须已启动且机器人号在线（端口 3001 监听）。
      WS 发送逻辑在 qq_ws.py；跨机（走笔记本 NapCat）见 send_qq_nb.py。
"""
import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from qq_ws import connect, make_log, send_private_msg

# ---------------- 配置 ----------------
WS_URL = "ws://127.0.0.1:3001"          # NapCat OneBot11 正向 WebSocket 地址
WS_TOKEN = "YOUR_WS_TOKEN"              # 与 onebot11 配置一致
TARGET_QQ = "YOUR_QQ_ID"                # 接收方：你的常用 QQ（私聊）

TEST_MSG = "【航小麦测试】这是航小麦发QQ通道的测试消息。收到说明链路通了！"

log = make_log("send_qq.log")


async def main():
    log("=" * 50)
    log("航小麦 发射端 · 发QQ消息测试")
    log("=" * 50)
    log(f"连接 NapCat WS: {WS_URL}")
    try:
        async with connect(WS_URL, WS_TOKEN) as ws:
            log("[连接] 成功，开始发送测试消息...")
            resp = await send_private_msg(ws, TARGET_QQ, TEST_MSG, log)
            log(f"[结果] send_private_msg 返回 status={resp.get('status', '?')}")
            if resp:
                log(f"[完成] 已尝试发送，响应：{json.dumps(resp, ensure_ascii=False)}")
            else:
                log("[完成] 无响应（可能发送失败，见上方日志）")
    except Exception as e:
        log(f"[错误] 连接/发送异常: {e}")
    log("=== 发射端测试结束 ===")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
