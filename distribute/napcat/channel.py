# -*- coding: utf-8 -*-
"""channel.py · NapCat 渠道（看板托管的常驻进程，OneBot11 联调用）
=================================================================
连上看板的渠道 WebSocket（**推送模式**），把条目通过本机 NapCat 的正向 WebSocket
发到指定 QQ（私聊）。配置从 `.env` 读：

    NAPCAT_WS_URL=ws://127.0.0.1:3001
    NAPCAT_WS_TOKEN=xxxx          # 与 onebot11 配置一致，没有就留空
    NAPCAT_TARGET_QQ=你的QQ号

没配置时**不会误标已发**：每条都如实回报 `ok=false`（看板保持未发、日志写清楚原因）。
看板「分发」页可启停本进程（`channels.command`）。

手动跑（仓库根目录）：
    .venv\\Scripts\\python.exe distribute\\napcat\\channel.py
    .venv\\Scripts\\python.exe distribute\\napcat\\channel.py --once 1
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from distribute.ws_channel import ChannelClient  # noqa: E402
from qq_ws import connect, make_log, send_private_msg  # noqa: E402

CHANNEL = "napcat"
KIND = "private"
log = make_log("napcat_channel.log")


def config() -> dict:
    """NapCat 连接参数（.env → 环境变量；缺一个就算没配置）。"""
    values = {}
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip()
    for key in ("NAPCAT_WS_URL", "NAPCAT_WS_TOKEN", "NAPCAT_TARGET_QQ"):
        if os.getenv(key):
            values[key] = os.getenv(key, "")
    return {
        "url": values.get("NAPCAT_WS_URL", "").strip(),
        "token": values.get("NAPCAT_WS_TOKEN", "").strip(),
        "target": values.get("NAPCAT_TARGET_QQ", "").strip(),
    }


def send_sync(item: dict) -> dict:
    """发一条（同步包一层异步）；失败如实回报，不谎报成功。"""
    cfg = config()
    if not cfg["url"] or not cfg["target"]:
        return {"ok": False, "detail": "NapCat 未配置（NAPCAT_WS_URL / NAPCAT_TARGET_QQ）"}
    body = str(item.get("zh") or item.get("raw") or "").strip()
    if not body:
        return {"ok": False, "detail": "条目没有可发正文"}
    from distribute.alison.ali_core.render import build_text, item_images
    from collect.image_filter import filter_image_paths
    text = build_text(item)
    images = filter_image_paths(item_images(item))
    message = [{"type": "text", "data": {"text": text}}]
    message.extend({"type": "image", "data": {"file": path if path.startswith(('http://', 'https://')) else Path(path).resolve().as_uri()}} for path in images)

    async def run() -> dict:
        try:
            async with connect(cfg["url"], cfg["token"]) as ws:
                resp = await send_private_msg(ws, cfg["target"], message, log)
                if resp and str(resp.get("status", "")).lower() in ("ok", "async"):
                    return {"ok": True}
                return {"ok": False, "detail": f"NapCat 返回 {resp.get('status') if resp else '无响应'}"}
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    return asyncio.run(run())


def main() -> int:
    args = sys.argv[1:]
    once = None
    if "--once" in args:
        try:
            once = int(args[args.index("--once") + 1])
        except (IndexError, ValueError):
            once = 1
    cfg = config()
    log("=" * 50)
    log(f"NapCat 渠道启动 | WS={cfg['url'] or '未配置'} | 目标={cfg['target'] or '未配置'}")
    client = ChannelClient(CHANNEL, "NapCat 联调", targets=[{"kind": KIND, "label": "私聊"}],
                           adapter="process", pull=False,
                           logger=lambda message: log(str(message)))
    client.run_forever(lambda item, source: send_sync(item), max_rounds=once)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
