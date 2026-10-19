# -*- coding: utf-8 -*-
"""Local event embeddings and a recoverable USearch HNSW candidate index."""
import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from collect.paths import DATA_DIR, DB_FILE, LOG_DIR
from process.logging_util import make_log, one_line

MODEL_ID = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
MODEL_VERSION = f"{MODEL_ID}@{MODEL_REVISION}"
DIMENSIONS = 1024
INDEX_FILE = DATA_DIR / "vectors" / "events.usearch"
MANIFEST_FILE = DATA_DIR / "state" / "event_vector_index.json"
_PRODUCTION_INDEX_FILE = INDEX_FILE.resolve()
_PRODUCTION_MANIFEST_FILE = MANIFEST_FILE.resolve()
MODEL_CACHE = DATA_DIR / "models"
CONTEXT_DIR = DATA_DIR / "work" / "embedding_tasks"
TOP_K = 12
SEARCH_K = 48
MIN_SIMILARITY = 0.35

log = make_log(LOG_DIR / "embedding.log")
_model = None
_model_error_logged = False
_index = None
_index_lock = threading.RLock()


def event_text(item):
    """Keep the embedded payload stable across collection and historical backfill."""
    body = str(item.get("raw") or "").strip()
    quoted = str(item.get("quoted_raw") or "").strip()
    if quoted:
        return f"正文：{body}\n被引用正文：{quoted}"
    return body


def input_hash(item):
    return hashlib.sha256(event_text(item).encode("utf-8")).hexdigest()


def _load_model(allow_download=False):
    global _model, _model_error_logged
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer

        MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        _model = SentenceTransformer(
            MODEL_ID,
            revision=MODEL_REVISION,
            cache_folder=str(MODEL_CACHE),
            device="cpu",
            local_files_only=not allow_download,
        )
        _model.max_seq_length = 8192
        log(f"[embedding] 本地模型已加载：{MODEL_VERSION}")
        return _model
    except Exception as exc:
        if not _model_error_logged:
            log(f"[embedding] 本地模型不可用，暂用原分析路径：{one_line(str(exc))}")
            _model_error_logged = True
        raise


def encode_text(text):
    text = str(text or "").strip()
    if not text:
        raise ValueError("事件正文为空，无法生成向量")
    vector = _load_model().encode(
        [text],
        batch_size=1,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )[0]
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    if vector.size != DIMENSIONS or not np.isfinite(vector).all():
        raise ValueError(f"embedding 维度或数值无效：{vector.size}")
    return vector


def _index_api():
    from usearch.index import Index

    return Index


def _counts(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n,COALESCE(MAX(index_key),0) AS maximum "
        "FROM item_embeddings WHERE model_version=?",
        (MODEL_VERSION,),
    ).fetchone()
    return int(row["n"]), int(row["maximum"])


def _sidecar_allowed(conn):
    """Keep test and scratch databases from overwriting the production ANN files."""
    if (Path(INDEX_FILE).resolve() != _PRODUCTION_INDEX_FILE
            or Path(MANIFEST_FILE).resolve() != _PRODUCTION_MANIFEST_FILE):
        return True
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        database_path = Path(row["file"]).resolve() if row and row["file"] else None
        return database_path == Path(DB_FILE).resolve()
    except (OSError, TypeError, IndexError):
        return False


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _new_index():
    Index = _index_api()
    return Index(
        ndim=DIMENSIONS,
        metric="cos",
        dtype="f32",
        connectivity=16,
        expansion_add=128,
        expansion_search=64,
    )


def _save_index(index, count, maximum):
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_FILE.with_suffix(".tmp")
    # USearch's Windows path API cannot open some Unicode workspace paths.
    # Serialize to bytes, then let Python perform the Unicode-safe atomic write.
    temporary.write_bytes(index.save())
    os.replace(temporary, INDEX_FILE)
    _atomic_json(MANIFEST_FILE, {
        "model_version": MODEL_VERSION,
        "dimensions": DIMENSIONS,
        "count": count,
        "max_index_key": maximum,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })


def _rebuild_index(conn):
    index = _new_index()
    cursor = conn.execute(
        "SELECT index_key,vector FROM item_embeddings WHERE model_version=? ORDER BY index_key",
        (MODEL_VERSION,),
    )
    while True:
        rows = cursor.fetchmany(512)
        if not rows:
            break
        keys = np.asarray([int(row["index_key"]) for row in rows], dtype=np.uint64)
        vectors = np.stack([
            np.frombuffer(row["vector"], dtype=np.float32, count=DIMENSIONS).copy()
            for row in rows
        ])
        index.add(keys, vectors)
    count, maximum = _counts(conn)
    _save_index(index, count, maximum)
    return index


def _get_index(conn):
    global _index
    if not _sidecar_allowed(conn):
        return _new_index()
    with _index_lock:
        count, maximum = _counts(conn)
        manifest = None
        try:
            manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        healthy = (
            _index is not None
            and manifest
            and manifest.get("model_version") == MODEL_VERSION
            and manifest.get("count") == count
            and manifest.get("max_index_key") == maximum
            and len(_index) == count
        )
        if healthy:
            return _index
        healthy_file = (
            INDEX_FILE.is_file()
            and manifest
            and manifest.get("model_version") == MODEL_VERSION
            and manifest.get("count") == count
            and manifest.get("max_index_key") == maximum
        )
        if healthy_file:
            try:
                _index = _index_api().restore(INDEX_FILE.read_bytes())
                if len(_index) == count:
                    return _index
            except Exception:
                pass
        _index = _rebuild_index(conn)
        return _index


def retrieve(conn, vector, exclude_item_id=None, limit=TOP_K):
    """Return formatted, processed records; vector similarity never makes the decision."""
    if not _sidecar_allowed(conn):
        return []
    if conn.execute("SELECT 1 FROM item_embeddings WHERE model_version=? LIMIT 1", (MODEL_VERSION,)).fetchone() is None:
        return []
    index = _get_index(conn)
    if not len(index):
        return []
    matches = index.search(np.asarray(vector, dtype=np.float32), min(SEARCH_K, len(index)))
    rows = []
    for match in matches:
        key = int(match.key)
        similarity = 1.0 - float(match.distance)
        if similarity < MIN_SIMILARITY:
            continue
        row = conn.execute(
            "SELECT i.id,i.ai_title,i.ai_summary,i.ts FROM item_embeddings e "
            "JOIN items i ON i.id=e.item_id "
            "WHERE e.index_key=? AND e.model_version=? AND i.stage='processed' AND i.id<>?",
            (key, MODEL_VERSION, int(exclude_item_id or -1)),
        ).fetchone()
        if row is None:
            continue
        title = str(row["ai_title"] or "").strip()
        summary = str(row["ai_summary"] or "").strip()
        if not title:
            continue
        rows.append((similarity, f"#{row['id']} {title} | {summary}".strip()))
        if len(rows) >= limit:
            break
    return [line for _, line in rows]


def prepare(item, conn):
    """Create the query vector and candidate list in the long-lived parent process."""
    vector = encode_text(event_text(item))
    candidates = retrieve(conn, vector, exclude_item_id=item.get("id"))
    return {
        "available": True,
        "item_id": int(item["id"]),
        "input_hash": input_hash(item),
        "model_version": MODEL_VERSION,
        "vector": vector.tolist(),
        "candidates": candidates,
        "indexed_count": _counts(conn)[0],
    }


def unavailable_context(item, error=None):
    if error:
        log(f"[embedding] item={item.get('id')} 生成失败，继续原分析：{one_line(str(error))}")
    return {
        "available": False,
        "item_id": int(item["id"]),
        "input_hash": input_hash(item),
        "model_version": MODEL_VERSION,
        "vector": None,
        "candidates": [],
        "indexed_count": 0,
    }


def write_context(item, conn):
    """Write an atomic worker handoff so parallel workers do not load duplicate models."""
    try:
        payload = prepare(item, conn)
    except Exception as exc:
        payload = unavailable_context(item, exc)
    path = CONTEXT_DIR / f"{int(item['id'])}-{time.time_ns()}.json"
    temporary = path.with_suffix(".tmp")
    try:
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        return path
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        log(f"[embedding] item={item['id']} 上下文暂存失败，子进程将独立尝试：{one_line(str(exc))}")
        return None


def read_context(path, item):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if (payload.get("item_id") != int(item["id"])
                or payload.get("input_hash") != input_hash(item)
                or payload.get("model_version") != MODEL_VERSION):
            return None
        vector = payload.get("vector")
        if payload.get("available") and vector is not None:
            values = np.asarray(vector, dtype=np.float32)
            if values.size != DIMENSIONS or not np.isfinite(values).all():
                return None
            payload["vector"] = values.tolist()
        else:
            payload["available"] = False
        return payload
    except (OSError, ValueError, TypeError):
        return None


def save_embedding(conn, item, context):
    """Stage a vector in the same DB transaction as the validated analysis result."""
    if not context or not context.get("available") or context.get("input_hash") != input_hash(item):
        return None
    values = np.asarray(context.get("vector"), dtype=np.float32).reshape(-1)
    if values.size != DIMENSIONS or not np.isfinite(values).all():
        return None
    conn.execute("DELETE FROM item_embeddings WHERE item_id=?", (int(item["id"]),))
    cursor = conn.execute(
        "INSERT INTO item_embeddings(item_id,model_version,input_hash,dimensions,vector,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (int(item["id"]), MODEL_VERSION, input_hash(item), DIMENSIONS, values.tobytes(),
         datetime.now().isoformat(timespec="seconds")),
    )
    return int(cursor.lastrowid)


def sync_committed(conn, item_id):
    """Add a committed vector to the sidecar; SQLite remains authoritative on crashes."""
    global _index
    if not _sidecar_allowed(conn):
        return False
    try:
        row = conn.execute(
            "SELECT index_key,vector FROM item_embeddings WHERE item_id=? AND model_version=?",
            (int(item_id), MODEL_VERSION),
        ).fetchone()
        if row is None:
            return False
        with _index_lock:
            count, maximum = _counts(conn)
            manifest = None
            try:
                manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            appendable = (
                _index is not None and manifest
                and manifest.get("model_version") == MODEL_VERSION
                and manifest.get("count") == count - 1
                and int(row["index_key"]) > int(manifest.get("max_index_key", 0))
            )
            if appendable:
                index = _index
                index.add(int(row["index_key"]), np.frombuffer(row["vector"], dtype=np.float32).copy())
                _save_index(index, count, maximum)
            else:
                # A crash or a batch change may have left an inconsistent sidecar.
                index = _rebuild_index(conn)
            _index = index
        return True
    except Exception as exc:
        log(f"[embedding] 索引同步失败，后续可由 SQLite 重建：{one_line(str(exc))}")
        return False


def backfill(conn, batch_size=64, progress=None):
    """Resumable history indexing; existing item analysis and delivery state stay untouched."""
    # SQLite has no built-in SHA-256; fetch processed rows and compare fingerprints in Python.
    rows = conn.execute("SELECT * FROM items WHERE stage='processed' ORDER BY id").fetchall()
    pending = []
    for row in rows:
        item = dict(row)
        existing = conn.execute(
            "SELECT input_hash FROM item_embeddings WHERE item_id=? AND model_version=?",
            (int(item["id"]), MODEL_VERSION),
        ).fetchone()
        if existing is None or existing["input_hash"] != input_hash(item):
            pending.append(item)
    done = failed = 0
    model = _load_model(allow_download=True)
    for offset in range(0, len(pending), max(1, int(batch_size))):
        chunk = pending[offset:offset + max(1, int(batch_size))]
        texts = [event_text(item) for item in chunk]
        try:
            vectors = model.encode(texts, batch_size=min(16, len(texts)), convert_to_numpy=True,
                                   normalize_embeddings=True, show_progress_bar=False)
        except Exception as exc:
            log(f"[embedding backfill] 批次失败 offset={offset}：{one_line(str(exc))}")
            failed += len(chunk)
            continue
        conn.execute("BEGIN IMMEDIATE")
        for item, vector in zip(chunk, vectors):
            current = conn.execute("SELECT * FROM items WHERE id=?", (int(item["id"]),)).fetchone()
            if current is None or current["stage"] != "processed" or input_hash(dict(current)) != input_hash(item):
                failed += 1
                continue
            vector = np.asarray(vector, dtype=np.float32).reshape(-1)
            if vector.size != DIMENSIONS or not np.isfinite(vector).all():
                failed += 1
                continue
            conn.execute("DELETE FROM item_embeddings WHERE item_id=?", (int(item["id"]),))
            conn.execute(
                "INSERT INTO item_embeddings(item_id,model_version,input_hash,dimensions,vector,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (int(item["id"]), MODEL_VERSION, input_hash(item), DIMENSIONS, vector.tobytes(),
                 datetime.now().isoformat(timespec="seconds")),
            )
            done += 1
        conn.commit()
        if progress:
            progress(done, failed, len(pending))
    if pending and _sidecar_allowed(conn):
        _rebuild_index(conn)
    return {"pending": len(pending), "embedded": done, "failed": failed}
