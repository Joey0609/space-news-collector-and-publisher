# -*- coding: utf-8 -*-
"""hx_ws.py · 渠道 WebSocket 客户端（插件侧自包含实现）
========================================================
插件要拷进麦麦的 `plugins/` 目录，**不能 import 仓库代码**，所以这里是
`distribute/ws_codec.py` + `distribute/ws_channel.py` 的镜像（纯标准库，不引依赖）。
自检 `selftest/check_channels.py` 会让它和仓库实现对着同一个真 WS 服务端跑，保证两边一致。

协议口径见 `dashboard/PROTOCOL.md`。用法：

    client = ChannelClient("maibot", "麦麦插件", [{"kind": "private", "label": "私聊"}],
                           adapter="plugin")
    client.run_forever(handle_item)      # handle_item(item, source) -> {"ok": True} / {"defer": 600}

⚠️ 改这里的行为时，`distribute/ws_channel.py` 要一起改（反之亦然）。
"""
import base64
import hashlib
import json
import os
import queue
import socket
import struct
import threading
import time

if __package__ in (None, ""):
    import hx_paths
else:
    from . import hx_paths

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 4 * 1024 * 1024
OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA
PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
RECONNECT_MIN, RECONNECT_MAX = 2.0, 30.0


class WSError(Exception):
    """握手/帧格式错误。"""


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((str(client_key).strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _parse_head(sock):
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise WSError("连接在握手前关闭")
        buffer += chunk
        if len(buffer) > 65536:
            raise WSError("请求头过大")
    head, _, rest = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers, rest


def _client_handshake(sock, host, port, path, timeout=10.0):
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
    sock.settimeout(timeout)
    request_line, headers, _ = _parse_head(sock)
    if "101" not in request_line:
        raise WSError(f"握手失败：{request_line.strip()}")
    if headers.get("sec-websocket-accept", "") != accept_key(key):
        raise WSError("Sec-WebSocket-Accept 不匹配")


def _encode_frame(opcode, payload, mask):
    length = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + key + masked


class WSSocket:
    """客户端侧的帧收发（发出去的帧一律掩码）。"""

    def __init__(self, sock, timeout=120.0):
        self.sock = sock
        self.sock.settimeout(timeout)

    def send(self, opcode, payload=b""):
        self.sock.sendall(_encode_frame(opcode, payload, mask=True))

    def send_text(self, text):
        self.send(OP_TEXT, str(text).encode("utf-8"))

    def send_ping(self, payload=b"hx"):
        self.send(OP_PING, payload)

    def send_close(self, code=1000):
        try:
            self.send(OP_CLOSE, struct.pack(">H", code))
        except Exception:
            pass

    @staticmethod
    def _read_exact(sock, count):
        buffer = b""
        while len(buffer) < count:
            chunk = sock.recv(count - len(buffer))
            if not chunk:
                raise WSError("连接已关闭")
            buffer += chunk
        return buffer

    def _read_frame(self):
        first, second = self._read_exact(self.sock, 2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(self.sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(self.sock, 8))[0]
        if length > MAX_MESSAGE:
            raise WSError(f"消息过大：{length} 字节")
        key = self._read_exact(self.sock, 4) if masked else b""
        payload = self._read_exact(self.sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def recv_message(self):
        fragments = []
        fragment_opcode = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_CLOSE:
                return "close", payload
            if opcode == OP_PING:
                try:
                    self.send(OP_PONG, payload)
                except Exception:
                    pass
                return "ping", payload
            if opcode == OP_PONG:
                return "pong", payload
            if opcode in (OP_TEXT, OP_BINARY):
                fragment_opcode = opcode
                fragments = [payload]
            elif opcode == OP_CONT:
                if fragment_opcode is None:
                    raise WSError("收到了没有开头的数据帧")
                fragments.append(payload)
            else:
                raise WSError(f"不支持的 opcode {opcode}")
            if fin:
                data = b"".join(fragments)
                if fragment_opcode == OP_BINARY:
                    return "binary", data
                return "text", data.decode("utf-8")

    def drain(self, budget=0.3):
        """关连接前读完对端剩下的数据（不然 close 会发 RST，对端可能丢最后一帧）。"""
        try:
            self.sock.settimeout(budget)
            while True:
                try:
                    if not self.sock.recv(65536):
                        break
                except (socket.timeout, OSError):
                    break
        except Exception:
            pass

    def close(self):
        try:
            self.drain()
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def default_port() -> int:
    try:
        return int(os.getenv("HX_WS_PORT", "") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def default_token() -> str:
    try:
        return hx_paths.TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class ChannelClient:
    """和 `distribute/ws_channel.py` 的 ChannelClient 同一套行为。"""

    def __init__(self, channel, label="", targets=None, host=DEFAULT_HOST, port=None,
                 token="", adapter="plugin", pull=False, logger=print):
        self.channel = str(channel).strip()
        self.label = str(label or channel).strip()
        self.targets = []
        for target in (targets or []):
            kind = str(target.get("kind") if isinstance(target, dict) else target).strip()
            label_text = (str(target.get("label") or "").strip()
                          if isinstance(target, dict) else "") or kind
            self.targets.append({"kind": kind, "label": label_text})
        self.host = host
        self.port = int(port or default_port())
        self.token = str(token or default_token())
        self.adapter = adapter
        self.pull = bool(pull)
        self.log = logger
        self.sources = []
        self.paused = False
        self.stopped = False
        self.connected = False
        self.last_error = ""
        self._ws = None
        self._send_lock = threading.Lock()
        self._waiter_lock = threading.Lock()
        self._waiters = {}
        self._reader_active = False

    @property
    def url(self):
        return f"ws://{self.host}:{self.port}/ws?token={self.token}&channel={self.channel}"

    def connect(self, timeout=10.0) -> bool:
        if not self.token:
            self.last_error = f"没有 token（{hx_paths.TOKEN_FILE} 不存在？）"
            self.log(f"[{self.channel}] {self.last_error}")
            return False
        path = f"/ws?token={self.token}&channel={self.channel}"
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as exc:
            self.last_error = f"连不上 {self.host}:{self.port}（{exc}）"
            return False
        try:
            _client_handshake(sock, self.host, self.port, path, timeout=timeout)
        except (WSError, OSError) as exc:
            self.last_error = f"握手失败：{exc}"
            try:
                sock.close()
            except Exception:
                pass
            return False
        self._ws = WSSocket(sock, timeout=120.0)
        self.connected = True
        capabilities = {"defer": True}
        if self.pull:
            capabilities["pull"] = True
        self.send_json({"type": "hello", "channel": self.channel, "label": self.label,
                        "version": PROTOCOL_VERSION, "adapter": self.adapter,
                        "targets": self.targets, "capabilities": capabilities})
        return True

    def close(self):
        self.connected = False
        if self._ws is not None:
            try:
                self._ws.send_close()
            except Exception:
                pass
            self._ws.close()
            self._ws = None

    def send_json(self, data) -> bool:
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

    def send_status(self, state, note=""):
        self.send_json({"type": "status", "state": state, "note": note})

    def send_result(self, source, item_id, ok, detail=""):
        self.send_json({"type": "result", "source": source, "item_id": item_id,
                        "ok": bool(ok), "detail": str(detail or "")[:300]})

    def send_defer(self, source, item_id, seconds, reason=""):
        self.send_json({"type": "defer", "source": source, "item_id": item_id,
                        "seconds": float(seconds), "reason": str(reason or "")[:120]})

    # ────────────────────────── 拉取模式 ──────────────────────────
    def _read_reply(self, predicate, timeout):
        """临时直读 socket，直到读到 predicate 认的回应（没有读线程时用）。"""
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
                self._dispatch(message, None)
        return None

    def want(self, source, count=1, timeout=10.0):
        """按自己的节奏要一批待发条目（拉取模式）。没连上/超时返回 []。"""
        if not self.pull:
            raise RuntimeError("这个客户端不是拉取模式（pull=False）")
        if not self.connected:
            return []
        box = None
        if self._reader_active:
            with self._waiter_lock:
                box = self._waiters.setdefault(source, queue.Queue())
            while not box.empty():
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

    def pending(self, source, timeout=5.0):
        """问服务端：这个 source 还有多少条未发。"""
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
    def run_forever(self, on_item, max_rounds=None):
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
                handled += self._session(on_item, max_rounds)
            except Exception as exc:
                self.last_error = str(exc)
                self.log(f"[{self.channel}] 会话中断：{exc}")
            self.close()
            if max_rounds is not None and handled >= max_rounds:
                return
            if self.stopped:
                return
            time.sleep(RECONNECT_MIN)

    def _session(self, on_item, max_rounds=None):
        handled = 0
        self._reader_active = True
        try:
            while not self.stopped:
                try:
                    kind, data = self._ws.recv_message()
                except socket.timeout:
                    continue
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
                    time.sleep(0.5)      # 留给服务端收完最后一条回执并写库
                    break
        finally:
            self._reader_active = False
        return handled

    def _dispatch(self, message, on_item):
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
                self.log(f"[{self.channel}] 已连接看板，但没有启用的推送目标")
            else:
                self.log(f"[{self.channel}] 本次启用 {len(self.sources)} 个目标：{', '.join(self.sources)}")
            if self.paused:
                self.log(f"[{self.channel}] 看板已暂停该渠道的推送")
            self.send_status("idle", "已连接" if self.sources else "等待启用推送目标")
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

    def _handle_item(self, message, on_item):
        source = str(message.get("source") or "")
        item = message.get("item") or {}
        item_id = item.get("id")
        try:
            outcome = on_item(item, source)
        except Exception as exc:
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
                         str(outcome.get("detail") or ""))
