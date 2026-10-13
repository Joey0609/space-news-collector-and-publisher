# -*- coding: utf-8 -*-
"""艾莉森 QQ 长连接：监听群事件，并供渠道通过同一 BotPy Client 发送。

未 @ 的群消息只有在 QQ 平台给机器人开通全量群消息权限时才会下发。
"""
import asyncio
import concurrent.futures
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

ALI = Path(__file__).resolve().parent
REPO = ALI.parents[1]
if str(REPO) in sys.path:
    sys.path.remove(str(REPO))
sys.path.insert(0, str(REPO))
if str(ALI) in sys.path:
    sys.path.remove(str(ALI))
sys.path.insert(0, str(ALI))  # 优先加载艾莉森目录内置的 botpy。

import botpy
from botpy.errors import AuthenticationFailedError
from botpy.message import GroupMessage

from distribute.alison.ali_core.env import credentials, is_sandbox, load_env, update_state  # noqa: E402
from distribute.alison.ali_core.groups import get_group, mark_group_removed, upsert_group  # noqa: E402
from distribute.alison.ali_core.log import log, setup_console_encoding  # noqa: E402


class AlisonClient(botpy.Client):
    async def on_connection_lost(self):
        ready = getattr(self, "_alison_ready", None)
        if ready is not None:
            ready.clear()

    async def on_resumed(self):
        ready = getattr(self, "_alison_ready", None)
        if ready is not None:
            ready.set()

    async def on_error(self, event_method: str, *args, **kwargs):
        # QQ event payloads may carry auth_token; never echo raw exceptions or arguments.
        log(f"[事件处理失败] event={event_method}")

    async def on_unhandled_gateway_event(self, event_name: str, reason: str):
        # SDK reports only event names and error classes; refuse arbitrary event text.
        event = event_name if re.fullmatch(r"[A-Z0-9_]{1,80}", event_name) else "INVALID_NAME"
        detail = reason if re.fullmatch(r"[A-Za-z]{1,80}", reason) else "unknown"
        log(f"[事件诊断] {event}：{detail}")

    async def on_ready(self):
        ready = getattr(self, "_alison_ready", None)
        if ready is not None:
            ready.set()
        name = getattr(getattr(self, "robot", None), "name", "?")
        mode = "沙箱" if getattr(self, "_alison_sandbox", False) else "正式"
        log(f"已连接 QQ {mode}机器人：{name}；监听入/退群和群消息。"
            "只有当前机器人所在群的事件会送达本连接。"
            "未 @ 的普通消息还需在对应 QQ 群开启「获取群内全部消息」并获得事件权限")
        if getattr(self, "_alison_sandbox", False):
            log("[环境提醒] 当前是沙箱机器人，无法接收正式机器人所在群的消息；"
                "如要监听正式群，请在看板配置对应 APPID/SECRET 和正式环境后重启渠道")

    async def _observe_group(self, message: GroupMessage, tag: str):
        if not getattr(self, "_discover_groups", True):
            return
        goid = str(getattr(message, "group_openid", "") or "").strip()
        if not goid:
            log(f"[{tag}] 未携带群 OpenID，无法登记群")
            return
        message_id = str(getattr(message, "id", "") or "").strip()
        if message_id:
            recent = getattr(self, "_seen_group_messages", None)
            if recent is None:
                recent = self._seen_group_messages = deque(maxlen=1024)
            key = (goid, message_id)
            if key in recent:
                return  # 全量模式下同一条 @ 消息可能也触发 GROUP_AT_MESSAGE_CREATE
            recent.append(key)
        author = getattr(message, "author", None)
        sender = str(getattr(author, "member_openid", "") or "未知成员")
        content = str(getattr(message, "content", "") or "").replace("\r", "\\r").replace("\n", "\\n")
        attachments = getattr(message, "attachments", None) or []
        body = content.strip() or (f"[非文本消息，附件 {len(attachments)} 个]" if attachments else "[空消息]")
        log(f"[{tag}] 群={goid} 发送者={sender} 内容={body}")
        known = get_group(goid)
        if not known or not known["is_member"]:
            upsert_group(goid)
            if not known:
                update_state(group_openid=goid)  # 只在首次发现时兼容旧调试脚本，别每条消息写 JSON
                log(f"[发现新群] 已自动加入看板群目录 OpenID={goid}（推送默认关闭）")

    async def on_group_add_robot(self, event):
        if not getattr(self, "_discover_groups", True):
            return
        goid = str(getattr(event, "group_openid", "") or "").strip()
        if goid:
            upsert_group(goid, joined_at=str(getattr(event, "timestamp", "") or ""))
            log(f"[入群] 已登记 OpenID={goid}")

    async def on_group_del_robot(self, event):
        if not getattr(self, "_discover_groups", True):
            return
        goid = str(getattr(event, "group_openid", "") or "").strip()
        if goid:
            mark_group_removed(goid)
            log(f"[退群] 已停用该群推送 OpenID={goid}")

    async def on_group_at_message_create(self, message: GroupMessage):
        await self._observe_group(message, "群@消息")

    async def on_group_message_create(self, message: GroupMessage):
        await self._observe_group(message, "群消息")

    async def on_c2c_message_create(self, message):
        if not getattr(self, "_discover_groups", True):
            return
        author = getattr(message, "author", None)
        uid = str(getattr(author, "id", None) or getattr(author, "user_openid", None) or "").strip()
        if uid:
            update_state(user_openid=uid)


class BotRuntime:
    """在独立事件循环中维持 QQ 长连接，供看板渠道同步等待发送回执。"""

    def __init__(self, settings: dict):
        self.settings = settings
        self.ready = threading.Event()
        self.client = None
        self.thread = None
        self.last_error = ""

    def start(self) -> bool:
        appid, secret = credentials(self.settings)
        if not appid or not secret:
            self.last_error = "未配置 APPID/SECRET"
            return False
        self.thread = threading.Thread(target=self._run, name="alison-botpy", daemon=True)
        self.thread.start()
        return True

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            intents = botpy.Intents(group_and_c2c_event=True)
            client = AlisonClient(intents=intents, is_sandbox=is_sandbox(self.settings),
                                  bot_log=False, ext_handlers=False)
            client._alison_ready = self.ready
            client._alison_sandbox = is_sandbox(self.settings)
            client._discover_groups = self.settings.get("ALISON_DISCOVER_GROUPS", "1") == "1"
            client._alison_send_lock = asyncio.Lock()
            client._alison_last_send = 0.0
            self.client = client
            appid, secret = credentials(self.settings)
            log(f"QQ 长连接启动（{'沙箱' if client._alison_sandbox else '正式'}）；"
                f"群事件发现={'开' if client._discover_groups else '关'}")
            client.run(appid=appid, secret=secret)
        except AuthenticationFailedError:
            self.last_error = "QQ 认证失败或出口 IP 不在白名单（HTTP 401）"
            log(f"[启动失败] {self.last_error}")
        except Exception as exc:
            self.last_error = f"QQ 长连接启动失败（{type(exc).__name__}）"
            log(f"[启动失败] {self.last_error}")
        finally:
            self.ready.clear()
            self.client = None
            loop.close()

    async def _send(self, group_openid: str, text: str, file_info: str) -> bool:
        from distribute.alison.ali_core.groups import get_group

        client = self.client
        if client is None or not self.ready.is_set():
            return False
        async with client._alison_send_lock:
            group = get_group(group_openid)
            if not group or not group["is_member"] or not group["push_enabled"] or not self.ready.is_set():
                return False
            remaining = 2.0 - (time.monotonic() - client._alison_last_send)
            if remaining > 0:
                await asyncio.sleep(remaining)
            if not self.ready.is_set():
                return False
            client._alison_last_send = time.monotonic()
            kwargs = {"group_openid": group_openid, "msg_type": 0, "content": text}
            if file_info:
                kwargs.update(msg_type=7, media={"file_info": file_info})
            result = await client.api.post_group_message(**kwargs)
            return bool(result)

    def send(self, group_openid: str, text: str, file_info: str = "") -> bool:
        self.last_error = ""
        if self.thread is None or not self.thread.is_alive():
            self.last_error = "QQ 长连接未启动或已停止"
            return False
        if not self.ready.wait(timeout=15):
            self.last_error = self.last_error or "QQ 长连接尚未就绪"
            return False
        client = self.client
        if client is None or not client.loop.is_running():
            self.last_error = "QQ 长连接已停止"
            return False
        future = asyncio.run_coroutine_threadsafe(self._send(group_openid, text, file_info), client.loop)
        try:
            sent = future.result(timeout=45)
            if not sent:
                self.last_error = "QQ 长连接中断、群已关闭或接口返回空结果"
            return sent
        except concurrent.futures.TimeoutError:
            future.cancel()
            self.last_error = "QQ 发送等待超时"
        except Exception as exc:
            self.last_error = f"QQ 发送失败（{type(exc).__name__}）"
        return False


def main():
    env = load_env()
    appid, secret = credentials(env)
    if not appid or not secret:
        log("未配置 APPID/SECRET，群发现网关不启动")
        return 1
    sandbox = is_sandbox(env)
    log(f"群发现网关启动（{'沙箱' if sandbox else '正式'}）；不会自动发送任何消息")
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass
    intents = botpy.Intents(group_and_c2c_event=True)
    log(f"群/C2C 事件订阅位={intents.value}（项目内置 BotPy：{Path(botpy.__file__).parent}）")
    client = AlisonClient(intents=intents, is_sandbox=sandbox,
                          bot_log=False, ext_handlers=False)
    client._alison_sandbox = sandbox
    try:
        client.run(appid=appid, secret=secret)
    except AuthenticationFailedError as exc:
        if "接口访问源IP不在白名单" in str(exc) or "接口访问源 IP 不在白名单" in str(exc):
            log("[启动失败] 正式环境接口拒绝当前出口 IP：不在 QQ 机器人 IP 白名单（HTTP 401）。"
                "请将本机出口 IP 加入该机器人的白名单，或在已获授权的服务器上运行监听")
        else:
            log("[启动失败] QQ API 认证失败（HTTP 401）；请核对机器人环境、凭据与 IP 白名单")
        return 1
    except Exception as exc:
        # SDK/HTTP errors may embed response bodies or tokens; never echo exception text.
        log(f"[启动失败] 登录或建立 QQ WebSocket 时发生 {type(exc).__name__}；"
            "请核对网络、机器人环境及 botpy.log 中的状态码")
        return 1
    return 0


if __name__ == "__main__":
    setup_console_encoding()
    sys.exit(main())
