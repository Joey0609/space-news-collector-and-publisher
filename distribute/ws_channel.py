# -*- coding: utf-8 -*-
"""ws_channel.py · 渠道 SDK（客户端）：连上看板 WS、注册、收条目、回报
=====================================================================
协议见 `dashboard/PROTOCOL.md`。渠道要做的全部事情：

    def send(item, source) -> dict      # 你的发送逻辑
        return {"ok": True}                       # 发成功 → 服务端写库
        return {"ok": False, "detail": "..."}     # 失败 → 保持未发，稍后重试
        return {"defer": 600, "reason": "静默"}   # 现在不方便 → 保持未发，600s 后再来
        raise RuntimeError("...")                 # 等价于 ok=False

    client = ChannelClient("alison", "艾莉森", [{"kind": "group_123", "label": "群 123"}])
    client.run_forever(send)

服务端地址/token 默认从本机取（`data/state/dashboard_token.txt`、`HX_WS_PORT` 或 8788），
也可显式传参。断线自动重连（退避 2→30 秒）。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
from pathlib import Path

from distribute.ws_codec import WSError, WSSocket, client_handshake

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "data" / "state" / "dashboard_token.txt"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
PROTOCOL_VERSION = 1
RECONNECT_MIN, RECONNECT_MAX = 2.0, 30.0


def default_port() -> int:
    try:
        return int(os.getenv("HX_WS_PORT", "") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def default_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class ChannelClient:
    """渠道客户端：注册 → 收 `item` → 回调发送 → 回报 → 断线重连。"""

    def __init__(self, channel: str, label: str = "", targets=None, host: str = DEFAULT_HOST,
                 port: int | None = None, token: str = "", adapter: str = "process",
                 pull: bool = False, logger=print):
        self.channel = str(channel).strip()
        self.label = str(label or channel).strip()
        self.targets = [{"kind": str(t.get("kind") if isinstance(t, dict) else t).strip(),
                         "label": str((t.get("label") if isinstance(t, dict) else t) or "").strip()
                                  or str(t.get("kind") if isinstance(t, dict) else t).strip()}
                        for t in (targets or [])]
        self.host = host
        self.port = int(port or default_port())
        self.token = str(token or default_token())
        self.adapter = adapter
        self.pull = bool(pull)                    # True = 我方按节奏 want（服务端不主动推）
        self.log = logger
        self.sources: list = []
        self.paused = False
        self.stopped = False
        self.connected = False
        self.last_error = ""
        self._ws: WSSocket | None = None
        self._send_lock = threading.Lock()
        self._waiter_lock = threading.Lock()
        self._waiters: dict = {}                  # source -> Queue（收 want 的回应）
        self._reader_active = False               # 有读线程时走队列，没有就 want 自己直读

    # ────────────────────────── 连接 ──────────────────────────

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws?token={self.token}&channel={self.channel}"

    def connect(self, timeout: float = 10.0) -> bool:
        """握手 + 发 hello，成功返回 True。失败不抛（重连交给 run_forever）。"""
        if not self.token:
            self.last_error = f"没有 token（{TOKEN_FILE} 不存在？）"
            self.log(f"[{self.channel}] {self.last_error}")
            return False
        path = f"/ws?token={self.token}&channel={self.channel}"
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as exc:
            self.last_error = f"连不上 {self.host}:{self.port}（{exc}）"
            return False
        try:
            client_handshake(sock, self.host, self.port, path, timeout=timeout)
        except (WSError, OSError) as exc:
            self.last_error = f"握手失败：{exc}"
            try:
                sock.close()
            except Exception:
                pass
            return False
        self._ws = WSSocket(sock, is_server=False, timeout=120.0)
        self.connected = True
        capabilities = {"defer": True}
        if self.pull:
            capabilities["pull"] = True
        self.send_json({
            "type": "hello", "channel": self.channel, "label": self.label,
            "version": PROTOCOL_VERSION, "adapter": self.adapter, "targets": self.targets,
            "capabilities": capabilities,
        })
        return True

    def update_targets(self, targets) -> bool:
        """重新 hello：群目录/开关变化后让服务端更新 source，连接不用断开。"""
        normalized = []
        for target in (targets or []):
            kind = str(target.get("kind") or "").strip() if isinstance(target, dict) else str(target).strip()
            if kind:
                label = str(target.get("label") or kind) if isinstance(target, dict) else kind
                normalized.append({"kind": kind, "label": label})
        if normalized == self.targets:
            return True
        self.targets = normalized
        if not self.connected:
            return False
        return self.send_json({
            "type": "hello", "channel": self.channel, "label": self.label,
            "version": PROTOCOL_VERSION, "adapter": self.adapter, "targets": self.targets,
            "capabilities": {"defer": True, **({"pull": True} if self.pull else {})},
        })

    def close(self) -> None:
        self.connected = False
        if self._ws is not None:
            try:
                self._ws.send_close()
            except Exception:
                pass
            self._ws.close()
            self._ws = None

    # ────────────────────────── 发消息 ──────────────────────────

    def send_json(self, data: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send_text(json.dumps(data, ensure_ascii=False))
            return True
        except Exception as exc:
            self.last_error = f"发送失败：{exc}"
            self.connected = False
            return False

    def send_status(self, state: str, note: str = "") -> None:
        self.send_json({"type": "status", "state": state, "note": note})

    def send_result(self, source: str, item_id, ok: bool, detail: str = "",
                    terminal: bool = False) -> None:
        self.send_json({"type": "result", "source": source, "item_id": item_id,
                        "ok": bool(ok), "detail": str(detail or "")[:300],
                        "terminal": bool(terminal)})

    def send_defer(self, source: str, item_id, seconds: float, reason: str = "") -> None:
        self.send_json({"type": "defer", "source": source, "item_id": item_id,
                        "seconds": float(seconds), "reason": str(reason or "")[:120]})

    # ────────────────────────── 拉取模式 ──────────────────────────

    def _read_reply(self, predicate, timeout: float):
        """临时直读 socket，直到读到 predicate 认的回应（用于没有读线程的场景）。

        插件是"读线程 + want"并用；调试脚本（`--once`）常常只有主线程，
        这时 want/pending 必须自己把回应读出来，否则会永远等不到。
        """
        ws = self._ws
        if ws is None:
            return None
        deadline = time.time() + max(0.1, timeout)
        while time.time() < deadline:
            try:
                kind, data = ws.recv_message()
            except socket.timeout:
                continue
            except (WSError, OSError):
                self.connected = False
                return None
            if kind == "close":
                self.connected = False
                return None
            if kind != "text":
                continue
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and predicate(message):
                return message
            if isinstance(message, dict):
                self._dispatch(message, None)     # 顺带处理 pause/ack 之类
        return None

    def want(self, source: str, count: int = 1, timeout: float = 10.0) -> list:
        """按自己的节奏要一批待发条目（拉取模式）。返回条目列表（可能为空）。

        没连上/超时返回 []（调用方按"这轮没内容"处理即可）。
        """
        if not self.pull:
            raise RuntimeError("这个客户端不是拉取模式（pull=False）")
        if not self.connected:
            return []
        box = None
        if self._reader_active:
            with self._waiter_lock:
                box = self._waiters.setdefault(source, queue.Queue())
            while not box.empty():                # 清掉上一次的残留回应
                try:
                    box.get_nowait()
                except queue.Empty:
                    break
        if not self.send_json({"type": "want", "source": source, "count": int(count)}):
            return []
        if box is None:
            message = self._read_reply(
                lambda msg: msg.get("type") == "items" and msg.get("source") == source, timeout)
        else:
            try:
                message = box.get(timeout=timeout)
            except queue.Empty:
                message = None
        if not message:
            self.last_error = f"want({source}) 没等到回应"
            return []
        items = message.get("items")
        return items if isinstance(items, list) else []

    def pending(self, source: str, timeout: float = 5.0) -> int:
        """问服务端：这个 source 还有多少条未发（拉取模式判断"有没有内容"用）。"""
        if not self.pull:
            raise RuntimeError("这个客户端不是拉取模式（pull=False）")
        if not self.connected:
            return 0
        box = None
        if self._reader_active:
            with self._waiter_lock:
                box = self._waiters.setdefault("pending:" + source, queue.Queue())
            while not box.empty():
                try:
                    box.get_nowait()
                except queue.Empty:
                    break
        if not self.send_json({"type": "pending", "source": source}):
            return 0
        if box is None:
            message = self._read_reply(
                lambda msg: msg.get("type") == "pending" and msg.get("source") == source, timeout)
        else:
            try:
                message = box.get(timeout=timeout)
            except queue.Empty:
                message = None
        if not message:
            return 0
        try:
            return int(message.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    # ────────────────────────── 主循环 ──────────────────────────

    def run_forever(self, on_item, max_rounds: int | None = None) -> None:
        """常驻：连上就收，断了就重连。`on_item(item, source) -> dict` 见模块说明。

        `max_rounds` 只给自检/调试用（收满 N 条就返回）。
        """
        backoff = RECONNECT_MIN
        handled = 0
        while not self.stopped:
            if not self.connect():
                self.log(f"[{self.channel}] {self.last_error}；{backoff:.0f}s 后重连")
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
                continue
            backoff = RECONNECT_MIN
            try:
                count = self._session(on_item, max_rounds)
                handled += count
            except Exception as exc:              # 任何异常都退到重连
                self.last_error = str(exc)
                self.log(f"[{self.channel}] 会话中断：{exc}")
            self.close()
            if max_rounds is not None and handled >= max_rounds:
                return
            if self.stopped:
                return
            time.sleep(RECONNECT_MIN)

    def _session(self, on_item, max_rounds: int | None = None) -> int:
        """一次连接内的事件循环；返回本轮处理的条目数。"""
        handled = 0
        self._reader_active = True
        try:
            while not self.stopped:
                try:
                    kind, data = self._ws.recv_message()
                except socket.timeout:
                    continue                      # 空闲：服务端会 ping，我们自动回 pong
                except (WSError, OSError) as exc:
                    self.last_error = str(exc)
                    break
                if kind == "close":
                    break
                if kind != "text":
                    continue
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                handled += self._dispatch(message, on_item)
                if max_rounds is not None and handled >= max_rounds:
                    # 一次性模式（自检 / --once）：留点时间让服务端收完最后一条回执并写库，
                    # 立刻 close 有把最后一条结果挤掉的风险
                    time.sleep(0.5)
                    break
        finally:
            self._reader_active = False
        return handled

    def _dispatch(self, message: dict, on_item) -> int:
        kind = str(message.get("type") or "")
        if kind == "welcome":
            self.sources = [str(x) for x in (message.get("sources") or [])]
            self.paused = not bool(message.get("enabled", True))
            added = [str(x) for x in (message.get("new_sources") or [])]
            if added:
                self.log(f"[{self.channel}] 新增推送目标：{', '.join(added)}")
            else:
                self.log(f"[{self.channel}] 渠道已登记，无需新增推送目标")
            if not self.sources:
                self.log(f"[{self.channel}] 已连接看板，但没有启用的群；请在分发 → 艾莉森 → 配置里启用群推送")
            else:
                self.log(f"[{self.channel}] 本次启用 {len(self.sources)} 个目标：{', '.join(self.sources)}")
            if self.paused:
                self.log(f"[{self.channel}] 看板已暂停该渠道的推送")
            self.send_status("idle", "已连接" if self.sources else "等待启用群推送")
            return 0
        if kind == "item":
            if on_item is None:                   # 拉取模式却收到推送：礼貌地让它等一会
                self.send_defer(str(message.get("source") or ""),
                                (message.get("item") or {}).get("id"), 60, "客户端为拉取模式")
                return 0
            self._handle_item(message, on_item)
            return 1
        if kind == "items":                       # 对 want 的回应
            source = str(message.get("source") or "")
            with self._waiter_lock:
                box = self._waiters.get(source)
            if box is not None:
                box.put(message)
            return 0
        if kind == "pending":                     # 对 pending 的回应
            source = str(message.get("source") or "")
            with self._waiter_lock:
                box = self._waiters.get("pending:" + source)
            if box is not None:
                box.put(message)
            return 0
        if kind == "ack":
            return 0
        if kind == "pause":
            self.paused = True
            self.log(f"[{self.channel}] 服务端要求暂停推送")
            return 0
        if kind == "resume":
            self.paused = False
            self.log(f"[{self.channel}] 服务端恢复推送")
            return 0
        if kind == "shutdown":
            self.log(f"[{self.channel}] 服务端要求退出：{message.get('reason') or ''}")
            self.stopped = True
            return 0
        if kind == "error":
            self.log(f"[{self.channel}] 服务端报错：{message.get('error')}")
            return 0
        return 0

    def _handle_item(self, message: dict, on_item) -> None:
        source = str(message.get("source") or "")
        item = message.get("item") or {}
        item_id = item.get("id")
        try:
            outcome = on_item(item, source)
        except Exception as exc:                  # 渠道自己的异常 = 这条没发成功
            self.send_result(source, item_id, False, f"{type(exc).__name__}: {exc}")
            return
        if not isinstance(outcome, dict):
            self.send_result(source, item_id, True)
            return
        if outcome.get("defer"):
            try:
                seconds = float(outcome["defer"])
            except (TypeError, ValueError):
                seconds = 300.0
            self.send_defer(source, item_id, seconds, str(outcome.get("reason") or ""))
            return
        self.send_result(source, item_id, bool(outcome.get("ok", True)),
                         str(outcome.get("detail") or ""),
                         terminal=bool(outcome.get("terminal")))
