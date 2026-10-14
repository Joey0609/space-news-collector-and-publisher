# 78-UMa / 航小麦 · AI 启动必读

## 当前结构

```text
collect/       数据收集：X 采集、图片、源适配器
process/       数据处理：一次完整 AI 分析与最终发/丢
 distribute/   数据分发：maibot / alison / napcat
dashboard/     本机数据看板与管理台
data/          运行期数据，不入库
config/        人维护配置，不入库
```

旧目录 `抓取脚本/`、`艾莉森网关/`、`脚本/`、`麦麦插件/`、`配置模板/`、`自检/` 已删除，不保留兼容壳。

## Python 环境

所有看板托管服务固定使用根目录 `.venv\Scripts\python.exe`，不要使用系统全局 Python。项目环境已安装 `twscrape`、`openai`、`httpx`、`python-dotenv`、`websockets`；QQ SDK 源码位于 `distribute/alison/botpy/`，不依赖外部 `qq-botpy` 安装包。登录、代理和扫描节奏配置来自根 `.env`；X 遍历账号唯一来源为 `collect/sources/x/x_accounts.csv`，UTF-8（支持 BOM），表头为 `名字,性质`。名字填用户名（可带 @）；性质允许暂留空，分类仅 `official` / `media` / `photographer` / `enthusiast` / `others`，目前采集按名字列读取，每轮重新读取。`official` 包含官方机构、项目及公司官方；未提供分类的保留账号暂标 `others`。名单缺失、为空或格式错误时明确报错，不回退到关注列表。旧 `WATCH_ACCOUNT` / `NEWS_ACCOUNTS` 配置不再生效。`COOKIES_PATH` 为空时会给出明确文件路径错误。



```powershell
python main_flow.py
python collect\news_fetch.py
python process\analyze_db.py
python dashboard\server.py
python selftest\run_all.py
```

看板默认地址：`http://127.0.0.1:8787`。启动终端会打印 token。默认只允许本机访问。

## 数据流

```text
collect/news_fetch.py
  -> data/raw/new_news_*.json + data/hx.sqlite3 items(stage=collected)
  -> process/analyze_db.py       启动一次完整性核对，之后消费 processing_events；
                                 一次 AI 请求产出译文、引用译文、标题、摘要、重要度、分数、标签、
                                 decision（发/丢）、invalid_reason、repeat_of；
                                 每条完整校验后原子提交 stage=processed 并确认同版本事件。
  -> stage='processed' AND decision='发' + items.pushed（**不再写队列文件**）
  -> 看板渠道 WebSocket（127.0.0.1:8788）→ maibot 插件 / 艾莉森 / NapCat
```

`main_flow.py` 调用采集与完整分析；看板处理服务直接运行完整分析入口。"该发"的条目就一直留在 SQLite 里，
由看板按渠道的 source 逐条发出去（见下「分发队列与渠道」）。
旧 JSON 处理入口、Judge、AI 兜底、四灯和规则加权已删除，不保留兼容壳。

## 分发队列与渠道（重要）

**待推送不是文件，是数据库状态**——只有两件事要判断：

- `items.stage = 'processed' AND items.decision = '发'`：完整处理链结束且最终判定要发。
- `items.pushed`：JSON 对象，记"这条发给过哪些 source"，例
  `{"maibot:private": true, "maibot:group_123": false, "alison:group_456": true}`。
  自己的 source 不是 `true` 就是"该我发"；**发成功后**才置 `true`（`pushes` 表另记发送时间）。

- source 命名统一为 `通道:目标`：maibot **每个目标一个 source**（`private` / `group_<群号>`），
  所以私聊先发过不会把群的那一份吃掉；艾莉森按已启用群 OpenID 注册 `alison:group_<group_openid>`。
- 新 source（加了新群、接了新通道）第一次注册时自动**回填**到已有行上：旧 source 全发过的行
  记 `true`（不把历史全量灌给新通道），还在途中的行记 `false`。
- 队列口径的唯一实现是 `distribute/push_db.py`（**只有看板侧读库**）。

**渠道 = 一个连上看板的 WebSocket 客户端**（协议见 `dashboard/PROTOCOL.md`，端口 8788）：

```
distribute/ws_codec.py   RFC6455 最小实现（纯标准库：握手 + 帧编解码）
dashboard/ws.py          渠道 WS 服务端（127.0.0.1:8788，token 鉴权，先鉴权后 101）
dashboard/channels.py    渠道注册表（channels 表）+ 逐条推送 + want/pending + 退避
distribute/ws_channel.py 渠道 SDK（连接/重连/hello/收条/result/defer/want/pending）
```

两种投递模式，渠道在 `hello.capabilities` 里选：

| 模式 | 谁发起 | 谁用 |
| --- | --- | --- |
| **推送**（默认） | 服务端逐条推 `item`，渠道回 `result` / `defer` | 艾莉森（`distribute/alison/channel.py`）、NapCat（`distribute/napcat/channel.py`） |
| **拉取**（`{"pull": true}`） | 渠道按自己节奏 `want`，服务端回 `items` | 麦麦插件（自己的活跃时段/间隔/每日汇总） |

- 渠道启动时会 `hello` 注册 → 服务端 `register_sources()`（新 source 回填）→ 扫描最终处理池，只推**确实没发过的**；
  `result ok=true` 才 `mark_pushed()` 写库并回 `ack`；`ok=false` / `defer` / 超时 → 条目保持未发 + 退避重试。
- 输出池以 SQLite 为唯一接口：推送模式在线时每轮轮询 `stage='processed' AND decision='发'`；拉取模式在渠道发 `want` 时读取同一池。新处理结果在下一次轮询/`want` 时可见；不依赖易丢的进程内事件。
- 插件侧 `hx_ws.py` 是 SDK 的自包含镜像（插件拷进麦麦部署，不能 import 仓库代码），
  自检 `check_channels.py` 的第 [5] 段让它和仓库实现跑同一个真 WS 服务端，保证两边一致。
- 内置渠道写在 `dashboard/channels.py` 的 `BUILTIN_CHANNELS`（maibot=plugin 外部渠道；
  alison/napcat=process 托管渠道，看板按 `channels.command` 启停进程）。
- 不读取旧 JSON 队列或旧群状态文件；分发队列与发送记录仅使用新结构 SQLite。

## 数据位置

- `data/raw/`：采集原始 JSON。
- `data/work/`：运行进度与临时输出，不保存多阶段分析中间结果。
- `data/images/`：图片。
- `data/queue/`：旧文件已清理；程序不再读取或写入 JSON 新闻队列与旧闻留档。
- `data/state/`：抓取基线、扫描游标、插件状态、看板 token、审计日志。
- `data/hx.sqlite3`：SQLite 台账，保存内容与分发记录（含 `items.pushed` 分发状态与 AI 无效原因）。
- `data/markdown/`：从数据库导出的可读归档。
- `config/`：功能开关、推送规则、推送默认、用户/群偏好。

## 硬规则

1. 不触发真实抓取、真实 QQ 发送，除非用户明确要求。
2. cookies、QQ 凭据从 `.env` 读取；AI 接入点及 API 密钥按用户要求保存到不入库的 `config/ai_endpoints.json`。凭据不提交。
3. 所有脚本必须写日志到 `logs/`。
4. JSON/TOML 写入要 UTF-8；中文 TOML key 必须加引号；重要 JSON 使用原子写。
5. 渠道**只**通过看板 WebSocket 拿内容并回报结果（服务端才写 `items.pushed`），
   不抓取、不翻译、不重新判定，也不许再往 JSON 队列文件里写。
6. source / kind 命名保持：`private`、`group_<群号>`，source = `通道:kind`。
7. 看板只绑定 `127.0.0.1`；写操作必须 token、白名单、审计。
8. 修改后至少跑：`python selftest\run_all.py` 和 `python -m compileall -q collect process distribute dashboard selftest tools main_flow.py`。

## 模块职责

### collect

- `collect/news_fetch.py`：采集入口。
- `collect/hx_core/scan.py`：分批、游标、增量、视频跳过。
- `collect/sources/x/x_client.py`：cookies、twscrape、读取同目录的 `x_accounts.csv`、按用户名查询最新推文。
- `collect/hx_core/images.py`：下载 X 原图。
- `collect/sources/`：未来源适配器；X 当前稳定实现仍由 `hx_core/scan.py` 执行。

当前限制：每个账号每轮只取最新一条；视频/GIF 跳过；`PAUSE_SECONDS` 尚未接线；账号多时轮询延迟较长。

### process

- `analyze_db.py`：唯一处理入口；每次成功 AI 请求拿齐完整归档与最终操作码。
  丢弃条目也生成译文、标题、摘要、重要度、评分和标签，无效原因由AI结合预设情形生成。
  本地只做输出契约和事实依据校验；失败记 analysis_attempts / analysis_error，冷却后轮转重试，无累计上限。
  结果提交与 processing_events 同版本确认在一个事务内；输入或generation变化时丢弃旧回复。
- `ai.py`：唯一完整分析提示词、三协议模型调用和输出校验；改提示词后执行 tools/dump_prompts.py。
- `analysis_contract.py`：处理服务与看板共享的完整性契约。


### distribute

- `push_db.py`：**分发队列的唯一口径**（读 `stage='processed' AND decision='发'`、按 source 查/标 `items.pushed`、
  登记新 source、旧 JSON 一次性迁移、看板用的待分发统计）。只有看板侧读库。
- `ws_codec.py` / `ws_channel.py`：渠道 WebSocket 的编解码与服务端/客户端 SDK（纯标准库）。
- `maibot/hangxiaomai_news/`：MaiBot 插件（**拉取模式**渠道），部署时把该目录拷进 MaiBot 的 `plugins/`；
  `hx_ws.py` 是 SDK 的自包含镜像，`hx_schedule.py` 保留它自己的活跃时段/间隔/每日首推节奏。
- `alison/channel.py`：艾莉森 QQ Bot 渠道（推送模式，每个已启用群独立 source）；
  与 `alison/gateway.py` 共用一个 BotPy 长连接发送和**持续监听**入群/退群、群 @ 和平台下发的全量群消息：终端打印群 OpenID、
  成员 OpenID 和正文，未知群自动入目录，**不得自动发测试消息**。`distribute/alison/botpy/` 是内置的
  qq-botpy 1.2.1 源码，已补 `GROUP_MESSAGE_CREATE` 解析器及群/C2C intent 名称；每个 QQ 群的机器人设置须开启「获取群内全部消息」
  且确认机器人有全量事件权限，才会下发未 @ 的消息；
  否则只能通过入群/@事件发现群。消息日志不能打印事件里的 auth_token 或附件 URL。
  配置弹窗仅授权后明文展示 `APPID/SECRET` 等，保存至不入库的 `distribute/alison/.env`。
  `alison/ali_core/groups.py` 在 SQLite `alison_groups` 管理逐群推送开关（新群默认关闭）。
  QQ 官方没有"获取所有历史已入群列表"接口：只能通过事件发现或手动补录 OpenID；
  `GET /v2/groups/{group_openid}/info` 仅刷新已知群信息，可能需要白名单权限。
  `推送.py` 是旧手动调试工具，发送入口也必须遵守逐群开关。
- `napcat/channel.py`：NapCat 联调渠道（需要 `.env` 里的 `NAPCAT_WS_URL/TOKEN/TARGET_QQ`，
  没配置就如实回报失败，不会误标已发）。

### dashboard

后端：`dashboard/server.py`（HTTP + 统一信封 API + 进程管理）、`dashboard/channels.py`（渠道注册表与推送调度）、
`dashboard/ws.py`（渠道 WebSocket 服务端，127.0.0.1:8788）、`dashboard/service_runner.py`（采集/处理循环）。
协议与 API 契约见 **`dashboard/PROTOCOL.md`**（改前端/渠道前先看它）。
看板服务器启动时自动启动 `process` 服务做全库完整性扫描；`collect` 保持手动启动，避免启动看板即触发真实抓取。
前端：`dashboard/webui/`，原生 HTML/CSS/JS，**顶部「处理 / 分发」双 tab**、运行摘要、内容列表占满整宽。

启动器（根目录，用户日常入口）：`run_dashboard.bat` → `run_dashboard.ps1`，服务器 Python 在**当前 BAT 终端**
前台运行；关闭该窗口就停止看板。`dashboard/server.py --open-browser` 会在 HTTP 端口绑定成功后自动打开
`http://127.0.0.1:8787`，避免浏览器抢先打开；异常退出时 BAT 会暂停保留报错。
`run_dashboard.vbs` 是刻意隐藏窗口的静默版。**`run_dashboard.ps1` 必须存成 UTF-8 带 BOM**：Windows PowerShell 5.1
对无 BOM 的 .ps1 按 GBK 解码；**`run_dashboard.bat` 必须是 ASCII + CRLF**，LF-only 会被 cmd.exe 拼行解析。
自检 `check_dashboard.py` 有断言盯着编码、前台进程与启动路径。

实时与分页（看板要和采集/翻译同步）：

- `items.rev`：每次改这一行就 +1（`collect/db.py` 采集更新、`analyze_db.py` 完整分析、看板改译文都 +1）。翻译是**就地更新**已有行、id 不变，
  只看 id 会漏掉"完整分析刚提交"，所以增量必须靠 rev。
- SSE（`/api/stream`）每秒推 `latest_id` / `latest_rev`；前端发现游标变大就拉
  `/api/snapshot?since_id=&since_rev=`，把"新条目 + 被更新过的条目"打补丁上屏（约 1 秒）。
- 前端每 15 秒（`RECONCILE_MS`）对"最新 ~ 当前显示的这一段"做一次全量核对
  （`/api/snapshot?offset=0&limit=shown`）：补漏、刷新变了的、删掉服务端已不存在的，防止 SSE 丢事件。
- 分页：默认 20 条（`PAGE_SIZE`），滚到视口下方还剩 10 条（`LOAD_AHEAD`）自动再加载 20 条
  （`/api/items?offset=&limit=`）；底部显示"已显示 X / 共 Y"和「加载更多」按钮。

## 数据库

`collect/db.py` 使用 SQLite 标准库（`hx_common/db.py` 是旧路径，已并入 collect）：

- `items`：来源、原文、译文、评分、判定和处理阶段，外加 **`pushed`**（分发状态：
  `{"通道:目标": true}`，见「分发队列」）与 `rev`（改动计数，看板实时同步靠它）。
- `processing_events`：采集发布新增/变化的原文条目；处理服务启动全库恢复扫描后，只消费这里的增量 ID。按 item 合并，`generation` 防止新版本被旧回执确认，失败事件带退避时间。
- `pushes`：`item_id` / `channel` / `kind` / `source` / `sent_at`，`UNIQUE(source, item_id)`
  —— 发送审计 + 幂等，防止同一条重复标记。
- `alison_groups`：按群 OpenID 存已知群信息、是否仍为成员、逐群推送开关（新群默认关闭）；
  不存 APPID/SECRET，凭据仅在不入库的 `distribute/alison/.env`。
- `meta`：schema 版本等键值；`push_sources` 存已注册的全部 source。

启用 WAL 和 busy timeout。数据库写失败不得阻断主流程。

## 单次分析数据重建

停机后执行 `.venv\Scripts\python.exe tools\reset_analysis_data.py --apply`。
按 collected_at 倒序保留15条，再从其余条目随机选15条；保存随机种子和保留ID。
只保留原文、引用、来源和图片，全部重新分析；发送记录清空，渠道配置保留。
先做离线数据库备份；新结构不导入旧分析字段或旧JSON队列。

## 当前验收状态

P1：完成。新目录、入口、导入和计划任务路径已修正。
P2：完成。`data/`、SQLite、Markdown 导出和迁移脚本已建立。
P3：完成。处理阶段已有独立翻译入口，RawItem 契约与源适配器骨架已建立。
P4/P5：完成第一版。看板后端和前端可本机启动，后续可继续扩充台账查询、图表和操作页。

最终验收：离线自检五项（collect / channels / Alison / MaiBot plugin / dashboard）全部通过；
不代表已经执行过真实 X 抓取或真实 QQ 发送。

## AI必须保证启动的dashboard有终端弹窗，而不是后台静默启动。如果需要暂停服务尽量只暂停对应的服务，而不是直接关闭整个dashboard。

AI 接入点独立设置处理间隔 `ANALYSIS_PAUSE_SECONDS` 和并行数量 `ANALYSIS_CONCURRENCY`（1至32，默认1）。`analyze_db.py` 调度独立的单条子进程，使用各自的 SQLite 连接，结果逐条提交；停止处理时必须停止整棵进程树。

图片规则：`collect/image_filter.py` 使用 OpenCV 定位二维码，二维码区域并集超过整图面积50%时排除该图（不删除原文件）。采集写库及分发读取均过滤，识别结果按文件修改时间缓存。艾莉森按正文/日期/全部配图顺序发送，多图分消息追加，正文和全部图片成功才回报成功；部分发送进度保存在 `data/state/alison_delivery_progress.json`，重试继续未完成部分。
