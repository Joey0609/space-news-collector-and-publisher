# -*- coding: utf-8 -*-
"""航小麦 · 联调发射端（台式机 → 笔记本 NapCat → 发QQ）
=====================================================
作用：台式机通过笔记本 NapCat 的 OneBot11 WebSocket(3001) 发 QQ 消息。
      这是"台式机麦麦生成 → 笔记本 NapCat → 发到你QQ"链路的关键一环。

运行（在 脚本 目录下）：
    ..\\MaiBot-main\\.venv\\Scripts\\python.exe send_qq_nb.py

依赖：websockets（麦麦 venv 已装）
配置：WS 指向笔记本 YOUR_LAPTOP_IP:3001，token 与笔记本 onebot11 一致。
      WS 发送逻辑在 qq_ws.py（与 send_qq.py 共用）。
"""
import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from qq_ws import connect, make_log, send_private_msg

# ---------- 配置：指向笔记本的 NapCat ----------
WS_URL = "ws://YOUR_LAPTOP_IP:3001"     # 笔记本 NapCat OneBot11 WS 地址
WS_TOKEN = "YOUR_WS_TOKEN"              # 与笔记本 onebot11 一致
TARGET_QQ = "YOUR_QQ_ID"                # 你的常用 QQ

TEST_MSG = "【航小麦·联调测试】这是从台式机经过笔记本NapCat发出的消息。链路通了！"

log = make_log("send_qq_nb.log")


async def main():
    log("=" * 50)
    log("航小麦 联调发射端（台式机→笔记本NapCat→发QQ）")
    log(f"连接: {WS_URL}")
    try:
        async with connect(WS_URL, WS_TOKEN) as ws:
            log("[连接] 成功")
            resp = await send_private_msg(ws, TARGET_QQ, TEST_MSG, log, echo_prefix="nb",
                                          attempts=30, per_try_timeout=5.0)
            if resp:
                log(f"[结果] 响应 status={resp.get('status')} "
                    f"data={json.dumps(resp.get('data'), ensure_ascii=False)}")
            else:
                log("[结果] 未收到明确响应（可能已发出）")
    except Exception as e:
        log(f"[错误] {e}")
    log("=== 联调测试结束 ===")


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
