"""按新浪标题词库清理本地数据库；默认预览，--apply 才备份并删除。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect.paths import DB_FILE, STATE_DIR, LOG_DIR
from collect.logging_util import make_log, setup_console_encoding
from collect.sources.sina.keywords import matches_title
from collect.sources._web.collector import archive


def clean(main_path=DB_FILE, state_path=STATE_DIR / "web_collect.sqlite3", apply=False):
    log = make_log(LOG_DIR / "clean_sina_keywords.log")
    with closing(sqlite3.connect(main_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        has_state = Path(state_path).exists()
        if has_state:
            conn.execute("ATTACH DATABASE ? AS ledger", (str(state_path),))
        # 锁住两个库，防止预览/备份/删除期间候选或新闻发生变化。
        conn.execute("BEGIN IMMEDIATE")
        try:
            candidates = [dict(row) for row in conn.execute("SELECT * FROM ledger.candidates WHERE source='sina'")] if has_state else []
            titles = {row["item_id"]: json.loads(row["payload"]).get("title", "")
                      for row in candidates if row["item_id"] is not None}
            news = [dict(row) for row in conn.execute("SELECT * FROM items WHERE source='sina'")]
            removed = [row for row in news if not matches_title(titles.get(row["id"], (row["raw"] or "").split('\n', 1)[0]))]
            excluded = [row for row in candidates if not matches_title(json.loads(row["payload"]).get("title"))]
            result = {"sina_before": len(news), "items_removed": len(removed), "sina_kept": len(news)-len(removed),
                      "candidates_removed": len(excluded), "applied": apply}
            log(json.dumps(result, ensure_ascii=False))
            if not apply:
                conn.rollback()
                return result
            stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")
            backup_dir = ROOT / "data" / "backups" / ("sina-keywords-" + stamp)
            backup_dir.mkdir(parents=True)
            # 独立连接做 SQLite 在线备份，包含 WAL 中已经提交的内容。
            for path in [Path(main_path)] + ([Path(state_path)] if has_state else []):
                with closing(sqlite3.connect(path, timeout=30)) as source, closing(sqlite3.connect(backup_dir / path.name)) as dest:
                    source.backup(dest)
            archive(backup_dir / "removed.json", {"summary": result, "items": removed, "candidates": excluded})
            for row in removed:
                for table in ("processing_events", "pushes", "push_attempts"):
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        conn.execute(f"DELETE FROM {table} WHERE item_id=?", (row["id"],))
                conn.execute("DELETE FROM items WHERE id=? AND source='sina'", (row["id"],))
            for row in excluded:
                conn.execute("DELETE FROM ledger.notices WHERE source='sina' AND url=?", (row["url"],))
                conn.execute("DELETE FROM ledger.candidates WHERE source='sina' AND identity=?", (row["identity"],))
            conn.commit()
            result["backup"] = str(backup_dir)
            log(json.dumps(result, ensure_ascii=False))
            return result
        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    setup_console_encoding()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    clean(apply=parser.parse_args().apply)
