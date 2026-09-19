# Selftest（改完代码先跑）

离线冒烟测试：不联网、不需要真实账号、不调用真实 DeepSeek、不发送 QQ 消息。

## 跑法

```powershell
python selftest\run_all.py
```

当前自检覆盖：

- `check_pipeline.py`：collect/process 入口、打分规则、条目构造、跳视频、真图筛选。
- `check_ali.py`：`distribute/alison` 导入、排版、候选选择。
- `check_plugin.py`：MaiBot 插件两种加载方式、Mixin、调度、Hook、开关和状态落盘。
- `stubs/`：`openai`、`twscrape`、`maibot_sdk` 的离线桩。
- `.sandbox/`：自检临时配置与日志，不是真实 `data/` 或 `config/`。

修改后建议同时执行：

```powershell
python -m compileall -q collect process distribute hx_common dashboard selftest tools main_flow.py
python selftest\run_all.py
python dashboard\server.py --help
python tools\migrate_layout.py --dry-run
```

真机抓取、真实数据库迁移、MaiBot 启动和 QQ 发送属于人工联调，不在离线自检范围内。
