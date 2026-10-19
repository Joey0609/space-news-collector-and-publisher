# -*- coding: utf-8 -*-
"""Run one local news item through BGE-M3 and time vector retrieval (no AI call)."""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collect.paths import DB_FILE, LOG_DIR
from process import embedding
from process.logging_util import make_log, setup_console_encoding

log = make_log(LOG_DIR / "embedding_benchmark.log")


def main():
    setup_console_encoding()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-id", type=int, help="指定数据库里的新闻 ID；默认选最近已处理的一条")
    args = parser.parse_args()

    # 只读连接：此体验程序不修改新闻、向量或处理状态。
    conn = sqlite3.connect(DB_FILE.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if args.item_id is None:
            row = conn.execute(
                "SELECT id,raw,quoted_raw,ai_title,stage FROM items "
                "WHERE stage='processed' AND TRIM(COALESCE(raw,''))<>'' "
                "ORDER BY COALESCE(ts,0) DESC,id DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id,raw,quoted_raw,ai_title,stage FROM items WHERE id=?",
                (args.item_id,),
            ).fetchone()
        if row is None or not str(row["raw"] or "").strip():
            raise RuntimeError("找不到有正文的新闻；可先不带参数运行，或检查指定的 --item-id。")

        item = dict(row)
        text = embedding.event_text(item)
        log(f"新闻 ID={item['id']}，标题={item['ai_title'] or '（无标题）'}，正文 {len(text)} 字符")
        log("正在加载本地 BGE-M3；模型未缓存时不会自动下载。")

        load_start = time.perf_counter()
        embedding._load_model(allow_download=False)
        load_seconds = time.perf_counter() - load_start

        encode_start = time.perf_counter()
        vector = embedding.encode_text(text)
        encode_seconds = time.perf_counter() - encode_start

        search_start = time.perf_counter()
        candidates = embedding.retrieve(conn, vector, exclude_item_id=item["id"], limit=5)
        search_seconds = time.perf_counter() - search_start

        log(f"模型加载：{load_seconds:.2f} 秒")
        log(f"单条向量计算：{encode_seconds:.2f} 秒")
        log(f"相似事件检索：{search_seconds:.3f} 秒")
        log(f"本次总耗时：{load_seconds + encode_seconds + search_seconds:.2f} 秒")
        log(f"匹配候选：{len(candidates)} 条")
        for candidate in candidates:
            log(f"候选：{candidate}")
        log("完成；没有调用 AI，也没有修改数据库内容。")
        return 0
    except Exception as exc:
        log(f"体验运行失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
