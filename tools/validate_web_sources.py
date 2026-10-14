"""真实源站验收，结果单独保存；不调用 AI，不连 QQ，不写生产新闻库。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect.sources._web.catalog import RSS_FEEDS, SNAPI_URL, SINA_URL
from collect.sources._web.collector import Collector, archive, client, error_kind
from collect.sources._web.parse import feed_page, snapi_page, sina_page
from collect.sources._web.store import Store
from collect.logging_util import make_log, setup_console_encoding
from collect import db


async def main(args):
    directory = ROOT / "data" / "validation" / args.run
    directory.mkdir(parents=True, exist_ok=True)
    log = make_log(ROOT / "logs" / (args.run + ".log"))
    rows = []
    gate = asyncio.Semaphore(4)
    sources = [("rss:" + key, label, url, feed_page) for key, label, url in RSS_FEEDS]
    sources += [("snapi", "Spaceflight News API", SNAPI_URL, snapi_page), ("sina", "新浪科技", SINA_URL, sina_page)]
    if args.sources:
        selected = set(args.sources.split(","))
        sources = [row for row in sources if row[0] in selected]
    db.DB_FILE = directory / "news.sqlite3"
    store = Store(directory / "state.sqlite3")
    async with client() as http:
        collector = Collector(http, store, log, directory / "raw", directory / "images")
        async def inspect(source, label, url, parser):
            async with gate:
                row = {"source": source, "label": label, "listing_url": url, "status": "pending", "checked_at": datetime.now().isoformat(), "articles": []}
                try:
                    response = await collector.get(url)
                    row["http"] = response.status_code
                    row["final_url"] = str(response.url)
                    body = response.content if parser == feed_page else response.json()
                    entries, next_url, problems = parser(body, str(response.url), label) if parser == feed_page else parser(body, str(response.url))
                    archive(directory / (source.replace(":", "_") + "_listing.json"), {"url": url, "response": response.text, "entries": entries, "next": next_url, "problems": problems})
                    row.update(discovered=len(entries), next=next_url, identity_errors=len(problems))
                    if not entries:
                        row["status"] = "empty"
                    elif args.discovery_only:
                        row["status"] = "listing_ok"
                        row["examples"] = entries[:3]
                    else:
                        for entry in entries[:args.samples]:
                            store.add(source, entry)
                        for pending in store.due(source)[:args.samples]:
                            payload = json.loads(pending["payload"])
                            item = await collector.collect_candidate(source, pending)
                            result = {"url": payload["url"], "title": payload["title"], "ok": item is not None}
                            if item:
                                result.update(chars=len(item["raw"]), images=len(item["images"]), item_id=item["id"])
                            else:
                                with store.connect() as conn:
                                    failure = conn.execute("SELECT error_type,detail FROM candidates WHERE source=? AND identity=?", (source, pending["identity"])).fetchone()
                                result.update(dict(failure))
                            row["articles"].append(result)
                        row["status"] = "ok" if all(item["ok"] for item in row["articles"]) else "partial" if any(item["ok"] for item in row["articles"]) else "failed"
                except Exception as exc:
                    kind, detail = error_kind(exc)
                    row.update(status="failed", error_type=kind, detail=detail)
                rows.append(row)
                archive(directory / "results.json", sorted(rows, key=lambda value: value["source"]))
                log(f"[验证] {source} {row['status']} HTTP={row.get('http', '-')} 候选={row.get('discovered', 0)} 正文成功={sum(item['ok'] for item in row['articles'])}")
        await asyncio.gather(*(inspect(*row) for row in sources))
    print(f"RESULTS={directory / 'results.json'}", flush=True)


if __name__ == "__main__":
    setup_console_encoding()
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default="web_live_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--sources")
    asyncio.run(main(parser.parse_args()))
