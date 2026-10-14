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
import math
from pathlib import Path
from datetime import datetime, timedelta

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
INTERVALS = {"collect": 600, "web": 900}
EVENT_POLL_SECONDS = 0.5
EVENT_BATCH_SIZE = 200


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
    key = "WEB_COLLECT_INTERVAL_SECONDS" if name == "web" else "COLLECT_INTERVAL_SECONDS"
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


def retry_cooldown() -> float:
    from process.model_endpoints import read_env
    try:
        delay = float(read_env(ROOT / '.env').get('TRANSLATE_RATE_LIMIT_WAIT', '20'))
        return max(1.0, delay) if math.isfinite(delay) else 20.0
    except (ValueError, OSError):
        return 20.0


def settle_processing_events(events, returncode: int) -> None:
    """Ack completed items; defer only the failed/incomplete IDs with backoff."""
    if not events:
        return
    item_ids = sorted({int(event["item_id"]) for event in events})
    delay = retry_cooldown()
    conn = _event_connection()
    try:
        placeholders = ",".join("?" for _ in item_ids)
        rows = conn.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders})", item_ids).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        from process.analysis_contract import missing_analysis_fields
        for event in events:
            item_id = int(event["item_id"])
            generation = int(event["generation"])
            item = by_id.get(item_id)
            done = item is None or (
                str(item.get("stage") or "") == "processed"
                and not missing_analysis_fields(item)
            )
            if done:
                conn.execute("DELETE FROM processing_events WHERE item_id=? AND generation=?",
                             (item_id, generation))
                continue
            # 到达本次尝试次数后移到队尾；短暂冷却避免队列只有失败项时空转。
            latest = conn.execute("SELECT MAX(created_at) FROM processing_events").fetchone()[0]
            tail = datetime.now()
            if latest:
                try:
                    tail = max(tail, datetime.fromisoformat(latest) + timedelta(microseconds=1))
                except ValueError:
                    pass
            conn.execute(
                "UPDATE processing_events SET available_at=?,attempts=attempts+1,created_at=? "
                "WHERE item_id=? AND generation=?",
                (time.time() + delay, tail.isoformat(timespec='microseconds'), item_id, generation),
            )
        conn.commit()
    finally:
        conn.close()


def restore_processing_events() -> None:
    """启动时找回旧重试上限造成的漏项，不改变累计次数和原有事件版本。"""
    conn = _event_connection(initialize=True)
    # 恢复旧逻辑已删除事件的失败项，以及离线导入的未完成项。
    from collect.db import ensure_processing_event
    from process.analysis_contract import processing_required
    for row in conn.execute("SELECT * FROM items WHERE raw IS NOT NULL AND TRIM(raw)<>''").fetchall():
        if processing_required(dict(row)):
            ensure_processing_event(conn, row['id'])
    conn.commit()
    conn.close()


def run_processing_subscription() -> None:
    """启动时恢复队列，之后只轮转持久事件，不周期性重扫全库。"""
    restore_processing_events()
    print("[process] 启动时执行一次全库完整性扫描", flush=True)
    startup = read_processing_events(due_only=False, limit=100000)
    if startup:
        result = run_command("process", [event['item_id'] for event in startup])
        settle_processing_events(startup, result)
    while True:
        events = read_processing_events(due_only=True)
        if not events:
            time.sleep(EVENT_POLL_SECONDS)
            continue
        item_ids = list(dict.fromkeys(int(event["item_id"]) for event in events))
        result = run_command("process", item_ids)
        settle_processing_events(events, result)


def collection_groups(names=None):
    """复用源选择配置；默认新来源全部启用，X 仍按用户原配置。"""
    from collect.sources import enabled_sources
    selected = [adapter.name for adapter in enabled_sources(names)]
    groups = {}
    if "x" in selected:
        groups["x"] = ["x"]
    web = [name for name in selected if name != "x"]
    if web:
        groups["web"] = web
    return groups


def next_collect_due(group, started, finished, interval):
    """X 延续结束后等待；web 按开始时间计时，错过的轮次不补跑。"""
    if group == "x":
        return finished + interval
    return started + (int(max(0, finished - started) // interval) + 1) * interval


def run_collection_loops():
    groups = collection_groups()
    if not groups:
        print("[collect] 没有启用的采集来源", flush=True)
        return
    due = {group: time.monotonic() for group in groups}
    running = {}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    try:
        while True:
            now = time.monotonic()
            for group, names in groups.items():
                if group in running:
                    process, started = running[group]
                    code = process.poll()
                    if code is None:
                        continue
                    print(f"[collect:{group}] 本轮结束，退出码 {code}", flush=True)
                    interval = configured_interval("web" if group == "web" else "collect")
                    due[group] = next_collect_due(group, started, now, interval)
                    del running[group]
                if now >= due[group]:
                    command = list(COMMANDS["collect"]) + ["--sources", ",".join(names)]
                    print(f"[collect:{group}] 开始一轮 {','.join(names)}", flush=True)
                    try:
                        running[group] = (subprocess.Popen(command, cwd=ROOT, env=env), now)
                    except OSError as exc:
                        print(f"[collect:{group}] 启动失败 {type(exc).__name__}", flush=True)
                        due[group] = now + 60
            time.sleep(0.25)
    finally:
        for process, _ in running.values():
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


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
    run_collection_loops()


if __name__ == "__main__":
    main()
