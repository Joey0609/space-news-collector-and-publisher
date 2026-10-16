"""离线可测的订阅、API 和全文解析；从不做主题筛选。"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
from .catalog import BODY_SELECTORS


class CollectError(Exception):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind


def normalize_url(url, base=""):
    if not str(url or "").strip():
        raise CollectError("MISSING_ARTICLE_IDENTITY", "缺少原文链接")
    parsed = urlsplit(urljoin(base, str(url or "").strip()))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise CollectError("MISSING_ARTICLE_IDENTITY", "没有可访问的 HTTP 原文地址")
    # 只删除确定的跟踪参数，不把查询参数全删掉。
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(query), ""))


def candidate(identity, url, title, publisher, published="", images=(), base=""):
    url = normalize_url(url, base)
    return {"identity": str(identity or url), "url": url, "title": str(title or "").strip(),
            "publisher": str(publisher or "").strip(), "published": str(published or ""),
            "images": list(images)}


def localname(tag):
    return tag.rsplit("}", 1)[-1].lower()


def feed_page(content, base, publisher):
    raw_bytes = content if isinstance(content, bytes) else content.encode()
    if re.search(rb"<title>[^<]*(?:moment|checking|captcha)", raw_bytes, re.I):
        raise CollectError("BOT_CHALLENGE", "订阅入口返回浏览器访问验证页")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        if re.search(rb"<title>[^<]*(?:moment|Moment|checking|Checking)", content if isinstance(content, bytes) else content.encode()):
            raise CollectError("BOT_CHALLENGE", "订阅入口返回浏览器访问验证页") from exc
        raise CollectError("FEED_INVALID", str(exc)) from exc
    if localname(root.tag) not in {"rss", "feed", "rdf"}:
        raise CollectError("FEED_INVALID", "响应不是 RSS/Atom")
    found, problems = [], []
    next_url = ""
    for container in [root] + [node for node in root if localname(node.tag) == "channel"]:
        for node in container:
            if localname(node.tag) == "link" and node.get("rel") == "next" and node.get("href"):
                next_url = urljoin(base, node.get("href"))
    for entry in root.iter():
        if localname(entry.tag) not in {"item", "entry"}:
            continue
        fields, images = {}, []
        url = ""
        for child in entry:
            name = localname(child.tag)
            value = "".join(child.itertext()).strip()
            fields.setdefault(name, value)
            if name == "link" and child.get("rel", "alternate") == "alternate":
                url = child.get("href") or value
            if name == "guid" and child.get("isPermaLink", "true") == "true" and not url and value.startswith(("http://", "https://")):
                url = value
            if name in {"content", "thumbnail", "enclosure"} and child.get("url"):
                if name != "enclosure" or child.get("type", "").startswith("image/"):
                    images.append(urljoin(base, child.get("url")))
        for child in entry.iter():
            if localname(child.tag) in {"content", "thumbnail"} and child.get("url"):
                if child.get("medium", "image") == "image":
                    images.append(urljoin(base, child.get("url")))
        try:
            found.append(candidate(fields.get("guid") or fields.get("id"), url,
                                   fields.get("title"), fields.get("author") or publisher,
                                   fields.get("pubdate") or fields.get("published") or fields.get("updated"), images, base))
        except CollectError as exc:
            problems.append((url or base, exc.kind, str(exc)))
    return found, next_url, problems


def snapi_page(data, base):
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise CollectError("API_SCHEMA_ERROR", "API 缺少 results 数组")
    found, problems = [], []
    for row in data["results"]:
        if not isinstance(row, dict):
            problems.append((base, "API_SCHEMA_ERROR", "文章对象格式错误"))
            continue
        try:
            found.append(candidate(row.get("id"), row.get("url"), row.get("title"),
                                   row.get("news_site") or "Spaceflight News API", row.get("published_at"),
                                   [row["image_url"]] if row.get("image_url") else []))
        except CollectError as exc:
            problems.append((row.get("url") or base, exc.kind, str(exc)))
    return found, urljoin(base, data["next"]) if data.get("next") else "", problems


def sina_page(data, base):
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise CollectError("API_SCHEMA_ERROR", "新浪 API 缺少 result.data 数组")
    if result.get("status", {}).get("code", 0) not in (0, "0"):
        raise CollectError("API_SCHEMA_ERROR", "新浪 API 返回错误状态")
    found, problems = [], []
    for row in result["data"]:
        try:
            # API 身份字段不可靠时以原文 URL 为稳定身份；不按标题筛选。
            found.append(candidate(row.get("id") or row.get("docid"), row.get("url") or row.get("link"),
                                   row.get("title"), row.get("media_name") or "新浪科技",
                                   row.get("ctime") or row.get("mtime") or ""))
        except (CollectError, AttributeError) as exc:
            problems.append((base, getattr(exc, "kind", "API_SCHEMA_ERROR"), str(exc)))
    fields = dict(parse_qsl(urlsplit(base).query))
    fields["page"] = str(int(fields.get("page", "1")) + 1)
    parsed = urlsplit(base)
    next_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(fields), "")) if result["data"] else ""
    return found, next_url, problems


def timestamp(value):
    if not value:
        return 0, ""
    try:
        text = str(value)
        if text.isdigit():
            dt = datetime.fromtimestamp(int(text), timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                # 无时区的数据不能假装具有准确 Unix 发布时间。
                return 0, text
        return int(dt.timestamp()), dt.isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return 0, str(value)


@dataclass
class Article:
    title: str
    body: str
    images: list[str]
    next_url: str = ""
    extractor: str = ""


def _json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)


def fulltext_page(content, base):
    soup = BeautifulSoup(content, "html.parser")
    for selector, kind in ((".g-recaptcha, #challenge-running, #cf-challenge-running, form#challenge-form", "BOT_CHALLENGE"),
                           ('.paywall, #paywall, [data-testid="paywall"]', "PAYWALL")):
        if soup.select_one(selector):
            raise CollectError(kind, "原文页面存在访问限制，不能确认全文")
    if soup.title and re.search(r"just a moment|one moment|access denied|captcha|verify you are human", soup.title.get_text(), re.I):
        raise CollectError("BOT_CHALLENGE", "原文返回访问验证页")
    structured = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            structured.extend(_json_objects(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    articles = [obj for obj in structured if any("article" in str(kind).lower()
                for kind in (obj.get("@type") if isinstance(obj.get("@type"), list) else [obj.get("@type", "")]))]
    if any(obj.get("isAccessibleForFree") in (False, "false", "False") for obj in articles):
        raise CollectError("PAYWALL", "文章声明非公开全文")
    host = urlsplit(base).hostname or ""
    selectors = next((value for domain, value in BODY_SELECTORS.items()
                      if host == domain or host.endswith("." + domain)), ())
    selectors = tuple(selectors) + (".entry-content", ".post-content", ".article-body", '[itemprop="articleBody"]', "article")
    body, extractor, containers = "", "", []
    for selector in selectors:
        selected = soup.select(selector)
        if not selected:
            continue
        containers = selected
        parts = []
        for node in selected:
            if host.endswith("nasaspaceflight.com"):
                for advert in node.select('a[href*="shop.nasaspaceflight.com"]'):
                    advert.decompose()
            for noise in node.select("script, style, nav, aside, form, .related, .related-posts, .advertisement, .ad-container, .newsletter, .post-item--feed, .author-card"):
                noise.decompose()
            text = node.get_text("\n", strip=True)
            # 要求实际正文结构，不把只有标题的 article 当全文。
            if text and (node.select_one("p, li, table, blockquote") or selector == '[data-component="text-block"]'):
                parts.append(text)
        if parts:
            body, extractor = "\n\n".join(parts), selector
            break
    if not body:
        for obj in articles:
            if str(obj.get("articleBody") or "").strip():
                body = BeautifulSoup(str(obj["articleBody"]), "html.parser").get_text("\n", strip=True)
                extractor = "jsonld:articleBody"
                break
    if not body:
        raise CollectError("BODY_NOT_FOUND", "未取得完整文章正文；摘要不能入池")
    if re.search(r"subscribe to (?:continue|read)|sign in to (?:continue|read)|unlock (?:this|the) article|订阅后阅读|登录后阅读全文", body, re.I):
        raise CollectError("FULLTEXT_UNVERIFIED", "正文含预览或登录提示")
    images = []
    media_containers = list(containers)
    # BBC 的文字块与图片块是兄弟节点，只在所属文章内寻找图片，排除页尾推荐。
    if host.endswith(("bbc.com", "bbc.co.uk")):
        scopes = []
        for node in containers:
            scope = node if node.name == "article" else node.find_parent("article")
            if scope is not None and all(scope is not other for other in scopes):
                scopes.append(scope)
        media_containers.extend(scopes)
    if host.endswith("defensescoop.com"):
        media_containers.extend(soup.select(".single-article__cover"))
    for node in media_containers:
        for img in node.select("img"):
            srcset = img.get("data-srcset") or img.get("srcset") or ""
            choices = []
            for value in srcset.split(","):
                fields = value.strip().split()
                if fields:
                    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)[wx]", fields[-1]) if len(fields) > 1 else None
                    weight = float(match.group(1)) if match else 0
                    choices.append((weight, fields[0]))
            url = max(choices)[1] if choices else (img.get("data-src") or img.get("data-original") or img.get("src"))
            if url and str(url).strip().lower() not in {"undefined", "null", "none", "#"} and not (str(img.get("width")) == "1" and str(img.get("height")) == "1"):
                try:
                    images.append(normalize_url(url, base))
                except CollectError:
                    pass
    for obj in articles:
        image = obj.get("image", [])
        for value in image if isinstance(image, list) else [image]:
            url = value.get("url") if isinstance(value, dict) else value
            if isinstance(url, str) and url:
                try:
                    images.append(normalize_url(url, base))
                except CollectError:
                    pass
    if not images:
        meta = soup.select_one('meta[property="og:image"]')
        if meta and meta.get("content"):
            images.append(normalize_url(meta["content"], base))
    heading = soup.select_one("h1")
    title = heading.get_text(" ", strip=True) if heading else next((str(obj.get("headline")) for obj in articles if obj.get("headline")), "")
    next_link = soup.select_one('link[rel="next"]')
    next_url = normalize_url(next_link["href"], base) if next_link and next_link.get("href") else ""
    return Article(title, body, list(dict.fromkeys(images)), next_url, extractor)


def digest(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()
