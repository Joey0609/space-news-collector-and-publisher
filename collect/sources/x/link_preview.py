# -*- coding: utf-8 -*-
"""从 X 帖文中的 t.co 短链抓取网页正文，作为引用附在原文后。"""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from collect.env import PROXY_URL
from collect.paths import image_path

MAX_LINKS = 5
MAX_ARTICLE_IMAGES = 8
MAX_IMAGES_PER_POST = 12
MAX_HTML_BYTES = 2_000_000
MAX_IMAGE_BYTES = 15_000_000
MAX_TEXT_CHARS = 12_000
REQUEST_TIMEOUT = 10
TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+", re.IGNORECASE)


def _validate_public_url(url: str) -> None:
    """只允许 HTTP(S) 公网目标，避免帖子链接访问本机或内网地址。"""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("只支持 HTTP(S) 链接")
    if parsed.username or parsed.password:
        raise ValueError("不允许带账号密码的链接")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"域名解析失败：{exc}") from exc
    if not addresses:
        raise ValueError("域名没有解析到地址")
    # 某些 DNS 会给常见双栈站点返回不可路由的占位 AAAA（但有有效公网 A）；
    # 使用 IPv4 时只核验 A 记录，纯 IPv6 站点则核验其 AAAA 记录。
    ipv4 = [address for address in addresses if address[0] == socket.AF_INET]
    selected = ipv4 or [address for address in addresses if address[0] == socket.AF_INET6]
    if not selected:
        raise ValueError("域名没有解析到 IPv4/IPv6 地址")
    for address in selected:
        ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("链接解析到非公网地址，已拒绝")


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        _validate_public_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


class _ArticleParser(HTMLParser):
    """优先读取 article/main 中的段落；站点结构不同则回退到全页段落。"""

    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.article_depth = 0
        self.main_depth = 0
        self.skip_depth = 0
        self.capture_depth = 0
        self.current = []
        self.article_paragraphs = []
        self.main_paragraphs = []
        self.all_paragraphs = []
        self.meta_description = ""
        self.image_candidates = []
        self.title_depth = 0
        self.title = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article":
            self.article_depth += 1
        elif tag == "main":
            self.main_depth += 1
        if tag in self._SKIP:
            self.skip_depth += 1
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in ("og:description", "description") and not self.meta_description:
                self.meta_description = attrs.get("content", "").strip()
            if key in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
                self._add_image(attrs.get("content", ""), 2_500)
        if tag in ("img", "source"):
            for key in ("data-original", "data-src", "data-lazy-src", "src"):
                self._add_image(attrs.get(key, ""), 0)
            for key in ("data-srcset", "srcset"):
                for candidate in attrs.get(key, "").split(","):
                    fields = candidate.strip().split()
                    if not fields:
                        continue
                    descriptor = fields[1] if len(fields) > 1 else "1x"
                    match = re.match(r"(\d+(?:\.\d+)?)(w|x)$", descriptor)
                    score = float(match.group(1)) if match else 1
                    self._add_image(fields[0], score)
        if tag == "title":
            self.title_depth += 1
        if tag == "p" and self.skip_depth == 0:
            self.capture_depth += 1
            if self.capture_depth == 1:
                self.current = []

    def handle_endtag(self, tag):
        if tag == "p" and self.capture_depth:
            self.capture_depth -= 1
            if self.capture_depth == 0:
                text = " ".join("".join(self.current).split())
                if text:
                    self.all_paragraphs.append(text)
                    if self.article_depth:
                        self.article_paragraphs.append(text)
                    if self.main_depth:
                        self.main_paragraphs.append(text)
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag in self._SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag == "article" and self.article_depth:
            self.article_depth -= 1
        elif tag == "main" and self.main_depth:
            self.main_depth -= 1

    def handle_data(self, data):
        if self.capture_depth and not self.skip_depth:
            self.current.append(data)
        if self.title_depth:
            self.title.append(data)

    def _add_image(self, url: str, score: float) -> None:
        url = str(url or "").strip()
        if url and not url.startswith("data:"):
            self.image_candidates.append((url, score))

    def get_text(self) -> str:
        paragraphs = self.article_paragraphs or self.main_paragraphs or self.all_paragraphs
        text = "\n\n".join(paragraphs).strip()
        if not text:
            text = self.meta_description or " ".join("".join(self.title).split())
        return text[:MAX_TEXT_CHARS].strip()

    def get_image_urls(self, base_url: str) -> list[str]:
        ordered = sorted(self.image_candidates, key=lambda pair: pair[1], reverse=True)
        urls = []
        for value, _score in ordered:
            absolute = urljoin(base_url, value)
            parsed = urlsplit(absolute)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                continue
            if parsed.path.lower().endswith(".svg"):
                continue
            if absolute not in urls:
                urls.append(absolute)
        return urls[:MAX_ARTICLE_IMAGES]


def _build_opener():
    handlers = [_PublicRedirectHandler()]
    if PROXY_URL:
        handlers.insert(0, urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
    return urllib.request.build_opener(*handlers)


def fetch_article_data(url: str) -> dict:
    """直接请求网页，提取正文、候选大图和最终页面信息。"""
    opener = _build_opener()
    current_url = url
    for _ in range(6):
        _validate_public_url(current_url)
        request = urllib.request.Request(current_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6",
        })
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                body = response.read(MAX_HTML_BYTES + 1)
                if len(body) > MAX_HTML_BYTES:
                    body = body[:MAX_HTML_BYTES]
                charset = response.headers.get_content_charset() or "utf-8"
                html = body.decode(charset, errors="replace") if "html" in content_type else ""
                status = response.status
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            raise ValueError(f"网页返回 HTTP {exc.code}") from exc

        next_url = _extract_client_redirect(html, current_url)
        if not next_url or next_url == current_url:
            if not html:
                is_image = content_type.split(";", 1)[0].strip() in {
                    "image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
                return {
                    "text": "", "images": [final_url] if is_image else [],
                    "status": status, "final_url": final_url, "content_type": content_type,
                }
            parser = _ArticleParser()
            parser.feed(html)
            return {
                "text": parser.get_text(),
                "images": parser.get_image_urls(final_url),
                "status": status,
                "final_url": final_url,
                "content_type": content_type,
            }
        current_url = next_url
    raise ValueError("网页客户端跳转次数过多")


def fetch_article_text(url: str) -> str:
    """直接以 Python urllib 请求短链并提取通用网页正文，不调用内容 API。"""
    return fetch_article_data(url)["text"]


def _extract_client_redirect(html: str, base_url: str) -> str:
    """识别短链页常见的 meta refresh 与 JavaScript location 跳转。"""
    normalized = html.replace("\\/", "/")
    meta = re.search(
        r"(?is)<meta\b[^>]*http-equiv\s*=\s*['\"]?refresh['\"]?[^>]*content\s*=\s*['\"][^'\"]*?url\s*=\s*([^; '\"]+)",
        normalized,
    )
    if not meta:
        meta = re.search(
            r"(?is)<meta\b[^>]*content\s*=\s*['\"][^'\"]*?url\s*=\s*([^; '\"]+)[^>]*http-equiv\s*=\s*['\"]?refresh",
            normalized,
        )
    script = re.search(
        r"(?is)(?:location\.(?:replace|assign)|window\.location\.replace)\s*\(\s*['\"](https?://[^'\"]+)",
        normalized,
    )
    if not script:
        script = re.search(
            r"(?is)(?:window\.)?location(?:\.href)?\s*=\s*['\"](https?://[^'\"]+)",
            normalized,
        )
    target = (meta.group(1) if meta else script.group(1) if script else "").strip()
    if not target:
        return ""
    return urljoin(base_url, target)


def _download_article_image(url: str, tweet_id: str, index: int, day: str, log) -> str:
    opener = _build_opener()
    try:
        _validate_public_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            suffix = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                "image/gif": ".gif", "image/avif": ".avif",
            }.get(content_type)
            if not suffix:
                raise ValueError(f"非支持的图片类型：{content_type or '未知'}")
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("图片超过 15 MB 限制")
        path = image_path(f"{tweet_id}_link_{index}{suffix}", day)
        from collect.image_store import store_image_bytes
        stored = store_image_bytes(data, path)
        if not stored: return ''
        log(f"[引用图] 下载成功：{path.name}")
        return stored
    except Exception as exc:
        log(f"[引用图] 下载失败：{url}：{exc}")
        return ""


def append_tco_references(raw: str, log=print, *, tweet_id="", day="", images=None) -> str:
    """把帖子中的 t.co 正文作为引用追加，并把网页大图下载到条目附件。"""
    raw = str(raw or "")
    links = list(dict.fromkeys(TCO_RE.findall(raw)))[:MAX_LINKS]
    if not links:
        return raw

    references = []
    seen_image_urls = set()
    for link in links:
        try:
            result = fetch_article_data(link)
            content = result["text"]
            if not content:
                log(f"[短链] 未提取到网页正文：{link}")
            else:
                references.append(f"引用{len(references) + 1}：（链接：{link}）【{content}】")
                log(f"[短链] 已获取引用正文：{link}（{len(content)} 字）")
            if images is not None and tweet_id:
                for image_url in result["images"]:
                    if len(images) >= MAX_IMAGES_PER_POST:
                        log(f"[引用图] 单条推文附件图已达上限 {MAX_IMAGES_PER_POST} 张")
                        break
                    if image_url in seen_image_urls:
                        continue
                    seen_image_urls.add(image_url)
                    path = _download_article_image(
                        image_url, str(tweet_id), len(images), day, log)
                    if path:
                        images.append(path)
        except Exception as exc:  # 单个外链失败不能影响主帖入库
            log(f"[短链] 获取失败：{link}：{exc}")
    if not references:
        return raw
    return raw.rstrip() + "\n\n" + "\n".join(references)
