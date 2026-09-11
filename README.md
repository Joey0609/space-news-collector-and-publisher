# 78-UMa · 航小麦

给 QQ 机器人「**麦麦 (MaiSaka)**」配的**航天新闻助手**：

> 自动盯 X 上的航天账号 → 翻译成中文 → 下载配图 → 打标打分 → 去重 → 交给麦麦，按受控节奏推送到私聊 / 群。

本项目只包含 **航小麦自有部分**；**麦麦本体 (MaiBot) 与 NapCat 为第三方，未包含在本仓库**。

## 目录结构
| 路径 | 说明 |
|---|---|
| `抓取脚本/` | 采集管线：`news_fetch.py`（增量抓取）、`judge.py`（打标打分）、`ai_fallback.py`（AI 兜底）、`main_flow.py`（主流程） |
| `麦麦插件/hangxiaomai_news/` | 麦麦推送插件（受控节奏推送、发图、转发取原帖等） |
| `配置模板/` | 推送默认 / 评分规则 / 功能开关 的模板（QQ 号已用占位符） |
| `项目指南.md` | 项目说明与设计（已脱敏） |

## 快速开始
1. **填环境变量**：把 `抓取脚本/.env.example` 复制为 `抓取脚本/.env`，填入 `PROXY_URL`、`DEEPSEEK_KEY`、`COOKIES_PATH` 等。
2. **装依赖**（Python 3.12）：`twscrape`、`openai`、`httpx`、`python-dotenv`。
3. **跑采集**：`python 抓取脚本/main_flow.py`（可配计划任务定时，如每 10 分钟）。
4. **装插件**：把 `麦麦插件/hangxiaomai_news/` 放进 MaiBot 的 `plugins/` 目录，并修改其 `config.toml` 里的 `target_qq` / `target_groups`。

## 安全说明
- 所有密钥、账号、Cookie 都从 `.env` 读取，**不入库**（见 `.gitignore`）。
- 代码与配置里的 `YOUR_QQ_ID` / `YOUR_GROUP_ID` / `YOUR_WS_TOKEN` 等均为**占位符**，请替换为你自己的。
- 请勿把 `.env`、`cookies`、数据库、日志等提交到仓库。

## 许可
见仓库说明。
