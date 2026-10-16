# 网页来源采集

新增采集器：`rss`（15 个独立发布者 RSS 入口）、`snapi`、`sina`。入口清单在 `catalog.py`，均默认启用；招投标没有接入。第三方来源清单许可见 `AUTOSPACENEWS_LICENSE.txt`。

## 运行

看板中手动启动采集服务后，网页来源按 `WEB_COLLECT_INTERVAL_SECONDS` 独立刷新，默认 900 秒；X 继续使用 `COLLECT_INTERVAL_SECONDS`，按其原有分批游标扫描。网页超时或长轮次不会阻止 X 下一轮，网页错过的时间点不重叠补跑。启动看板本身不会启动采集。

`SOURCES` 留空为全部采集器；也可填写 `x,rss,snapi,sina` 的子集，修改后重启采集服务。间隔可在看板采集配置中调整。命令行参数为本次运行覆盖，不修改 `.env`：

```powershell
# 以下命令会执行真实采集，只在确实要采集时运行。
.\.venv\Scripts\python.exe main_flow.py --collect-only --sources rss,snapi,sina
.\.venv\Scripts\python.exe collect\news_fetch.py --sources rss
```

命令只跑一轮；周期刷新由看板托管服务负责。X 与网页组有跨入口互斥锁，避免看板和手动命令重复运行同一组。

## 采集边界

- 不做关键词/主题/标题相似过滤，不按 24 小时或 7 天限制新闻。
- 遍历入口公开可枚举的条目与分页，不扫描额外的全站归档。每源每轮最多推进 4 页发现请求，进度保存在账本，下一轮先检查最新页再继续回收。4 页是单轮工作预算，不是永久条数上限。
- 对每个候选访问原文页面，提取完整文章正文并跟随明确的正文分页；不使用 RSS/API 摘要顶替全文，不截 200 字。保存原始页面字节、正文、提取器和元信息供核对。
- 优先站点文章容器与完整 JSON-LD `articleBody`；只有 `summary/description`、错误页、验证码、付费预览或未取得正文时记录 WARNING，不写 `items`。
- 优先获取正文内配图、原图或较大 `srcset` 版本；没有正文图片时补充订阅/API 图片。图片下载失败独立记录 WARNING；正文完整的文章仍可入池。
- 内部身份为来源键加稳定 ID，无 ID 则用规范化 URL；同来源 ID/URL 成功后直接跳过。不检查、不覆盖文章后续更新。不同来源相同 URL 分别入池，第二阶段统一分析。
- 正文失败候选单独保留并按 15 分钟至 6 小时退避重试；失败状态不会变成已成功记录。
- `collect.db.upsert_raw` 写入首次成功新闻并发布既有 `processing_events`；不调用 AI，也不写 `items.pushed` 或分发 JSON 队列。

## 数据与状态

| 位置 | 内容 |
| --- | --- |
| `data/state/web_collect.sqlite3` | 每源成功身份、失败候选、分页进度、来源状态、独立 WARNING |
| `data/raw/web/listings/` | 列表响应和分页证据 |
| `data/raw/web/articles/` | 完整正文、页面原始字节与提取信息 |
| `data/images/web/` | 下载到本机的图片 |
| `data/hx.sqlite3` | 只有正文成功的新闻；正常进入处理订阅 |
| `logs/collect_web.log`、`logs/news_fetch.log` | 网络采集及 WARNING 日志 |

看板的网页来源区显示最近每源状态和最多 50 条当前 WARNING，包含可点击的原文地址、错误类型和失败阶段。失败候选不计入新闻数量或处理池。正文恢复成功后清除对应正文 WARNING。

## 验证与限制

`selftest/check_web_sources.py` 使用内存 HTTP 替身和临时 SQLite，不接触真实源、模型或 QQ。覆盖全文、短通告、图片、分页、来源内去重、跨来源保留、正文失败恢复、处理事件、看板契约和独立调度。

2026-10-14 已逐一执行 21 个入口的真实抓取验收，记录见根目录 `源站线上验收报告.md`。SatNews 地址和 Air & Space Forces 正文适配已修复；CNN、NYT 和 The Verge Space 已于 2026-10-15 移除；当前共 17 个入口。仍有访问验证、付费限制、404 和间歇连接失败，不能宣称所有来源持续可用。没有启用 Firecrawl 自动兜底；每轮仍实际尝试入口，失败候选退避重试。HTML 文章容器或 JSON-LD 是提取依据，抽样成功不等于所有页面均已验证完整；未取得全文的文章不会送入处理。
