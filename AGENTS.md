# 78-UMa / 航小麦 · AI 启动必读

## 当前结构

```text
collect/       数据收集：X 采集、图片、源适配器
process/       数据处理：翻译、打标、评分、AI 兜底
 distribute/   数据分发：maibot / alison / napcat
dashboard/     本机数据看板与管理台
hx_common/     路径、契约、SQLite、Markdown 基础
data/          运行期数据，不入库
config/        人维护配置，不入库
```

旧目录 `抓取脚本/`、`艾莉森网关/`、`脚本/`、`麦麦插件/`、`配置模板/`、`自检/` 已删除，不保留兼容壳。

## Python 环境

所有看板托管服务固定使用根目录 `.venv\Scripts\python.exe`，不要使用系统全局 Python。项目环境已安装 `twscrape`、`openai`、`httpx`、`python-dotenv`、`websockets`、`qq-botpy`。采集配置来自根 `.env`；`WATCH_ACCOUNT` 为空时默认使用 `Space_Alioth`，`COOKIES_PATH` 为空时会给出明确文件路径错误。



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
  -> process/translate_db.py     缺译文→完整分析；有译文但缺灯/缺分/缺标签→只补判断（不动 zh）。
                                 产出：zh 译文 / 四盏灯 is_news·not_repeat·not_comment·not_sensitive /
                                 ai_score AI 价值分(0~100) / ai_title / ai_summary / importance / ai_tags，
                                 stage=translated
  -> process/judge.py            四绿→发、任一红→丢（老条目走规则分+阈值），score=AI分/rules_score/decision，stage=judged
  -> process/ai_fallback.py      final_decision，stage=processed
  -> data/queue/待推送.json
  -> distribute/maibot 或 distribute/alison
```

`main_flow.py` 会按上述顺序调用采集、翻译、判定、AI 兜底，并把最终判定为“发”的条目放入队列。
（`process/translate.py` / `judge.py` 也支持旧的 JSON 入参，但主流程只走 SQLite。）

## 数据位置

- `data/raw/`：采集原始 JSON。
- `data/work/`：翻译、打分、AI 兜底中间结果。
- `data/images/`：图片。
- `data/queue/`：`待推送.json`、`已发记录.json`、内容留档和队列档案。
- `data/state/`：抓取基线、扫描游标、插件状态、看板 token、审计日志。
- `data/hx.sqlite3`：SQLite 台账，保存内容与分发记录。
- `data/markdown/`：从数据库导出的可读归档。
- `config/`：功能开关、推送规则、推送默认、用户/群偏好。

## 硬规则

1. 不触发真实抓取、真实 QQ 发送，除非用户明确要求。
2. 密钥、cookies、QQ 号只从 `.env` 读取，不提交。
3. 所有脚本必须写日志到 `logs/`。
4. JSON/TOML 写入要 UTF-8；中文 TOML key 必须加引号；重要 JSON 使用原子写。
5. 分发端只消费队列，不抓取、不翻译、不重新判定。
6. `sent_to` 的 kind 保持：`private`、`group_<群号>`。
7. 看板只绑定 `127.0.0.1`；写操作必须 token、白名单、审计。
8. 修改后至少跑：`python selftest\run_all.py` 和 `python -m compileall -q collect process distribute hx_common dashboard selftest tools main_flow.py`。

## 模块职责

### collect

- `collect/news_fetch.py`：采集入口。
- `collect/hx_core/scan.py`：分批、游标、增量、视频跳过。
- `collect/hx_core/x_client.py`：cookies、twscrape、关注列表、最新推文。
- `collect/hx_core/images.py`：下载 X 原图。
- `collect/sources/`：未来源适配器；X 当前稳定实现仍由 `hx_core/scan.py` 执行。

当前限制：每个账号每轮只取最新一条；视频/GIF 跳过；`PAUSE_SECONDS` 尚未接线；账号多时轮询延迟较长。

### process

- `translate_db.py`：主流程入口，两遍，**缺什么就重做什么**：① 缺译文（zh 空/旧占位符/翻译失败）→ 完整分析（译文 + **四盏灯** `is_news/not_repeat/not_comment/not_sensitive` + **AI 价值分 `ai_score`(0~100)** + 标题/要点/重要度 + 标签）；② 有译文但**缺灯/缺 AI 分/缺标签** → 只补判断（**不重新翻译、不覆盖人工译文**，`--meta-only` 可只跑这遍）。判"不重复"时会带上"最近已入库标题清单"（`AI_REPEAT_CONTEXT` 条，0=关）。失败按"临时/永久/认证"分别记账重试，两遍各有自己的上限（`translate_attempts` / `meta_attempts`）。
- `translate.py`：保留的 JSON 版翻译入口，逻辑同上，读写 `data/raw` 与 `data/work`。
- `judge.py`：**四绿直接发、任一红直接丢**（`[gate] "AI分下限" > 0` 时四绿但 AI 分太低也丢；老条目没点灯时回到规则分+阈值+AI兜底）。`items.score` 优先用 AI 分、没分才用 `rules_score`。标签取自 `ai_tags` + `重要:高/中/低`；白名单/排除/阈值照旧。`[gate] 三灯放行 = false` 可退回纯打分。
- `ai.py`：全部提示词在这里；`tools/dump_prompts.py` 会把真实提示词导出成 `提示词.md`（改完提示词记得重跑它）。
- `ai_fallback.py`：边缘分数最终判断。
- `process_core_contract.py`：RawItem 归一化/校验起点。

### distribute

- `distribute/maibot/hangxiaomai_news/`：MaiBot 插件，部署时把该目录拷进 MaiBot 的 `plugins/`。
- `distribute/alison/`：QQ 官方机器人图文转述。
- `distribute/napcat/`：NapCat OneBot11 联调。

### dashboard

后端：`dashboard/server.py`，标准库 HTTP、JSON API、SSE、token、审计、采集/处理/自检操作。
前端：`dashboard/webui/`，原生 HTML/CSS/JS，深色界面、响应式布局、队列动画、减少动画模式。

实时与分页（看板要和采集/翻译同步）：

- `items.rev`：每次改这一行就 +1（`collect/db.py` 采集更新、`translate_db.py` 翻译/补标、
  `judge.py`、`ai_fallback.py`、看板改译文都 +1）。翻译是**就地更新**已有行、id 不变，
  只看 id 会漏掉"译文/四盏灯刚写好"，所以增量必须靠 rev。
- SSE（`/api/stream`）每秒推 `latest_id` / `latest_rev`；前端发现游标变大就拉
  `/api/snapshot?since_id=&since_rev=`，把"新条目 + 被更新过的条目"打补丁上屏（约 1 秒）。
- 前端每 15 秒（`RECONCILE_MS`）对"最新 ~ 当前显示的这一段"做一次全量核对
  （`/api/snapshot?offset=0&limit=shown`）：补漏、刷新变了的、删掉服务端已不存在的，防止 SSE 丢事件。
- 分页：默认 20 条（`PAGE_SIZE`），滚到视口下方还剩 10 条（`LOAD_AHEAD`）自动再加载 20 条
  （`/api/items?offset=&limit=`）；底部显示"已显示 X / 共 Y"和「加载更多」按钮。

## 数据库

`hx_common/db.py` 使用 SQLite 标准库：

- `items`：来源、原文、译文、评分、判定和处理阶段。
- `pushes`：通道、kind、发送时间，唯一键防重复。
- `meta`：schema 版本。

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

最终验收：离线自检三项全部通过；不代表已经执行过真实 X 抓取或真实 QQ 发送。
