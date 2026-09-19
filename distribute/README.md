# 分发协议

所有分发通道只负责消费 `data/queue/待推送.json`：

1. 选择自己的 `kind` 且不在 `sent_to` 的条目。
2. 成功发送后再写回 `sent_to`。
3. 追加 `已发记录.json`，并向 SQLite `pushes` 写入幂等记录。
4. 不抓取、不翻译、不修改评分判定。

当前通道：

- `maibot/`：MaiBot 插件，受控节奏推送。
- `alison/`：QQ 官方机器人，图文转述。
- `napcat/`：OneBot11 联调发送端。

未来网页/API 通道遵循同一协议。
