"""Recompute image features, merge confirmed duplicate assets, and relink news rows."""
import contextlib
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collect.image_store import ImageStore, better_picture, features, picture_match
from collect.logging_util import make_log

log = make_log(ROOT / "logs" / "image_dedup_reindex.log")


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _metadata_changed(row, meta):
    return any((row[key] or 0) != (meta[key] or 0) for key in
               ("mime", "width", "height", "byte_size", "quality", "phash")) or \
        bytes(row["preview"] or b"") != bytes.fromhex(meta["preview"])


def _reindex(store, conn, rows):
    prepared = {}
    for number, row in enumerate(rows, 1):
        path = Path(row["path"])
        if not path.is_file():
            raise RuntimeError(f"图片资产文件缺失，停止重建：{path}")
        prepared[row["id"]] = features(path.read_bytes())
        if number % 100 == 0 or number == len(rows):
            log(f"特征重算进度 {number}/{len(rows)}")

    tables = (
        "image_signatures", "image_hash_buckets", "image_crop_signatures",
        "image_crop_buckets", "image_local_features",
    )
    conn.execute("BEGIN IMMEDIATE")
    for table in tables:
        conn.execute(f"DELETE FROM {table}")
    changed_assets = set()
    for row in rows:
        meta = prepared[row["id"]]
        if _metadata_changed(row, meta):
            changed_assets.add(row["id"])
            conn.execute("""UPDATE image_assets SET sha256=?,mime=?,width=?,height=?,byte_size=?,quality=?,
                          phash=?,preview=?,version=version+1,updated_at=? WHERE id=?""",
                         (meta["sha256"], meta["mime"], meta["width"], meta["height"], meta["byte_size"],
                          meta["quality"], meta["phash"], bytes.fromhex(meta["preview"]), time.time(), row["id"]))
        store._signature(conn, row["id"], meta)
    conn.commit()
    return prepared, changed_assets


def _candidate_pairs(conn, prepared):
    buckets = defaultdict(list)
    for band, value, phash, asset_id in conn.execute(
            "SELECT band,value,phash,asset_id FROM image_crop_buckets"):
        buckets[(band, value)].append((phash, asset_id))

    pairs = set()
    ids = sorted(prepared)
    for number, asset_id in enumerate(ids, 1):
        meta = prepared[asset_id]
        probes = meta.get("crop_hashes") or ([meta["phash"]] if meta.get("phash") else [])
        if not probes:
            continue
        votes = defaultdict(int)
        for probe in probes:
            for band, value in enumerate(bytes.fromhex(probe)):
                for stored_hash, other_id in buckets.get((band, value), ()):
                    if other_id != asset_id:
                        votes[(other_id, stored_hash)] += 1
        probe_ints = [int(value, 16) for value in probes]
        candidate_ids = {
            other_id for (other_id, stored_hash), count in votes.items()
            if count >= 2 and stored_hash and
            min((int(stored_hash, 16) ^ value).bit_count() for value in probe_ints)
            <= (20 if meta.get("alpha_channel") else 16)
        }
        pairs.update(tuple(sorted((asset_id, other_id))) for other_id in candidate_ids)
        if number % 200 == 0 or number == len(ids):
            log(f"候选检索进度 {number}/{len(ids)}")

    sha_groups = defaultdict(list)
    for asset_id, meta in prepared.items():
        sha_groups[meta["sha256"]].append(asset_id)
    for group in sha_groups.values():
        for index, asset_id in enumerate(group):
            for other_id in group[index + 1:]:
                pairs.add(tuple(sorted((asset_id, other_id))))
    return pairs


def _confirmed_pairs(pairs, prepared):
    confirmed = {}
    for number, (left, right) in enumerate(sorted(pairs), 1):
        a, b = prepared[left], prepared[right]
        if a["sha256"] == b["sha256"]:
            confirmed[(left, right)] = (10**9, 0.0)
        else:
            match = picture_match(a, b) or picture_match(b, a)
            if match:
                confirmed[(left, right)] = (int(match["inliers"]),
                                            float(match["new_area"] + match["old_area"]))
        if number % 200 == 0 or number == len(pairs):
            log(f"图像匹配进度 {number}/{len(pairs)}，已确认 {len(confirmed)} 对")
    return confirmed


def _complete_link_groups(ids, confirmed):
    owner = {asset_id: asset_id for asset_id in ids}
    groups = {asset_id: {asset_id} for asset_id in ids}
    edges = sorted(confirmed, key=lambda pair: confirmed[pair], reverse=True)
    for left, right in edges:
        group_left, group_right = owner[left], owner[right]
        if group_left == group_right:
            continue
        members_left, members_right = groups[group_left], groups[group_right]
        if not all(tuple(sorted((a, b))) in confirmed
                   for a in members_left for b in members_right):
            continue
        members_left.update(members_right)
        for asset_id in members_right:
            owner[asset_id] = group_left
        del groups[group_right]
    return [sorted(group) for group in groups.values() if len(group) > 1]


def _winner(group, rows_by_id, prepared):
    ordered = sorted(group, key=lambda ident: (rows_by_id[ident]["created_at"], ident))
    best = ordered[0]
    for asset_id in ordered[1:]:
        if better_picture(prepared[asset_id], prepared[best]):
            best = asset_id
    return best


def _merge(conn, groups, rows_by_id, prepared, changed_assets):
    replacements = {}
    for group in groups:
        best = _winner(group, rows_by_id, prepared)
        for asset_id in group:
            if asset_id != best:
                replacements[asset_id] = best
    paths = {asset_id: rows_by_id[asset_id]["path"] for asset_id in replacements}
    path_map = {canonical(paths[loser]): rows_by_id[winner]["path"]
                for loser, winner in replacements.items()}
    affected_ids = set()
    placeholders = ",".join("?" for _ in changed_assets | set(replacements))
    if placeholders:
        affected_ids.update(row[0] for row in conn.execute(
            f"SELECT DISTINCT item_id FROM image_refs WHERE asset_id IN ({placeholders})",
            tuple(changed_assets | set(replacements))))
    metadata_affected_ids = set(affected_ids)
    path_updated_ids = set()

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
            target = path_map.get(canonical(value), value)
            changed |= target != value
            key = canonical(target)
            if key in seen:
                changed = True
                continue
            seen.add(key)
            updated.append(target)
        if changed:
            conn.execute("UPDATE items SET images=?,rev=rev+1 WHERE id=?",
                         (json.dumps(updated, ensure_ascii=False), row["id"]))
            affected_ids.add(row["id"])
            path_updated_ids.add(row["id"])

    if affected_ids:
        marks = ",".join("?" for _ in affected_ids)
        conn.execute(f"DELETE FROM image_refs WHERE item_id IN ({marks})", tuple(affected_ids))
        path_to_id = {canonical(row["path"]): row["id"]
                      for row in conn.execute("SELECT id,path FROM image_assets")}
        for item in conn.execute(f"SELECT id,images FROM items WHERE id IN ({marks})", tuple(affected_ids)):
            try:
                images = json.loads(item["images"] or "[]")
            except (TypeError, ValueError):
                continue
            if isinstance(images, list):
                for value in images:
                    asset_id = path_to_id.get(canonical(value))
                    if asset_id:
                        conn.execute("INSERT OR IGNORE INTO image_refs(item_id,asset_id) VALUES(?,?)",
                                     (item["id"], asset_id))

    for loser, winner in replacements.items():
        signatures = conn.execute("SELECT sha256,phash FROM image_signatures WHERE asset_id=?",
                                  (loser,)).fetchall()
        conn.executemany("INSERT OR IGNORE INTO image_signatures(sha256,asset_id,phash) VALUES(?,?,?)",
                         [(row["sha256"], winner, row["phash"]) for row in signatures])
        for table, columns in (
            ("image_hash_buckets", "band,value,asset_id"),
            ("image_crop_signatures", "asset_id,phash"),
            ("image_crop_buckets", "band,value,phash,asset_id"),
        ):
            names = columns.split(",")
            data = conn.execute(f"SELECT {columns} FROM {table} WHERE asset_id=?", (loser,)).fetchall()
            if data:
                values = []
                for row in data:
                    entry = list(row)
                    entry[names.index("asset_id")] = winner
                    values.append(tuple(entry))
                conn.executemany(f"INSERT OR IGNORE INTO {table}({columns}) VALUES("+
                                 ",".join("?" for _ in names)+")", values)
        blocked = conn.execute("SELECT blacklisted_at FROM image_blacklist WHERE asset_id=?",
                               (loser,)).fetchone()
        if blocked:
            conn.execute("INSERT OR IGNORE INTO image_blacklist(asset_id,blacklisted_at) VALUES(?,?)",
                         (winner, blocked[0]))
        conn.execute("DELETE FROM image_local_features WHERE asset_id=?", (loser,))
        conn.execute("DELETE FROM image_blacklist WHERE asset_id=?", (loser,))
        conn.execute("DELETE FROM image_signatures WHERE asset_id=?", (loser,))
        conn.execute("DELETE FROM image_hash_buckets WHERE asset_id=?", (loser,))
        conn.execute("DELETE FROM image_crop_signatures WHERE asset_id=?", (loser,))
        conn.execute("DELETE FROM image_crop_buckets WHERE asset_id=?", (loser,))
        conn.execute("DELETE FROM image_refs WHERE asset_id=?", (loser,))
        conn.execute("DELETE FROM image_assets WHERE id=?", (loser,))

    metadata_only_ids = metadata_affected_ids - path_updated_ids
    if metadata_only_ids:
        marks = ",".join("?" for _ in metadata_only_ids)
        conn.execute(f"UPDATE items SET rev=rev+1 WHERE id IN ({marks})", tuple(metadata_only_ids))
    conn.commit()
    removed = 0
    for loser in replacements:
        path = Path(paths[loser]).resolve()
        path.relative_to((ROOT / "data" / "images" / "library").resolve())
        if path.exists():
            path.unlink()
            removed += 1
    return replacements, len(affected_ids), removed


def run():
    store = ImageStore()
    with store.locked(), contextlib.closing(store.connect()) as conn:
        rows = conn.execute("SELECT * FROM image_assets ORDER BY id").fetchall()
        if conn.execute("SELECT COUNT(*) FROM image_pending").fetchone()[0]:
            raise RuntimeError("图片库存在待恢复升级记录，停止重建")
        if not rows:
            log("图片库为空")
            return {"assets": 0, "duplicate_groups": 0, "merged_assets": 0, "updated_news": 0}
        log(f"开始重算 {len(rows)} 个资产的特征")
        prepared, changed_assets = _reindex(store, conn, rows)
        rows = conn.execute("SELECT * FROM image_assets ORDER BY id").fetchall()
        rows_by_id = {row["id"]: row for row in rows}
        candidate_pairs = _candidate_pairs(conn, prepared)
        confirmed = _confirmed_pairs(candidate_pairs, prepared)
        groups = _complete_link_groups(sorted(prepared), confirmed)
        # Refresh every item's image versions after a full-library reindex, including
        # when a previous interrupted pass already updated the asset metadata.
        merged, updated_news, removed_files = _merge(
            conn, groups, rows_by_id, prepared, set(prepared))
        dangling = conn.execute("SELECT COUNT(*) FROM image_refs r LEFT JOIN image_assets a "
                                "ON a.id=r.asset_id WHERE a.id IS NULL").fetchone()[0]
        if dangling:
            raise RuntimeError(f"清理后发现 {dangling} 条悬空图片引用")
        result = {"assets_scanned": len(prepared), "candidate_pairs": len(candidate_pairs),
                  "confirmed_pairs": len(confirmed), "duplicate_groups": len(groups),
                  "merged_assets": len(merged), "updated_news": updated_news,
                  "removed_files": removed_files, "updated_feature_assets": len(changed_assets),
                  "dangling_refs": dangling}
        log("图片去重重关联完成：" + json.dumps(result, ensure_ascii=False))
        return result


if __name__ == "__main__":
    try:
        print(json.dumps(run(), ensure_ascii=False))
    except Exception as exc:
        log(f"图片去重重关联失败：{type(exc).__name__}: {exc}")
        raise
