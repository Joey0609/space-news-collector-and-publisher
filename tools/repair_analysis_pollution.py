"""删除已确认的错误条目，先备份并保留发送审计；默认只预览。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "process"))
from ai import validate_analysis_grounding
from collect.logging_util import make_log, setup_console_encoding


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        temporary = Path(stream.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def repair(database, apply=False, backup_root=None, log=None, item_ids=None):
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(database)
    log = log or make_log(ROOT / "logs" / "repair_analysis_pollution.log")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        affected = []
        for row in conn.execute("SELECT * FROM items WHERE TRIM(COALESCE(zh,''))<>'' AND TRIM(COALESCE(ai_title,''))<>''"):
            if item_ids is not None:
                continue
            try:
                validate_analysis_grounding({"zh": row["zh"], "quoted_zh": row["quoted_zh"],
                                            "title": row["ai_title"], "summary": row["ai_summary"]},
                                           row["raw"], row["quoted_raw"])
            except ValueError as exc:
                affected.append((dict(row), str(exc)))
        if item_ids is not None:
            ids = sorted({int(value) for value in item_ids})
            if any(value <= 0 for value in ids):
                raise ValueError("条目 ID 必须是正整数")
            for item_id in ids:
                row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                if row:
                    affected.append((dict(row), "用户确认删除先前排查出的污染条目"))
        ids = [row["id"] for row, _ in affected]
        result = {"applied": False, "affected": len(ids), "item_ids": ids}
        if apply and ids:
            directory = Path(backup_root or ROOT / "data" / "backups") / ("analysis-pollution-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            directory.mkdir(parents=True)
            with closing(sqlite3.connect(directory / "hx.sqlite3")) as backup:
                conn.backup(backup)
            write_json(directory / "affected.json", affected)
            with conn:
                for row, detail in affected:
                    # 删除错误新闻及待处理事件；保留历史发送审计，不抹除已经发生的发送。
                    changed = conn.execute(
                        "DELETE FROM items WHERE id=? AND raw IS ? AND ai_title IS ?",
                        (row["id"], row["raw"], row["ai_title"])).rowcount
                    if changed != 1:
                        raise RuntimeError("原文并发变化，修复已回滚，请重新预览")
                    conn.execute("DELETE FROM processing_events WHERE item_id=?", (row["id"],))
            result.update(applied=True, backup=str(directory))
        log(json.dumps(result, ensure_ascii=False))
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    setup_console_encoding()
    parser = argparse.ArgumentParser(description="执行 --apply 前请停止处理与分发服务；所有修改先备份，不清除发送记录")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "hx.sqlite3")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--item-ids", help="明确授权删除的已排查条目 ID，逗号分隔；不填则重新检查污染")
    args = parser.parse_args()
    result = repair(args.db, args.apply, item_ids=args.item_ids.split(",") if args.item_ids else None)
    output = ROOT / "data" / "state" / "analysis_pollution_repair.json"
    write_json(output, result)
