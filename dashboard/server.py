# -*- coding: utf-8 -*-
"""航小麦本机看板：内容台账与采集、处理、分发三服务控制。"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE = DATA / "state"
LOGS = ROOT / "logs"
WEB = ROOT / "dashboard" / "webui"
TOKEN_FILE = STATE / "dashboard_token.txt"
AUDIT = STATE / "audit.jsonl"
COOKIE_FILE = STATE / "x_cookies.txt"
LOCK = threading.Lock()
PROCESSES: dict[str, subprocess.Popen] = {}
SERVICE_NAMES = {"collect": "采集", "process": "处理", "distribute": "分发"}
CONFIG_FIELDS = {
    "collect": ["PROXY_URL", "COOKIES_PATH", "WATCH_ACCOUNT", "NEWS_ACCOUNTS", "SCAN_BATCH", "PAUSE_SECONDS", "COLLECT_INTERVAL_SECONDS"],
    "process": ["DEEPSEEK_KEY", "PROCESS_INTERVAL_SECONDS"],
    "distribute": ["DISTRIBUTE_CHANNEL"],
}
SECRET_FIELDS = {"DEEPSEEK_KEY"}



def token() -> str:
    STATE.mkdir(parents=True, exist_ok=True)
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def audit(action: str, payload: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"ts": time.time(), "action": action, "payload": payload}, ensure_ascii=False) + "\n")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default
    except Exception:
        return default


def env_values() -> dict[str, str]:
    path = ROOT / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip()
    return values


def normalize_cookies(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise ValueError("Cookie 内容为空")
    if text.startswith("# Netscape HTTP Cookie File") or "\t" in text and "\n" in text:
        return text + "\n"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    rows = []
    if isinstance(parsed, dict):
        parsed = parsed.get("cookies", parsed)
        if isinstance(parsed, dict):
            parsed = [{"name": key, "value": value} for key, value in parsed.items()]
    if isinstance(parsed, list):
        for cookie in parsed:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name", "")).strip()
            value = str(cookie.get("value", ""))
            if not name:
                continue
            domain = str(cookie.get("domain", ".x.com") or ".x.com")
            if "x.com" not in domain:
                domain = ".x.com"
            path = str(cookie.get("path", "/") or "/")
            secure = "TRUE" if cookie.get("secure", True) else "FALSE"
            expires = cookie.get("expirationDate", cookie.get("expires", 0)) or 0
            try:
                expires = str(int(float(expires)))
            except (TypeError, ValueError):
                expires = "0"
            rows.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    else:
        for part in text.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                rows.append(f".x.com\tTRUE\t/\tTRUE\t0\t{name.strip()}\t{value.strip()}")
    if not rows:
        raise ValueError("无法识别 Cookie 格式；请粘贴 Header String、JSON 或 Netscape cookies.txt")
    return "# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n"


def save_cookies(raw: str) -> None:
    normalized = normalize_cookies(raw)
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = COOKIE_FILE.with_suffix(".tmp")
    temporary.write_text(normalized, encoding="utf-8")
    temporary.replace(COOKIE_FILE)
    save_service_config("collect", {"COOKIES_PATH": str(COOKIE_FILE)})


def service_config(name: str) -> dict:
    if name not in CONFIG_FIELDS:
        raise ValueError("未知服务")
    values = env_values()
    fields = []
    for key in CONFIG_FIELDS[name]:
        value = values.get(key, "")
        fields.append({"key": key, "value": value, "secret": key in SECRET_FIELDS})
    result = {"service": name, "fields": fields}
    if name == "collect":
        result["cookie_text"] = COOKIE_FILE.read_text(encoding="utf-8") if COOKIE_FILE.exists() else ""
        result["cookie_path"] = str(COOKIE_FILE)
    return result


def save_service_config(name: str, updates: dict) -> None:
    if name not in CONFIG_FIELDS:
        raise ValueError("未知服务")
    allowed = set(CONFIG_FIELDS[name])
    clean = {}
    for key, value in updates.items():
        if key in allowed and isinstance(value, (str, int, float)):
            text = str(value).strip()
            if "\n" in text or "\r" in text:
                raise ValueError("配置值不能包含换行")
            if key in SECRET_FIELDS and text == "":
                continue
            clean[key] = text
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written = set()
    result = []
    for line in lines:
        key, separator, _ = line.partition("=")
        name_on_line = key.strip()
        if separator and name_on_line in clean:
            result.append(f"{name_on_line}={clean[name_on_line]}")
            written.add(name_on_line)
        else:
            result.append(line)
    for key, value in clean.items():
        if key not in written:
            result.append(f"{key}={value}")
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


def all_items(since_id: int = 0, limit: int = 500) -> list[dict]:
    """Read the unified content list from SQLite; JSON is no longer the dashboard source.

    since_id > 0 时只返回 id 更大的新条目（增量），供前端直接往顶部追加。
    """
    try:
        import sqlite3
        db = DATA / "hx.sqlite3"
        if not db.exists():
            return []
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        if since_id > 0:
            cursor = conn.execute(
                "SELECT * FROM items WHERE id > ? ORDER BY id DESC LIMIT ?",
                (since_id, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM items ORDER BY COALESCE(published_ts,0) DESC, id DESC LIMIT ?",
                (limit,),
            )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        for row in rows:
            if isinstance(row.get("images"), str):
                try:
                    row["images"] = json.loads(row["images"])
                except Exception:
                    row["images"] = []
            row["ori_url"] = row.get("ori_url") or row.get("url") or ""
            row["images"] = [str(p) for p in (row.get("images") or []) if str(p).strip()]
            row["_stage"] = row.get("stage", "")
        return rows
    except Exception as exc:
        print(f"[看板] SQLite 查询失败: {exc}", flush=True)
        return []


def item_stats() -> tuple[int, int]:
    """返回 (总条目数, 当前最大 id)：前者用于计数显示，后者是前端的增量游标。"""
    try:
        import sqlite3
        db = DATA / "hx.sqlite3"
        if not db.exists():
            return 0, 0
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(MAX(id), 0) AS latest FROM items"
        ).fetchone()
        conn.close()
        return int(row[0] or 0), int(row[1] or 0)
    except Exception as exc:
        print(f"[看板] SQLite 统计失败: {exc}", flush=True)
        return 0, 0


def services() -> dict:
    result = {}
    with LOCK:
        for name, label in SERVICE_NAMES.items():
            process = PROCESSES.get(name)
            running = bool(process and process.poll() is None)
            result[name] = {
                "name": name,
                "label": label,
                "state": "运行中" if running else "已停止",
                "running": running,
                "pid": process.pid if process else None,
            }
    return result


def summary() -> dict:
    """轻量状态：不含条目正文，供 SSE 每秒推送与前端判断是否需要增量拉取。"""
    pending = read_json(DATA / "queue" / "待推送.json", [])
    total, latest_id = item_stats()
    return {
        "services": services(),
        "item_count": total,
        "latest_id": latest_id,
        "pending_count": len(pending) if isinstance(pending, list) else 0,
        "env_ready": (ROOT / ".env").exists(),
        "configurable_services": list(CONFIG_FIELDS),
        "ts": time.time(),
    }


def snapshot(since_id: int = 0) -> dict:
    """since_id > 0 时 items 只包含新条目，前端据此只往顶部插入、不重绘旧内容。"""
    data = summary()
    data["items"] = all_items(since_id)
    data["incremental"] = since_id > 0
    return data


def pump_service_output(name: str, process: subprocess.Popen, log_stream) -> None:
    """Tee a managed service's combined output to the dashboard terminal and log file."""
    label = SERVICE_NAMES.get(name, name)
    try:
        if process.stdout is None:
            return
        for raw in iter(process.stdout.readline, ""):
            if not raw:
                break
            line = raw.rstrip("\r\n")
            stamped = f"[{label}] {line}"
            print(stamped, flush=True)
            log_stream.write(stamped + "\n")
            log_stream.flush()
    except Exception as exc:
        print(f"[{label}] 输出转发失败: {exc}", flush=True)
    finally:
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass
        try:
            log_stream.close()
        except Exception:
            pass
        print(f"[{label}] 服务输出流已结束，退出码={process.poll()}", flush=True)


def start_service(name: str) -> int:
    if name not in SERVICE_NAMES:
        raise ValueError("未知服务")
    with LOCK:
        existing = PROCESSES.get(name)
        if existing and existing.poll() is None:
            print(f"[{SERVICE_NAMES[name]}] 已在运行，PID={existing.pid}", flush=True)
            return existing.pid
        LOGS.mkdir(parents=True, exist_ok=True)
        log_stream = (LOGS / f"dashboard_{name}.log").open("a", encoding="utf-8")
        command = [sys.executable, str(ROOT / "dashboard" / "service_runner.py"), name]
        print(f"[{SERVICE_NAMES[name]}] 启动：{' '.join(command)}", flush=True)
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # 本进程按 UTF-8 读服务输出，整棵子进程树也必须按 UTF-8 写、并且不缓冲，
            # 否则服务日志会卡在缓冲区，本终端看不到实时输出。
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        PROCESSES[name] = process
        threading.Thread(
            target=pump_service_output,
            args=(name, process, log_stream),
            name=f"dashboard-output-{name}",
            daemon=True,
        ).start()
        audit("service_start", {"service": name, "pid": process.pid})
        return process.pid


def stop_service(name: str) -> bool:
    with LOCK:
        process = PROCESSES.get(name)
        if process and process.poll() is None:
            process.terminate()
            audit("service_stop", {"service": name, "pid": process.pid})
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "HXDashboard/2.0"

    def send_json(self, code: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, target: Path, max_age: int = 0) -> None:
        """Send a file with validators so the browser can cache and revalidate it.

        max_age > 0 用于图片这类不再变动的资源；0 表示每次都回源校验，命中 ETag 走 304。
        """
        content_types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        stat = target.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        cache_control = f"private, max-age={max_age}" if max_age else "no-cache"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        self.end_headers()
        self.wfile.write(raw)

    def send_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for _ in range(120):
            try:
                payload = json.dumps(summary(), ensure_ascii=False)
                self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(1)
            except Exception:
                break

    def auth(self) -> bool:
        return self.headers.get("X-HX-Token", "") == token()

    def query_since_id(self) -> int:
        """Read ?since_id=N for incremental pulls; missing or invalid means a full load."""
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        try:
            return max(0, int(query.get("since_id", ["0"])[0]))
        except (TypeError, ValueError):
            return 0

    def do_GET(self) -> None:
        if self.path.startswith("/api/snapshot"):
            return self.send_json(200, snapshot(since_id=self.query_since_id()))
        if self.path == "/api/stream":
            return self.send_sse()
        if self.path.startswith("/api/image/"):
            raw = unquote(self.path.split("/api/image/", 1)[1].split("?", 1)[0])
            image_root = (DATA / "images").resolve()
            target = (image_root / raw.replace("\\", "/")).resolve()
            if image_root not in target.parents or not target.is_file():
                return self.send_json(404, {"error": "image not found"})
            return self.send_file(target, max_age=604800)
        if self.path.startswith("/api/service/config/"):
            if not self.auth():
                return self.send_json(401, {"error": "invalid token"})
            try:
                return self.send_json(200, service_config(self.path.rsplit("/", 1)[-1]))
            except Exception as exc:
                return self.send_json(400, {"error": str(exc)})
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/index.html"
        target = (WEB / path.lstrip("/")).resolve()
        if WEB.resolve() not in target.parents:
            return self.send_json(403, {"error": "forbidden"})
        if target.exists() and target.is_file():
            return self.send_file(target)
        return self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.auth():
            return self.send_json(401, {"error": "invalid token"})
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) if length else b"{}")
        except Exception:
            payload = {}
        if self.path == "/api/cookies/import":
            try:
                save_cookies(str(payload.get("content", "")))
                audit("cookie_import", {"path": str(COOKIE_FILE)})
                return self.send_json(200, {"ok": True, "path": str(COOKIE_FILE)})
            except Exception as exc:
                return self.send_json(400, {"error": str(exc)})
        if self.path == "/api/item/update":
            try:
                import sqlite3
                item_id=int(payload.get('id'))
                allowed={'zh','raw','score','decision','reason','content_type','ori_url'}
                values={k:payload[k] for k in allowed if k in payload}
                if not values: raise ValueError('没有可修改字段')
                db=DATA/'hx.sqlite3'; conn=sqlite3.connect(db)
                sets=','.join(f'{k}=?' for k in values); params=list(values.values())+[item_id]
                conn.execute(f'UPDATE items SET {sets}, processed_at=? WHERE id=?', list(values.values())+[time.strftime('%Y-%m-%dT%H:%M:%S'),item_id]); conn.commit(); conn.close()
                audit('item_update',{'id':item_id,'fields':list(values)}); return self.send_json(200,{'ok':True,'id':item_id})
            except Exception as exc: return self.send_json(400,{'error':str(exc)})
        if self.path == "/api/item/delete":
            try:
                item_id=int(payload.get('id')); conn=__import__('sqlite3').connect(DATA/'hx.sqlite3'); conn.execute('DELETE FROM items WHERE id=?',(item_id,)); conn.commit(); conn.close(); audit('item_delete',{'id':item_id}); return self.send_json(200,{'ok':True})
            except Exception as exc: return self.send_json(400,{'error':str(exc)})
        if self.path == "/api/service/config":
            try:
                name = str(payload.get("service", ""))
                values = payload.get("values", {})
                if not isinstance(values, dict):
                    raise ValueError("配置格式错误")
                save_service_config(name, values)
                audit("service_config", {"service": name, "keys": sorted(values)})
                return self.send_json(200, {"ok": True})
            except Exception as exc:
                return self.send_json(400, {"error": str(exc)})
        if self.path == "/api/service/toggle":
            name = str(payload.get("service", ""))
            try:
                enabled = bool(payload.get("enabled"))
                value = start_service(name) if enabled else stop_service(name)
                return self.send_json(200, {"ok": True, "value": value})
            except Exception as exc:
                return self.send_json(400, {"error": str(exc)})
        return self.send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args) -> None:
        pass


class QuietHTTPServer(ThreadingHTTPServer):
    """Do not print noisy Windows socket-abort tracebacks when browsers close SSE."""
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    print("HX dashboard token:", token(), flush=True)
    QuietHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
