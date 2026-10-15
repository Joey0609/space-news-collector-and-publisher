# 航小麦 · 渠道协议 & 看板 API（v1，冻结）

分发队列的真值在数据库（`items.stage='processed' AND items.decision='发'` + `items.pushed`，见 `distribute/push_db.py`）。
**渠道不再自己查库**：渠道连上服务端 → 注册 → 服务端把"这个渠道还没发过的"条目**一条一条**推给它
→ 渠道回执 → 成功的才写库。一份实现两处镜像（仓库 `distribute/`、麦麦插件目录），自检对照。

```
                     ┌────────────── dashboard/server.py ──────────────┐
   浏览器 ──HTTP/SSE──│  /api/*（统一 {ok,data} 信封）+ /api/stream     │
                     │  dashboard/ws.py   :8788  RFC6455（纯标准库）    │
                     └───────┬──────────────────────────────┬──────────┘
                             │ dashboard/channels.py        │
                             │  注册表 / 逐条推送 / 退避     │
                   maibot ───┘（插件主动连）        alison ──┘（看板可启停的常驻进程）
```

---

## 0. 处理输入订阅

待处理列表按真实 `processing_events` 队列展示：正在处理项在最上方，其后为已到期的队列项，再后为冷却中和启动待恢复的项；同类项按 `created_at,item_id` 排序。列表每秒同步一次，队头标记「队头」。处理程序记录全部并行任务的 `started_at` 和事件 `generation`，队列 API 返回 `processing / elapsed_seconds / queue_position`；正在处理状态优先于之前的错误，时长在弹窗内逐秒更新，停止服务后清除过期标记。

看板启动处理服务时先对 SQLite 做一次全库完整性扫描，用来恢复服务停止期间积累的记录。启动扫描结束后，处理服务不再周期性重扫 `items`：

1. `collect.db.upsert_raw()` 在同一事务中写入新增/变化的原文，并发布到 `processing_events`。
2. 处理服务等待到期事件，按事件里的 `item_id` 调用一次完整 AI 分析；不会把全库查询作为后续增量入口。
3. 成功完成的事件确认后删除；失败条目保留事件并移到队尾，没有累计重试上限。
4. 事件按 item 合并并带 generation，处理过程中同一条内容再次变化时，新一代事件不会被旧处理结果确认掉。
5. `ANALYSIS_MAX_ATTEMPTS` 是单条每次轮到时最多完整分析几次；仍失败先处理后续内容，队尾轮回来继续尝试。超时与格式错误都不永久放弃；失败项移到队尾时设置 `ANALYSIS_RATE_LIMIT_WAIT` 秒冷却（至少 1 秒），期间先处理其他到期事件。启动时恢复尚未完成的事件，保留累计次数。

`processing_events` 是持久订阅队列，因此处理服务重启时可以通过启动全库扫描恢复；在运行期间，新采集内容走事件增量。

---

## 1. WebSocket 通道（`ws://127.0.0.1:8788/ws`）

- 端口：默认 **8788**（HTTP 8787 + 1），可用 `--ws-port` / 环境变量 `HX_WS_PORT` 改。
- 只绑 `127.0.0.1`；**必须带 token**：`GET /ws?token=<TOKEN>&channel=<渠道id>`。
- 帧：只发**文本帧**，内容是 JSON；服务端支持 ping/pong 与 close；不做分片（消息都很小）。
- 时间：`ts` 一律 Unix 秒。

### 1.1 渠道 → 服务端

| `type` | 字段 | 说明 |
| --- | --- | --- |
| `hello` | `channel` `label` `version` `adapter` `targets[]` `capabilities{}` | 注册。`targets[].kind` 是目标（`private` / `group_<群号>`），`label` 只给人看。允许省略 `channel`（用 URL 里的） |
| `result` | `source` `item_id` `ok` `detail` | 单条回执。`ok=true` → 服务端 `mark_pushed` 并回 `ack` |
| `defer` | `source` `item_id` `seconds` `reason` | 现在不能发（静默时段/限流）。条目**保持未发**，服务端 `seconds` 后再试 |
| `want` | `source` `count` | **拉取模式专用**：我现在要发，给我最多 `count` 条（服务端回 `items`）。只有 `capabilities.pull = true` 的渠道能用 |
| `status` | `state` `note` | 渠道自报状态：`idle`/`sending`/`error`，看板直接显示 |
| `bye` | — | 要断开了（服务端按断线处理） |

### 1.2 服务端 → 渠道

| `type` | 字段 | 说明 |
| --- | --- | --- |
| `welcome` | `channel` `label` `registered` `new_sources[]` `sources[]` `enabled` `ts` | 注册结果。`new_sources` 非空 = 这次是新注册（新 source 已回填到历史行） |
| `item` | `source` `item{}` | **推送模式**：推一条。每个 source 同时只有一条在途，等回执才推下一条 |
| `items` | `source` `items[]` | **拉取模式**：对 `want` 的回应（空数组 = 没有待发） |
| `ack` | `source` `item_id` `rev` | 已写库（`pushed` 置 true、`pushes` 记审计、`rev` +1） |
| `pause` / `resume` | — | 看板启用/停用该渠道（或 `channels.enabled=0`）。收到 `pause` 后别再发，等服务端恢复 |
| `shutdown` | `reason` | 服务端要求渠道退出（看板停止托管进程时） |

### 1.2.1 两种投递模式（渠道在 `hello.capabilities` 里选）

| 模式 | `capabilities` | 谁发起 | 谁用 |
| --- | --- | --- | --- |
| **推送** | 不写 `pull`（或 `false`） | 服务端逐条推 `item`，渠道回 `result`/`defer` | 没有自己节奏的通道（艾莉森、NapCat、以后的网页/API 通道） |
| **拉取** | `{"pull": true}` | 渠道按自己的节奏 `want`，服务端回 `items` | 有活跃时段/间隔/每日汇总的通道（麦麦插件） |

拉取模式下服务端**不主动推** `item`；渠道拿到 `items` 后对每一条回 `result`（成功即写库）。
`want` 拿到的条目服务端记为"在途"，不会同时再推给同一个 source。

`item` 里的字段（与旧队列 JSON 兼容，渲染代码不用改）：
`id` `platform` `source` `account` `orig_account` `forward_comment` `quoted_account`
`quoted_raw` `quoted_zh` `quoted_url` `ts` `date` `content_type` `raw` `zh` `images[]`
`ori_url` `score`（= `ai_score`）`ai_title` `ai_summary` `decision` `invalid_reason`
`importance` `ai_tags` `decision` `pushed{}`。

### 1.3 推送规则（服务端）

1. 收到 `hello` → `register_sources(通道的每个 target 一个 source)`；新 source 自动回填历史行。
2. 从 `pending(source=...)` 取该渠道**未发送**的（`stage='processed' AND decision='发'`、发布时间在最近 5 天内且 `pushed[source] != true`），id 升序，
   逐条推。**注册过还是新注册都一样**：都只推"确实没发过的"。
3. 回执处理：
   - `result ok=true` → `mark_pushed([item_id], source)` → 回 `ack` → 推下一条。
   - `result ok=false` → 保持未发，该 source 退避（默认 300 秒），推下一个 source 的条目。
   - `defer seconds` → 保持未发，该 source 退避 `seconds`（钳制 30 ~ 3600）。
   - 120 秒没回执 → 按 `defer 60` 处理并记日志（不回滚任何东西）。
4. 渠道 `enabled=0`、断线、收到 `pause` → 该渠道不推；重新启用/重连后继续从第 2 步来。
5. 推送模式的在线会话每轮扫描同一个 SQLite 输出池；没条目时每 5 秒再看一次（空闲不计日志）。处理服务把行写成 `stage='processed'` 后，它会在下一轮扫描中出现。拉取模式不主动扫描，渠道按自己的节奏发 `want`，服务端从同一查询取数。

这就是第二阶段到第三阶段的输出订阅：**数据库轮询订阅**，不是进程内事件通知。`hello` 注册 source 后推送会话马上开始扫描；重启或断线后重连会重新扫描整个最终输出池，所以不会因错过一次通知漏条目。

### 1.4 渠道注册表（数据库表 `channels`）

| 列 | 说明 |
| --- | --- |
| `id` | 渠道 id（`maibot` / `alison` / `napcat`） |
| `label` | 显示名 |
| `adapter` | `plugin`（外部进程主动连，看板只控制启用） / `process`（看板托管进程，可启停） |
| `command` | `adapter=process` 时看板启动它的命令行（JSON 数组） |
| `enabled` | 看板开关：0 = 不推（服务端发 `pause`） |
| `targets` | JSON：`[{"kind","label"}]` |
| `last_seen` | 最近一次 `hello`/心跳时间 |
| `note` | 渠道 `status.note` 或服务端备注 |

---

## 2. HTTP API（统一信封）

**所有** `/api/*` 的 JSON 响应都是：

```json
{"ok": true,  "data": { ... }}
{"ok": false, "error": "人话错误信息"}
```

HTTP 状态码仍有意义（200 / 400 / 401 / 404 / 500）。写操作要 `X-HX-Token`（值 = `data/state/dashboard_token.txt`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 首屏一次拿全：`services`、`process_progress`、`channels`、`stats`、`env_ready`、`configurable`、`ts` |
| GET | `/api/process/backlog` | 完整处理池条目：含缺失项、累计尝试次数、每次轮到尝试次数；`stuck_count=0`，失败项持续轮转 |
| GET | `/api/items?offset=&limit=` | 分页条目 |
| GET | `/api/snapshot?offset=&limit=&since_id=&since_rev=` | 首屏/加载更多/增量补丁（`incremental` 标记） |
| GET | `/api/pushes?limit=` | 最近分发尝试（`push_attempts`，含成功/失败与群名）；`pushes` 只保留成功审计 |
| GET | `/api/channels` | 渠道列表（含每个 target 的待发条数、累计发送数） |
| GET | `/api/channels/endpoint` | `{"ws_url","token","protocol":1}`；必须带 `X-HX-Token`，不得向未授权访问者泄露 token |
| GET | `/api/stream` | SSE，每 1 秒 `event: state`，`data:` 同 `/api/state` |
| GET | `/api/image/<相对路径>` | 图片二进制（不变） |
| GET | `/api/config/<service>` | `collect` / `process` 的配置字段（含 `env` 现值、字段提示） |
| POST | `/api/config` | `{service, values}` 保存配置 |
| GET | `/api/config/ai` | token 鉴权，返回接入点列表、`active_id`、配置 `version`；不返回密钥 |
| POST | `/api/config/ai` | `{endpoints, version}` 保存列表与编辑；不改变当前使用项，空密钥保留原值 |
| POST | `/api/config/ai/use` | `{endpoints, version, id}` 保存编辑并明确切换使用项；完整配置验证后原子写入 |
| POST | `/api/service/toggle` | `{service, enabled}`，`service ∈ {collect, process}` |
| POST | `/api/channels/toggle` | `{channel, enabled}`：`plugin` 渠道 = 启用/停用推送；`process` 渠道 = 真的启停进程 |
| POST | `/api/item/update` | `{id, ...可改字段}` |
| POST | `/api/item/delete` | `{id}` |
| POST | `/api/cookies/import` | `{content}` |
| GET | `/api/config/alison` | 需看板 token：仅配置弹窗明文回显 APPID/SECRET/环境/图文设置 + 已知群目录 + 刷新进度，JSON 禁止缓存 |
| POST | `/api/config/alison` | `{values:{APPID,SECRET,SANDBOX,ALISON_WITH_IMAGE,ALISON_DISCOVER_GROUPS}}`；审计只写字段名 |
| POST | `/api/config/alison/groups/add` | `{group_openid}`：手动补录已知群 OpenID，默认关闭推送 |
| POST | `/api/config/alison/groups/toggle` | `{group_openid,enabled}`：逐群开关；已退群不能开启 |
| POST | `/api/config/alison/groups/refresh` | `{}` 手动刷新全部已知群；`{"missing_only":true}` 在打开群目录时自动补齐尚未查询的新群资料。后台 30 QPM 调 QQ 官方 GET `/v2/groups/{group_openid}/info`（此 API 可能只对白名单机器人开放） |

QQ 官方文档只提供[按 OpenID 获取指定群信息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_info.get.html)，没有枚举全部已入群群聊的接口。
群目录通过[机器人入群事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_add_robot.html)、退群事件和群消息维护；
历史未记录的群可在配置弹窗手动补录 OpenID 后刷新。开启艾莉森渠道时，BotPy 长连接负责群事件与正式分发；**不会自动发任何 QQ 测试消息**。

`services` 里只剩 **`collect` / `process`**（分发不再是"一个服务"，而是若干渠道，见 `/api/channels`）。
看板进程启动后自动启动 `process` 服务扫描全库；采集服务仍需手动启动。

`channels[]` 元素：

```json
{
  "id": "maibot", "label": "麦麦插件", "adapter": "plugin", "managed": false,
  "enabled": true, "connected": true, "last_seen": "2026-09-21T10:00:00",
  "note": "idle", "pending": 3, "sent_total": 12,
  "targets": [
    {"kind": "private", "label": "私聊", "source": "maibot:private", "pending": 2, "sent_total": 5},
    {"kind": "group_123", "label": "群 123", "source": "maibot:group_123", "pending": 1, "sent_total": 7}
  ]
}
```

`stats` = `{item_count, pending_count, latest_id, latest_rev}`。

---

## 3. 看板页面结构

- 顶部栏：标题 + **tab 切换**（`处理` / `分发`）+ 更新时间。
- **处理** tab：
  - 第一行卡片：`采集服务`、`处理服务`、`运行摘要`（运行摘要顶替原来的"分发服务"卡片位置）。
  - 处理卡片显示当前处理阶段/条目、剩余待处理数；点击剩余数弹出完整待处理列表，列出缺失灯/字段、累计次数和轮转重试状态。
  - 下面内容列表**占满 100% 宽度**（原来的右侧 OVERVIEW 栏取消，摘要已在上面）。
- **分发** tab：渠道卡片（状态徽标、targets、待发、累计发送、最近活动、`启动/停止`、`配置`），
  下面"最近分发记录"表（`/api/pushes`）。
- 详情 / 配置两个 `<dialog>` 保留原有交互（含 `i` 图标 tooltip、最终判定与无效原因、筛选、分页）。

处理服务的配置入口分为 **AI 配置** 和 **处理配置**，分别打开独立弹窗。处理配置保留左右两栏字段；AI 配置左侧卡片点击仅切换编辑对象，左侧「添加接入点」下方的「使用此接入点」按钮应用当前选中的接入点，才切换实际使用项。保存 AI 配置不隐式切换使用项，当前使用项禁止删除。

所有弹窗点击遮罩、按 Esc 或点关闭按钮时：未修改直接关闭；有未保存修改则提供「保存并关闭 / 不保存 / 继续编辑」。保存失败保持弹窗和输入；AI 卡片选择、详情语言切换和 Alison 群目录刷新不算修改。保存期间不允许关闭；弹窗内开始拖选文字、结束于遮罩不触发关闭。

多个接入点（含密钥、各自的处理间隔和并行数量）保存在不入库的 `config/ai_endpoints.json`，顶层 `active_id` 指定使用项，`endpoints` 保存列表。JSON 不存在时只为首次迁移读取旧 `.env`；创建后以 JSON 为唯一配置来源。UTF-8 原子写入，版本仅校验 AI 文件，其他服务的配置保存不再干扰 AI 保存。审计仅记录接入点 ID，接口不返回明文密钥。AI 调用前检查 JSON 的变化，下一次请求读取新配置。
# 网页采集状态扩展（2026-10-14）

`/api/state`、`/api/snapshot` 和 SSE 的状态新增可选 `collection`，兼容原有字段：

```json
{
  "sources": [{"source":"rss:spacenews","label":"SpaceNews","last_run":0,"state":"ok","discovered":0,"collected":0}],
  "warnings": [{"source":"snapi","url":"https://example.org/article","stage":"fulltext","error_type":"BODY_NOT_FOUND","detail":"未取得完整正文","updated_at":0}],
  "failed_count": 0
}
```

此处 `source` 是采集来源键，不是渠道投递 source。失败候选不写入 `items`，不计入处理池或待发队列。看板只读采集账本；`warnings` 返回最近 50 条当前告警，详细采集日志仍写入 `logs/`。

`collection.sources` 固定包含清单内全部 21 个网页来源，未运行的来源也返回，另保留账本中未知来源。每行新增 `type`、`url`、`started_at`、`stored_count`（当前主库本来源条数）、`total_collected`（账本已成功候选数）、`pending_count`、`failed_count`、`warning_count`、`last_error`。`state` 为 `never`（尚未采集）、`running`（采集中）、`ok`、`warning`、`error` 或 `interrupted`（上次采集进程已退出且未完成）。`last_run` 是最近采集进度更新时间；`discovered` 和 `collected` 分别表示本轮新增候选、本轮入库数量，采集中随候选登记和成功入库实时增长，失败收尾保留已完成的计数。正文失败和待采集分开统计，尚未运行不显示为失败。

采集配置增加 `WEB_COLLECT_INTERVAL_SECONDS`（默认 900，整数秒且至少 10）和 `SOURCES`（`x,rss,snapi,sina` 子集，空为全部）。web 与 X 独立调度，仍由一个“采集”服务统一启停；修改 SOURCES 后重启采集服务。采集启动仍需要手动操作，渠道协议和发送回执规则不变。

历史候选或已入库数据存在、但没有完整轮次状态时返回 `unrecorded`（状态待确认）。账本读取失败仍返回全部来源，状态为 `error`，未知数量返回 `null`，前端显示“—”，不伪报为零。

每个 AI 接入点独立保存 `ANALYSIS_PAUSE_SECONDS`（每个并行任务结束后的间隔，非负秒数）和 `ANALYSIS_CONCURRENCY`（1至32，默认1）。处理配置不再编辑间隔。运行中的请求继续完成，下一条任务读取新的接入点，调度器同步调整并行上限和间隔。

图片条目新增 `image_versions`（路径到文件版本），用于共享图片升级后的浏览器缓存刷新。稳定 `.img` 路径按实际编码返回 MIME。渠道状态新增 `inflight`（拉取出去尚未回报的条数），整理图片前必须停用渠道并等待归零。
