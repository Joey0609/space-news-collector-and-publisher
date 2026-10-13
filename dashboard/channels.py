# -*- coding: utf-8 -*-
"""channels.py · 渠道注册表 + 逐条推送调度（看板侧的"分发中心"）
==================================================================
渠道 = 一个连上来的客户端（麦麦插件、艾莉森进程、以后的网页/API 通道），协议见
`dashboard/PROTOCOL.md`。这个模块负责：

1. **注册**：收到 `hello` → `push_db.register_sources()`（新 source 自动回填历史行）
   → 渠道写进 `channels` 表（id / label / adapter / targets / last_seen）。
2. **推送**：每个连接一个推送线程，按 target（= source）轮流取
   `pending(source=...)`，**一条一条**发 `item`，等回执。
3. **回执**：`ok` → `push_db.mark_pushed()` 并回 `ack`；`defer`/失败/超时 → 该 source
   退避一段时间，条目保持未发（下次接着推）。
4. **控制**：看板 `enabled` 开关 → 给渠道发 `pause` / `resume`；`process` 类渠道由
   看板启停进程（command 存在 `channels.command`）。

线程模型：WS 服务端每个连接一个读线程（`dashboard/ws.py`），本模块每个渠道一个推送线程，
回执通过 `queue.Queue` 从读线程递给推送线程。所有共享状态用 `LOCK` 保护。
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path

from distribute import push_db

ROOT = Path(__file__).resolve().parents[1]
DB_FILE = ROOT / "data" / "hx.sqlite3"

ACK_TIMEOUT = 120.0        # 等一条回执的最长时间；超时按"稍后再来"处理
IDLE_SLEEP = 5.0           # 没活干时的轮询间隔
DEFER_MIN, DEFER_MAX = 30.0, 3600.0
FAIL_BACKOFF = 300.0       # 回执 ok=false（没说多久）时的退避
TIMEOUT_BACKOFF = 60.0     # 回执超时的退避

# 托管渠道（看板负责启停进程）与外部渠道（进程在别处，看板只能启用/停用推送）
ADAPTER_PLUGIN = "plugin"
ADAPTER_PROCESS = "process"
KNOWN_ADAPTERS = (ADAPTER_PLUGIN, ADAPTER_PROCESS)

LOCK = threading.RLock()
SESSIONS: dict = {}        # channel_id -> ChannelSession（只存内存：连上才有）

# 内置渠道：看板启动时写进 channels 表（已存在就不动用户改过的字段）。
# `command` 里 {python} 代表本项目 venv 的解释器（看板替换），adapter=process 的渠道看板能启停。
BUILTIN_CHANNELS = (
    {"id": "maibot", "label": "麦麦插件", "adapter": ADAPTER_PLUGIN, "command": [],
     "enabled": True, "note": "插件在麦麦进程里，看板只控制启用/停用推送"},
    {"id": "alison", "label": "艾莉森", "adapter": ADAPTER_PROCESS,
     "command": ["{python}", "distribute/alison/channel.py"],
     "enabled": False, "note": "QQ 官方机器人图文转述；看板可启停"},
    {"id": "napcat", "label": "NapCat 联调", "adapter": ADAPTER_PROCESS,
     "command": ["{python}", "distribute/napcat/channel.py"],
     "enabled": False, "note": "OneBot11 联调发送端；需要 NAPCAT_* 配置"},
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _item_id_key(value):
    """Normalize JSON integer IDs without accepting floats or booleans."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text[0] == "-" and text[1:].isdigit())):
            try:
                return int(text)
            except ValueError:
                return None
    return None


# ────────────────────────── 数据库 ──────────────────────────

def connect(db_file=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file or DB_FILE), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY, label TEXT DEFAULT '',"
                 " adapter TEXT DEFAULT 'plugin', command TEXT DEFAULT '',"
                 " enabled INTEGER DEFAULT 1, targets TEXT DEFAULT '[]',"
                 " last_seen TEXT DEFAULT '', note TEXT DEFAULT '')")
    conn.commit()
    return conn


def _row_targets(value) -> list:
    """targets 既能是数据库里的 JSON 字符串，也能是 `hello` 里直接给的列表。"""
    if isinstance(value, str):
        try:
            data = json.loads(value) if value else []
        except Exception:
            data = []
    elif isinstance(value, list):
        data = value
    else:
        data = []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and str(item.get("kind") or "").strip():
            out.append({"kind": str(item["kind"]).strip(),
                        "label": str(item.get("label") or item["kind"]).strip()})
        elif isinstance(item, str) and item.strip():
            out.append({"kind": item.strip(), "label": item.strip()})
    return out


def _row_to_channel(row) -> dict:
    return {
        "id": str(row["id"]),
        "label": str(row["label"] or row["id"]),
        "adapter": str(row["adapter"] or ADAPTER_PLUGIN),
        "command": str(row["command"] or ""),
        "enabled": bool(row["enabled"]),
        "targets": _row_targets(row["targets"]),
        "last_seen": str(row["last_seen"] or ""),
        "note": str(row["note"] or ""),
    }


def upsert_channel(channel_id: str, label: str = "", adapter: str = ADAPTER_PLUGIN,
                   targets=None, command: str = "", note: str = "",
                   touch: bool = True, db_file=None) -> None:
    """登记/刷新渠道行。已存在时只覆盖传进来的非空字段（enabled 永远保留用户设置）。"""
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        return
    conn = connect(db_file)
    try:
        row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        current = _row_to_channel(row) if row is not None else None
        merged = {
            "label": label or (current or {}).get("label") or channel_id,
            "adapter": adapter if adapter in KNOWN_ADAPTERS else (current or {}).get("adapter", ADAPTER_PLUGIN),
            "targets": json.dumps(targets if targets is not None else (current or {}).get("targets", []),
                                  ensure_ascii=False),
            "command": command or (current or {}).get("command", ""),
            "note": note or (current or {}).get("note", ""),
            "last_seen": now_iso() if touch else (current or {}).get("last_seen", ""),
            "enabled": 1 if current is None or current.get("enabled", True) else 0,
        }
        conn.execute(
            "INSERT INTO channels(id, label, adapter, command, enabled, targets, last_seen, note)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
            " label=excluded.label, adapter=excluded.adapter, command=excluded.command,"
            " targets=excluded.targets, last_seen=excluded.last_seen, note=excluded.note",
            (channel_id, merged["label"], merged["adapter"], merged["command"],
             merged["enabled"], merged["targets"], merged["last_seen"], merged["note"]))
        conn.commit()
    finally:
        conn.close()


def touch_channel(channel_id: str, note: str = "", db_file=None) -> None:
    conn = connect(db_file)
    try:
        if note:
            conn.execute("UPDATE channels SET last_seen=?, note=? WHERE id=?",
                         (now_iso(), note, channel_id))
        else:
            conn.execute("UPDATE channels SET last_seen=? WHERE id=?", (now_iso(), channel_id))
        conn.commit()
    finally:
        conn.close()


def channel_row(channel_id: str, db_file=None) -> dict:
    conn = connect(db_file)
    try:
        row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        return _row_to_channel(row) if row is not None else {}
    finally:
        conn.close()


def set_enabled(channel_id: str, enabled: bool, db_file=None) -> dict:
    """看板开关：写库 + 给在线渠道发 pause/resume。"""
    conn = connect(db_file)
    try:
        conn.execute("UPDATE channels SET enabled=? WHERE id=?",
                     (1 if enabled else 0, channel_id))
        conn.commit()
        row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        info = _row_to_channel(row) if row is not None else {}
    finally:
        conn.close()
    session = SESSIONS.get(channel_id)
    if session is not None:
        session.set_enabled(bool(enabled))
    return info


def active_sources(db_file=None) -> list[str]:
    """当前启用的 source；艾莉森以逐群开关为准，不算已停用群的历史 source。"""
    ensure_builtin_channels(db_file)
    conn = connect(db_file)
    try:
        rows = conn.execute("SELECT id,targets FROM channels WHERE enabled=1").fetchall()
    finally:
        conn.close()
    sources = []
    for row in rows:
        if row["id"] == "alison":
            from distribute.alison.ali_core.groups import list_groups
            sources.extend(f"alison:group_{entry['group_openid']}"
                           for entry in list_groups(enabled_only=True, db_file=db_file))
        else:
            sources.extend(f"{row['id']}:{target['kind']}"
                           for target in _row_targets(row["targets"]))
    return sources


def sent_counts(db_file=None) -> dict:
    """每个 source 累计发送条数（pushes 表）。"""
    conn = connect(db_file)
    try:
        rows = conn.execute("SELECT source, COUNT(*) AS n FROM pushes GROUP BY source").fetchall()
        return {str(row["source"]): int(row["n"]) for row in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def recent_pushes(limit: int = 20, db_file=None) -> list:
    """最近分发尝试：成功和失败都展示，艾莉森目标显示群名。"""
    conn = connect(db_file)
    try:
        rows = conn.execute(
            "SELECT p.item_id,p.channel,p.kind,p.source,p.attempted_at,p.status,p.detail,"
            " i.account,i.zh,i.ai_title,g.group_name"
            " FROM push_attempts p LEFT JOIN items i ON i.id=p.item_id"
            " LEFT JOIN alison_groups g ON p.channel='alison'"
            " AND g.group_openid=substr(p.kind,7)"
            " ORDER BY p.id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for row in rows:
        title = str(row["ai_title"] or "").strip() or str(row["zh"] or "").strip()[:40]
        out.append({
            "item_id": row["item_id"], "channel": row["channel"], "kind": row["kind"],
            "source": row["source"], "sent_at": row["attempted_at"],
            "status": row["status"], "detail": row["detail"],
            "label": row["group_name"] or ("群资料查询中…" if row["channel"] == "alison" else row["kind"]),
            "account": row["account"], "zh": row["zh"], "title": title,
        })
    return out


def ensure_builtin_channels(db_file=None) -> list:
    """把内置渠道登记进表（幂等）。

    新库第一次启动看板就能看到三个渠道；已存在的行**不覆盖用户设置**，
    但会在 `command` 空着时补上内置启动命令（渠道先连上来、内置定义后加的这种情况）。
    """
    conn = connect(db_file)
    added = []
    try:
        for spec in BUILTIN_CHANNELS:
            row = conn.execute("SELECT id, command FROM channels WHERE id=?",
                               (spec["id"],)).fetchone()
            command = json.dumps(list(spec.get("command") or []), ensure_ascii=False)
            if row is None:
                conn.execute(
                    "INSERT INTO channels(id, label, adapter, command, enabled, targets,"
                    " last_seen, note) VALUES(?,?,?,?,?,?,?,?)",
                    (spec["id"], spec["label"], spec["adapter"], command,
                     1 if spec.get("enabled") else 0, "[]", "", spec.get("note", "")))
                added.append(spec["id"])
            elif spec.get("command") and not str(row["command"] or "").strip():
                conn.execute("UPDATE channels SET command=? WHERE id=?", (command, spec["id"]))
                added.append(spec["id"])
        conn.commit()
    finally:
        conn.close()
    if added:
        print(f"[渠道] 已登记内置渠道：{', '.join(added)}", flush=True)
    return added


def list_channels(db_file=None) -> list:
    """看板用：渠道 + 每个 target 的待发/累计发送 + 在线状态。"""
    ensure_builtin_channels(db_file)
    conn = connect(db_file)
    try:
        rows = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
    finally:
        conn.close()
    counts = sent_counts(db_file)
    out = []
    for row in rows:
        info = _row_to_channel(row)
        sources = [f"{info['id']}:{t['kind']}" for t in info["targets"]]
        pending_items = push_db.pending_for_sources(sources, db_file=db_file) if sources else []
        pending_ids = {it["id"] for it in pending_items}
        targets = []
        for target in info["targets"]:
            source = f"{info['id']}:{target['kind']}"
            targets.append({
                "kind": target["kind"],
                "label": target["label"],
                "source": source,
                "pending": sum(1 for it in pending_items
                               if it["pushed"].get(source) is not True),
                "sent_total": counts.get(source, 0),
            })
        session = SESSIONS.get(info["id"])
        info.update({
            "managed": info["adapter"] == ADAPTER_PROCESS,
            "connected": bool(session and session.alive),
            "status": (session.status if session else ("未连接" if info["enabled"] else "已停用")),
            "pending": len(pending_ids),
            "sent_total": sum(entry["sent_total"] for entry in targets),
            "targets": targets,
        })
        out.append(info)
    return out


# ────────────────────────── 会话与推送 ──────────────────────────

class ChannelSession:
    """一个在线渠道：推送模式由推送线程发条目、读线程递回执；拉取模式由渠道自己 `want`。"""

    def __init__(self, channel_id: str, label: str, adapter: str, targets: list, connection,
                 pull: bool = False):
        self.id = channel_id
        self.label = label
        self.adapter = adapter
        self.targets = targets                    # [{"kind","label"}]
        self.sources = [f"{channel_id}:{t['kind']}" for t in targets]
        self.connection = connection
        self.pull = bool(pull)                    # True = 渠道按自己的节奏 want，服务端不主动推
        self.acks: queue.Queue = queue.Queue()
        self.defer_until: dict = {}               # source -> 渠道主动延后时间
        self.retry_after: dict = {}               # (source, item_id) -> 失败后重试时间
        self.inflight: dict = {}                  # source -> {item_id: 时间}（want 出去还没回执的）
        self.status = "idle"
        self.alive = True
        self.enabled = True
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._push_loop, name=f"hx-push-{channel_id}",
                                       daemon=True)

    # ── 控制 ──
    def start(self) -> None:
        self.thread.start()

    def stop(self, reason: str = "") -> None:
        self.alive = False
        self.enabled = False
        self.stop_event.set()
        with self.acks.mutex:                     # 唤醒可能在等回执的推送线程
            self.acks.queue.clear()
        self.acks.put({"type": "_closed", "reason": reason})

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.send({"type": "resume" if enabled else "pause"})
        if not enabled:
            with self.acks.mutex:
                self.acks.queue.clear()
            self.acks.put({"type": "_paused"})
        self.status = "idle" if enabled else "已停用"

    # ── 消息 ──
    def send(self, message: dict) -> bool:
        try:
            self.connection.send_json(message)
            return True
        except Exception:
            self.alive = False
            return False

    def deliver(self, message: dict) -> None:
        """读线程调用：把渠道回执递给推送线程。"""
        self.acks.put(message)

    def set_status(self, state: str, note: str = "") -> None:
        self.status = str(state or "idle")
        touch_channel(self.id, note or self.status)

    # ── 拉取模式：渠道说"我现在要发" ──
    def handle_pending(self, source: str) -> None:
        """`pending` → 回这个 source 还有多少条未发（拉取模式问"还有内容吗"用）。"""
        if source not in self.sources:
            self.send({"type": "error", "error": f"source {source} 不属于本渠道"})
            return
        try:
            count = len(push_db.pending(source=source))
        except Exception as exc:
            self.send({"type": "error", "error": f"读队列失败：{exc}"})
            return
        self.send({"type": "pending", "source": source, "count": count})

    def handle_want(self, source: str, count) -> None:
        """`want` → 回 `items`。条目记为在途（不会同时被推送线程再推一次）。"""
        if source not in self.sources:
            self.send({"type": "error", "error": f"source {source} 不属于本渠道"})
            return
        try:
            limit = max(1, min(int(count or 1), 50))
        except (TypeError, ValueError):
            limit = 1
        try:
            blocked = push_db.reconcile_deliveries(source)
            items = [item for item in push_db.pending(source=source) if item["id"] not in blocked]
        except Exception as exc:
            self.send({"type": "error", "error": f"读队列失败：{exc}"})
            return
        now = time.time()
        slot = self.inflight.setdefault(source, {})
        # 在途太久（渠道拿了却没回执）就清掉，免得永远不再给它
        for item_id in [key for key, at in slot.items() if now - at > 600]:
            slot.pop(item_id, None)
        in_flight_ids = set(slot)
        picked = [item for item in items
                  if _item_id_key(item.get("id")) not in in_flight_ids][:limit]
        for item in picked:
            item_id = _item_id_key(item.get("id"))
            if item_id is not None:
                slot[item_id] = now
        self.send({"type": "items", "source": source, "items": picked})

    def handle_result(self, source: str, item_id, ok: bool, detail: str = "") -> None:
        """拉取模式的回执：直接落库（推送模式走 acks 队列，由推送线程处理）。"""
        if source not in self.sources:
            return
        item_key = _item_id_key(item_id)
        slot = self.inflight.get(source, {})
        if item_key is None or item_key not in slot:
            print(f"[渠道:{self.id}] 忽略非在途回执 {source} item={item_id}", flush=True)
            return
        slot.pop(item_key, None)
        if not ok:
            self._record_attempt(source, item_id, "failed", detail or "未说明")
            print(f"[渠道:{self.id}] {source} 条目 {item_id} 发送失败：{detail or '未说明'}", flush=True)
            return
        try:
            push_db.mark_pushed([item_id], source)
            self._record_attempt(source, item_id, "success")
            self.send({"type": "ack", "source": source, "item_id": item_id})
            print(f"[渠道:{self.id}] 已发 {source} item={item_id}", flush=True)
        except Exception as exc:
            self._record_attempt(source, item_id, "sent_unrecorded", str(exc))
            print(f"[渠道:{self.id}] 标记已发失败 item={item_id}: {exc}", flush=True)

    # ── 推送循环 ──
    def _push_loop(self) -> None:
        if self.pull:                             # 拉取模式：不主动推，只等渠道 want
            print(f"[渠道:{self.id}] 拉取模式（服务端不主动推，等渠道 want）", flush=True)
            while not self.stop_event.wait(IDLE_SLEEP):
                if not self.alive:
                    break
            self.alive = False
            return
        while not self.stop_event.is_set():
            if not self.alive:
                break
            if not self.enabled:
                self.stop_event.wait(IDLE_SLEEP)
                continue
            worked = False
            for source in self.sources:
                if self.stop_event.is_set() or not self.alive:
                    break
                if not self.enabled:
                    break
                if time.time() < self.defer_until.get(source, 0.0):
                    continue
                try:
                    unrecorded = push_db.reconcile_deliveries(source)
                    items = push_db.pending(source=source)
                except Exception as exc:          # 库暂时读不了：等下一轮
                    print(f"[渠道:{self.id}] 读队列失败: {exc}", flush=True)
                    break
                if not items:
                    continue
                now = time.time()
                pending_ids = {entry["id"] for entry in items}
                for key in list(self.retry_after):
                    if key[0] == source and key[1] not in pending_ids:
                        self.retry_after.pop(key, None)
                item = next((entry for entry in items
                             if entry["id"] not in unrecorded and
                             now >= self.retry_after.get((source, entry["id"]), 0.0)), None)
                if item is None:
                    continue
                if not self.send({"type": "item", "source": source, "item": item}):
                    self.alive = False
                    break
                worked = True
                try:
                    reply = self._wait_for_reply(source, item, ACK_TIMEOUT)
                except queue.Empty:
                    self._retry_later(source, item["id"], TIMEOUT_BACKOFF)
                    self._record_attempt(source, item["id"], "failed", "渠道回执超时")
                    print(f"[渠道:{self.id}] {source} 条目 {item['id']} 回执超时，"
                          f"{TIMEOUT_BACKOFF:.0f}s 后重试", flush=True)
                    continue
                self._handle_reply(source, item, reply)
            if not worked:
                self.stop_event.wait(IDLE_SLEEP)
        self.alive = False
        print(f"[渠道:{self.id}] 推送线程结束", flush=True)

    def _wait_for_reply(self, source: str, item: dict, timeout: float) -> dict:
        """Ignore stale/mismatched replies and wait for this item's own result."""
        item_id = _item_id_key(item.get("id"))
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            reply = self.acks.get(timeout=remaining)
            kind = str(reply.get("type") or "")
            if kind in ("_closed", "_paused"):
                return reply
            reply_source = str(reply.get("source") or "")
            reply_id = _item_id_key(reply.get("item_id"))
            if kind in ("result", "defer") and reply_source == source and reply_id == item_id:
                return reply
            print(f"[渠道:{self.id}] 忽略不匹配回执 type={kind or '?'} "
                  f"source={reply_source or '?'} item={reply.get('item_id')}", flush=True)

    def _retry_later(self, source: str, item_id: int, seconds: float) -> None:
        self.retry_after[(source, item_id)] = time.time() + max(0.0, float(seconds))

    def _record_attempt(self, source: str, item_id: int, status: str, detail: str = "") -> None:
        try:
            push_db.record_attempt(item_id, source, status, detail)
        except Exception as exc:
            print(f"[渠道:{self.id}] 分发记录写入失败 item={item_id}: {exc}", flush=True)

    def _handle_reply(self, source: str, item: dict, reply: dict) -> None:
        kind = str(reply.get("type") or "")
        if kind == "_closed":
            self.alive = False
            return
        if kind == "_paused":
            return
        if kind in ("result", "defer") and (
                str(reply.get("source") or "") != source
                or _item_id_key(reply.get("item_id")) != _item_id_key(item.get("id"))):
            # Defensive guard: this method must never mark a different item sent,
            # even if a future caller bypasses _wait_for_reply().
            print(f"[渠道:{self.id}] 忽略不匹配回执 source={reply.get('source')} "
                  f"item={reply.get('item_id')}；当前={source} item={item.get('id')}", flush=True)
            return
        if kind == "result" and reply.get("ok"):
            try:
                push_db.mark_pushed([item["id"]], source)
            except Exception as exc:              # QQ 已发但写库失败：禁止重发，避免重复刷屏
                self._record_attempt(source, item["id"], "sent_unrecorded", str(exc))
                self._retry_later(source, item["id"], float("inf"))
                print(f"[渠道:{self.id}] 标记已发失败 item={item['id']}: {exc}", flush=True)
                return
            self._record_attempt(source, item["id"], "success")
            self.send({"type": "ack", "source": source, "item_id": item["id"]})
            print(f"[渠道:{self.id}] 已发 {source} item={item['id']} "
                  f"@{item.get('account') or '?'}", flush=True)
            return
        if kind == "defer":
            try:
                seconds = float(reply.get("seconds") or TIMEOUT_BACKOFF)
            except (TypeError, ValueError):
                seconds = TIMEOUT_BACKOFF
            seconds = min(max(seconds, DEFER_MIN), DEFER_MAX)
            self.defer_until[source] = time.time() + seconds
            print(f"[渠道:{self.id}] {source} 条目 {item['id']} 延后 {seconds:.0f}s"
                  f"（{reply.get('reason') or '未说明'}）", flush=True)
            return
        if kind == "result":
            self._retry_later(source, item["id"], FAIL_BACKOFF)
            self._record_attempt(source, item["id"], "failed", str(reply.get("detail") or "未说明"))
            print(f"[渠道:{self.id}] {source} 条目 {item['id']} 发送失败："
                  f"{reply.get('detail') or '未说明'}；{FAIL_BACKOFF:.0f}s 后重试", flush=True)
            return
        # 其它消息（status 之类混进来了）：不惩罚，接着推
        print(f"[渠道:{self.id}] 忽略回执类型 {kind!r}", flush=True)


# ────────────────────────── 对外入口（WS 服务端调用）──────────────────────────

def register_connection(connection, message: dict) -> dict:
    """处理 `hello`：写注册表、登记 source、起推送线程，返回 `welcome` 内容。"""
    channel_id = str(message.get("channel") or connection.channel or "").strip()
    if not channel_id:
        raise ValueError("hello 缺少 channel")
    label = str(message.get("label") or channel_id).strip()
    adapter = str(message.get("adapter") or ADAPTER_PLUGIN).strip()
    if adapter not in KNOWN_ADAPTERS:
        adapter = ADAPTER_PLUGIN
    targets = _row_targets(message.get("targets") or [])
    # A channel may be online with zero enabled targets (e.g. Alison before any group is enabled).
    # In that state it registers no new source and cannot receive an item.
    capabilities = message.get("capabilities") or {}
    pull = bool(capabilities.get("pull")) if isinstance(capabilities, dict) else False

    connection.channel = channel_id
    sources = [f"{channel_id}:{t['kind']}" for t in targets]
    new_sources = push_db.register_sources(sources)
    upsert_channel(channel_id, label=label, adapter=adapter, targets=targets)

    with LOCK:
        old = SESSIONS.get(channel_id)
        if old is not None and old.connection is connection:
            # 更新同一连接的目标（群开关/群名变化）：别重建会话和线程，免得重置退避/丢回执。
            old.targets = targets
            old.sources = sources
            old.label = label
            session = old
        else:
            if old is not None:                   # 真正重连才收掉旧会话
                old.stop("重连")
            info = channel_row(channel_id)
            session = ChannelSession(channel_id, label, adapter, targets, connection, pull=pull)
            session.enabled = bool(info.get("enabled", True))
            SESSIONS[channel_id] = session
            session.start()
    target_note = f"已启用目标 {len(sources)} 个" if sources else "无已启用目标，等待群开关"
    print(f"[渠道:{channel_id}] {'新增推送目标：' + ', '.join(new_sources) if new_sources else '渠道已登记'}"
          f"；{target_note}{'；拉取模式' if pull else ''}", flush=True)
    return {
        "type": "welcome",
        "channel": channel_id,
        "label": label,
        "registered": bool(new_sources),
        "new_sources": new_sources,
        "sources": sources,
        "enabled": session.enabled,
        "pull": pull,
        "ts": time.time(),
    }


def handle_message(connection, message: dict) -> None:
    """处理渠道发来的消息（`hello` 之外的都转给会话）。"""
    channel_id = str(connection.channel or "").strip()
    with LOCK:
        session = SESSIONS.get(channel_id)
    kind = str(message.get("type") or "")
    if kind == "hello":
        connection.send_json(register_connection(connection, message))
        return
    if session is None or session.connection is not connection:
        connection.send_json({"type": "error", "error": "还没 hello 或连接已被新会话替换"})
        return
    if kind == "result":
        if session.pull:
            session.handle_result(str(message.get("source") or ""),
                                  message.get("item_id"), bool(message.get("ok")),
                                  str(message.get("detail") or ""))
        else:
            # 只认当前在途的那条（防止乱序/重放把别的条目标成已发）
            session.deliver(message)
        return
    if kind == "want":
        if not session.pull:
            connection.send_json({"type": "error", "error": "本渠道不是拉取模式，服务端会主动推"})
            return
        session.handle_want(str(message.get("source") or ""), message.get("count"))
        return
    if kind == "pending":
        session.handle_pending(str(message.get("source") or ""))
        return
    if kind == "defer":
        session.deliver(message)
        return
    if kind == "status":
        session.set_status(str(message.get("state") or "idle"), str(message.get("note") or ""))
        return
    if kind == "ping":
        connection.send_json({"type": "pong", "ts": time.time()})
        return
    if kind == "bye":
        connection.close()
        return
    connection.send_json({"type": "error", "error": f"未知消息类型 {kind}"})


def unregister_connection(connection) -> None:
    """连接断开：收掉会话（条目保持未发，下次连上继续）。"""
    channel_id = str(connection.channel or "").strip()
    if not channel_id:
        return
    with LOCK:
        session = SESSIONS.get(channel_id)
        if session is not None and session.connection is connection:
            SESSIONS.pop(channel_id, None)
        else:
            session = None
    if session is not None:
        session.stop("断开")
        print(f"[渠道:{channel_id}] 已断开", flush=True)


def disconnect_channel(channel_id: str, reason: str = "看板停止") -> bool:
    """看板主动踢掉一个在线渠道（托管渠道停止进程前用）。"""
    with LOCK:
        session = SESSIONS.get(channel_id)
    if session is None:
        return False
    session.send({"type": "shutdown", "reason": reason})
    session.stop(reason)
    return True


def channel_state(channel_id: str) -> dict:
    with LOCK:
        session = SESSIONS.get(channel_id)
    if session is None:
        return {}
    return {"connected": session.alive, "status": session.status,
            "targets": [t["kind"] for t in session.targets]}
