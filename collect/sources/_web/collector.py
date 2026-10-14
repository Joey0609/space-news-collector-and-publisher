"""发现候选→全文/图片→SQLite；不调用 AI，不修改分发状态。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import base64
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from collect import db
from collect.env import PROXY_URL
from collect.logging_util import make_log
from collect.paths import RAW_DIR, IMAGE_DIR, LOG_DIR
from ..contract import CollectResult
from .parse import CollectError, digest, fulltext_page, normalize_url, timestamp
from .store import Store
from ..sina.keywords import accepts_candidate

web_log = make_log(LOG_DIR / "collect_web.log", echo=False)


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def archive(path, payload):
    atomic_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def error_kind(exc):
    if isinstance(exc, CollectError):
        return exc.kind, str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT", "请求超时"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return ("RATE_LIMITED" if status == 429 else "LOGIN_REQUIRED" if status == 401 else "HTTP_ERROR"), f"HTTP {status}"
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.DecodingError)):
        return "INCOMPLETE_RESPONSE", type(exc).__name__
    if isinstance(exc, httpx.ConnectError) and any(token in str(exc).lower() for token in ("ssl", "certificate", "tls")):
        return "TLS_ERROR", "TLS 连接验证失败"
    if isinstance(exc, httpx.RequestError):
        return "CONNECT_ERROR", type(exc).__name__
    if isinstance(exc, (ValueError, TypeError)):
        return "API_SCHEMA_ERROR", type(exc).__name__
    return "COLLECT_ERROR", type(exc).__name__


class Collector:
    def __init__(self, client, store=None, log=print, raw_dir=None, image_dir=None, writer=None):
        self.client = client
        self.store = store or Store()
        self.log = log
        self.raw_dir = Path(raw_dir or RAW_DIR) / "web"
        self.image_dir = Path(image_dir or IMAGE_DIR) / "web"
        self.writer = writer or db.upsert_raw
        self.semaphore = asyncio.Semaphore(4)

    def message(self, text):
        web_log(text)
        self.log(text)

    def warn(self, source, url, stage, kind, detail):
        # 只记录可审计的必要原因；HTTP 库异常不打印代理/凭据。
        message = f"WARNING source={source} stage={stage} type={kind} url={url} detail={detail}"
        self.message(message)
        try:
            self.store.notice(source, url, stage, kind, detail)
        except Exception:
            self.log(f"WARNING source={source} stage=state type=DB_WRITE_ERROR url={url} detail=告警账本写入失败")

    async def get(self, url):
        normalize_url(url)
        self.message(f"[网页请求] {url}")
        response = await self.client.get(url)
        if response.status_code in {403, 503} and b"text/html" in response.headers.get("content-type", "").encode():
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.content, "html.parser")
            title = soup.title.get_text().lower() if soup.title else ""
            if any(word in title for word in ("just a moment", "one moment", "verify you are human", "captcha")) or soup.select_one("#challenge-running, #cf-challenge-running, form#challenge-form"):
                raise CollectError("BOT_CHALLENGE", f"HTTP {response.status_code} 返回访问验证页")
        response.raise_for_status()
        if len(response.content) > 32 * 1024 * 1024:
            raise CollectError("INCOMPLETE_RESPONSE", "响应超过处理容量，拒绝截断后入池")
        return response

    async def discover(self, source, label, start, parser, page_budget=4):
        """每轮从最新页发现，同时恢复历史公开分页；预算只延迟，不永久丢页。"""
        stored_cursor = self.store.cursor(source)
        current, visited, fingerprints = start, set(), set()
        discovered, failed = 0, False
        for index in range(page_budget):
            if not current or current in visited:
                if current:
                    self.warn(source, current, "listing", "PAGINATION_LOOP", "分页链接循环")
                    failed = True
                self.store.cursor(source, "")
                break
            visited.add(current)
            try:
                response = await self.get(current)
                payload = response.content if parser.__name__ == "feed_page" else response.json()
                if parser.__name__ == "feed_page":
                    entries, next_url, problems = parser(payload, str(response.url), label)
                else:
                    entries, next_url, problems = parser(payload, str(response.url))
                fingerprint = digest(json.dumps([(row["identity"], row["url"]) for row in entries]))
                if entries and fingerprint in fingerprints:
                    raise CollectError("PAGINATION_LOOP", "接口重复返回同一页，停止此轮并保留重试")
                fingerprints.add(fingerprint)
                archive(self.raw_dir / "listings" / digest(source) / (digest(current) + ".json"),
                        {"source": source, "url": current, "received_at": time.time(),
                         "response": response.text, "next": next_url})
                for url, kind, detail in problems:
                    self.warn(source, url, "identity", kind, detail)
                for row in entries:
                    if not accepts_candidate(source, row):
                        continue
                    discovered += int(self.store.add(source, row))
                self.store.clear_notice(source, current, "listing")
                # 最新页与回收进度分开；每轮先看最新，再继续未完成的分页。
                current = stored_cursor if index == 0 and stored_cursor and stored_cursor != start else next_url
                stored_cursor = ""
                self.store.cursor(source, current or "")
            except Exception as exc:
                kind, detail = error_kind(exc)
                self.warn(source, current, "listing", kind, detail)
                if current != start:
                    self.store.cursor(source, current)
                failed = True
                break
        return discovered, failed

    async def image(self, source, url):
        try:
            response = await self.get(url)
            mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                      "image/webp": ".webp", "image/avif": ".avif", "image/svg+xml": ".svg"}.get(mime)
            data = response.content
            signatures = {".jpg": data.startswith(b"\xff\xd8\xff"), ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                          ".gif": data.startswith((b"GIF87a", b"GIF89a")), ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
                          ".avif": b"ftyp" in data[:16], ".svg": b"<svg" in data[:1024]}
            if not suffix or not signatures.get(suffix):
                raise CollectError("IMAGE_DOWNLOAD_ERROR", "响应不是有效图片")
            target = self.image_dir / (digest(url) + suffix)
            atomic_bytes(target, response.content)
            self.store.clear_notice(source, url, "image")
            return str(target)
        except Exception as exc:
            kind, detail = error_kind(exc)
            self.warn(source, url, "image", "IMAGE_DOWNLOAD_ERROR", f"{kind}: {detail}")
            return ""

    async def collect_candidate(self, source, row):
        async with self.semaphore:
            payload = json.loads(row["payload"])
            if not accepts_candidate(source, payload):
                self.store.exclude(source, row["identity"])
                self.message(f"[标题预筛] {source} 跳过未命中关键词的旧候选")
                return None
            url, visited, pages, image_urls = payload["url"], set(), [], []
            current = url
            try:
                # 崩溃发生在主库写入后/账本确认前时，恢复成功而不改写已入库正文。
                origin_id = source + ":" + digest(payload["identity"])
                if self.writer == db.upsert_raw:
                    with closing(db.open_db()) as conn:
                        existing = conn.execute("SELECT id FROM items WHERE dedup_key=?", (f"web:{origin_id}",)).fetchone()
                    if existing:
                        self.store.success(source, row["identity"], existing[0])
                        return None
                while current:
                    if current in visited:
                        raise CollectError("INCOMPLETE_RESPONSE", "正文分页循环，不能确认完整文章")
                    visited.add(current)
                    response = await self.get(current)
                    content_type = response.headers.get("content-type", "").lower()
                    if "html" not in content_type:
                        raise CollectError("BODY_PARSE_ERROR", "原文不是 HTML 文章页面")
                    part = fulltext_page(response.content, str(response.url))
                    pages.append({"url": str(response.url), "html": response.text,
                                  "original_bytes_base64": base64.b64encode(response.content).decode("ascii"),
                                  "body": part.body, "extractor": part.extractor})
                    image_urls.extend(part.images)
                    if not payload.get("title") and part.title:
                        payload["title"] = part.title
                    next_url = part.next_url
                    if next_url and urlsplit(next_url).hostname != urlsplit(url).hostname:
                        raise CollectError("INCOMPLETE_RESPONSE", "正文下一页跨站，无法确认文章完整性")
                    current = next_url
                body = "\n\n".join(page["body"] for page in pages)
                # 正文图片优先；订阅缩略图只在正文没有图片时兜底。
                if not image_urls:
                    image_urls.extend(payload.get("images", []))
                paths = []
                for image_url in dict.fromkeys(image_urls):
                    path = await self.image(source, image_url)
                    if path:
                        paths.append(path)
                ts, date = timestamp(payload.get("published"))
                raw = (payload.get("title", "") + "\n\n" + body).strip()
                item = {"platform": "web", "source": source, "origin_id": origin_id,
                        "account": payload.get("publisher") or source, "raw": raw,
                        "content_type": "文章", "ts": ts, "date": date,
                        "ori_url": url, "images": paths}
                archive(self.raw_dir / "articles" / (digest(origin_id) + ".json"),
                        {"source": source, "candidate": payload, "pages": pages,
                         "image_urls": list(dict.fromkeys(image_urls)), "item": item,
                         "received_at": time.time()})
                try:
                    item["id"] = self.writer(item)
                except Exception as exc:
                    raise CollectError("DB_WRITE_ERROR", type(exc).__name__) from exc
                self.store.success(source, row["identity"], item["id"])
                self.message(f"[网页入库] {source} item_id={item['id']} 正文 {len(raw)} 字 图片 {len(paths)}")
                return item
            except Exception as exc:
                kind, detail = error_kind(exc)
                self.store.failure(source, row["identity"], kind, detail)
                self.warn(source, url, "fulltext", kind, detail)
                return None

    async def source(self, source, label, start, parser):
        self.message(f"[网页来源] {source} {label} 开始 {start}")
        try:
            self.store.status(source, label, "running", 0, 0)
            discovered, listing_failed = await self.discover(source, label, start, parser)
            pending = self.store.due(source)
            items = []
            # 分批调度协程，待回收列表大时也不创建无限任务。
            for offset in range(0, len(pending), 16):
                results = await asyncio.gather(*(self.collect_candidate(source, row) for row in pending[offset:offset + 16]))
                items.extend(row for row in results if row is not None)
            # 已退避失败也应统计，不仅统计立即到期的失败。
            with closing(self.store.connect()) as conn:
                failed = conn.execute("SELECT COUNT(*) FROM candidates WHERE source=? AND status='failed'", (source,)).fetchone()[0]
            self.store.status(source, label, "warning" if failed or listing_failed else "ok", discovered, len(items))
            return CollectResult(source=source, items=items, scanned=len(pending), failed=failed + int(listing_failed))
        except Exception as exc:
            kind, detail = error_kind(exc)
            self.warn(source, start, "source", kind, detail)
            try:
                self.store.status(source, label, "error", None, None)
            except Exception:
                pass
            return CollectResult(source=source, failed=1, error=detail)


def client():
    return httpx.AsyncClient(proxy=PROXY_URL or None, trust_env=False, timeout=30, follow_redirects=True,
                             headers={"User-Agent": "78UMa/1.0 (Space news collector)", "Accept": "*/*"})
