"""独立采集账本：失败不写 items；状态与正文成功分开。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from collect.paths import STATE_DIR
from .catalog import RSS_FEEDS, SNAPI_URL, SINA_URL

STATE_DB = STATE_DIR / "web_collect.sqlite3"


class Store:
    def __init__(self, path=None):
        self.path = Path(path or STATE_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS candidates(
                    source TEXT, identity TEXT, url TEXT, payload TEXT,
                    status TEXT DEFAULT 'pending', item_id INTEGER,
                    attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
                    error_type TEXT DEFAULT '', detail TEXT DEFAULT '',
                    PRIMARY KEY(source,identity));
                CREATE INDEX IF NOT EXISTS candidate_url_idx ON candidates(source,url);
                CREATE INDEX IF NOT EXISTS candidate_due_idx ON candidates(source,status,next_try);
                CREATE INDEX IF NOT EXISTS candidate_status_idx ON candidates(status);
                CREATE TABLE IF NOT EXISTS listing(source TEXT PRIMARY KEY,cursor TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS notices(
                    source TEXT, url TEXT, stage TEXT, error_type TEXT, detail TEXT,
                    updated_at REAL, PRIMARY KEY(source,url,stage));
                CREATE TABLE IF NOT EXISTS source_status(
                    source TEXT PRIMARY KEY,label TEXT,last_run REAL,state TEXT,
                    discovered INTEGER DEFAULT 0,collected INTEGER DEFAULT 0);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(source_status)")}
            for name in ("run_pid", "started_at"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE source_status ADD COLUMN {name} REAL DEFAULT 0")
            conn.commit()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def add(self, source, candidate):
        # ID 与 URL 都限于 source；其他来源同一 URL 不查、不合并。
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT identity FROM candidates WHERE source=? AND (identity=? OR url=?) LIMIT 1",
                               (source, candidate["identity"], candidate["url"])).fetchone()
            if row:
                return False
            conn.execute("INSERT INTO candidates(source,identity,url,payload) VALUES(?,?,?,?)",
                         (source, candidate["identity"], candidate["url"], json.dumps(candidate, ensure_ascii=False)))
            conn.execute("UPDATE source_status SET discovered=discovered+1,last_run=? WHERE source=? AND state='running'",
                         (time.time(), source))
            conn.commit()
        return True

    def due(self, source):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM candidates WHERE source=? AND status IN ('pending','failed') AND next_try<=? ORDER BY rowid",
                (source, time.time()))]

    def exclude(self, source, identity):
        with closing(self.connect()) as conn:
            conn.execute("UPDATE candidates SET status='filtered',error_type='',detail='' WHERE source=? AND identity=?",
                         (source, identity))
            conn.execute("DELETE FROM notices WHERE source=? AND url=(SELECT url FROM candidates WHERE source=? AND identity=?)",
                         (source, source, identity))
            conn.commit()

    def success(self, source, identity, item_id):
        with closing(self.connect()) as conn:
            previous = conn.execute("SELECT status FROM candidates WHERE source=? AND identity=?", (source, identity)).fetchone()
            conn.execute("UPDATE candidates SET status='success',item_id=?,error_type='',detail='' WHERE source=? AND identity=?",
                         (item_id, source, identity))
            conn.execute("DELETE FROM notices WHERE source=? AND url=(SELECT url FROM candidates WHERE source=? AND identity=?) AND stage='fulltext'",
                         (source, source, identity))
            if previous and previous[0] != "success":
                conn.execute("UPDATE source_status SET collected=collected+1,last_run=? WHERE source=? AND state='running'",
                             (time.time(), source))
            conn.commit()

    def failure(self, source, identity, error_type, detail):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT attempts FROM candidates WHERE source=? AND identity=?", (source, identity)).fetchone()
            attempts = int(row[0]) + 1
            delay = min(900 * 2 ** min(attempts - 1, 5), 21600)
            conn.execute("UPDATE candidates SET status='failed',attempts=?,next_try=?,error_type=?,detail=? WHERE source=? AND identity=?",
                         (attempts, time.time() + delay, error_type, detail, source, identity))
            conn.commit()

    def notice(self, source, url, stage, error_type, detail):
        with closing(self.connect()) as conn:
            conn.execute("INSERT OR REPLACE INTO notices VALUES(?,?,?,?,?,?)",
                         (source, url, stage, error_type, detail, time.time()))
            conn.commit()

    def clear_notice(self, source, url, stage):
        with closing(self.connect()) as conn:
            conn.execute("DELETE FROM notices WHERE source=? AND url=? AND stage=?", (source, url, stage))
            conn.commit()

    def cursor(self, source, value=None):
        with closing(self.connect()) as conn:
            if value is not None:
                conn.execute("INSERT OR REPLACE INTO listing VALUES(?,?)", (source, value))
                conn.commit()
                return value
            row = conn.execute("SELECT cursor FROM listing WHERE source=?", (source,)).fetchone()
            return row[0] if row else ""

    def status(self, source, label, state, discovered, collected):
        with closing(self.connect()) as conn:
            old = conn.execute("SELECT * FROM source_status WHERE source=?", (source,)).fetchone()
            now = time.time()
            conn.execute("""INSERT OR REPLACE INTO source_status
                         (source,label,last_run,state,discovered,collected,run_pid,started_at) VALUES(?,?,?,?,?,?,?,?)""",
                         (source, label, now, state,
                          discovered if discovered is not None else (old["discovered"] if old else 0),
                          collected if collected is not None else (old["collected"] if old else 0),
                          os.getpid() if state == "running" else 0,
                          now if state == "running" else (old["started_at"] if old else 0)))
            conn.commit()


def source_inventory():
    return [{"source": "rss:" + key, "label": label, "type": "RSS", "url": url}
            for key, label, url in RSS_FEEDS] + [
                {"source": "snapi", "label": "Spaceflight News API", "type": "API", "url": SNAPI_URL},
                {"source": "sina", "label": "新浪科技", "type": "列表", "url": SINA_URL}]


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == 5  # 无查询权限时不误判已停止。
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def snapshot(path=None, news_path=None):
    # 看板只读；没有运行过采集时不创建账本。
    target = Path(path or STATE_DB)
    states, counts, notices = {}, {}, []
    if target.exists():
        with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            states = {row["source"]: dict(row) for row in conn.execute("SELECT * FROM source_status")}
            for row in conn.execute("SELECT source,status,COUNT(*) AS n FROM candidates GROUP BY source,status"):
                counts.setdefault(row["source"], {})[row["status"]] = row["n"]
            notices = [dict(row) for row in conn.execute("SELECT * FROM notices ORDER BY updated_at DESC")]
    known = {row["source"] for row in source_inventory()}
    counts = {source: stats for source, stats in counts.items() if source in known}
    notices = [notice for notice in notices if notice["source"] in known]
    stored = None
    if news_path is not None and Path(news_path).exists():
        with closing(sqlite3.connect(Path(news_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            stored = dict(conn.execute("SELECT source,COUNT(*) FROM items GROUP BY source"))
    rows, alive = [], {}
    inventory = source_inventory()
    for entry in inventory:
        source = entry["source"]
        row = {**entry, "last_run": 0, "state": "never", "discovered": 0, "collected": 0, **states.get(source, {})}
        pid = row.get("run_pid", 0)
        if row["state"] == "running":
            if pid not in alive:
                alive[pid] = process_alive(pid)
            if not alive[pid]:
                row["state"] = "interrupted"
        stats = counts.get(source, {})
        row.update(total_collected=stats.get("success", 0), pending_count=stats.get("pending", 0),
                   failed_count=stats.get("failed", 0),
                   stored_count=stored.get(source, 0) if stored is not None else stats.get("success", 0))
        warnings = [notice for notice in notices if notice["source"] == source]
        if source not in states and (stats or row["stored_count"] or warnings):
            row["state"] = "unrecorded"
        row["warning_count"] = len(warnings)
        row["last_error"] = warnings[0] if warnings else None
        rows.append(row)
    return {"sources": rows, "warnings": notices[:50],
            "failed_count": sum(stats.get("failed", 0) for stats in counts.values())}
