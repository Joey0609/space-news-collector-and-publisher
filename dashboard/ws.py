# -*- coding: utf-8 -*-
"""ws.py · 渠道 WebSocket 服务端（纯标准库，默认 127.0.0.1:8788）
=================================================================
只做传输与鉴权，业务在 `dashboard/channels.py`：

    握手（token 校验）→ 每连接一个读线程 → JSON 消息 → channels.handle_message()
                                           ↘ 断开时 channels.unregister_connection()

- 只绑本机；必须带 token：`GET /ws?token=<TOKEN>&channel=<渠道id>`
- 空闲 30 秒发一次 ping；连续 10 次没收到任何数据就关掉（避免挂死连接）
- 发送走连接自带的锁（推送线程和读线程都可能发），保证帧不交错
"""
from __future__ import annotations

import json
import socket
import threading
import time

from distribute.ws_codec import WSError, WSSocket, accept_upgrade, parse_query, read_upgrade

PING_INTERVAL = 30.0
MAX_IDLE_ROUNDS = 10          # 30s × 10 = 5 分钟没有数据就断开
READ_TIMEOUT = PING_INTERVAL
CONN_LOCK = threading.Lock()  # 保护 connections 列表


class Connection:
    """一条已握手的渠道连接。"""

    def __init__(self, ws: WSSocket, peer: str = ""):
        self.ws = ws
        self.peer = peer
        self.channel = ""
        self.closed = False
        self.lock = threading.Lock()

    def send_json(self, data: dict) -> None:
        if self.closed:
            raise ConnectionError("连接已关闭")
        payload = json.dumps(data, ensure_ascii=False)
        with self.lock:                       # 同一时刻只允许一个线程写
            self.ws.send_text(payload)

    def send_ping(self) -> None:
        with self.lock:
            self.ws.send_ping()

    def close(self) -> None:
        self.closed = True
        self.ws.close()

    def describe(self) -> str:
        return f"{self.channel or '未注册'}@{self.peer}"


class ChannelWSServer:
    """渠道 WS 服务端：start() 起后台线程，stop() 关掉全部连接。"""

    def __init__(self, host: str, port: int, token: str, handle_message, on_disconnect=None):
        self.host = host
        self.port = port
        self.token = token
        self.handle_message = handle_message          # (connection, message) -> None
        self.on_disconnect = on_disconnect            # (connection) -> None
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.connections: list[Connection] = []
        self.running = False
        self.error = ""

    # ── 生命周期 ──
    def start(self) -> bool:
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(16)
            server.settimeout(1.0)
        except OSError as exc:
            self.error = str(exc)
            print(f"[渠道WS] 启动失败 {self.host}:{self.port} — {exc}", flush=True)
            return False
        self.sock = server
        self.running = True
        self.thread = threading.Thread(target=self._serve, name="hx-ws-server", daemon=True)
        self.thread.start()
        print(f"[渠道WS] 已监听 ws://{self.host}:{self.port}/ws", flush=True)
        return True

    def stop(self) -> None:
        self.running = False
        for connection in list(self.connections):
            try:
                connection.close()
            except Exception:
                pass
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"

    # ── 接受连接 ──
    def _serve(self) -> None:
        while self.running:
            try:
                client, address = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client, address),
                             name="hx-ws-conn", daemon=True).start()
        print("[渠道WS] 监听线程结束", flush=True)

    def _reject(self, client: socket.socket, code: int, reason: str) -> None:
        try:
            body = reason.encode("utf-8")
            client.sendall((f"HTTP/1.1 {code} {reason}\r\n"
                            "Content-Type: text/plain; charset=utf-8\r\n"
                            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii")
                           + body)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _handle(self, client: socket.socket, address) -> None:
        peer = f"{address[0]}:{address[1]}"
        # 先读请求、校验 path 与 token，**最后**才回 101：token 不对必须在升级前拒掉
        try:
            head = read_upgrade(client)
        except WSError as exc:
            self._reject(client, 400, str(exc))
            return
        except Exception:
            self._reject(client, 400, "handshake failed")
            return
        query = parse_query(head.get("query", ""))
        if head.get("path") not in ("/ws", "/ws/"):
            return self._reject(client, 404, "not found")
        if str(query.get("token", "")) != self.token:
            print(f"[渠道WS] {peer} token 不正确，拒绝", flush=True)
            return self._reject(client, 401, "invalid token")
        try:
            accept_upgrade(client, head["key"])
        except OSError:
            try:
                client.close()
            except Exception:
                pass
            return

        ws = WSSocket(client, is_server=True, timeout=READ_TIMEOUT)
        connection = Connection(ws, peer)
        connection.channel = str(query.get("channel", "") or "")
        with CONN_LOCK:
            self.connections.append(connection)
        print(f"[渠道WS] {peer} 已连接（channel={connection.channel or '?'}）", flush=True)

        idle_rounds = 0
        try:
            while self.running and not connection.closed:
                try:
                    kind, data = ws.recv_message()
                except socket.timeout:
                    # 空闲：发个 ping 探活（帧都很小且一次写完，正常不会卡在半帧上）
                    idle_rounds += 1
                    if idle_rounds > MAX_IDLE_ROUNDS:
                        print(f"[渠道WS] {connection.describe()} 长时间无数据，断开", flush=True)
                        break
                    connection.send_ping()
                    continue
                except WSError as exc:
                    print(f"[渠道WS] {connection.describe()} 帧错误：{exc}", flush=True)
                    break
                except (ConnectionError, OSError):
                    break
                idle_rounds = 0
                if kind == "close":
                    break
                if kind != "text":
                    continue                       # ping/pong 已在 codec 里处理
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    connection.send_json({"type": "error", "error": "消息不是合法 JSON"})
                    continue
                if not isinstance(message, dict):
                    connection.send_json({"type": "error", "error": "消息必须是 JSON 对象"})
                    continue
                try:
                    self.handle_message(connection, message)
                except Exception as exc:            # 单条消息出错不能弄死连接
                    print(f"[渠道WS] {connection.describe()} 处理消息出错: {exc}", flush=True)
                    try:
                        connection.send_json({"type": "error", "error": str(exc)})
                    except Exception:
                        break
        finally:
            if self.on_disconnect is not None:
                try:
                    self.on_disconnect(connection)
                except Exception as exc:
                    print(f"[渠道WS] 断开清理失败: {exc}", flush=True)
            connection.close()
            with CONN_LOCK:
                if connection in self.connections:
                    self.connections.remove(connection)
            print(f"[渠道WS] {connection.describe()} 连接结束", flush=True)
