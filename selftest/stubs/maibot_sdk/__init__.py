# -*- coding: utf-8 -*-
"""验证用桩模块：maibot_sdk 的最小替身。

只实现本插件用到的部分：Field / PluginConfigBase / MaiBotPlugin / HookHandler / types。
目的是在没有麦麦宿主的机器上检查：插件能否被"按文件路径"加载（最坏情况）、
类是否装配完整（Mixin 方法齐不齐）、一轮调度是否真能跑通。
"""
from . import types  # noqa: F401


class FieldInfo:
    def __init__(self, default=None, default_factory=None, description=""):
        self.default = default
        self.default_factory = default_factory
        self.description = description

    def get_default(self):
        if self.default_factory is not None:
            return self.default_factory()
        return self.default


def Field(default=None, default_factory=None, description=""):  # noqa: N802
    return FieldInfo(default=default, default_factory=default_factory, description=description)


class PluginConfigBase:
    """极简版：按注解 + FieldInfo 默认值构造实例（支持嵌套配置类）。"""

    def __init__(self, **kwargs):
        for klass in reversed(type(self).__mro__):
            for name in getattr(klass, "__annotations__", {}) or {}:
                if name.startswith("_"):
                    continue
                if name in kwargs:
                    setattr(self, name, kwargs[name])
                    continue
                cur = getattr(type(self), name, None)
                if isinstance(cur, FieldInfo):
                    setattr(self, name, cur.get_default())
                elif isinstance(cur, type) and issubclass(cur, PluginConfigBase):
                    setattr(self, name, cur())


class MaiBotPlugin:
    """极简版宿主基类：只提供 config / ctx 两个插槽。"""

    config_model = None

    def __init__(self, ctx=None, config=None, **kwargs):
        self.ctx = ctx
        self.config = config if config is not None else (self.config_model() if self.config_model else None)


def HookHandler(event, name="", description="", mode=None, order=None):  # noqa: N802
    """把元信息挂到函数上，返回原函数（宿主真正加载时读的就是这些）。"""
    def deco(fn):
        fn.__hook_event__ = event
        fn.__hook_name__ = name
        fn.__hook_mode__ = mode
        fn.__hook_order__ = order
        return fn
    return deco
