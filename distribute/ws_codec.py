# -*- coding: utf-8 -*-
"""ws_codec.py · WebSocket（RFC6455）最小实现：握手 + 帧编解码（纯标准库）
==========================================================================
只为航小麦的渠道通道服务，够用就好：

- 只处理**文本帧**（payload 是 JSON），服务端→客户端不掩码、客户端→服务端掩码
- 支持 7/16/64 位长度、分片重组、ping/pong、close
- 不做扩展协商（permessage-deflate 一律不答应），不做子协议

服务端在 `dashboard/ws.py` 用它；渠道 SDK 在 `distribute/ws_channel.py` 用它。
麦麦插件侧有一份自包含镜像（`hangxiaomai_news/hx_ws.py`），自检里有一条对照用例。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 4 * 1024 * 1024      # 单条消息上限（4MB）；超了直接关连接

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WSError(Exception):
    """握手/帧格式错误（调用方通常直接关连接）。"""


# ────────────────────────── 握手 ──────────────────────────

def accept_key(client_key: str) -> str:
    """Sec-WebSocket-Key → Sec-WebSocket-Accept。"""
    digest = hashlib.sha1((str(client_key).strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_head(sock: socket.socket, limit: int = 65536) -> tuple:
    """从 socket 读 HTTP 请求头（读到空行为止），返回 (请求行, 头字典, 剩余字节)。"""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise WSError("连接在握手前关闭")
        buffer += chunk
        if len(buffer) > limit:
            raise WSError("请求头过大")
    head, _, rest = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers, rest


def read_upgrade(sock: socket.socket) -> dict:
    """服务端：读并校验升级请求，**先不发 101**。

    返回 {"path","query","key","headers"}；调用方校验完 path/token 之后再调用
    `accept_upgrade()` 回 101 —— 鉴权必须在 101 之前，否则 token 不对的连接也会被
    当成握手成功（客户端会以为连上了）。
    """
    request_line, headers, _ = parse_head(sock)
    parts = request_line.split(" ")
    if len(parts) < 2 or parts[0].upper() != "GET":
        raise WSError("只支持 GET 升级请求")
    if "websocket" not in headers.get("upgrade", "").lower():
        raise WSError("不是 WebSocket 升级请求")
    key = headers.get("sec-websocket-key", "")
    if not key:
        raise WSError("缺少 Sec-WebSocket-Key")
    path, _, query = parts[1].partition("?")
    return {"path": path, "query": query, "key": key, "headers": headers}


def accept_upgrade(sock: socket.socket, key: str) -> None:
    """服务端：回 101 Switching Protocols。"""
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
        "\r\n"
    )
    sock.sendall(response.encode("ascii"))


def server_handshake(sock: socket.socket) -> dict:
    """读升级请求 + 直接回 101（不做 path/token 校验；ws.py 走的是拆开的两步）。"""
    head = read_upgrade(sock)
    accept_upgrade(sock, head["key"])
    return head


def client_handshake(sock: socket.socket, host: str, port: int, path: str,
                     timeout: float = 10.0) -> None:
    """客户端握手：发 Upgrade 请求并校验 101 + Sec-WebSocket-Accept。"""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    sock.settimeout(timeout)
    request_line, headers, rest = parse_head(sock)
    if "101" not in request_line:
        raise WSError(f"握手失败：{request_line.strip()}")
    if headers.get("sec-websocket-accept", "") != accept_key(key):
        raise WSError("Sec-WebSocket-Accept 不匹配")
    del rest


# ────────────────────────── 帧 ──────────────────────────

def encode_frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    """编一个完整的、FIN=1 的帧（我们的消息都很小，不分片）。"""
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


def _apply_mask(payload: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))


class WSSocket:
    """在一个已握手的 socket 上收发消息（服务端 / 客户端两种角色）。"""

    def __init__(self, sock: socket.socket, is_server: bool, timeout: float = 60.0):
        self.sock = sock
        self.is_server = is_server          # 服务端发的帧不掩码；客户端必须掩码
        self.sock.settimeout(timeout)

    # ── 发送 ──
    def send(self, opcode: int, payload: bytes = b"") -> None:
        self.sock.sendall(encode_frame(opcode, payload, mask=not self.is_server))

    def send_text(self, text: str) -> None:
        self.send(OP_TEXT, str(text).encode("utf-8"))

    def send_json(self, data: dict) -> None:
        self.send_text(json.dumps(data, ensure_ascii=False))

    def send_ping(self, payload: bytes = b"hx") -> None:
        self.send(OP_PING, payload)

    def send_close(self, code: int = 1000) -> None:
        try:
            self.send(OP_CLOSE, struct.pack(">H", code))
        except Exception:
            pass

    @staticmethod
    def _read_exact(sock: socket.socket, count: int) -> bytes:
        buffer = b""
        while len(buffer) < count:
            chunk = sock.recv(count - len(buffer))
            if not chunk:
                raise WSError("连接已关闭")
            buffer += chunk
        return buffer

    def _read_frame(self) -> tuple:
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
            payload = _apply_mask(payload, key)
        return fin, opcode, payload

    def recv_message(self) -> tuple:
        """收一条完整消息，返回 (类型, 数据)。

        类型：`text`（数据是 str）、`close`、`ping`（已自动回 pong）、`pong`。
        普通数据帧会被自动重组（分片）；对端关闭/出错抛 WSError 或返回 `close`。
        """
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
                try:
                    return "text", data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WSError(f"文本帧不是 UTF-8：{exc}") from exc

    def drain(self, budget: float = 0.3) -> None:
        """关连接前把对端还没被我读走的数据读完。

        为什么需要：接收缓冲区里还有没读的数据时直接 close，TCP 会发 RST，
        对端可能丢掉刚收到的最后一帧（实测表现就是"最后一条回执没了、条目没标已发"）。
        """
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

    def close(self) -> None:
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


def parse_query(query: str) -> dict:
    """`a=1&b=2` → {"a": "1", "b": "2"}（不做 URL 解码之外的花活）。"""
    from urllib.parse import parse_qs
    return {key: values[0] for key, values in parse_qs(query or "").items()}
