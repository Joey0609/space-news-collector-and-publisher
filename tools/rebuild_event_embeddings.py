# -*- coding: utf-8 -*-
"""为已处理新闻补建本机语义向量；可重复运行，失败项下次续跑。"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collect.db import open_db
from process import embedding
from process.logging_util import make_log, setup_console_encoding

log = make_log(ROOT / "logs" / "embedding_backfill.log")


def main():
    setup_console_encoding()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--dry-run", action="store_true", help="只统计待生成数量，不加载模型")
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 256:
        parser.error("--batch-size 必须在 1 到 256 之间")

    from dashboard.service_runner import acquire_instance_lock

    lock = acquire_instance_lock("analysis_worker")
    if lock is None:
        log("处理服务仍在运行。请先在看板暂停处理，再重试向量回填。")
        return 2
    conn = open_db()
    try:
        rows = conn.execute("SELECT * FROM items WHERE stage='processed' ORDER BY id").fetchall()
        pending = 0
        for row in rows:
            item = dict(row)
            existing = conn.execute(
                "SELECT input_hash FROM item_embeddings WHERE item_id=? AND model_version=?",
                (int(item["id"]), embedding.MODEL_VERSION),
            ).fetchone()
            if existing is None or existing["input_hash"] != embedding.input_hash(item):
                pending += 1
        if args.dry_run:
            log(f"待回填 {pending} / 已处理 {len(rows)} 条，模型={embedding.MODEL_VERSION}")
            return 0

        def progress(done, failed, total):
            log(f"[向量回填] 完成 {done}/{total}，失败 {failed}")

        result = embedding.backfill(conn, batch_size=args.batch_size, progress=progress)
        log(f"[向量回填结果] 待处理={result['pending']} 完成={result['embedded']} 失败={result['failed']}")
        return 0 if result["failed"] == 0 else 1
    except Exception as exc:
        log(f"[向量回填失败] {type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
