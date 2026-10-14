# 78-UMa / 航小麦 · AI 启动必读

## 当前结构

```text
collect/       数据收集：X 采集、图片、源适配器
process/       数据处理：翻译、打标、评分、AI 兜底
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
python process\translate_db.py
python process\judge.py
python process\ai_fallback.py
python dashboard\server.py
python selftest\run_all.py
python tools\migrate_layout.py --dry-run
```

看板默认地址：`http://127.0.0.1:8787`。启动终端会打印 token。默认只允许本机访问。

## 数据流

```text
collect/news_fetch.py
  -> data/raw/new_news_*.json + data/hx.sqlite3 items(stage=collected)
  -> process/translate_db.py     看板启动时全库扫描一次；之后消费采集写入的 processing_events 增量订阅；
                                 缺少任一必需字段→同一次完整分析重做。
                                 产出：zh 译文 / 四盏灯 is_news·not_repeat·not_comment·not_sensitive /
                                 ai_score AI 价值分(0~100) / ai_title / ai_summary / importance / ai_tags，
                                 stage=translated
  -> process/judge.py            四绿→发、任一红→丢（老条目走规则分+阈值），rules_score/decision/reason，stage=judged
  -> process/ai_fallback.py      decision（最终判定直接写回），stage=processed
  -> stage='processed' AND decision='发' + items.pushed（**不再写队列文件**）
  -> 看板渠道 WebSocket（127.0.0.1:8788）→ maibot 插件 / 艾莉森 / NapCat
```

`main_flow.py` 会按上述顺序调用采集、翻译、判定、AI 兜底。"该发"的条目就一直留在 SQLite 里，
由看板按渠道的 source 逐条发出去（见下「分发队列与渠道」）。
（`process/translate.py` / `judge.py` 也支持旧的 JSON 入参，但主流程只走 SQLite。）

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
- 旧 `data/queue/待推送.json` / `已发记录.json` 只在第一次注册 source 时**读一次**做迁移
  （`push_db._seed_legacy`），之后再也不写；队列上限、队列档案、`sent_to` 那套文件语义都作废了。

## 数据位置

- `data/raw/`：采集原始 JSON。
- `data/work/`：翻译、打分、AI 兜底中间结果。
- `data/images/`：图片。
- `data/queue/`：**历史遗留**。`待推送.json` / `已发记录.json` / `内容留档.json` 里，前两个只在
  首次迁移时被读一次（见「分发队列」），内容留档仍是"队列空时发旧闻"的兜底素材。
- `data/state/`：抓取基线、扫描游标、插件状态、看板 token、审计日志。
- `data/hx.sqlite3`：SQLite 台账，保存内容与分发记录（含 `items.pushed` 分发状态）。
- `data/markdown/`：从数据库导出的可读归档。
- `config/`：功能开关、推送规则、推送默认、用户/群偏好。

## 硬规则

1. 不触发真实抓取、真实 QQ 发送，除非用户明确要求。
2. 密钥、cookies、QQ 号只从 `.env` 读取，不提交。
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

- `translate_db.py`：处理服务启动时全库扫描一次；之后只处理 `collect.db` 发布到 `processing_events` 的新增/变更条目 ID。缺译文、四灯、有效 AI 分、标签、标题、正文清理、新闻要点/重要度或引用译文任一项时，都用同一次完整分析重做；失败统一记在 `translate_attempts` / `translate_error`，通过订阅事件延迟重试。判"不重复"时会带上"最近已入库标题清单"（`AI_REPEAT_CONTEXT` 条，0=关）。
- `translate.py`：保留的 JSON 版翻译入口，逻辑同上，读写 `data/raw` 与 `data/work`。
- `judge.py`：**四绿直接发、任一红直接丢**（`[gate] "AI分下限" > 0` 时四绿但 AI 分太低也丢；老条目没点灯时回到规则分+阈值+AI兜底）。`items.score` 优先用 AI 分、没分才用 `rules_score`。标签取自 `ai_tags` + `重要:高/中/低`；白名单/排除/阈值照旧。`[gate] 三灯放行 = false` 可退回纯打分。
- `ai.py`：全部提示词在这里；`tools/dump_prompts.py` 会把真实提示词导出成 `提示词.md`（改完提示词记得重跑它）。
- `ai_fallback.py`：边缘分数最终判断。
- `process_core_contract.py`：RawItem 归一化/校验起点。

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

- `items.rev`：每次改这一行就 +1（`collect/db.py` 采集更新、`translate_db.py` 完整分析、
  `judge.py`、`ai_fallback.py`、看板改译文都 +1）。翻译是**就地更新**已有行、id 不变，
  只看 id 会漏掉"译文/四盏灯刚写好"，所以增量必须靠 rev。
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

## 迁移

旧部署数据可先执行：

```powershell
python tools\migrate_layout.py --dry-run
python tools\migrate_layout.py --apply
```

执行前停止计划任务、MaiBot 和艾莉森定时推送。迁移工具会尝试移动旧 JSON、图片、状态，并初始化数据库。

## 当前验收状态

P1：完成。新目录、入口、导入和计划任务路径已修正。
P2：完成。`data/`、SQLite、Markdown 导出和迁移脚本已建立。
P3：完成。处理阶段已有独立翻译入口，RawItem 契约与源适配器骨架已建立。
P4/P5：完成第一版。看板后端和前端可本机启动，后续可继续扩充台账查询、图表和操作页。

最终验收：离线自检五项（collect / channels / Alison / MaiBot plugin / dashboard）全部通过；
不代表已经执行过真实 X 抓取或真实 QQ 发送。

## AI必须保证启动的dashboard有终端弹窗，而不是后台静默启动。如果需要暂停服务尽量只暂停对应的服务，而不是直接关闭整个dashboard。
