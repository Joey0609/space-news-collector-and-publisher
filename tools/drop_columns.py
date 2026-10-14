# -*- coding: utf-8 -*-
"""航小麦 · 精简 items 表：删掉重复/无意义的列（默认只预演，不动数据）

用法：
    python tools/drop_columns.py                                  # 预演：看现在有哪些列、打算删哪些
    python tools/drop_columns.py --drop a,b,c --apply             # 真删（会先自动备份）
    python tools/drop_columns.py --db other.sqlite3 --dry-run      # 对指定库预演（测试用）

为什么用"重建表"而不是 ALTER TABLE DROP COLUMN：
    - 老 SQLite 不支持 DROP COLUMN；
    - 重建能顺手把列顺序、默认值整理干净，且只在保留列上做 INSERT ... SELECT，不会丢数据。
安全措施：
    - 默认 dry-run，只有显式 --apply 才写；
    - --apply 前自动 WAL checkpoint + 备份 data/hx.sqlite3.bak-<时间戳>；
    - 整个过程一个事务，出错自动回滚，失败不动原表。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# 建议删除的列（配合代码改动；分组便于只删一组）
LEGACY_EMPTY = ["published_ts", "published_date", "title", "url", "has_media"]   # 旧版遗留、全空
DUPLICATED = ["final_decision", "tweet_id", "labels", "score"]                   # 纯重复/纯派生
LEGACY_PROCESS = ["meta_attempts", "meta_error"]                                  # 已移除的独立元数据判定计数
SUGGESTED = LEGACY_EMPTY + DUPLICATED + LEGACY_PROCESS

# 唯一键所在列，绝不能删
PROTECTED = {"id", "dedup_key"}

TEMP_TABLE = "items_rebuilt"


def table_columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(items)")]


def table_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='items'").fetchone()
    return row[0] if row else ""


def keep_columns(cols: list[str], drop: set[str]) -> list[str]:
    return [c for c in cols if c not in drop]


def report(db: Path, drop: list[str]) -> tuple[list[str], list[str]]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cols = table_columns(conn)
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        print(f"库：{db}")
        print(f"当前 {len(cols)} 列，{total} 行")
        bad = [c for c in drop if c in PROTECTED]
        if bad:
            raise SystemExit(f"不能删受保护列：{bad}")
        missing = [c for c in drop if c not in cols]
        real = [c for c in drop if c in cols]
        print(f"要删（{len(real)}）：{', '.join(real) or '（无）'}")
        if missing:
            print(f"库里本来就没有：{', '.join(missing)}")
        print(f"删完剩 {len(cols) - len(real)} 列")
        print()
        print("各列现有数据量：")
        for name in real:
            n = conn.execute(
                f"SELECT COUNT(*) FROM items WHERE {name} IS NOT NULL "
                f"AND TRIM(COALESCE(CAST({name} AS TEXT), '')) NOT IN ('', '[]', '0')"
            ).fetchone()[0]
            flag = "  ← 全空，删了没有任何损失" if n == 0 else f"  ← 有 {n} 行有值，删了就没了"
            print(f"   {name:<18}{flag}")
        return real, keep_columns(cols, set(real))
    finally:
        conn.close()


def rebuild(db: Path, drop: list[str], backup: bool = True) -> None:
    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = db.with_name(f"{db.name}.bak-{stamp}")
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        shutil.copy2(db, target)
        print(f"[备份] {target}")

    conn = sqlite3.connect(db)
    try:
        cols = table_columns(conn)
        real = [c for c in drop if c in cols]
        if not real:
            print("[跳过] 没有要删的列")
            return
        keep = keep_columns(cols, set(real))
        schema = table_sql(conn)
        # 用原表定义生成新表定义：去掉要删的列 + 换表名
        new_schema = schema
        for name in real:
            new_schema = _strip_column(new_schema, name)
        new_schema = new_schema.replace("CREATE TABLE items", f"CREATE TABLE {TEMP_TABLE}", 1)
        new_schema = new_schema.replace('CREATE TABLE "items"', f'CREATE TABLE "{TEMP_TABLE}"', 1)

        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS {TEMP_TABLE}")
        conn.execute(new_schema)
        col_list = ", ".join(keep)
        conn.execute(f"INSERT INTO {TEMP_TABLE} ({col_list}) SELECT {col_list} FROM items")
        moved = conn.execute(f"SELECT COUNT(*) FROM {TEMP_TABLE}").fetchone()[0]
        conn.execute("DROP TABLE items")
        conn.execute(f"ALTER TABLE {TEMP_TABLE} RENAME TO items")
        conn.commit()
        conn.execute("VACUUM")
        cols_after = table_columns(conn)
        print(f"[完成] 搬了 {moved} 行；{len(cols)} 列 → {len(cols_after)} 列")
        print(f"[完成] 现在的列：{', '.join(cols_after)}")
    except Exception:
        conn.rollback()
        print("[失败] 已回滚，原表没动")
        raise
    finally:
        conn.close()


def _strip_column(schema: str, column: str) -> str:
    """从 CREATE TABLE 语句里删掉某一列的定义（按逗号分段，够用且不去碰别的列）。"""
    head, sep, body = schema.partition("(")
    if not sep:
        return schema
    inner, sep2, tail = body.rpartition(")")
    if not sep2:
        return schema
    parts = [p.strip() for p in inner.split(",")]
    kept = [p for p in parts if not _defines(p, column)]
    return head + "(" + ", ".join(kept) + ")" + tail


def _defines(part: str, column: str) -> bool:
    name = part.strip().split()[0] if part.strip() else ""
    return name.strip('"[]`') .lower() == column.lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="删掉 items 表里重复/无意义的列")
    parser.add_argument("--db", default=str(REPO / "data" / "hx.sqlite3"))
    parser.add_argument("--drop", default="", help="逗号分隔；留空用内置建议列表")
    parser.add_argument("--apply", action="store_true", help="真的改（默认只预演）")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"[错误] 找不到数据库：{db}")
        return 1
    drop = [x.strip() for x in args.drop.split(",") if x.strip()] or list(SUGGESTED)

    real, keep = report(db, drop)
    print()
    print(f"保留 {len(keep)} 列：{', '.join(keep)}")
    print()
    if not args.apply:
        print("（预演结束，什么都没改。确认无误后加 --apply 执行）")
        return 0
    rebuild(db, drop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
