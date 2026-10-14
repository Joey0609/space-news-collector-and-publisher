"""RSS 采集入口，所有订阅默认启用。"""
import asyncio
from ..contract import CollectResult
from .._web.catalog import RSS_FEEDS
from .._web.collector import Collector, client
from .._web.parse import feed_page


class RSSAdapter:
    name = "rss"
    platform = "web"

    async def collect(self, log=print, state=None):
        async with client() as http:
            collector = Collector(http, store=state, log=log)
            # 限制订阅并发；单订阅自己的失败不影响其他 18 个入口。
            gate = asyncio.Semaphore(3)
            async def run(key, label, url):
                async with gate:
                    return await collector.source("rss:" + key, label, url, feed_page)
            results = await asyncio.gather(*(run(*feed) for feed in RSS_FEEDS))
        return CollectResult(source=self.name, items=[item for result in results for item in result.items],
                             scanned=sum(result.scanned for result in results), failed=sum(result.failed for result in results))


ADAPTER = RSSAdapter()
