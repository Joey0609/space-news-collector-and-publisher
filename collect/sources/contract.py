# -*- coding: utf-8 -*-
"""采集源的公共契约。

所有源适配器（`collect/sources/<name>/adapter.py`）都实现 `collect()` 并返回本类型，
采集入口只看这个结构，不关心具体是 X 还是别的站点。
"""
from dataclasses import dataclass, field


@dataclass
class CollectResult:
    """一个源跑完一轮采集的结果。"""

    source: str                      # 源名，例如 "x"
    items: list[dict] = field(default_factory=list)   # 归一化后的 RawItem
    scanned: int = 0                 # 本轮实际检查了多少个账号/目标
    skipped: int = 0                 # 主动跳过的（视频、重复、无文本…）
    failed: int = 0                  # 出错的目标数
    rate_limited: bool = False       # 是否疑似被限流（供入口决定要不要提前收工）
    error: str = ""                  # 整体失败时的原因
