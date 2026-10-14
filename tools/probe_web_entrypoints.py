"""检查官方首页公布的订阅入口和请求响应，结果仅写验证目录。"""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect.env import PROXY_URL
from collect.logging_util import make_log, setup_console_encoding


async def main():
    log = make_log(ROOT / "logs" / "web_entrypoint_probe.log")
    async with httpx.AsyncClient(proxy=PROXY_URL or None, trust_env=False, follow_redirects=True, timeout=20,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"}) as client:
        for url in sys.argv[1:]:
            try:
                response = await client.get(url)
                target = ROOT / "data" / "validation" / "entrypoints" / (__import__('hashlib').sha256(url.encode()).hexdigest() + ".html")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
                soup = BeautifulSoup(response.content, "html.parser")
                feeds = [urljoin(str(response.url), node.get("href")) for node in soup.select('link[type="application/rss+xml"],link[type="application/atom+xml"]') if node.get("href")]
                links = [urljoin(str(response.url), node.get("href")) for node in soup.select('a[href]') if any(word in (node.get("href", "") + node.get_text()).lower() for word in ['rss', '/feed', 'subscribe'])][:15]
                log(json.dumps({"url":url,"status":response.status_code,"final":str(response.url),"mime":response.headers.get('content-type'),"start":response.text[:90],"feeds":feeds,"links":links,"saved":str(target)},ensure_ascii=False))
            except Exception as exc:
                log(f"{url}: {type(exc).__name__}")


setup_console_encoding()
asyncio.run(main())
