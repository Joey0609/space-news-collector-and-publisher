# -*- coding: utf-8 -*-
"""
航小麦 · 联调发射端（台式机 → 笔记本 NapCat → 发QQ）
=====================================================
作用：台式机通过笔记本 NapCat 的 OneBot11 WebSocket(3001) 发QQ消息。
      这是"台式机麦麦生成 → 笔记本 NapCat → 发到你QQ"链路的关键一环。

运行：
    E:\AI程序员\03_实用程序\航小Q\航小麦\MaiBot-main\.venv\Scripts\python.exe 脚本\send_qq_nb.py

依赖：websockets（麦麦 venv 已装）
配置：WS 指向笔记本 YOUR_LAPTOP_IP:3001，token 与 onebot11 一致。
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import websockets

# ---------- 配置：指向笔记本的 NapCat ----------
WS_URL = "ws://YOUR_LAPTOP_IP:3001"     # 笔记本 NapCat OneBot11 WS 地址
WS_TOKEN = "YOUR_WS_TOKEN"  # 与笔记本 onebot11 一致
TARGET_QQ = "YOUR_QQ_ID"                  # 你的常用 QQ

# 项目根 = 航小麦
ROOT = Path(__file__).parent.parent
LOG_FILE = ROOT / "logs" / "send_qq_nb.log"

TEST_MSG = "【航小麦·联调测试】这是从台式机经过笔记本NapCat发出的消息。链路通了！"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def main():
    log("=" * 50)
    log("航小麦 联调发射端（台式机→笔记本NapCat→发QQ）")
    log(f"连接: {WS_URL}")
    try:
        headers = {"Authorization": f"Bearer {WS_TOKEN}"} if WS_TOKEN else {}
        async with websockets.connect(WS_URL, additional_headers=headers, open_timeout=15) as ws:
            log("[连接] 成功")
            action = {
                "action": "send_private_msg",
                "params": {"user_id": TARGET_QQ, "message": TEST_MSG},
                "echo": f"nb_{int(time.time()*1000)}",
            }
            await ws.send(json.dumps(action, ensure_ascii=False))
            log(f"[发送] 私聊 {TARGET_QQ}: {TEST_MSG}")
            # 等响应
            got = False
            for _ in range(30):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                data = json.loads(raw)
                if data.get("echo") == action["echo"]:
                    log(f"[结果] 响应 status={data.get('status')} data={json.dumps(data.get('data'),ensure_ascii=False)}")
                    got = True
                    break
            if not got:
                log("[结果] 未收到明确响应（可能已发出）")
    except Exception as e:
        log(f"[错误] {e}")
    log("=== 联调测试结束 ===")


if __name__ == "__main__":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(main())
