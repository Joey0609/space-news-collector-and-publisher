"""停机时重建单次分析数据库：最近入库15条 + 不重复随机15条。"""
import argparse
import json
import os
import random
import secrets
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect.db import ensure_schema, ensure_processing_event
from distribute.push_db import connect as queue_connect
from collect.logging_util import make_log, setup_console_encoding

log = make_log(ROOT / "logs" / "reset_analysis_data.log")
RAW_KEYS = ("id", "platform", "source", "origin_id", "dedup_key", "account", "orig_account", "forward_comment", "content_type", "raw", "ts", "date", "ori_url", "images", "collected_at", "quoted_account", "quoted_raw", "quoted_url")


def require_stopped():
    for port in (8787, 8788):
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("请先手动关闭 dashboard 和托管服务")


def image_paths(rows, image_root):
    paths = set()
    for row in rows:
        for value in json.loads(row.get("images") or "[]"):
            path = Path(value).resolve()
            path.relative_to(image_root)
            paths.add(path)
    return paths


def reset(root=ROOT, seed=None, apply=False):
    root = Path(root).resolve()
    database = root / "data" / "hx.sqlite3"
    if apply:
        require_stopped()
    source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    rows = [dict(row) for row in source.execute("SELECT " + ",".join(RAW_KEYS) + " FROM items ORDER BY collected_at DESC,id DESC")]
    seed = secrets.randbits(64) if seed is None else int(seed)
    newest = rows[:15]
    sampled = random.Random(seed).sample(rows[15:], min(15, len(rows[15:])))
    selected = newest + sampled
    result = {"before": len(rows), "retained": len(selected), "deleted": len(rows)-len(selected), "seed": seed, "newest_ids": [row["id"] for row in newest], "random_ids": [row["id"] for row in sampled]}
    if not apply:
        source.close()
        log(json.dumps(result, ensure_ascii=False))
        return result

    image_root = (root / "data" / "images").resolve()
    keep_images = image_paths(selected, image_root)
    backup = root / "data" / "backups" / ("single-analysis-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(parents=True, exist_ok=False)
    saved = sqlite3.connect(backup / "hx.sqlite3")
    try:
        source.backup(saved)
    finally:
        saved.close()
    draft = backup / "new.sqlite3"
    target = queue_connect(draft)
    placeholders = ",".join("?" for _ in RAW_KEYS)
    for row in sorted(selected, key=lambda r: (r.get("ts") or 0, r["id"])):
        target.execute("INSERT INTO items(" + ",".join(RAW_KEYS) + ") VALUES(" + placeholders + ")", [row[key] for key in RAW_KEYS])
        ensure_processing_event(target, row["id"])
    for table in ("channels", "alison_groups"):
        columns = [r[1] for r in target.execute(f"PRAGMA table_info({table})")]
        for row in source.execute("SELECT " + ",".join(columns) + f" FROM {table}"):
            target.execute(f"INSERT INTO {table}(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(row))
    registry = source.execute("SELECT value FROM meta WHERE key='push_sources'").fetchone()
    if registry:
        target.execute("INSERT INTO meta(key,value) VALUES('push_sources',?)", (registry[0],))
    target.commit()
    assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert target.execute("SELECT COUNT(*) FROM items").fetchone()[0] == len(selected)
    target.close()
    source.close()
    # 停机后清掉旧 WAL，避免替换后的新主文件被旧日志污染。
    original = sqlite3.connect(database, timeout=10)
    checkpoint = original.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    original.close()
    if checkpoint[0]:
        raise RuntimeError("数据库仍被占用，未替换；请关闭剩余服务")
    (backup / "selection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    require_stopped()
    for suffix in ("-wal", "-shm"):
        Path(str(database)+suffix).unlink(missing_ok=True)
    os.replace(draft, database)

    files_removed = bytes_removed = 0
    # 文件删除前逐个确认绝对路径位于指定内容目录；不跟随外部链接。
    for directory in (image_root, root / "data" / "raw", root / "data" / "work", root / "data" / "markdown", root / "data" / "queue"):
        directory = directory.resolve()
        directory.relative_to((root / "data").resolve())
        for file in directory.rglob("*"):
            if not file.is_file():
                continue
            resolved = file.resolve()
            resolved.relative_to(directory)
            if directory == image_root and resolved in keep_images:
                continue
            bytes_removed += file.stat().st_size
            file.unlink()
            files_removed += 1
    result.update(backup=str(backup), files_removed=files_removed, bytes_removed=bytes_removed)
    (backup / "selection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    setup_console_encoding()
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    reset(seed=args.seed, apply=args.apply)
