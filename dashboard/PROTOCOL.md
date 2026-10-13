# 航小麦 · 渠道协议 & 看板 API（v1，冻结）

分发队列的真值在数据库（`items.decision='发'` + `items.pushed`，见 `distribute/push_db.py`）。
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
`ori_url` `score`（= `ai_score` 有值就用，否则 `rules_score`）`ai_title` `ai_summary`
`importance` `ai_tags` `decision` `pushed{}`。

### 1.3 推送规则（服务端）

1. 收到 `hello` → `register_sources(通道的每个 target 一个 source)`；新 source 自动回填历史行。
2. 从 `pending(source=...)` 取该渠道**未发送**的（`decision='发'` 且 `pushed[source] != true`），id 升序，
   逐条推。**注册过还是新注册都一样**：都只推"确实没发过的"。
3. 回执处理：
   - `result ok=true` → `mark_pushed([item_id], source)` → 回 `ack` → 推下一条。
   - `result ok=false` → 保持未发，该 source 退避（默认 300 秒），推下一个 source 的条目。
   - `defer seconds` → 保持未发，该 source 退避 `seconds`（钳制 30 ~ 3600）。
   - 120 秒没回执 → 按 `defer 60` 处理并记日志（不回滚任何东西）。
4. 渠道 `enabled=0`、断线、收到 `pause` → 该渠道不推；重新启用/重连后继续从第 2 步来。
5. 队列里没有未发的条目 → 每 5 秒再看一次（空闲不计日志）。

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
| GET | `/api/process/backlog` | 唯一完整分析队列中的所有条目（翻译、四灯、标题、分数、标签或正文任一缺失），含缺失字段、尝试次数和重试上限状态；处理卡片剩余条数弹窗使用 |
| GET | `/api/items?offset=&limit=` | 分页条目 |
| GET | `/api/snapshot?offset=&limit=&since_id=&since_rev=` | 首屏/加载更多/增量补丁（`incremental` 标记） |
| GET | `/api/pushes?limit=` | 最近分发尝试（`push_attempts`，含成功/失败与群名）；`pushes` 只保留成功审计 |
| GET | `/api/channels` | 渠道列表（含每个 target 的待发条数、累计发送数） |
| GET | `/api/channels/endpoint` | `{"ws_url","token","protocol":1}`；必须带 `X-HX-Token`，不得向未授权访问者泄露 token |
| GET | `/api/stream` | SSE，每 1 秒 `event: state`，`data:` 同 `/api/state` |
| GET | `/api/image/<相对路径>` | 图片二进制（不变） |
| GET | `/api/config/<service>` | `collect` / `process` 的配置字段（含 `env` 现值、字段提示） |
| POST | `/api/config` | `{service, values}` 保存配置 |
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
  - 处理卡片显示当前处理阶段/条目、剩余待处理数；点击剩余数弹出完整待处理列表，列出缺失灯/字段和重试上限。
  - 下面内容列表**占满 100% 宽度**（原来的右侧 OVERVIEW 栏取消，摘要已在上面）。
- **分发** tab：渠道卡片（状态徽标、targets、待发、累计发送、最近活动、`启动/停止`、`配置`），
  下面"最近分发记录"表（`/api/pushes`）。
- 详情 / 配置两个 `<dialog>` 保留原有交互（含 `i` 图标 tooltip、四盏灯、筛选、分页）。
