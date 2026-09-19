# 78-UMa · 航小麦

给 QQ 机器人「**麦麦 (MaiSaka)**」配的**航天新闻助手**：

> 自动盯 X 上的航天账号 → 翻译成中文 → 下载配图 → 打标打分 → 去重 → 交给麦麦，按受控节奏推送到私聊 / 群。

本项目只包含 **航小麦自有部分**；**麦麦本体 (MaiBot) 与 NapCat 为第三方，未包含在本仓库**。

> 🤖 **如果你是 AI（或接手的人）**：先读 [`AGENTS.md`](AGENTS.md)（启动必读：模块地图、数据契约、
> 硬规则、已知坑、怎么跑），需求与历史看 `项目指南.md`。

## 目录结构
| 路径 | 说明 |
|---|---|
| `collect/` | 数据收集：X 增量采集、图片、源适配器；入口 `collect/news_fetch.py` |
| `process/` | 数据处理：`translate.py`、`judge.py`、`ai_fallback.py`，输出到 `data/work/` 与队列 |
| `distribute/maibot/` | MaiBot 插件，部署时把 `hangxiaomai_news/` 拷入 MaiBot 的 `plugins/` |
| `distribute/alison/` | QQ 官方机器人艾莉森：图文转述、网关、诊断脚本 |
| `distribute/napcat/` | NapCat OneBot11 联调发送端 |
| `hx_common/` | 共享路径、契约、SQLite 台账、Markdown 导出 |
| `data/` | 运行期数据：raw/work/images/queue/state/markdown/hx.sqlite3，不入库 |
| `config_templates/` | 推送默认 / 评分规则 / 功能开关模板 |
| `dashboard/` | 本机可视化看板：队列、状态、日志、运行控制；默认 `127.0.0.1:8787` |
| `selftest/` | 离线冒烟测试：`run_all.py` 一键跑 |
| `AGENTS.md` | **AI 启动必读**：模块地图 / 数据契约 / 硬规则 / 看板 / 已知坑 |
| `项目指南.md` | 项目需求与历史文档 |

## 快速开始
1. **填环境变量**：把根目录 `.env.example` 复制为根目录 `.env`，填入 `PROXY_URL`、`MODEL_API_BASE` / `MODEL_NAME` / `MODEL_API_KEY`、`COOKIES_PATH` 等。
2. **装依赖**（Python 3.12+）：采集需要 `twscrape`、`openai`、`httpx`、`python-dotenv`；看板只依赖 Python 标准库。
3. **跑完整流程**：`python main_flow.py`（可配根目录 `run_main_flow.bat/.ps1/.vbs` 每 10 分钟执行）。
4. **单独跑采集**：`python collect/news_fetch.py`；处理：`python process/translate_db.py`（翻译 + 是否新闻 + 标题/要点/重要度 + 标签，并自动给老条目补标）、`python process/judge.py`、`python process/ai_fallback.py`。
5. **启动看板**：`python dashboard/server.py`，打开 `http://127.0.0.1:8787`；启动终端会打印管理 token。
6. **安装插件**：把 `distribute/maibot/hangxiaomai_news/` 放进 MaiBot 的 `plugins/` 目录，并修改 `config.toml` 的 `target_qq` / `target_groups`。
7. **艾莉森（可选）**：在 `distribute/alison/.env` 填 `APPID` / `SECRET`；先运行 `python distribute/alison/gateway.py` 记录 `group_openid`，再运行 `python distribute/alison/推送.py --once`。
8. **迁移旧数据**：先 `python tools/migrate_layout.py --dry-run`，确认后 `--apply`。
9. **改完代码先自检**：`python selftest/run_all.py`。

## 安全说明
- 所有密钥、账号、Cookie 都从 `.env` 读取，**不入库**（见 `.gitignore`）。
- 代码与配置里的 `YOUR_QQ_ID` / `YOUR_GROUP_ID_1` / `YOUR_GROUP_ID_2` / `YOUR_WS_TOKEN` / `YOUR_LAPTOP_IP` / `YOUR_APPID` 等均为**占位符**，请替换为你自己的。
- 请勿把 `.env`、`cookies`、数据库、日志、`state.json` 等提交到仓库。

## 许可
见仓库说明。
