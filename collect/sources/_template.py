# -*- coding: utf-8 -*-
"""新采集源模板 —— 复制下面的写法就能加一个源，采集入口不用改。

步骤
----
1) 建目录 `collect/sources/<名字>/`（**下划线开头的目录会被注册表跳过**）；
2) 把本文件内容复制成 `<名字>/adapter.py`，改掉 `name` / `platform`，实现 `collect()`；
3) 完成 —— `collect/sources/__init__.py` 的 `discover()` 会发现它，
   `collect/news_fetch.py` 下一轮就会自动把它跑起来。

要点
----
- `collect()` 必须返回 `CollectResult`（见 `sources/contract.py`）；
- 条目（items）要用统一字段：`platform / source / origin_id / account / raw /
  content_type / ts / date / ori_url / images`，去重靠 `platform:origin_id`；
- 失败不要抛出去：把原因放进 `CollectResult.error` / `failed`，一个源挂了不该影响别的源；
- 一切网络交互都要 `log(...)`，方便在看板终端排查。
"""
from typing import Any, Callable

from ..contract import CollectResult


class TemplateAdapter:
    name = "replace-me"      # 唯一名字，.env 的 SOURCES 用它挑选
    platform = "web"         # 写进 items.platform

    async def collect(self, log: Callable[[str], None] = print, state: Any = None) -> CollectResult:
        log(f"[{self.name}] 开始采集（模板，未实现）")
        # 真正实现时：
        #   1) 抓取页面 / 调接口，每一步都 log 出来
        #   2) 归一化成统一字段的 items
        #   3) 交给 collect.db.upsert_raw 去重入库（或返回给入口统一入库）
        return CollectResult(source=self.name, items=[], scanned=0,
                             error="模板未实现：请照 _template.py 的说明实现 collect()")


# 注册表靠这一行发现本适配器（复制到 <名字>/adapter.py 时保留）
ADAPTER = TemplateAdapter()
