# -*- coding: utf-8 -*-
"""采集源注册表：程序遍历这里拿到全部已注册的源。

约定
----
每个源是一个包：`collect/sources/<name>/`，并在 `adapter.py` 里放一个 `ADAPTER` 实例：

    class XAdapter:
        name = "x"                  # 唯一名字（.env 的 SOURCES 用它挑选）
        platform = "x"              # 写进 items.platform
        async def collect(self, log=print, state=None) -> CollectResult: ...

    ADAPTER = XAdapter()

新增一个源只要照做，`discover()` 会自动发现它，采集入口不需要改任何代码。
下划线开头的包（例如 `_template` 所在的位置）会被跳过。
"""
from __future__ import annotations

import importlib
import asyncio
import pkgutil
from pathlib import Path
from typing import Callable, Iterable

from .contract import CollectResult

__all__ = [
    "CollectResult",
    "SOURCES",
    "all_sources",
    "enabled_sources",
    "collect_all",
    "discover",
]


def discover() -> list:
    """扫描本目录下所有子包，收集 `<包>.adapter.ADAPTER`。导入失败的包会被跳过并提示。"""
    found: list = []
    here = Path(__file__).resolve().parent
    for info in sorted(pkgutil.iter_modules([str(here)]), key=lambda i: i.name):
        if not info.ispkg or info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}.adapter")
        except Exception as exc:                       # noqa: BLE001
            print(f"[源注册] 跳过 {info.name}：导入 adapter 失败 {exc}")
            continue
        adapter = getattr(module, "ADAPTER", None)
        if adapter is None:
            print(f"[源注册] 跳过 {info.name}：adapter.py 里没有 ADAPTER")
            continue
        found.append(adapter)
    return found


# 全部已注册的源（import 本模块时发现一次）
SOURCES: list = discover()


def all_sources() -> list:
    """所有已注册的源。"""
    return list(SOURCES)


def enabled_sources(names: Iterable[str] | None = None) -> list:
    """要启用哪些源：默认全部；`names` 或 .env 的 `SOURCES` 可以只挑一部分。

    `SOURCES=x`（逗号分隔）时只跑列出的源，顺序也按列出的顺序。
    """
    if names is None:
        from collect.env import ENABLED_SOURCES
        names = ENABLED_SOURCES
    wanted = list(dict.fromkeys(str(n).strip() for n in (names or []) if str(n).strip()))
    if not wanted:
        return all_sources()

    by_name = {str(getattr(s, "name", "")): s for s in SOURCES}
    picked = []
    for name in wanted:
        adapter = by_name.get(name)
        if adapter is None:
            print(f"[源注册] .env 里的 SOURCES 有未知的源：{name}（已注册：{sorted(by_name)}）")
            continue
        picked.append(adapter)
    return picked


async def collect_all(log: Callable[[str], None] = print, names: Iterable[str] | None = None) -> list[dict]:
    """遍历所有启用的源各跑一轮，返回合并后的条目。

    一个源失败不影响其它源；每个源的开始/结束/条数都会写日志。
    """
    sources = enabled_sources(names)
    if not sources:
        log("[源] 没有启用任何采集源")
        return []

    log(f"[源] 本轮启用 {len(sources)} 个源：{[str(getattr(s, 'name', '?')) for s in sources]}")
    async def run(adapter):
        name = str(getattr(adapter, "name", "?"))
        log(f"[源] {name} 开始采集")
        try:
            result = await adapter.collect(log)
        except Exception as exc:                       # noqa: BLE001
            log(f"[源] {name} 异常：{exc}")
            return []
        extra = f"，失败 {result.failed}" if result.failed else ""
        extra += "，疑似限流" if result.rate_limited else ""
        log(f"[源] {name} 结束：新增 {len(result.items)} 条{extra}"
            + (f"，错误：{result.error}" if result.error else ""))
        return result.items

    results = await asyncio.gather(*(run(adapter) for adapter in sources))
    items = [item for result in results for item in result]

    log(f"[源] 全部结束，合计新增 {len(items)} 条")
    return items
