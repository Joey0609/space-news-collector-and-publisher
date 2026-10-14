# -*- coding: utf-8 -*-
"""看板托管的采集循环与处理增量订阅。
=====================================================
分发**不再是一个服务**：它是若干"渠道"（麦麦插件 / 艾莉森 / NapCat），各自连看板的
渠道 WebSocket，由 `dashboard/channels.py` 调度、看板"分发"页启停。见 dashboard/PROTOCOL.md。

处理服务启动时全库扫描一次，之后只消费 `processing_events` 里的新增/变更条目 ID。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
DB_FILE = ROOT / "data" / "hx.sqlite3"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if not PROJECT_PYTHON.is_file():
    raise RuntimeError(f"项目虚拟环境不存在: {PROJECT_PYTHON}；请先创建并安装依赖")
COMMANDS = {
    "collect": [str(PROJECT_PYTHON), str(ROOT / "main_flow.py"), "--collect-only"],
    "process": [str(PROJECT_PYTHON), str(ROOT / "main_flow.py"), "--process-only"],
}
INTERVALS = {"collect": 600}
EVENT_POLL_SECONDS = 0.5
EVENT_BATCH_SIZE = 200
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600


def acquire_instance_lock(name: str):
    """Prevent two dashboard instances from running the same database loop concurrently."""
    lock_dir = ROOT / "data" / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"service_{name}.lock").open("a+b")
    try:
        if os.name == "nt":
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def configured_interval(name: str) -> int:
    key = "COLLECT_INTERVAL_SECONDS"
    try:
        values = {}
        env_file = ROOT / ".env"
        for line in env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []:
            field, separator, value = line.partition("=")
            if separator:
                values[field.strip()] = value.strip()
        return max(10, int(values.get(key, INTERVALS[name])))
    except (ValueError, OSError):
        return INTERVALS[name]


def run_command(name: str, item_ids=None) -> int:
    command = list(COMMANDS[name])
    if name == "process" and item_ids is not None:
        command.extend(("--item-ids", ",".join(str(int(value)) for value in item_ids)))
    if name == "process":
        label = f"订阅增量 {len(item_ids)} 条" if item_ids is not None else "全库启动扫描"
    else:
        label = "采集一轮"
    print(f"[{name}] 开始一轮（{label}）", flush=True)
    # 看板按 UTF-8 读本进程输出；本进程也要让子进程按 UTF-8 写、并且不缓冲。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    result = subprocess.run(command, cwd=ROOT, env=env)
    print(f"[{name}] 本轮结束，退出码 {result.returncode}", flush=True)
    return result.returncode


def _event_connection(initialize=False):
    """Open the inbox DB, initializing the shared schema only when needed."""
    conn = sqlite3.connect(str(DB_FILE), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processing_events'").fetchone()
    if initialize or not exists:
        from collect.db import ensure_schema
        ensure_schema(conn)
    return conn


def read_processing_events(due_only=True, limit=EVENT_BATCH_SIZE):
    conn = _event_connection()
    try:
        sql = "SELECT item_id,generation,attempts FROM processing_events"
        args = []
        if due_only:
            sql += " WHERE available_at<=?"
            args.append(time.time())
        sql += " ORDER BY created_at,item_id LIMIT ?"
        args.append(max(1, int(limit)))
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _max_attempts() -> int:
    try:
        values = {}
        env_file = ROOT / ".env"
        for line in env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []:
            field, separator, value = line.partition("=")
            if separator:
                values[field.strip()] = value.strip()
        return max(1, int(values.get("TRANSLATE_MAX_ATTEMPTS", "3")))
    except (ValueError, OSError):
        return 3


def settle_processing_events(events, returncode: int) -> None:
    """Ack completed items; defer only the failed/incomplete IDs with backoff."""
    if not events:
        return
    item_ids = sorted({int(event["item_id"]) for event in events})
    conn = _event_connection()
    try:
        placeholders = ",".join("?" for _ in item_ids)
        rows = conn.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders})", item_ids).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        from process.analysis_contract import missing_analysis_fields
        max_attempts = _max_attempts()
        for event in events:
            item_id = int(event["item_id"])
            generation = int(event["generation"])
            item = by_id.get(item_id)
            done = item is None or (
                str(item.get("stage") or "") == "processed"
                and not missing_analysis_fields(item)
            )
            stuck = bool(item and missing_analysis_fields(item)
                         and int(item.get("translate_attempts") or 0) >= max_attempts)
            if returncode == 0 and (done or stuck):
                conn.execute("DELETE FROM processing_events WHERE item_id=? AND generation=?",
                             (item_id, generation))
                continue
            delay = min(RETRY_BASE_SECONDS * (2 ** min(int(event.get("attempts") or 0), 6)),
                        RETRY_MAX_SECONDS)
            if returncode != 0:
                delay = min(delay, RETRY_BASE_SECONDS)
            conn.execute(
                "UPDATE processing_events SET available_at=?,attempts=attempts+1 "
                "WHERE item_id=? AND generation=?",
                (time.time() + delay, item_id, generation),
            )
        conn.commit()
    finally:
        conn.close()


def run_processing_subscription() -> None:
    """Run one startup repair scan, then consume only queued input revisions."""
    conn = _event_connection(initialize=True)
    conn.close()
    print("[process] 启动时执行一次全库完整性扫描", flush=True)
    result = run_command("process")
    settle_processing_events(read_processing_events(due_only=False, limit=100000), result)
    while True:
        events = read_processing_events(due_only=True)
        if not events:
            time.sleep(EVENT_POLL_SECONDS)
            continue
        item_ids = sorted({int(event["item_id"]) for event in events})
        result = run_command("process", item_ids)
        settle_processing_events(events, result)


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in {"collect", "process"}:
        raise SystemExit("usage: service_runner.py collect|process"
                         "（分发已改为渠道，见 dashboard/PROTOCOL.md）")
    lock = acquire_instance_lock(name)
    if lock is None:
        print(f"[{name}] 已有同类服务在运行，本实例退出以避免重复处理", flush=True)
        return
    if name == "process":
        run_processing_subscription()
        return
    while True:
        run_command(name)
        time.sleep(configured_interval(name))


if __name__ == "__main__":
    main()
