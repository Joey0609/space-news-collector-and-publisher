from .._web.catalog import SNAPI_URL
from .._web.collector import Collector, client
from .._web.parse import snapi_page


class SnapiAdapter:
    name = "snapi"
    platform = "web"

    async def collect(self, log=print, state=None):
        async with client() as http:
            return await Collector(http, store=state, log=log).source("snapi", "Spaceflight News API", SNAPI_URL, snapi_page)


ADAPTER = SnapiAdapter()
