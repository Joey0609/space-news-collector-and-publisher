# -*- coding: utf-8 -*-
"""航小麦本机看板：内容台账与采集、处理、分发三服务控制。"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:      # 队列口径在 distribute/push_db.py（直接读 SQLite）
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
STATE = DATA / "state"
PROCESS_PROGRESS_FILE = STATE / "process_progress.json"
LOGS = ROOT / "logs"
WEB = ROOT / "dashboard" / "webui"
TOKEN_FILE = STATE / "dashboard_token.txt"
AUDIT = STATE / "audit.jsonl"
COOKIE_FILE = STATE / "x_cookies.txt"
# 待分发数直接问最终输出池（SQLite：stage=processed、decision='发' 且 source 未发），不再读 待推送.json
from distribute.push_db import pending_count as queue_pending_count  # noqa: E402
from process.analysis_contract import missing_analysis_fields, processing_required  # noqa: E402
from dashboard import alison_config  # noqa: E402
from dashboard import channels as channel_hub  # noqa: E402
from dashboard.ws import ChannelWSServer  # noqa: E402
from process import model_endpoints  # noqa: E402

LOCK = threading.Lock()
PROCESSES: dict[str, subprocess.Popen] = {}
# 服务 = 看板固定托管的那两个循环；分发改为"渠道"（见 /api/channels 与 channels.py）
SERVICE_NAMES = {"collect": "采集", "process": "处理"}
CONFIG_FIELDS = {
    "collect": ["PROXY_URL", "COOKIES_PATH", "SCAN_BATCH", "PAUSE_SECONDS", "COLLECT_INTERVAL_SECONDS", "WEB_COLLECT_INTERVAL_SECONDS", "SOURCES"],
    "process": ["AI_REPEAT_CONTEXT", "ANALYSIS_MAX_ATTEMPTS", "ANALYSIS_INLINE_RETRIES", "ANALYSIS_RATE_LIMIT_WAIT"],
}
CHANNEL_KEY_PREFIX = "channel:"
WS_PORT = int(os.getenv("HX_WS_PORT", "") or 8788)
WS_SERVER: ChannelWSServer | None = None
SECRET_FIELDS = {"MODEL_API_KEY"}
# 单选字段：看板前端会渲染成下拉框（只允许这三种协议）
CHOICE_FIELDS = {
    "MODEL_PROTOCOL": ["openai", "responses", "anthropic"],
}
# 未配置时在表单里显示的默认值（避免用户面对空白不知道填什么）
# 注意：模型地址/名称没有默认服务商，留空就是"必须自己填"。
FIELD_DEFAULTS = {
    "WEB_COLLECT_INTERVAL_SECONDS": "900",
    "MODEL_PROTOCOL": "openai",
    "MODEL_API_BASE": "",
    "MODEL_NAME": "",
    "MODEL_TIMEOUT": "90",
    "MODEL_MAX_TOKENS": "0",
    "AI_REPEAT_CONTEXT": "40",
    "ANALYSIS_MAX_ATTEMPTS": "3",
    "ANALYSIS_INLINE_RETRIES": "2",
    "ANALYSIS_RATE_LIMIT_WAIT": "20",
    "ANALYSIS_PAUSE_SECONDS": "0.5",
}



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
    return model_endpoints.read_env(ROOT / ".env")


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
        value = values.get(key, "") or FIELD_DEFAULTS.get(key, "")
        fields.append({
            "key": key,
            "value": value,
            "secret": key in SECRET_FIELDS,
            # 非空表示这是单选字段，前端渲染成下拉框
            "choices": CHOICE_FIELDS.get(key, []),
        })
    result = {"service": name, "fields": fields, "env": {key: ("***" if key in SECRET_FIELDS else values.get(key, ""))
                                                         for key in CONFIG_FIELDS[name]}}
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
            if key == "WEB_COLLECT_INTERVAL_SECONDS":
                if not text.isdigit() or int(text) < 10:
                    raise ValueError("网页刷新间隔必须是不小于 10 的整数秒")
            if key == "SOURCES" and set(value.strip() for value in text.split(",") if value.strip()) - {"x", "rss", "snapi", "sina"}:
                raise ValueError("启用来源只允许 x,rss,snapi,sina；留空全部启用")
            if name == "process":
                try:
                    number = float(text)
                    if not math.isfinite(number) or number < 0:
                        raise ValueError()
                    if key in {"AI_REPEAT_CONTEXT", "ANALYSIS_MAX_ATTEMPTS", "ANALYSIS_INLINE_RETRIES"}:
                        if int(text) < (1 if key == "ANALYSIS_MAX_ATTEMPTS" else 0):
                            raise ValueError()
                except ValueError as exc:
                    raise ValueError("处理参数必须是有效的非负数；次数和条数必须为整数，最大处理尝试次数至少为 1") from exc
            clean[key] = text
    write_env(clean)


def write_env(clean: dict) -> None:
    """串行化配置写入并原子替换，保留其他服务配置和注释。"""
    with LOCK:
        _write_env_unlocked(clean)


def _write_env_unlocked(clean: dict) -> None:
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
    temporary = path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_ai_config(payload: dict, use=False) -> dict:
    """保存编辑不会改变使用项；只有明确的 use 操作才切换。"""
    path = ROOT / ".env"
    with LOCK:
        if payload.get("version") != model_endpoints.version(path):
            raise ValueError("配置已被其他窗口修改，请关闭弹窗后重新打开")
        old, active = model_endpoints.read_settings(path)
        if use:
            active = str(payload.get("id", ""))
        endpoints = model_endpoints.validate_endpoints(payload.get("endpoints"), old, active, activating=use)
        model_endpoints.save_settings(path, endpoints, active)
        result = model_endpoints.public_settings(path)
    audit("ai_use" if use else "ai_config", {"active_id": active, "endpoint_ids": [item["id"] for item in endpoints]})
    return result


ITEM_SEARCH_FIELDS = ("raw", "zh", "ai_title", "ai_summary", "quoted_raw", "quoted_zh",
                      "account", "orig_account", "quoted_account", "forward_comment", "ai_tags", "invalid_reason", "ori_url")
ITEM_SORTS = {
    "published": "COALESCE(ts,0) DESC, id DESC",
    "collected": "(collected_at IS NOT NULL AND collected_at <> '') DESC, collected_at DESC, id DESC",
    "processed": "(processed_at IS NOT NULL AND processed_at <> '') DESC, processed_at DESC, id DESC",
}


def all_items(offset: int = 0, limit: int = 20, query: str = "", sort: str = "published") -> list[dict]:
    """按搜索词和选择的时间顺序分页读取台账。"""
    return _item_rows("", "LIMIT ? OFFSET ?", (max(1, int(limit)), max(0, int(offset))),
                      search=query, sort=sort)


def changed_items(since_id: int = 0, since_rev: int = 0, limit: int = 300) -> list[dict]:
    """变了的东西：新条目（id 更大）或被更新过的老条目（rev 更大）。

    翻译是**就地更新**已有行，id 不变，所以只看 id 会漏掉"完整分析刚提交"这类变化；
    rev 由各写入方（analyze_db / 看板改译文）每次 +1。
    """
    if since_id <= 0 and since_rev <= 0:
        return []
    return _item_rows(
        "WHERE id > ? OR COALESCE(rev, 1) > ?",
        "LIMIT ?",
        (int(since_id), int(since_rev), max(1, int(limit))),
    )


def _item_rows(where_sql: str, tail_sql: str, params: tuple, *, search: str = "", sort: str = "published") -> list[dict]:
    """台账查询：WHERE 必须排在 ORDER BY 前面，分页的 LIMIT 排在后面。"""
    try:
        import sqlite3
        db = DATA / "hx.sqlite3"
        if not db.exists():
            return []
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        clauses = [where_sql[6:]] if where_sql.startswith("WHERE ") else []
        query = str(search or "").strip()[:200]
        query_params = []
        if query:
            clauses.append("(" + " OR ".join(f"COALESCE({field},'') LIKE ?" for field in ITEM_SEARCH_FIELDS) + ")")
            pattern = f"%{query}%"
            query_params.extend([pattern] * len(ITEM_SEARCH_FIELDS))
        combined_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = ITEM_SORTS.get(sort, ITEM_SORTS["published"])
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM items {combined_where} ORDER BY {order} {tail_sql}",
            (*query_params, *params) if not where_sql else (*params[:2], *query_params, *params[2:]),
        ).fetchall()]
        conn.close()
    except Exception as exc:
        print(f"[看板] SQLite 查询失败: {exc}", flush=True)
        return []
    for row in rows:
        if isinstance(row.get("images"), str):
            try:
                row["images"] = json.loads(row["images"])
            except Exception:
                row["images"] = []
        row["ori_url"] = row.get("ori_url") or ""
        row["images"] = [str(p) for p in (row.get("images") or []) if str(p).strip()]
        row["_stage"] = row.get("stage", "")
        # 分数统一取自 AI 完整分析
        score = row.get("ai_score")
        row["score"] = score
    return rows


def item_stats() -> tuple[int, int, int]:
    """返回 (总条目数, 最大 id, 最大 rev)：前两个用于计数与增量，rev 用于"哪些行被更新过"。"""
    try:
        import sqlite3
        db = DATA / "hx.sqlite3"
        if not db.exists():
            return 0, 0, 0
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(MAX(id), 0) AS latest, "
            "COALESCE(MAX(rev), 0) AS top_rev FROM items"
        ).fetchone()
        conn.close()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    except Exception as exc:
        print(f"[看板] SQLite 统计失败: {exc}", flush=True)
        return 0, 0, 0


def matching_item_count(search: str = "") -> int:
    """搜索框当前匹配的台账条数。"""
    query = str(search or "").strip()[:200]
    try:
        db = DATA / "hx.sqlite3"
        if not db.exists():
            return 0
        conn = sqlite3.connect(db)
        if query:
            pattern = f"%{query}%"
            predicate = " OR ".join(f"COALESCE({field},'') LIKE ?" for field in ITEM_SEARCH_FIELDS)
            row = conn.execute(f"SELECT COUNT(*) FROM items WHERE {predicate}", [pattern] * len(ITEM_SEARCH_FIELDS)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        conn.close()
        return int(row[0] or 0)
    except Exception as exc:
        print(f"[看板] 搜索计数读取失败: {exc}", flush=True)
        return 0


def services() -> dict:
    """采集/处理两个"服务"（分发不再是一个服务，而是一堆渠道，见 /api/channels）。"""
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


def _max_process_attempts() -> int:
    try:
        return max(1, int(env_values().get("ANALYSIS_MAX_ATTEMPTS", "3")))
    except (TypeError, ValueError):
        return 3


def _read_process_progress() -> dict:
    try:
        value = json.loads(PROCESS_PROGRESS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def active_process_progress() -> dict:
    """同一份运行状态供摘要与队列使用，停止后不保留过期的处理标记。"""
    progress = _read_process_progress()
    running = bool(services().get("process", {}).get("running"))
    active = bool(running and progress.get("running"))
    entries = progress.get("active_items")
    if not isinstance(entries, list):
        entries = [progress["current"]] if isinstance(progress.get("current"), dict) else []
    normalized = []
    for item in entries if active else []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        row = dict(item)
        try:
            start = float(item.get("started_at") or 0)
        except (TypeError, ValueError):
            start = 0
        if not math.isfinite(start) or start < 0:
            start = 0
        row["started_at"] = start or None
        row["elapsed_seconds"] = max(0, time.time() - start) if start else None
        normalized.append(row)
    return {"running": active, "phase": progress.get("phase") if active else ("等待下一轮" if running else "已停止"),
            "active_items": normalized, "current": normalized[0] if normalized else None,
            "index": progress.get("index", 0) if active else 0, "total": progress.get("total", 0) if active else 0}


def process_backlog(include_items: bool = True) -> dict:
    """全库扫描处理池；缺失项与处理器使用同一完整性契约。"""
    db = DATA / "hx.sqlite3"
    if not db.exists():
        return {"items": [], "remaining_count": 0, "ready_count": 0, "stuck_count": 0}
    max_attempts = _max_process_attempts()
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        has_events = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='processing_events'").fetchone()
        if has_events:
            rows = conn.execute("SELECT i.*,e.created_at AS queue_created_at,e.available_at AS queue_available_at,"
                                "e.generation AS queue_generation FROM items i LEFT JOIN processing_events e ON e.item_id=i.id "
                                "WHERE i.raw IS NOT NULL AND TRIM(i.raw)<>'' ORDER BY e.created_at,i.id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM items WHERE raw IS NOT NULL AND TRIM(raw)<>'' ORDER BY id").fetchall()
        conn.close()
    except Exception as exc:
        print(f"[看板] 处理队列读取失败: {exc}", flush=True)
        return {"items": [], "remaining_count": 0, "ready_count": 0, "stuck_count": 0}

    candidates = [dict(row) for row in rows if processing_required(dict(row))]
    if not include_items:
        return {"items": [], "remaining_count": len(candidates), "ready_count": len(candidates),
                "stuck_count": 0}

    items = []
    ready_count = stuck_count = 0
    progress = active_process_progress()
    active_by_id = {str(item["id"]): item for item in progress["active_items"]}
    now = time.time()
    def queue_order(item):
        current = active_by_id.get(str(item["id"]))
        if current and current.get("generation") is not None and item.get("queue_generation") != current["generation"]:
            current = None
        item["_current_progress"] = current
        return (0 if current else (1 if item.get("queue_created_at") and (item.get("queue_available_at") or 0) <= now else 2),
                item.get("queue_created_at") or "9999", item["id"])
    candidates.sort(key=queue_order)
    for position, item in enumerate(candidates, 1):
        missing = missing_analysis_fields(item)
        if not missing:
            missing.append("流程阶段")
        attempts = int(item.get("analysis_attempts") or 0)
        stuck = False
        ready_count += 1
        items.append({
            "id": item.get("id"), "origin_id": item.get("origin_id"),
            "account": item.get("account") or "未知账号",
            "title": str(item.get("ai_title") or "").strip(),
            "raw": str(item.get("raw") or "")[:240],
            "stage": item.get("stage") or "", "phase": "正在完整分析" if item.get("_current_progress") else "待完整分析",
            "missing": missing, "attempts": attempts, "max_attempts": max_attempts,
            "stuck": stuck, "error": str(item.get("analysis_error") or "")[:240],
            "queue_position": position, "queued": bool(item.get("queue_created_at")),
            "retry_at": item.get("queue_available_at") or 0,
            "processing": bool(item.get("_current_progress")),
            "started_at": (item.get("_current_progress") or {}).get("started_at"),
            "elapsed_seconds": (item.get("_current_progress") or {}).get("elapsed_seconds"),
        })

    return {"items": items, "remaining_count": len(items),
            "ready_count": ready_count, "stuck_count": stuck_count}


def process_progress() -> dict:
    backlog = process_backlog(include_items=False)
    active_progress = active_process_progress()
    return {
        **active_progress,
        "remaining_count": backlog["remaining_count"],
        "ready_count": backlog["ready_count"],
        "stuck_count": backlog["stuck_count"],
    }


def channel_process_running(channel_id: str) -> bool:
    with LOCK:
        process = PROCESSES.get(channel_key(channel_id))
        return bool(process and process.poll() is None)


def state() -> dict:
    """首屏/SSE 用的全量轻量状态：服务 + 渠道 + 统计（不含条目正文）。"""
    try:
        pending_total = queue_pending_count(sources=channel_hub.active_sources())
    except Exception as exc:
        print(f"[看板] 待分发统计失败: {exc}", flush=True)
        pending_total = 0
    total, latest_id, latest_rev = item_stats()
    try:
        channel_list = channel_hub.list_channels()
    except Exception as exc:
        print(f"[看板] 渠道状态读取失败: {exc}", flush=True)
        channel_list = []
    for channel in channel_list:
        if channel.get("adapter") == channel_hub.ADAPTER_PROCESS:
            channel["running"] = channel_process_running(channel["id"])
    try:
        from collect.sources._web.store import snapshot as collection_snapshot
        collection_state = collection_snapshot(STATE / "web_collect.sqlite3", news_path=DATA / "hx.sqlite3")
    except Exception:
        from collect.sources._web.store import source_inventory
        warning = {"source": "采集账本", "url": "", "stage": "state",
                   "error_type": "STATE_READ_ERROR", "detail": "采集账本读取失败", "updated_at": time.time()}
        collection_state = {"sources": [{**row, "state": "error", "last_error": warning,
                                         "collected": None, "stored_count": None, "pending_count": None,
                                         "failed_count": None} for row in source_inventory()],
                            "warnings": [warning], "failed_count": 0}
    return {
        "collection": collection_state,
        "services": services(),
        "process_progress": process_progress(),
        "channels": channel_list,
        "stats": {
            "item_count": total,
            "latest_id": latest_id,
            "latest_rev": latest_rev,
            "pending_count": pending_total,
        },
        "env_ready": (ROOT / ".env").exists(),
        "configurable": list(CONFIG_FIELDS),
        "ws_url": WS_SERVER.url if WS_SERVER is not None else f"ws://127.0.0.1:{WS_PORT}/ws",
        "ts": time.time(),
    }


def summary() -> dict:
    """兼容老调用：老字段拍平在顶层（`/api/snapshot` 与 SSE 用）。"""
    data = state()
    stats = data["stats"]
    return {
        "services": data["services"],
        "process_progress": data["process_progress"],
        "collection": data["collection"],
        "channels": data["channels"],
        "item_count": stats["item_count"],
        "latest_id": stats["latest_id"],
        "latest_rev": stats["latest_rev"],
        "pending_count": stats["pending_count"],
        "env_ready": data["env_ready"],
        "configurable_services": data["configurable"],
        "ws_url": data["ws_url"],
        "ts": data["ts"],
    }


def snapshot(offset: int = 0, limit: int = 20, since_id: int = 0, since_rev: int = 0,
             search: str = "", sort: str = "published") -> dict:
    """since_id/since_rev 任一 > 0 → 只回"新条目 + 被更新过的条目"（实时补丁）；
    否则回第 offset 页（默认 20 条）给首屏与"滚动加载更多"。"""
    data = summary()
    changes = since_id > 0 or since_rev > 0
    data["incremental"] = changes
    data["result_total"] = matching_item_count(search)
    if changes:
        data["items"] = changed_items(since_id, since_rev)
    else:
        data["items"] = all_items(offset, limit, query=search, sort=sort)
        data["offset"] = max(0, int(offset))
        data["limit"] = max(1, int(limit))
        data["has_more"] = max(0, int(offset)) + len(data["items"]) < data["result_total"]
    return data


def pump_service_output(name: str, process: subprocess.Popen, log_stream) -> None:
    """Tee a managed service's combined output to the dashboard terminal and log file."""
    label = label_for(name)
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


def channel_key(channel_id: str) -> str:
    """渠道进程在看板进程表里的键（和被托管的"服务"分开，互不覆盖）。"""
    return f"{CHANNEL_KEY_PREFIX}{channel_id}"


def label_for(key: str) -> str:
    """日志里显示的名字：服务名 → 渠道显示名 → 原样。"""
    if key in SERVICE_NAMES:
        return SERVICE_NAMES[key]
    if key.startswith(CHANNEL_KEY_PREFIX):
        channel_id = key[len(CHANNEL_KEY_PREFIX):]
        info = channel_hub.channel_row(channel_id)
        return f"渠道:{info.get('label') or channel_id}"
    return key


def _spawn(key: str, command: list) -> int:
    """起一个被看板托管的子进程（服务/渠道都走这里），返回 PID。"""
    label = label_for(key)
    with LOCK:
        existing = PROCESSES.get(key)
        if existing and existing.poll() is None:
            print(f"[{label}] 已在运行，PID={existing.pid}", flush=True)
            return existing.pid
        LOGS.mkdir(parents=True, exist_ok=True)
        log_stream = (LOGS / f"dashboard_{key.replace(':', '_')}.log").open("a", encoding="utf-8")
        print(f"[{label}] 启动：{' '.join(command)}", flush=True)
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
        PROCESSES[key] = process
        threading.Thread(
            target=pump_service_output,
            args=(key, process, log_stream),
            name=f"dashboard-output-{key}",
            daemon=True,
        ).start()
        return process.pid


def start_service(name: str) -> int:
    if name not in SERVICE_NAMES:
        raise ValueError("未知服务")
    pid = _spawn(name, [sys.executable, str(ROOT / "dashboard" / "service_runner.py"), name])
    audit("service_start", {"service": name, "pid": pid})
    return pid


def start_channel_process(channel_id: str) -> int:
    """托管渠道（adapter=process）：按 `channels.command` 起进程。"""
    info = channel_hub.channel_row(channel_id)
    if not info:
        raise ValueError("未知渠道")
    raw = str(info.get("command") or "").strip()
    if not raw:
        raise ValueError(f"渠道 {channel_id} 没有配置启动命令")
    try:
        command = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"渠道 {channel_id} 的启动命令不是合法 JSON：{exc}") from exc
    if not isinstance(command, list) or not command:
        raise ValueError(f"渠道 {channel_id} 的启动命令必须是 JSON 数组")
    command = [str(sys.executable) if str(part) == "{python}" else str(part) for part in command]
    pid = _spawn(channel_key(channel_id), command)
    audit("channel_start", {"channel": channel_id, "pid": pid})
    return pid


def _taskkill(pid: int) -> str:
    """Windows：taskkill /F /T 杀整棵进程树，返回说明。"""
    last_error = None
    # 先试着拿到 taskkill 的输出（便于排查）；建不了管道（受限环境）就退化成静默执行。
    for kwargs in (
        {"capture_output": True, "text": True, "errors": "replace"},
        {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
    ):
        try:
            result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], **kwargs)
            if "capture_output" in kwargs:
                output = ((result.stdout or "") + (result.stderr or "")).strip().replace("\n", " ")
                return f"taskkill/T exit={result.returncode} {output}".strip()[:200]
            return f"taskkill/T exit={result.returncode}"
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    return f"taskkill 执行失败: {last_error}"


def _kill_tree(process) -> str:
    """把整棵子进程树杀掉，返回用于日志的说明。

    为什么不能只 process.terminate()：service_runner.py 只是"循环壳"，
    它下面还有 main_flow.py，main_flow 下面还有 analyze_db.py
    这些孙子进程。Windows 上 terminate() 只结束直接子进程（TerminateProcess 不递归），
    于是壳死了、正在跑的那一轮处理照旧跑完 —— 看板显示"已停止"但模型请求还在继续。
    所以这里在 Windows 上用 taskkill /T 杀整棵树；taskkill 被拒时至少退回 terminate()。
    """
    pid = process.pid
    if os.name == "nt":
        detail = _taskkill(pid)
        if "exit=0" in detail:
            return detail
        try:
            process.terminate()          # 退一步：至少把直接子进程结束掉
            return detail + " + terminate()"
        except Exception as exc:         # noqa: BLE001
            return detail + f" + terminate 失败: {exc}"
    try:
        import signal
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return "killpg SIGTERM"
    except Exception as exc:  # noqa: BLE001
        try:
            process.terminate()
            return f"killpg 失败({exc}) + terminate()"
        except Exception as inner:  # noqa: BLE001
            return f"killpg 失败({exc}) terminate 也失败({inner})"


def stop_process(key: str) -> bool:
    """停掉一个被托管的进程（服务/渠道通用）：连同它正在跑的那一轮一起结束。"""
    with LOCK:
        process = PROCESSES.get(key)
        running = bool(process and process.poll() is None)
    if not running or process is None:
        return False

    detail = _kill_tree(process)
    try:                                  # 收尸，避免子进程变僵尸、也让状态立刻变"已停止"
        process.wait(timeout=10)
    except Exception:                     # noqa: BLE001
        try:
            process.kill()
        except Exception:
            pass
    print(f"[{label_for(key)}] 已停止 PID={process.pid}（{detail}）", flush=True)
    audit("process_stop", {"key": key, "pid": process.pid, "detail": detail})
    return True


def stop_service(name: str) -> bool:
    """停止采集/处理服务。"""
    return stop_process(name)


def stop_channel_process(channel_id: str) -> bool:
    """停止托管渠道进程（在线连接由 channel_hub.disconnect_channel 处理）。"""
    return stop_process(channel_key(channel_id))


def toggle_channel(channel_id: str, enabled: bool) -> dict:
    """看板开关一个渠道。

    - `adapter=plugin`（麦麦插件活在麦麦进程里）：只启用/停用推送 —— 库里置位 + 给在线连接发
      `pause`/`resume`，进程仍由麦麦自己管。
    - `adapter=process`（艾莉森这类看板托管进程）：真的启停进程，顺带置位。
    """
    info = channel_hub.channel_row(channel_id)
    if not info:
        raise ValueError(f"未知渠道 {channel_id!r}")
    info = channel_hub.set_enabled(channel_id, enabled)
    adapter = str(info.get("adapter") or channel_hub.ADAPTER_PLUGIN)
    result = {"channel": channel_id, "enabled": enabled, "adapter": adapter}
    if adapter == channel_hub.ADAPTER_PROCESS:
        if enabled:
            result["pid"] = start_channel_process(channel_id)
            result["running"] = True
        else:
            channel_hub.disconnect_channel(channel_id, "看板停止")
            stop_channel_process(channel_id)
            result["running"] = False
    audit("channel_toggle", {"channel": channel_id, "enabled": enabled, "adapter": adapter})
    return result


def update_item(payload: dict) -> None:
    """修改原文需重新分析；人工编辑结果也必须符合完整输出契约。"""
    item_id = int(payload.get('id'))
    allowed = {'zh','raw','decision','invalid_reason','content_type','ori_url'}
    values = {key:payload[key] for key in allowed if key in payload}
    if not values: raise ValueError('没有可修改字段')
    conn = sqlite3.connect(DATA/'hx.sqlite3')
    conn.row_factory = sqlite3.Row
    try:
        current = conn.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone()
        if current is None: raise ValueError('内容不存在')
        changed_raw = 'raw' in values and values['raw'] != current['raw']
        if changed_raw:
            values.update(zh='',quoted_zh='',ai_title='',ai_summary='',importance='',ai_tags='',ai_score=None,repeat_of='',decision=None,invalid_reason='',stage='collected',analysis_attempts=0,analysis_error='',processed_at=None)
        else:
            candidate = dict(current);candidate.update(values)
            if candidate.get('stage') == 'processed' and missing_analysis_fields(candidate):
                raise ValueError('修改后的内容不符合完整分析契约')
            if 'zh' in values: values['analysis_error']=''
        sets=','.join(f'{key}=?' for key in values)
        conn.execute(f'UPDATE items SET {sets},rev=rev+1 WHERE id=?',[*values.values(),item_id])
        if changed_raw:
            from collect.db import enqueue_processing_event
            enqueue_processing_event(conn,item_id)
        conn.commit()
    finally:
        conn.close()
    audit('item_update',{'id':item_id,'fields':sorted(values)})


class Handler(BaseHTTPRequestHandler):
    server_version = "HXDashboard/3.0"

    # ────────────────────────── 响应（统一信封）──────────────────────────
    def send_json(self, code: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def ok(self, data=None, code: int = 200) -> None:
        """成功：{"ok": true, "data": ...}。"""
        self.send_json(code, {"ok": True, "data": data if data is not None else {}})

    def fail(self, error, code: int = 400) -> None:
        """失败：{"ok": false, "error": "人话"}。"""
        self.send_json(code, {"ok": False, "error": str(error)})

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
        """SSE：每秒一个 `event: state`，data 与 `/api/state` 的 data 相同。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for _ in range(120):
            try:
                payload = json.dumps(state(), ensure_ascii=False)
                self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(1)
            except Exception:
                break

    def auth(self) -> bool:
        return self.headers.get("X-HX-Token", "") == token()

    def query_int(self, name: str, default: int = 0) -> int:
        """读 ?name=N（缺失/非法用默认值）。"""
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        try:
            return max(0, int(query.get(name, [str(default)])[0]))
        except (TypeError, ValueError):
            return default

    def query_text(self, name: str, default: str = "") -> str:
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        return str(query.get(name, [default])[0])[:200]

    # ────────────────────────── GET 路由 ──────────────────────────
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/state":
                return self.ok(state())
            if path == "/api/snapshot":
                return self.ok(snapshot(
                    offset=self.query_int("offset", 0),
                    limit=self.query_int("limit", 20) or 20,
                    since_id=self.query_int("since_id", 0),
                    since_rev=self.query_int("since_rev", 0),
                    search=self.query_text("q"),
                    sort=self.query_text("sort", "published"),
                ))
            if path == "/api/items":
                offset, limit = self.query_int("offset", 0), self.query_int("limit", 20) or 20
                search, sort = self.query_text("q"), self.query_text("sort", "published")
                return self.ok({"items": all_items(offset, limit, query=search, sort=sort), "offset": offset,
                                "limit": limit, "total": matching_item_count(search)})
            if path == "/api/process/backlog":
                return self.ok(process_backlog())
            if path == "/api/channels":
                return self.ok({"channels": state()["channels"]})
            if path == "/api/channels/endpoint":
                if not self.auth():
                    return self.fail("invalid token", 401)
                return self.ok({
                    "ws_url": WS_SERVER.url if WS_SERVER is not None else f"ws://127.0.0.1:{WS_PORT}/ws",
                    "host": "127.0.0.1",
                    "port": WS_PORT,
                    "token": token(),
                    "protocol": 1,
                })
            if path == "/api/pushes":
                return self.ok({"pushes": channel_hub.recent_pushes(self.query_int("limit", 20) or 20)})
            if path == "/api/stream":
                return self.send_sse()
            if path.startswith("/api/image/"):
                raw = unquote(self.path.split("/api/image/", 1)[1].split("?", 1)[0])
                image_root = (DATA / "images").resolve()
                target = (image_root / raw.replace("\\", "/")).resolve()
                if image_root not in target.parents or not target.is_file():
                    return self.fail("image not found", 404)
                return self.send_file(target, max_age=604800)
            if path == "/api/config/alison":
                if not self.auth():
                    return self.fail("invalid token", 401)
                return self.ok(alison_config.config())
            if path == "/api/config/ai":
                if not self.auth():
                    return self.fail("invalid token", 401)
                return self.ok(model_endpoints.public_settings(ROOT / ".env"))
            if path.startswith("/api/config/") or path.startswith("/api/service/config/"):
                if not self.auth():
                    return self.fail("invalid token", 401)
                return self.ok(service_config(path.rsplit("/", 1)[-1]))
        except Exception as exc:
            return self.fail(exc)
        # 静态文件（前端）
        if path == "/":
            path = "/index.html"
        target = (WEB / path.lstrip("/")).resolve()
        if WEB.resolve() not in target.parents:
            return self.fail("forbidden", 403)
        if target.exists() and target.is_file():
            return self.send_file(target)
        return self.fail("not found", 404)

    # ────────────────────────── POST 路由 ──────────────────────────
    def do_POST(self) -> None:
        if not self.auth():
            return self.fail("invalid token", 401)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) if length else b"{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/service/toggle":
                name = str(payload.get("service", ""))
                if name not in SERVICE_NAMES:
                    raise ValueError(f"未知服务 {name!r}（分发请用 /api/channels/toggle）")
                enabled = bool(payload.get("enabled"))
                value = start_service(name) if enabled else stop_service(name)
                return self.ok({"service": name, "enabled": enabled, "value": value})
            if path == "/api/channels/toggle":
                return self.ok(toggle_channel(str(payload.get("channel", "")),
                                              bool(payload.get("enabled"))))
            if path in ("/api/config/ai", "/api/config/ai/use"):
                return self.ok(save_ai_config(payload, use=path.endswith("/use")))
            if path == "/api/config/alison":
                result = alison_config.save(payload.get("values", {}))
                audit("alison_config", {"keys": result["keys"]})
                return self.ok(result)
            if path == "/api/config/alison/groups/add":
                group = alison_config.add_group(payload.get("group_openid", ""))
                audit("alison_group_add", {"group_openid": group["group_openid"]})
                return self.ok({"group": group})
            if path == "/api/config/alison/groups/toggle":
                group = alison_config.toggle_group(payload.get("group_openid", ""),
                                                   payload.get("enabled") is True)
                audit("alison_group_toggle", {"group_openid": group["group_openid"],
                                              "enabled": group["push_enabled"]})
                return self.ok({"group": group})
            if path == "/api/config/alison/groups/refresh":
                result = alison_config.start_refresh(missing_only=payload.get("missing_only") is True)
                audit("alison_groups_refresh", {"total": result["total"]})
                return self.ok({"refresh": result})
            if path in ("/api/config", "/api/service/config"):
                name = str(payload.get("service", ""))
                values = payload.get("values", {})
                if not isinstance(values, dict):
                    raise ValueError("配置格式错误")
                save_service_config(name, values)
                audit("service_config", {"service": name, "keys": sorted(values)})
                return self.ok({"service": name})
            if path == "/api/item/update":
                update_item(payload)
                return self.ok({"id": int(payload.get("id"))})
            if path == "/api/item/delete":
                item_id = int(payload.get("id"))
                conn = sqlite3.connect(DATA / "hx.sqlite3")
                conn.execute("DELETE FROM items WHERE id=?", (item_id,))
                conn.commit()
                conn.close()
                audit("item_delete", {"id": item_id})
                return self.ok({"id": item_id})
            if path == "/api/cookies/import":
                save_cookies(str(payload.get("content", "")))
                audit("cookie_import", {"path": str(COOKIE_FILE)})
                return self.ok({"path": str(COOKIE_FILE)})
        except Exception as exc:
            return self.fail(exc)
        return self.fail("not found", 404)

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
    global WS_SERVER, WS_PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--ws-port", type=int, default=WS_PORT,
                        help="渠道 WebSocket 端口（默认 8788，或环境变量 HX_WS_PORT）")
    parser.add_argument("--open-browser", action="store_true",
                        help="HTTP 端口绑定成功后在浏览器打开看板")
    parser.add_argument("--no-auto-process", action="store_true", help="本机验收时不自动启动模型处理")
    parser.add_argument("--no-channel-server", action="store_true", help="本机验收时不启动渠道连接与投递")
    args = parser.parse_args()
    WS_PORT = args.ws_port
    dashboard_token = token()
    print("HX dashboard token:", dashboard_token, flush=True)
    channel_hub.ensure_builtin_channels()
    startup_pending = queue_pending_count(sources=channel_hub.active_sources())
    print(f"[分发] 启动扫描最终输出池：当前启用 source 待发 {startup_pending} 条", flush=True)

    # 渠道 WebSocket：和 HTTP 同一个进程、独立端口；起不来不拖垮看板（只记日志）
    if not args.no_channel_server:
        WS_SERVER = ChannelWSServer("127.0.0.1", WS_PORT, dashboard_token,
                                handle_message=channel_hub.handle_message,
                                on_disconnect=channel_hub.unregister_connection)
    if WS_SERVER is not None and not WS_SERVER.start():
        print(f"[渠道WS] 端口 {WS_PORT} 起不来，渠道将无法连接（看板其余功能可用）", flush=True)
    print(f"渠道请连：ws://127.0.0.1:{WS_PORT}/ws?token=<上面的 token>", flush=True)

    try:
        with QuietHTTPServer((args.host, args.port), Handler) as httpd:
            if args.open_browser:
                def open_page():
                    try:
                        import webbrowser
                        webbrowser.open(f"http://127.0.0.1:{args.port}/")
                    except Exception as exc:
                        print(f"[看板] 自动打开浏览器失败: {exc}", flush=True)

                opener = threading.Timer(0.15, open_page)
                opener.daemon = True
                opener.start()
            print(f"[看板] 服务器终端已就绪：http://127.0.0.1:{args.port}/"
                  "（关闭此终端即停止服务）", flush=True)
            if not args.no_auto_process:
                process_pid = start_service("process")
                print(f"[处理] 看板启动时自动启动处理订阅服务，PID={process_pid}", flush=True)
            httpd.serve_forever()
    finally:
        if WS_SERVER is not None:
            WS_SERVER.stop()


if __name__ == "__main__":
    main()
