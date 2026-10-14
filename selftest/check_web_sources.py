"""新增网页来源离线验收：HTTP 全部使用内存替身，主库仅用临时库。"""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from collections import Counter
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from collect import db
from collect.sources._web.catalog import RSS_FEEDS
from collect.sources._web.collector import Collector
from collect.sources._web.parse import (CollectError, candidate, feed_page, fulltext_page,
                                        normalize_url, sina_page, snapi_page, timestamp)
from collect.sources._web.store import Store, snapshot
from collect.sources.sina.keywords import matches_title
from dashboard.service_runner import collection_groups, next_collect_due

BODY = "全部正文保留，包括数字 12345 和合同条件。" * 40
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS6kAAAAASUVORK5CYII=")


def page(text=BODY, extra=""):
    return f'<html><h1>新合同 deal</h1><article><p>{text}</p>{extra}</article></html>'


class ParsingTests(unittest.TestCase):
    def test_live_entrypoint_and_scoped_media_regressions(self):
        rows, _, _ = feed_page(b'<a class="container__link--type-article" href="/2026/10/14/tech/story">News</a>',
                               'https://edition.cnn.com/business/tech', 'CNN')
        self.assertEqual(len(rows), 1)
        with self.assertRaises(CollectError) as error:
            feed_page(b'<html><title>One moment, please...</title></html>', 'https://spacepolicyonline.com/feed/', 'SPO')
        self.assertEqual(error.exception.kind, 'BOT_CHALLENGE')
        article = fulltext_page('<article><div data-component="text-block"><p>Full text</p></div>'
                                '<div data-component="image-block"><img src="/body.jpg"></div></article>'
                                '<div data-component="image-block"><img src="/recommend.jpg"></div>',
                                'https://www.bbc.com/news/article')
        self.assertEqual(article.images, ['https://www.bbc.com/body.jpg'])
        article = fulltext_page('<div class="post-body"><p>Entire official article</p><img src="undefined"></div>',
                                'https://www.airandspaceforces.com/story/')
        self.assertEqual(article.body, 'Entire official article')
        self.assertFalse(article.images)
        article = fulltext_page('<article class="single-article"><div class="single-article__content-inner"><p>Actual body</p></div>'
                                '<figure class="single-article__cover"><img src="/cover.jpg"></figure>'
                                '<article class="post-item--feed"><p>Unrelated</p></article></article>', 'https://defensescoop.com/story')
        self.assertEqual(article.body, 'Actual body')
        self.assertEqual(article.images, ['https://defensescoop.com/cover.jpg'])

    def test_collection_inventory_counts_progress_and_legacy_schema(self):
        from collect.sources._web import store as module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state.sqlite3"
            initial = snapshot(path)
            self.assertFalse(path.exists())
            self.assertEqual(len(initial["sources"]), 21)
            self.assertTrue(all(row["state"] == "never" for row in initial["sources"]))
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE source_status(source TEXT PRIMARY KEY,label TEXT,last_run REAL,state TEXT,discovered INTEGER,collected INTEGER)")
                conn.execute("INSERT INTO source_status VALUES('snapi','API',1,'ok',2,1)")
                conn.commit()
            store = Store(path)
            store.status("sina", "新浪科技", "running", 0, 0)
            store.add("sina", candidate("a", "https://example.test/a", "卫星", "Sina"))
            store.add("sina", candidate("b", "https://example.test/b", "卫星", "Sina"))
            store.add("sina", candidate("c", "https://example.test/c", "卫星", "Sina"))
            store.success("sina", "a", 1)
            store.success("sina", "a", 1)  # 重复确认不能重复计数。
            store.failure("sina", "b", "BODY_NOT_FOUND", "未找到正文")
            store.notice("sina", "https://example.test/b", "fulltext", "BODY_NOT_FOUND", "未找到正文")
            row = next(row for row in snapshot(path)["sources"] if row["source"] == "sina")
            self.assertEqual((row["state"], row["collected"], row["discovered"]), ("running", 1, 3))
            self.assertEqual((row["stored_count"], row["pending_count"], row["failed_count"]), (1, 1, 1))
            self.assertEqual(row["last_error"]["detail"], "未找到正文")
            with patch.object(module, "process_alive", return_value=False):
                row = next(row for row in snapshot(path)["sources"] if row["source"] == "sina")
                self.assertEqual(row["state"], "interrupted")
            store.status("sina", "新浪科技", "error", None, None)
            row = next(row for row in snapshot(path)["sources"] if row["source"] == "sina")
            self.assertEqual(row["collected"], 1)
            news = root / "news.sqlite3"
            with closing(sqlite3.connect(news)) as conn:
                conn.execute("CREATE TABLE items(source TEXT)")
                conn.commit()
            row = next(row for row in snapshot(path, news_path=news)["sources"] if row["source"] == "sina")
            self.assertEqual(row["stored_count"], 0)  # 已删除新闻不计入库内总数。
            store.add("snapi", candidate("old", "https://example.test/old", "news", "API"))
            with closing(store.connect()) as conn:
                conn.execute("DELETE FROM source_status WHERE source='snapi'")
                conn.commit()
            row = next(row for row in snapshot(path)["sources"] if row["source"] == "snapi")
            self.assertEqual(row["state"], "unrecorded")

    def test_collection_unreadable_still_shows_all_sources(self):
        from dashboard import server
        with patch("collect.sources._web.store.snapshot", side_effect=sqlite3.DatabaseError("bad database")):
            data = server.state()["collection"]
        self.assertEqual(len(data["sources"]), 21)
        self.assertTrue(all(row["state"] == "error" and row["stored_count"] is None for row in data["sources"]))

    def test_sina_cleanup_backup_and_source_scope(self):
        from tools import clean_sina_keywords as cleaner
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main, state = root / "hx.sqlite3", root / "web_collect.sqlite3"
            with closing(sqlite3.connect(main)) as conn:
                conn.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,source TEXT,raw TEXT);"
                                   "CREATE TABLE processing_events(item_id INTEGER PRIMARY KEY);"
                                   "CREATE TABLE pushes(item_id INTEGER); CREATE TABLE push_attempts(item_id INTEGER);")
                conn.executemany("INSERT INTO items VALUES(?,?,?)", [(1, "sina", "手机发布\n\n正文提到卫星"),
                                                                      (2, "sina", "卫星发射\n正文"), (3, "x", "手机发布")])
                for table in ("processing_events", "pushes", "push_attempts"):
                    conn.execute(f"INSERT INTO {table} VALUES(1)")
                conn.commit()
            store = Store(state)
            store.add("sina", candidate("phone", "https://example.test/phone", "手机发布", "Sina"))
            store.success("sina", "phone", 1)
            store.add("sina", candidate("old", "https://example.test/old", "金融新闻", "Sina"))
            store.add("snapi", candidate("other", "https://example.test/other", "手机发布", "API"))
            with patch.object(cleaner, "ROOT", root), patch.object(cleaner, "LOG_DIR", root / "logs"):
                preview = cleaner.clean(main, state)
                self.assertEqual(preview["items_removed"], 1)
                with closing(sqlite3.connect(main)) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)
                result = cleaner.clean(main, state, apply=True)
            with closing(sqlite3.connect(main)) as conn:
                self.assertEqual([row[0] for row in conn.execute("SELECT id FROM items ORDER BY id")], [2, 3])
                for table in ("processing_events", "pushes", "push_attempts"):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            with closing(sqlite3.connect(Path(result["backup"]) / main.name)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)
            self.assertFalse(store.due("sina"))
            self.assertEqual(len(store.due("snapi")), 1)

    def test_sina_keyword_matching(self):
        for title in ("卫星互联网新进展", "蓝箭航天完成试验", "sPaCeX新计划", "ＳＰＡＣＥＸ新闻",
                      "Blue-Origin新合同", "NASA与ESA合作", "New satellites deployed",
                      "OSIRIS–REx sample", "Amazon Leo announcement", "SLS space launch"):
            self.assertTrue(matches_title(title), title)
        for title in ("Tesla推出新车", "discussion of business", "亚马逊购物节", "华为新手机",
                      "LEO手机", "发射新品回收旧手机", "办公空间设计", "", None):
            self.assertFalse(matches_title(title), title)

    def test_all_feeds_and_no_topic_filter(self):
        self.assertEqual(len(RSS_FEEDS), 19)
        self.assertEqual(len({row[0] for row in RSS_FEEDS}), 19)
        xml = '<rss><channel>' + ''.join(f'<item><guid>{i}</guid><title>航空 review {i}</title><link>https://example.test/{i}</link></item>' for i in range(40)) + '</channel></rss>'
        rows, _, errors = feed_page(xml, "https://example.test/feed", "Publisher")
        self.assertEqual(len(rows), 40)  # 不截前30条，也不筛标题。
        self.assertFalse(errors)
        rows, _, errors = feed_page('<rss><channel><item><guid>x</guid><title>no URL</title></item></channel></rss>', "https://example.test/feed", "Publisher")
        self.assertFalse(rows)
        self.assertEqual(errors[0][1], "MISSING_ARTICLE_IDENTITY")

    def test_atom_pagination_and_api_metadata(self):
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"><link rel="next" href="?page=2"/><entry><id>id1</id><title>science</title><link href="/news"/><published>2026-10-14T01:00:00Z</published></entry></feed>'
        rows, next_url, _ = feed_page(xml, "https://example.test/feed", "Publisher")
        self.assertEqual(rows[0]["identity"], "id1")
        self.assertEqual(rows[0]["url"], "https://example.test/news")
        self.assertEqual(next_url, "https://example.test/feed?page=2")
        rows, next_url, _ = snapi_page({"results": [{"id": 7, "url": "https://example.test/news", "title": "science", "summary": "仅摘要"}], "next": "?offset=100"}, "https://example.test/api")
        self.assertNotIn("summary", rows[0])
        self.assertTrue(next_url.endswith("offset=100"))
        rows, _, _ = sina_page({"result": {"data": [{"url": "https://example.test/science", "title": "非航天标题"}]}}, "https://example.test/sina?page=1")
        self.assertEqual(len(rows), 1)

    def test_full_body_short_notice_tables_and_images(self):
        article = fulltext_page(page(extra='<table><tr><td>表格数据</td></tr></table><figure><img src="/small.png" srcset="/small.png 100w, /large.png 1200w"><figcaption>图注</figcaption></figure>'), "https://example.test/news")
        self.assertIn(BODY, article.body)
        self.assertIn("表格数据", article.body)
        self.assertIn("图注", article.body)
        self.assertEqual(article.images, ["https://example.test/large.png"])
        self.assertEqual(fulltext_page(page("已入轨。"), "https://example.test/short").body, "已入轨。")
        for content in ('<html><meta property="og:description" content="summary"></html>', '<div class="paywall">subscribe</div>', page("Subscribe to continue")):
            with self.assertRaises(CollectError):
                fulltext_page(content, "https://example.test/news")

    def test_structured_full_body_and_identity(self):
        content = '<script type="application/ld+json">' + json.dumps({"@type": "NewsArticle", "articleBody": BODY}) + '</script>'
        self.assertEqual(fulltext_page(content, "https://example.test/news").body, BODY)
        self.assertEqual(normalize_url("https://example.test/a?utm_source=feed&id=5#part"), "https://example.test/a?id=5")
        self.assertEqual(timestamp("")[0], 0)
        self.assertGreater(timestamp("2026-10-14T01:00:00Z")[0], 0)
        self.assertEqual(timestamp("2026-10-14 01:00:00")[0], 0)

    def test_independent_schedule(self):
        self.assertEqual(collection_groups(["x", "rss", "snapi", "sina"]), {"x": ["x"], "web": ["rss", "snapi", "sina"]})
        self.assertEqual(next_collect_due("x", 0, 120, 600), 720)
        self.assertEqual(next_collect_due("web", 0, 120, 900), 900)
        self.assertEqual(next_collect_due("web", 0, 1900, 900), 2700)

    def test_slow_web_does_not_block_x_or_overlap(self):
        from dashboard import service_runner as runner
        clock, started, processes = [0], [], []
        ticks = iter([1, 601, 900, 951, 1202, 1800])
        class StopLoop(Exception):
            pass
        class Process:
            pid = 999999
            def __init__(self, command, **kwargs):
                self.group = "x" if command[-1] == "x" else "web"
                self.started = clock[0]
                self.stopped = False
                assert not any(other.group == self.group and other.poll() is None for other in processes)
                started.append((self.group, self.started))
                processes.append(self)
            def poll(self):
                duration = 950 if self.group == "web" else 0.5
                return 0 if self.stopped or clock[0] - self.started >= duration else None
            def terminate(self):
                self.stopped = True
            kill = terminate
            def wait(self, **kwargs):
                self.stopped = True
                return 0
        def sleep(_):
            try:
                clock[0] = next(ticks)
            except StopIteration:
                raise StopLoop()
        with patch.object(runner, "collection_groups", return_value={"x": ["x"], "web": ["rss", "snapi", "sina"]}), \
             patch.object(runner.time, "monotonic", side_effect=lambda: clock[0]), patch.object(runner.time, "sleep", side_effect=sleep), \
             patch.object(runner, "configured_interval", side_effect=lambda group: 900 if group == "web" else 600), \
             patch.object(runner.subprocess, "Popen", Process), patch.object(runner.subprocess, "run"):
            with self.assertRaises(StopLoop):
                runner.run_collection_loops()
        self.assertEqual([time for group, time in started if group == "web"], [0, 1800])
        self.assertEqual([time for group, time in started if group == "x"], [0, 601, 1800])
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_new_configuration_defaults_and_validation(self):
        from dashboard import server
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "ROOT", Path(directory)):
            config = server.service_config("collect")
            self.assertEqual(next(row["value"] for row in config["fields"] if row["key"] == "WEB_COLLECT_INTERVAL_SECONDS"), "900")
            with self.assertRaises(ValueError):
                server.save_service_config("collect", {"WEB_COLLECT_INTERVAL_SECONDS": "abc"})
            with self.assertRaises(ValueError):
                server.save_service_config("collect", {"SOURCES": "unknown"})
            server.save_service_config("collect", {"WEB_COLLECT_INTERVAL_SECONDS": "900", "SOURCES": ""})
            self.assertIn("WEB_COLLECT_INTERVAL_SECONDS=900", Path(directory, ".env").read_text(encoding="utf-8"))


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sina_filter_keeps_pagination_and_rejects_old_candidates(self):
        start = "https://example.test/sina?page=1"
        for index, title in ((1, "手机发布"), (2, "卫星发射")):
            data = {"result": {"data": [{"id": index, "url": f"https://example.test/{index}", "title": title}]}}
            self.responses[f"https://example.test/sina?page={index}"] = (200, "application/json", json.dumps(data).encode())
        self.responses["https://example.test/sina?page=3"] = (200, "application/json", b'{"result":{"data":[]}}')
        count, failed = await self.collector.discover("sina", "Sina", start, sina_page)
        self.assertEqual(count, 1)
        self.assertFalse(failed)
        self.assertEqual(self.calls["https://example.test/sina?page=3"], 1)
        self.assertEqual(self.store.due("sina")[0]["identity"], "2")
        self.add("sina", "old")
        old = next(row for row in self.store.due("sina") if row["identity"] == "old")
        self.assertIsNone(await self.collector.collect_candidate("sina", old))
        self.assertEqual(self.calls[old["url"]], 0)
        self.assertNotIn("old", [row["identity"] for row in self.store.due("sina")])

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db = db.DB_FILE
        db.DB_FILE = self.root / "news.sqlite3"
        self.store = Store(self.root / "state.sqlite3")
        self.calls = Counter()
        self.responses = {}
        self.logs = []
        def handler(request):
            url = str(request.url)
            self.calls[url] += 1
            response = self.responses[url]
            return httpx.Response(response[0], headers={"content-type": response[1]}, content=response[2])
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.collector = Collector(self.http, self.store, self.logs.append, self.root / "raw", self.root / "images")

    async def asyncTearDown(self):
        await self.http.aclose()
        db.DB_FILE = self.old_db
        self.temp.cleanup()

    def add(self, source, identity="id", url="https://example.test/news"):
        self.store.add(source, candidate(identity, url, "news", "Publisher"))

    async def test_success_once_cross_source_kept_and_processing_event(self):
        url = "https://example.test/news"
        self.responses[url] = (200, "text/html", page(extra='<img src="/image.png">').encode())
        self.responses["https://example.test/image.png"] = (200, "image/png", PNG)
        for source in ("rss:test", "snapi"):
            self.add(source)
            result = await self.collector.collect_candidate(source, self.store.due(source)[0])
            self.assertIn(BODY, result["raw"])
            self.assertTrue(Path(result["images"][0]).is_file())
            self.assertFalse(self.store.due(source))
            self.assertFalse(self.store.add(source, candidate("new-alias-id", url, "changed title", "Publisher")))
        with closing(db.open_db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM processing_events").fetchone()[0], 2)
            self.assertEqual({row[0] for row in conn.execute("SELECT stage FROM items")}, {"collected"})
        self.assertEqual(self.calls[url], 2)  # 每来源一次，成功后不检测文章更新。
        archives = list((self.root / "raw" / "web" / "articles").glob("*.json"))
        self.assertEqual(len(archives), 2)
        self.assertIn(BODY, json.loads(archives[0].read_text(encoding="utf-8"))["item"]["raw"])

    async def test_missing_body_warning_no_news_then_recover(self):
        url = "https://example.test/news"
        self.responses[url] = (200, "text/html", b'<meta property="og:description" content="summary">')
        self.store.add("sina", candidate("id", url, "卫星新闻", "Publisher"))
        self.assertIsNone(await self.collector.collect_candidate("sina", self.store.due("sina")[0]))
        with closing(db.open_db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM processing_events").fetchone()[0], 0)
        state = snapshot(self.store.path)
        self.assertEqual(state["failed_count"], 1)
        self.assertEqual(state["warnings"][0]["url"], url)
        self.assertEqual(state["warnings"][0]["error_type"], "BODY_NOT_FOUND")
        self.assertIn("WARNING", self.logs[-1])
        self.responses[url] = (200, "text/html", page().encode())
        with closing(self.store.connect()) as conn:
            conn.execute("UPDATE candidates SET next_try=0")
            conn.commit()
        result = await self.collector.collect_candidate("sina", self.store.due("sina")[0])
        self.assertIsNotNone(result)
        self.assertEqual(snapshot(self.store.path)["warnings"], [])

    async def test_http_error_and_image_failure(self):
        url = "https://example.test/news"
        self.responses[url] = (403, "text/html", b'blocked')
        self.add("rss:test")
        self.assertIsNone(await self.collector.collect_candidate("rss:test", self.store.due("rss:test")[0]))
        self.assertEqual(snapshot(self.store.path)["warnings"][0]["error_type"], "HTTP_ERROR")
        other = "https://example.test/other"
        self.responses[other] = (200, "text/html", page(extra='<img src="/bad.png">').encode())
        self.responses["https://example.test/bad.png"] = (404, "text/html", b'not image')
        self.add("rss:test", "other", other)
        item = await self.collector.collect_candidate("rss:test", self.store.due("rss:test")[0])
        self.assertIsNotNone(item)
        self.assertFalse(item["images"])
        self.assertTrue(any(row["error_type"] == "IMAGE_DOWNLOAD_ERROR" for row in snapshot(self.store.path)["warnings"]))

    async def test_all_pagination_resumes_after_budget(self):
        start = "https://example.test/api?page=1"
        for index in range(1, 7):
            data = {"results": [{"id": index, "title": "x", "url": f"https://example.test/news/{index}"}],
                    "next": f"https://example.test/api?page={index+1}" if index < 6 else None}
            self.responses[f"https://example.test/api?page={index}"] = (200, "application/json", json.dumps(data).encode())
        count, failed = await self.collector.discover("snapi", "API", start, snapi_page, page_budget=3)
        self.assertEqual(count, 3)
        self.assertFalse(failed)
        self.assertTrue(self.store.cursor("snapi").endswith("page=4"))
        await self.collector.discover("snapi", "API", start, snapi_page, page_budget=4)
        self.assertEqual(len(self.store.due("snapi")), 6)
        self.assertEqual(self.calls[start], 2)

    async def test_article_pagination_and_failed_tail_not_admitted(self):
        url = "https://example.test/news"
        self.responses[url] = (200, "text/html", page("第一页", '<link rel="next" href="/news?page=2">').encode())
        self.responses[url + "?page=2"] = (200, "text/html", page("第二页完整内容").encode())
        self.add("rss:test")
        item = await self.collector.collect_candidate("rss:test", self.store.due("rss:test")[0])
        self.assertIn("第一页", item["raw"])
        self.assertIn("第二页完整内容", item["raw"])
        self.add("snapi")
        self.responses[url + "?page=2"] = (500, "text/html", b'failed')
        self.assertIsNone(await self.collector.collect_candidate("snapi", self.store.due("snapi")[0]))
        with closing(db.open_db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    async def test_crash_recovery_does_not_modify_successful_news(self):
        source, url = "snapi", "https://example.test/news"
        self.responses[url] = (200, "text/html", page().encode())
        self.add(source)
        item = await self.collector.collect_candidate(source, self.store.due(source)[0])
        with closing(self.store.connect()) as conn:
            conn.execute("UPDATE candidates SET status='pending'")
            conn.commit()
        self.responses[url] = (200, "text/html", page("修改后的正文").encode())
        self.assertIsNone(await self.collector.collect_candidate(source, self.store.due(source)[0]))
        self.assertEqual(self.calls[url], 1)
        with closing(db.open_db()) as conn:
            self.assertEqual(conn.execute("SELECT raw FROM items WHERE id=?", (item["id"],)).fetchone()[0], item["raw"])

    async def test_listing_loop_error_isolated_and_dashboard_snapshot(self):
        from dashboard import server
        start = "https://example.test/api?page=1"
        data = {"results": [{"id": 1, "url": "https://example.test/news"}], "next": start}
        self.responses[start] = (200, "application/json", json.dumps(data).encode())
        _, failed = await self.collector.discover("snapi", "API", start, snapi_page)
        self.assertTrue(failed)
        self.assertEqual(snapshot(self.store.path)["warnings"][0]["error_type"], "PAGINATION_LOOP")
        self.store.status("snapi", "API", "warning", 1, 0)
        # 看板只读取测试账本；不启动看板自动处理，也不连接渠道。
        (self.root / "web_collect.sqlite3").write_bytes(b"")
        with patch.object(server, "STATE", self.root), patch.object(server, "item_stats", return_value=(0, 0, 0)), \
             patch.object(server, "queue_pending_count", return_value=0), patch.object(server.channel_hub, "list_channels", return_value=[]), \
             patch.object(server.channel_hub, "active_sources", return_value=[]), patch("collect.sources._web.store.snapshot", return_value=snapshot(self.store.path)):
            state = server.state()
            self.assertEqual(len(state["collection"]["sources"]), 21)
            self.assertEqual(next(row for row in state["collection"]["sources"] if row["source"] == "snapi")["state"], "warning")
            self.assertEqual(server.summary()["collection"]["warnings"][0]["url"], start)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    unittest.main(verbosity=2)
