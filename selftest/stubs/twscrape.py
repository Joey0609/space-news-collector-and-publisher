# -*- coding: utf-8 -*-
"""验证用的桩模块：twscrape 的最小替身。"""


async def gather(it):
    return []


class AccountsPool:
    def __init__(self, db_file=None, **kwargs):
        self.db_file = db_file

    async def add_account(self, **kwargs):
        return None


class API:
    def __init__(self, pool=None, **kwargs):
        self.pool = pool
