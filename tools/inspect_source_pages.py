"""对已发现的故障页面检查公开正文结构，不输出整页到终端。"""
import asyncio
import json
import sys
from pathlib import Path
from bs4 import BeautifulSoup
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect.env import PROXY_URL
from collect.logging_util import make_log, setup_console_encoding
from collect.sources._web.parse import _json_objects, digest
from curl_cffi.requests import AsyncSession


async def main():
    log = make_log(ROOT / 'logs' / 'inspect_source_pages.log')
    async with AsyncSession(impersonate='chrome', proxy=PROXY_URL or None, timeout=25) as http:
        async def inspect(url):
            try:
                response = await http.get(url)
                target = ROOT / 'data' / 'validation' / 'browser_http' / (digest(url) + '.html')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
                soup = BeautifulSoup(response.content, 'html.parser')
                objects = []
                for node in soup.select('script[type="application/ld+json"]'):
                    try:
                        objects += [obj for obj in _json_objects(json.loads(node.get_text())) if 'article' in str(obj.get('@type','')).lower()]
                    except ValueError:
                        pass
                containers = sorted([(len(node.find_all('p')),node.name,node.get('class'),node.get('id')) for node in soup.find_all(['div','section','article']) if len(node.find_all('p'))>=3],key=lambda row:row[0],reverse=True)[:12]
                log(json.dumps({'url':url,'status':response.status_code,'final':str(response.url),'mime':response.headers.get('content-type'),'title':soup.title.get_text() if soup.title else '',
                                'start':response.text[:55],'articles':[{'headline':obj.get('headline'),'accessible':obj.get('isAccessibleForFree'),'body':len(str(obj.get('articleBody','')))} for obj in objects], 'containers':containers,'saved':str(target)},ensure_ascii=False))
            except Exception as exc:
                log(f'{url}: {type(exc).__name__}')
        await asyncio.gather(*(inspect(url) for url in sys.argv[1:]))


setup_console_encoding()
asyncio.run(main())
