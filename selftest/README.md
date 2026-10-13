# Selftest（改完代码先跑）

离线冒烟测试：不联网、不需要真实账号、不调用真实模型 API、不发送 QQ 消息。

## 跑法

```powershell
python selftest\run_all.py
```

当前自检覆盖：

- `check_pipeline.py`：collect/process 入口、打分规则、条目构造、跳视频、真图筛选；
  末段 [9]/[9b] 覆盖**分发队列口径**（pushed 字段、source 注册/回填、标记已发、旧 JSON 一次性迁移）。
- `check_channels.py`：**分发渠道**——真 socket 起一个 WS 服务端，跑通注册 → 推送/拉取 →
  回执 → 写库 → 退避；含鉴权、内置渠道、插件镜像客户端。
- `check_ali.py`：`distribute/alison` 导入、排版和候选挑选；沙箱 `.env` / SQLite 群目录、
  明文配置保存、逐群开关、官网信息 API 的 mock 刷新及无权限回退、入群/退群事件。
- `check_plugin.py`：MaiBot 插件两种加载方式、Mixin、调度、Hook、开关和状态落盘；
  调度用例用**渠道替身**（不连 WS），验证"按节奏 want → 发 → 回报 ok/false"。
- `check_dashboard.py`：服务标签、HTTP API 契约（`{ok,data}` 信封 + 渠道接口），
  token 保护的艾莉森配置/逐群开关和 no-store 响应，停止服务杀整棵树、分页与增量快照、启动器。
- `stubs/`：`openai`、`twscrape`、`maibot_sdk` 的离线桩。
- `.sandbox/`：自检临时配置与日志，不是真实 `data/` 或 `config/`。

修改后建议同时执行：

```powershell
python -m compileall -q collect process distribute dashboard selftest tools main_flow.py
python selftest\run_all.py
python dashboard\server.py --help
python tools\migrate_layout.py --dry-run
```

真机抓取、真实数据库迁移、MaiBot 启动和 QQ 发送属于人工联调，不在离线自检范围内。
