"""Move legacy news image paths into the shared image library and relink items."""
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collect.image_store import ImageStore, sync_refs
from collect.logging_util import make_log

log = make_log(ROOT / "logs" / "image_dedup_reindex.log")


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def run():
    image_root = (ROOT / "data" / "images").resolve()
    store = ImageStore()
    with contextlib.closing(store.connect()) as conn:
        managed = {canonical(row[0]) for row in conn.execute("SELECT path FROM image_assets")}
        legacy = {}
        for item_id, raw in conn.execute("SELECT id,images FROM items"):
            try:
                images = json.loads(raw or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(images, list):
                continue
            for value in images:
                source = Path(str(value)).resolve()
                try:
                    source.relative_to(image_root)
                except ValueError:
                    continue
                key = canonical(source)
                if key not in managed and source.is_file():
                    legacy[key] = source

    replacements = {}
    for number, (key, source) in enumerate(sorted(legacy.items()), 1):
        target = store.store(source.read_bytes())
        replacements[key] = str(Path(target).resolve()) if target else None
        if number % 20 == 0 or number == len(legacy):
            log(f"历史图片入库进度 {number}/{len(legacy)}")

    affected = set()
    with contextlib.closing(store.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in conn.execute("SELECT id,images FROM items").fetchall():
            try:
                images = json.loads(row["images"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(images, list):
                continue
            updated, seen, changed = [], set(), False
            for value in images:
                key = canonical(value)
                target = replacements.get(key, value)
                if key in replacements:
                    changed = True
                    if target is None:
                        continue
                target = str(target)
                target_key = canonical(target)
                if target_key in seen:
                    changed = True
                    continue
                seen.add(target_key)
                updated.append(target)
            if changed:
                conn.execute("UPDATE items SET images=?,rev=rev+1 WHERE id=?",
                             (json.dumps(updated, ensure_ascii=False), row["id"]))
                sync_refs(conn, row["id"], updated)
                affected.add(row["id"])
        conn.commit()

    referenced = set()
    with contextlib.closing(store.connect()) as conn:
        for (raw,) in conn.execute("SELECT images FROM items"):
            try:
                images = json.loads(raw or "[]")
            except (TypeError, ValueError):
                continue
            if isinstance(images, list):
                referenced.update(canonical(value) for value in images)

    removed = 0
    for key, source in legacy.items():
        if key not in referenced and source.is_file():
            source.relative_to(image_root)
            source.unlink()
            removed += 1
    result = {"legacy_paths": len(legacy), "relinked_news": len(affected),
              "removed_legacy_files": removed,
              "filtered_images": sum(target is None for target in replacements.values())}
    log("历史图片关联完成：" + json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    run()
