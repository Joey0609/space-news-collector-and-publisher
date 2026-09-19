# 78-UMa 三段式重构执行方案

## 目标

仓库按数据生命周期拆成三部分：

```text
collect/       原始数据收集：X 与未来其它源，原文、来源、图片
process/       数据处理：翻译、AI 分析、规则筛选、去重、队列、台账
 distribute/   数据分发：麦麦插件、艾莉森、NapCat、未来网页/API
```

看板 `dashboard/` 是网页展示与管理控制面；`hx_common/` 是三部分共享的路径、契约、数据库和导出基础；`data/` 是运行期数据，`config/` 是人维护的配置。

## 阶段与验收

### P1：结构搬迁，行为零变化

- 建立 `collect/`、`process/`、`distribute/`、`hx_common/`、`tools/`。
- 采集入口、处理入口、分发入口按职责移动，保留原文件名。
- 路径常量集中到 `hx_common/paths.py`。
- 旧入口保留兼容壳，计划任务和手动命令暂时不失效。
- `config_templates/`、`selftest/` 完成英文目录迁移。
- `python -m compileall` 与既有离线自检通过。

### P2：数据层

- 建立 `data/raw`、`data/work`、`data/images`、`data/queue`、`data/state`、`data/markdown`。
- SQLite `data/hx.sqlite3` 保存内容台账与分发台账。
- markdown 由数据库幂等导出。
- `tools/migrate_layout.py` 支持 `--dry-run` / `--apply`，迁移旧 `配置/`、`抓取脚本/output/`、图片和 JSON 路径。
- 数据写入失败不得阻断主流程。

### P3：处理边界

- 采集只负责原文、来源、图片，不调用模型翻译。
- 处理负责翻译、打标、评分、AI 兜底、队列和数据库更新。
- 建立 `RawItem` 数据契约与校验。
- 建立 `collect/sources/` 源适配器注册表，先迁移 X，预留 SpaceNews/NOTAM/MSI。

### P4-P5：看板

- P4：标准库 HTTP 后端、SQLite 查询、操作白名单、token、审计、SSE、任务进程、日志流。
- P5：原生 HTML/CSS/JS 前端，概览、队列、台账、分发、日志、设置六页；自绘图表、FLIP 列表动画、深浅色、响应式、键盘可达和 `prefers-reduced-motion`。
- 默认监听 `127.0.0.1:8787`，远程访问必须显式开启并提供 token。

### P6：文档与收口

- 更新 `AGENTS.md`、`README.md`、`project_guide` 提示、各部分 README。
- 更新自检为 7 类检查：collect、process、plugin、alison、db、migration、dashboard。
- 明确兼容壳删除时机与真实部署步骤。

## 数据契约

### RawItem

采集产出统一包含：

```json
{
  "platform": "x",
  "source": "x",
  "origin_id": "platform-native-id",
  "account": "publisher",
  "orig_account": "",
  "forward_comment": "",
  "title": "",
  "raw": "original content",
  "zh": "",
  "url": "",
  "published_ts": 0,
  "published_date": "",
  "content_type": "文本",
  "images": [],
  "has_media": false,
  "collected_at": "",
  "stage": "collected"
}
```

`zh` 在 P3 由 process 填写。队列继续保留原有字段，并追加 `id/source/platform/origin_id/url`。`sent_to` 的 `private`、`group_<id>` 语义保持不变。

## 数据库

SQLite 表：

- `items`：原始内容、译文、标签、分数、判定、处理阶段。
- `pushes`：分发通道、目标 kind、发送时间，唯一键防重复。
- `meta`：schema 版本与迁移元信息。

连接启用 WAL 和 busy timeout；所有 DB 写入 try/except 降级记录日志。

## 看板操作边界

只读：概览、趋势、队列、台账、日志、markdown 预览、配置状态。

常规：运行采集、运行处理、导出 markdown、自检。

确认后：编辑译文、删除/置顶队列、修改配置、切换功能开关、重置 sent_to。

高风险：手动向指定 QQ/群发送，必须 token、二次确认、限流和审计。麦麦手动发送通过 `data/state/手动任务.json` 交给插件执行，避免复制宿主会话逻辑。

## 迁移规则

1. 先停止计划任务、MaiBot、艾莉森临时推送。
2. `migrate_layout.py --dry-run` 只报告。
3. `migrate_layout.py --apply` 创建备份清单后迁移。
4. JSON 中旧图片绝对路径统一改成 `data/images/...`，读取端继续兼容绝对路径。
5. 迁移后跑全量自检，再启动服务。
6. 旧路径兼容壳确认不再使用后再删除。

## 不在本次范围

- 真正接入 SpaceNews、NOTAM、MSI；只提供适配器模板。
- 商用网页/API 对外部署；本期看板仅本机管理台。
- 视频下载/发送、事件级去重、基线和游标完全迁入数据库。
- 不触发真实抓取、真实 QQ 消息发送。
