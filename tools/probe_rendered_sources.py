"""用独立无头浏览器读取公开页面；不使用个人浏览器资料，不点击验证码或登录。"""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect.env import PROXY_URL
from collect.sources._web.parse import digest
from collect.logging_util import make_log, setup_console_encoding
from playwright.async_api import async_playwright


async def main():
    log = make_log(ROOT / 'logs' / 'browser_source_probe.log')
    proxy = None
    if PROXY_URL:
        parsed = urlsplit(PROXY_URL)
        proxy = {'server': f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'}
        if parsed.username:
            proxy.update(username=parsed.username, password=parsed.password or '')
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True, proxy=proxy)
        for url in sys.argv[1:]:
            context = await browser.new_context()
            page = await context.new_page()
            try:
                parsed = urlsplit(url)
                home = f'{parsed.scheme}://{parsed.netloc}/'
                await page.goto(home, wait_until='domcontentloaded', timeout=30000)
                for _ in range(20):
                    title = await page.title()
                    if not any(word in title.lower() for word in ['moment','checking','challenge','请稍候','请稍等']):
                        break
                    await asyncio.sleep(1)
                payload = await page.evaluate('''async url => {const r=await fetch(url); return {status:r.status,url:r.url,headers:Object.fromEntries(r.headers.entries()),body:await r.text()}}''', url)
                target = ROOT / 'data' / 'validation' / 'rendered' / (digest(url) + '.json')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(payload,ensure_ascii=False), encoding='utf-8')
                log(json.dumps({'url':url,'status':payload['status'],'title':title,'mime':payload['headers'].get('content-type'),'start':payload['body'][:70],'saved':str(target)},ensure_ascii=False))
            except Exception as exc:
                log(f'{url}: {type(exc).__name__}')
            finally:
                await context.close()
        await browser.close()


setup_console_encoding()
asyncio.run(main())
