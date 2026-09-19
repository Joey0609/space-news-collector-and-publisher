# -*- coding: utf-8 -*-
"""验证用的桩模块：只在本地没有装 twscrape/openai 时让 import 通过。"""


class _Resp:
    def __init__(self, content=""):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class _Completions:
    def create(self, **kwargs):
        return _Resp("")


class _Chat:
    completions = _Completions()


class OpenAI:  # noqa: N801
    def __init__(self, **kwargs):
        self.chat = _Chat()
