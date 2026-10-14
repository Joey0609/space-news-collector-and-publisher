from .._web.catalog import SINA_URL
from .._web.collector import Collector, client
from .._web.parse import sina_page


class SinaAdapter:
    name = "sina"
    platform = "web"

    async def collect(self, log=print, state=None):
        async with client() as http:
            return await Collector(http, store=state, log=log).source("sina", "新浪科技", SINA_URL, sina_page)


ADAPTER = SinaAdapter()
