# -*- coding: utf-8 -*-
"""Template for future sources such as SpaceNews, NOTAM and MSI."""
SOURCE = "replace-me"
PLATFORM = "web"

async def fetch(log, state=None) -> list[dict]:
    """Implement: fetch, normalize, deduplicate by platform/origin_id, and return RawItems."""
    raise NotImplementedError
