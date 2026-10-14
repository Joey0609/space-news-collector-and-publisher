# -*- coding: utf-8 -*-
"""groups.py · 艾莉森已知群目录与逐群推送开关。

QQ 群 Bot API 提供"按 group_openid 查群信息"，但没有列出所有历史入群群聊的 API。
所以群目录由两部分维护：
1. BotPy 收到 GROUP_ADD_ROBOT / GROUP_DEL_ROBOT、或在群里收到消息时写入群 OpenID。
2. Dashboard 对已知 OpenID 调官方 `/v2/groups/{group_openid}/info` 刷新群名/简介/分类/标签/人数。

首次升级时把旧 `state.json` 里唯一记住的 group_openid 补进目录；其余早已加入但没有被旧网关记过的群，
需要新收到入群事件，或在 Dashboard 手动补录 OpenID（平台没有官方"列出全部群"接口）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .paths import HX_ROOT

DB_FILE = HX_ROOT / "data" / "hx.sqlite3"


def connect(db_file=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file or DB_FILE), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alison_groups("
        "group_openid TEXT PRIMARY KEY, group_name TEXT DEFAULT '',"
        "group_finger_memo TEXT DEFAULT '', group_class_text TEXT DEFAULT '',"
        "group_tags TEXT DEFAULT '[]', group_member_num INTEGER, joined_at TEXT DEFAULT '',"
        "is_member INTEGER DEFAULT 1, push_enabled INTEGER DEFAULT 0,"
        "last_checked TEXT DEFAULT '', last_error TEXT DEFAULT '', updated_at TEXT DEFAULT '')"
    )
    conn.commit()
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _tags_json(value) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return json.dumps([str(x) for x in parsed], ensure_ascii=False)
        except Exception:
            pass
    if isinstance(value, list):
        return json.dumps([str(x) for x in value], ensure_ascii=False)
    return "[]"


def upsert_group(group_openid: str, info: dict | None = None, *, joined_at: str = "",
                 is_member: bool = True, db_file=None) -> dict:
    """发现或刷新一个群。保留已有 push_enabled（首次插入默认关闭）。"""
    goid = str(group_openid or "").strip()
    if not goid:
        raise ValueError("group_openid 不能为空")
    info = info if isinstance(info, dict) else {}
    conn = connect(db_file)
    try:
        now = _now()
        conn.execute(
            "INSERT INTO alison_groups(group_openid,group_name,group_finger_memo,group_class_text,"
            "group_tags,group_member_num,joined_at,is_member,push_enabled,last_checked,last_error,updated_at)"
            " VALUES(?,?,?,?,?,?,?, ?,0,?,?,?)"
            " ON CONFLICT(group_openid) DO UPDATE SET"
            " group_name=CASE WHEN excluded.group_name<>'' THEN excluded.group_name ELSE alison_groups.group_name END,"
            " group_finger_memo=CASE WHEN excluded.group_finger_memo<>'' THEN excluded.group_finger_memo ELSE alison_groups.group_finger_memo END,"
            " group_class_text=CASE WHEN excluded.group_class_text<>'' THEN excluded.group_class_text ELSE alison_groups.group_class_text END,"
            " group_tags=CASE WHEN excluded.group_tags<>'[]' THEN excluded.group_tags ELSE alison_groups.group_tags END,"
            " group_member_num=COALESCE(excluded.group_member_num,alison_groups.group_member_num),"
            " joined_at=CASE WHEN excluded.joined_at<>'' THEN excluded.joined_at ELSE alison_groups.joined_at END,"
            " is_member=excluded.is_member,"
            " last_checked=CASE WHEN excluded.last_checked<>'' THEN excluded.last_checked ELSE alison_groups.last_checked END,"
            " last_error=CASE WHEN excluded.last_checked<>'' THEN excluded.last_error ELSE alison_groups.last_error END,"
            " updated_at=excluded.updated_at",
            (goid, str(info.get("group_name") or "").strip(),
             str(info.get("group_finger_memo") or "").strip(),
             str(info.get("group_class_text") or "").strip(),
             _tags_json(info.get("group_tags")),
             _as_int(info.get("group_member_num")), str(joined_at or ""),
             1 if is_member else 0, now if info else "", str(info.get("last_error") or ""), now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_group(goid, db_file=db_file)


def _as_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_refresh_error(group_openid: str, error: str, db_file=None) -> None:
    """刷新失败只标这个群的状态，不改变用户设置的推送开关。"""
    conn = connect(db_file)
    try:
        conn.execute("UPDATE alison_groups SET last_checked=?,last_error=?,updated_at=?"
                     " WHERE group_openid=?",
                     (_now(), str(error or "")[:240], _now(), str(group_openid or "").strip()))
        conn.commit()
    finally:
        conn.close()


def mark_group_removed(group_openid: str, db_file=None) -> None:
    goid = str(group_openid or "").strip()
    if not goid:
        return
    conn = connect(db_file)
    try:
        conn.execute("INSERT OR IGNORE INTO alison_groups(group_openid,is_member,push_enabled,updated_at)"
                     " VALUES(?,0,0,?)", (goid, _now()))
        conn.execute("UPDATE alison_groups SET is_member=0,push_enabled=0,updated_at=? WHERE group_openid=?",
                     (_now(), goid))
        conn.commit()
    finally:
        conn.close()


def set_push_enabled(group_openid: str, enabled: bool, db_file=None) -> dict:
    conn = connect(db_file)
    try:
        cur = conn.execute("UPDATE alison_groups SET push_enabled=?,updated_at=? WHERE group_openid=?",
                           (1 if enabled else 0, _now(), str(group_openid or "").strip()))
        if cur.rowcount == 0:
            raise ValueError("群目录里找不到这个 OpenID")
        conn.commit()
    finally:
        conn.close()
    return get_group(group_openid, db_file=db_file)


def get_group(group_openid: str, db_file=None) -> dict:
    conn = connect(db_file)
    try:
        row = conn.execute("SELECT * FROM alison_groups WHERE group_openid=?",
                           (str(group_openid or "").strip(),)).fetchone()
        return _row(row) if row else {}
    finally:
        conn.close()


def _row(row) -> dict:
    if row is None:
        return {}
    try:
        tags = json.loads(row["group_tags"] or "[]")
    except Exception:
        tags = []
    return {
        "group_openid": str(row["group_openid"] or ""),
        "group_name": str(row["group_name"] or ""),
        "group_finger_memo": str(row["group_finger_memo"] or ""),
        "group_class_text": str(row["group_class_text"] or ""),
        "group_tags": tags if isinstance(tags, list) else [],
        "group_member_num": row["group_member_num"],
        "joined_at": str(row["joined_at"] or ""),
        "is_member": bool(row["is_member"]),
        "push_enabled": bool(row["push_enabled"]),
        "last_checked": str(row["last_checked"] or ""),
        "last_error": str(row["last_error"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def list_groups(*, members_only: bool = False, enabled_only: bool = False, db_file=None) -> list:
    conn = connect(db_file)
    try:
        where = []
        if members_only:
            where.append("is_member=1")
        if enabled_only:
            where.append("push_enabled=1")
            where.append("is_member=1")
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = conn.execute("SELECT * FROM alison_groups" + clause +
                            " ORDER BY is_member DESC,push_enabled DESC,group_name,group_openid").fetchall()
        return [_row(row) for row in rows]
    finally:
        conn.close()
