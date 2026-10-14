# 分发协议

**队列不是文件，是数据库状态**；**渠道不是写库的程序，而是连上看板的 WebSocket 客户端**。
完整协议见 [`../dashboard/PROTOCOL.md`](../dashboard/PROTOCOL.md)。

```
items.stage = 'processed' AND items.decision = '发'  ← 完整处理结束且最终判定要发
items.pushed   = {"<source>": true}    ← 我这个 source 发过没有（服务端写，渠道不碰库）
pushes                                 ← 每次成功发送的审计（source/item_id/sent_at）
```

## 两种投递模式

| 模式 | `hello.capabilities` | 谁发起 | 谁用 |
| --- | --- | --- | --- |
| **推送** | 不写 `pull` | 服务端逐条推 `item`，渠道回 `result` / `defer` | 艾莉森、NapCat |
| **拉取** | `{"pull": true}` | 渠道按自己节奏 `want`，服务端回 `items`；`pending` 可问还有多少 | 麦麦插件 |

- 渠道连上就 `hello` 注册（`channel` + 每个 `target` 一个 source）→ 服务端 `register_sources()`
  （新 source 会**回填**到历史行：旧 source 全发过的记 `true`，不把历史灌给新通道）。
- **只有 `result ok=true` 才会写库**（`pushed` 置 true + `pushes` 记时间 + `rev` +1）；
  `ok=false` / `defer` / 回执超时 → 条目保持未发，退避后重试。**失败千万别报成功**，否则内容就丢了。
- 粒度到"目标"而不是"通道"：私聊发过不会把群的那份也标成已发。

## 代码位置

- `push_db.py`：队列口径的唯一实现（只有看板侧读库）。
- `ws_codec.py`：RFC6455 最小实现（纯标准库，握手 + 帧编解码）。
- `ws_channel.py`：渠道 SDK（连接/重连/hello/收条/`result`/`defer`/`want`/`pending`）。
- `alison/channel.py`：艾莉森渠道（推送模式；看板「分发」页可启停）。
  所有启用群各自作为 `alison:group_<group_openid>` 注册；关闭群即不再注册该 source，
  发送前还会再查一次开关。`alison/gateway.py` 维持 BotPy 长连接，发送消息并接收入/退群及消息事件，
  **不再自动发送任何测试消息**。同目录 `推送.py` / `临时定时推送.py` 是手动调试工具，
  群消息发送函数也会检查群推送开关。

## QQ Bot 配置与群目录

- 看板「分发」→「艾莉森 QQ Bot」→「配置」弹窗，明文设置 APPID、SECRET、SANDBOX、图文及群发现开关。
  仅授权的配置 API 返回明文凭据，不写日志/审计/渠道摘要；配置保存在 `alison/.env`（不入库）。
- 群目录在 `data/hx.sqlite3` 的 `alison_groups` 表；来自 BotPy 入/退群事件、群消息及手动补录。
  已收到的群消息在艾莉森终端打印群 OpenID、成员 OpenID 和正文（不打印 auth_token 或附件链接），
  发现未知群自动入目录；新增群**默认关闭推送**，由用户逐群开启。
  [QQ 官方全量群消息事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)
  **要求在每个 QQ 群的机器人设置中把「可获取的群消息范围」设为「获取群内全部消息」，**
  并确认机器人拥有全量事件权限。`distribute/alison/botpy/` 是从 qq-botpy 1.2.1 整包内置的 MIT 许可源码，
  补入群全量事件解析器和 `group_and_c2c_event` 名称（与原 `public_messages` 同为 `1 << 25`）；
  无须安装外部 `qq-botpy` 包。若平台未开启，全量普通消息不会下发，仍可通过入群事件和群 @ 消息发现群。
  关闭群不丢历史推送状态，重新开启还可接着发。
- QQ 官方只提供[按已知 OpenID 查群基本信息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_info.get.html)，
  **没有列出全部历史已加入群的 API**。因此旧网关没见过的群必须等待事件，或手动补录 OpenID。
  「刷新群信息」只查询已知群，可显示名称/简介/分类/标签/人数，30 QPM；该接口可能需要白名单权限。
- 艾莉森渠道用同一个 BotPy 长连接监听群事件并发送消息；群发现开关只控制事件登记。
  更换凭据、沙箱环境或群发现开关后重启渠道；群推送开关在运行中约 3 秒内生效。
  `TARGET_GROUP_NUMBER` 已从配置移除：调试脚本的 `--group <已启用群OpenID>` 可显式选择群，
  不指定时只选群目录中第一个已启用群，不再引用旧固定群号。
  启动日志中的「渠道已登记」只是渠道 ID 已存在，不表示已发送旧新闻；没有启用群时会明确写出等待原因。

- `napcat/channel.py`：NapCat 联调渠道（需要 `.env` 的 `NAPCAT_WS_URL/TOKEN/TARGET_QQ`；
  没配置就如实回报失败，不会误标已发）。
- `maibot/hangxiaomai_news/`：MaiBot 插件（拉取模式）。`hx_ws.py` 是 SDK 的自包含镜像
  （插件按目录拷贝部署，不能 import 仓库代码）；自检 `check_channels.py` 第 [5] 段让两边
  跑同一个真 WS 服务端，保证行为一致。

## 加一个新渠道

1. 在 `dashboard/channels.py` 的 `BUILTIN_CHANNELS` 加一行（`id` / `label` / `adapter` /
   `command`，`adapter=process` 的才能被看板启停）。
2. 写一个入口脚本，用 `distribute.ws_channel.ChannelClient` 连
   `ws://127.0.0.1:8788/ws?token=<看板 token>`，`run_forever(on_item)` 里实现"发一条"，
   返回 `{"ok": True}` / `{"ok": False, "detail": ...}` / `{"defer": 秒数, "reason": ...}`。
3. 跑 `python selftest\run_all.py`（`check_channels.py` 覆盖注册/推送/拉取/回执/写库）。
