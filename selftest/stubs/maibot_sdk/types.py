# -*- coding: utf-8 -*-
"""验证用桩模块：maibot_sdk.types。"""


class HookMode:
    BLOCKING = "blocking"
    NON_BLOCKING = "non_blocking"


class HookOrder:
    EARLY = 0
    NORMAL = 50
    LATE = 100
