# -*- coding: utf-8 -*-
"""分发队列（SQLite）：待推送不再是 JSON 文件，而是 items 表里的一个状态字段。
================================================================================
一条内容"该不该发给某个通道"只判断一件事：`items.pushed` 里那个 source 是不是 true。

    items.stage = 'processed' AND decision='发' ← 完整处理结束且最终判定要发
    items.pushed   = {"maibot:private": true,
                      "maibot:group_123": false,
                      "alison:group_456": true}

约定
----
- **source 命名**：`通道:目标`。maibot 是"每个目标一个 source"（private / group_<群号>），
  所以私聊先发过也不会把群的那一份吃掉（旧 `sent_to` 的语义原样保留）。
- **新 source**（新群、新通道）第一次注册时回填到已有的行上：
  已经"旧 source 全都发过"的行 → 新 source 记 true（不把历史全量灌给新通道）；
  还在途中的行、以及注册之后新建的行 → 记 false（新通道照样能取到）。
- 发送成功后由分发端调用 `mark_pushed()` 置 true，同时往 `pushes` 表写一条幂等审计。
- 分发只读写新结构 SQLite，不读取旧 JSON 队列。

任何入口先 `ensure_schema()`（collect/db.py）或本模块的 `_ensure()`，缺列会自动补。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB_FILE = REPO / "data" / "hx.sqlite3"
PASSED_SQL = "TRIM(COALESCE(decision, '')) = '发' AND COALESCE(stage, '') = 'processed'"
PENDING_MAX_AGE_SECONDS = 5 * 24 * 60 * 60


def _pending_cutoff_ts() -> int:
    """统一按发布时间过滤：只允许最近 5 天的内容进入任一渠道队列。"""
    return int(time.time()) - PENDING_MAX_AGE_SECONDS


# ────────────────────────── source 命名 ──────────────────────────

def source_of(channel: str, kind: str) -> str:
    """通道 + 目标 → source 名（例：maibot + group_123 → "maibot:group_123"）。"""
    return f"{str(channel or '').strip()}:{str(kind or '').strip()}"


def split_source(source: str) -> tuple:
    """source 名 → (通道, 目标)；没有冒号时通道为空。"""
    text = str(source or "")
    channel, sep, kind = text.partition(":")
    return (channel, kind if sep else "")


# ────────────────────────── 连接与建表 ──────────────────────────

def connect(db_file=None) -> sqlite3.Connection:
    """连接唯一的新结构数据库（WAL + busy timeout）。"""
    path = Path(db_file or DB_FILE)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    from collect.db import ensure_schema
    ensure_schema(conn)
    return conn


def record_attempt(item_id: int, source: str, status: str, detail: str = "", db_file=None) -> None:
    """保留每次分发结果；失败不能写入 pushes 或 items.pushed。"""
    channel, kind = split_source(source)
    conn = connect(db_file)
    try:
        conn.execute("INSERT INTO push_attempts(item_id,channel,kind,source,attempted_at,status,detail)"
                     " VALUES(?,?,?,?,?,?,?)",
                     (int(item_id), channel, kind, source, _now(), status, str(detail or "")[:300]))
        conn.commit()
    finally:
        conn.close()


def reconcile_deliveries(source: str, db_file=None) -> set[int]:
    """QQ 已确认送达但记账失败时，只补记数据库，绝不再次调用发送接口。"""
    conn = connect(db_file)
    try:
        ids = {int(row[0]) for row in conn.execute(
            "SELECT item_id FROM push_attempts WHERE source=? AND status='sent_unrecorded'",
            (source,))}
    finally:
        conn.close()
    blocked = set()
    for item_id in ids:
        try:
            mark_pushed([item_id], source, db_file=db_file)
            conn = connect(db_file)
            try:
                conn.execute("UPDATE push_attempts SET status='success',detail='已补记发送成功'"
                             " WHERE source=? AND item_id=? AND status='sent_unrecorded'",
                             (source, item_id))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            blocked.add(item_id)
    return blocked


# ────────────────────────── 小工具 ──────────────────────────

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_pushed(value) -> dict:
    """把 pushed 列解析成 {source: bool}；坏数据当空字典。"""
    if isinstance(value, dict):
        return {str(k): bool(v) for k, v in value.items()}
    try:
        data = json.loads(value) if value else {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): bool(v) for k, v in data.items()}


def _parse_images(value) -> list:
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        data = json.loads(value) if value else []
    except Exception:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _row_to_item(row) -> dict:
    """数据库行 → 分发端用的条目字典（和旧队列 JSON 的字段尽量一致）。"""
    item = dict(row)
    item["pushed"] = _parse_pushed(item.get("pushed"))
    item["images"] = _parse_images(item.get("images"))
    score = item.get("ai_score")
    item["score"] = score if score is not None else 0
    return item


def _meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else default


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _registry(conn) -> list:
    """已知的全部 source（新 source 注册后进这里，便于看板/日志展示）。"""
    try:
        data = json.loads(_meta_get(conn, "push_sources", "[]") or "[]")
    except Exception:
        data = []
    return [str(x) for x in data] if isinstance(data, list) else []


def _set_registry(conn, sources: list) -> None:
    _meta_set(conn, "push_sources", json.dumps(list(sources), ensure_ascii=False))


# ────────────────────────── 注册新 source ──────────────────────────

def register_sources(sources, db_file=None) -> list:
    """注册分发端自己的 source（每次启动/每轮调用都行，重复调用无副作用）。

    返回本次**新增**的 source 列表。新增时按下面规则回填已有行：
      · 该行旧 source 全都 true → 新 source 记 true（这行早就分发完了，不灌历史）
      · 该行还有旧 source 没发 → 新 source 记 false（新通道也能取）
      · 该行 pushed 还是空的（新条目 / 谁都没发过）→ 新 source 记 false
    （键一律显式写进去，看板上"这条发给了哪些通道"是自解释的。）
    """
    wanted = [str(s).strip() for s in (sources or []) if str(s).strip()]
    if not wanted:
        return []
    conn = connect(db_file)
    try:
        known = _registry(conn)
        new = [s for s in wanted if s not in known]
        if not new:
            return []
        full = known + new
        for row in conn.execute("SELECT id, pushed FROM items").fetchall():
            pushed = _parse_pushed(row["pushed"])
            done = bool(pushed) and all(pushed.get(s) is True for s in known)
            for source in new:
                pushed[source] = bool(done)
            conn.execute("UPDATE items SET pushed=? WHERE id=?",
                         (json.dumps(pushed, ensure_ascii=False), row["id"]))
        _set_registry(conn, full)
        conn.commit()
        return new
    finally:
        conn.close()


def registry(db_file=None) -> list:
    """已知的全部 source。"""
    conn = connect(db_file)
    try:
        return _registry(conn)
    finally:
        conn.close()


# ────────────────────────── 读待推送 ──────────────────────────

def pending(source: str = "", db_file=None) -> list:
    """最近 5 天内且 `stage='processed' AND decision='发'` 的条目，按入库顺序。

    给了 source 就只回"这个 source 还没发过"的（pushed 里不是 true 的都算没发）。
    发布时间缺失或超过 5 天的条目不进入任何渠道的待发队列。
    """
    conn = connect(db_file)
    try:
        rows = conn.execute(f"SELECT * FROM items WHERE {PASSED_SQL} "
                            "AND CAST(ts AS INTEGER) >= ? ORDER BY id ASC",
                            (_pending_cutoff_ts(),)).fetchall()
        items = [_row_to_item(row) for row in rows]
    finally:
        conn.close()
    if source:
        items = [it for it in items if it["pushed"].get(source) is not True]
    return items


def pending_for_sources(sources, db_file=None) -> list:
    """至少还有一个 source 没发过的条目（旧"队列非空"的判断）。"""
    wanted = [str(s) for s in (sources or []) if str(s)]
    if not wanted:
        return []
    return [it for it in pending(db_file=db_file)
            if any(it["pushed"].get(s) is not True for s in wanted)]


def is_pushed(item: dict, source: str) -> bool:
    """这条目对这个 source 发过没有。"""
    return _parse_pushed((item or {}).get("pushed")).get(source) is True


def pending_count(db_file=None, sources=None) -> int:
    """待分发条目数（去重）：至少一个*当前启用* source 尚未发出的条目。

    `sources=None` 保持原口径（全部已注册 source）；给空列表时返回 0。
    """
    conn = connect(db_file)
    try:
        known = _registry(conn) if sources is None else [str(s) for s in sources if str(s)]
        rows = conn.execute(f"SELECT pushed FROM items WHERE {PASSED_SQL} "
                            "AND CAST(ts AS INTEGER) >= ?",
                            (_pending_cutoff_ts(),)).fetchall()
    finally:
        conn.close()
    if not known:
        return len(rows) if sources is None else 0
    return sum(1 for row in rows
               if any(_parse_pushed(row["pushed"]).get(s) is not True for s in known))


def describe(db_file=None) -> str:
    """日志用：每个已知 source 各还有多少条待发。"""
    conn = connect(db_file)
    try:
        known = _registry(conn)
    finally:
        conn.close()
    if not known:
        count = pending_count(db_file)
        return f"未注册 source；decision=发 共 {count} 条"
    items = pending(db_file=db_file)
    parts = []
    for source in known:
        left = sum(1 for it in items if it["pushed"].get(source) is not True)
        parts.append(f"{source} 待发 {left}")
    return " · ".join(parts)


# ────────────────────────── 标记已发 ──────────────────────────

def mark_pushed(item_ids, source: str, db_file=None) -> int:
    """把若干条目在这个 source 上置 true，并写 pushes 审计（幂等）。返回实际改动条数。

    分发端**发送成功后**才调用；失败千万别调，否则这条就永远不会再发了。
    """
    ids = []
    for value in (item_ids if isinstance(item_ids, (list, tuple, set)) else [item_ids]):
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    source = str(source or "").strip()
    if not ids or not source:
        return 0
    channel, kind = split_source(source)
    conn = connect(db_file)
    try:
        if source not in _registry(conn):          # 兜底：没注册过也补上，状态别丢
            _set_registry(conn, _registry(conn) + [source])
        changed = 0
        now = _now()
        for item_id in ids:
            row = conn.execute("SELECT pushed FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                continue
            pushed = _parse_pushed(row["pushed"])
            if pushed.get(source) is not True:
                pushed[source] = True
                conn.execute("UPDATE items SET pushed=?, rev=COALESCE(rev, 0) + 1 WHERE id=?",
                             (json.dumps(pushed, ensure_ascii=False), item_id))
                changed += 1
            conn.execute("INSERT OR IGNORE INTO pushes(item_id,channel,kind,source,sent_at)"
                         " VALUES(?,?,?,?,?)", (item_id, channel, kind, source, now))
            if conn.execute("SELECT 1 FROM pushes WHERE source=? AND item_id=?",
                            (source, item_id)).fetchone() is None:
                raise RuntimeError(f"已发审计未落库 item={item_id} source={source}")
        conn.commit()
        return changed
    finally:
        conn.close()
