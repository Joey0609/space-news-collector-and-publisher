# -*- coding: utf-8 -*-
"""把代码里真正发出去的提示词原样导出成 markdown（避免手抄出错）。"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO, REPO / "process"):
    sys.path.insert(0, str(p))

import ai  # noqa: E402

KNOWN = ["#56 SpaceX 获 NASA 近 10 亿美元载人合同", "#54 星舰第 11 次试飞完成"]
RAW = "SpaceX just got nearly a billion dollars from NASA to fly three more astronaut missions to the International Space Station"
ZH = "SpaceX 刚从 NASA 拿到近 10 亿美元，再飞三次载人任务前往国际空间站。"

parts = []
parts.append("# 给 AI 的提示词全文\n")
parts.append("> 这份文件是用 `process/ai.py` 里的真实提示词生成的（不是手抄）。")
parts.append("> 想改提示词就改 `process/ai.py`，改完重跑这个脚本即可刷新：")
parts.append("> `.venv\\Scripts\\python.exe tools\\dump_prompts.py`\n")

parts.append("## 1. 完整分析（翻译 + 四盏灯 + 标题/要点/重要度 + 标签）\n")
parts.append("调用点：`translate_db.py` 新条目那一遍。`{known}`、`{source}` 是自动拼进去的两段（没有就整段消失）。\n")
parts.append("```text")
parts.append(ai.build_analysis_prompt(RAW, "SPACEdotcom", KNOWN))
parts.append("```\n")

parts.append("## 2. 存量补标（只点四盏灯 + 打标，不翻译）\n")
parts.append("调用点：`translate_db.py` 补标那一遍（已有译文、没点灯的老条目）。\n")
parts.append("```text")
parts.append(ai.build_meta_prompt(RAW, ZH, "SPACEdotcom", KNOWN))
parts.append("```\n")

parts.append("## 3. 老条目语义打标兜底（只在条目完全没有 ai_tags 时才发）\n")
parts.append("调用点：`judge.py`。正常流程不会走到这里。\n")
parts.append("```text")
parts.append(ai.build_semantic_prompt(ZH))
parts.append("```\n")

parts.append("## 4. 边缘分数 AI 兜底（只有分数落在低阈值和高阈值之间才发）\n")
parts.append("调用点：`ai_fallback.py`。走四盏灯判定时不会用到。\n")
parts.append("```text")
parts.append(ai.build_fallback_prompt(
    {"zh": ZH, "raw": RAW, "ai_title": "SpaceX 获 NASA 近 10 亿美元载人合同",
     "ai_summary": "NASA 追加约 10 亿美元，再飞三次载人任务。", "importance": "中"},
    {"like": {"喜欢关键词": ["星舰", "载人"]}},
    {"like": {"喜欢关键词": ["猎鹰9"]}},
))
parts.append("```\n")

parts.append("## 5. 模型必须回的 JSON 形状\n")
parts.append("```json")
parts.append('{"zh":"译文",'
             '"is_news":true,'
             '"not_repeat":true,'
             '"repeat_of":"",'
             '"not_comment":true,'
             '"not_sensitive":true,'
             '"title":"标题（≤20字）",'
             '"summary":"要点（≤60字）",'
             '"importance":"高|中|低",'
             '"score":92,'
             '"tags":["领域:发射","对象:星舰","机构:SpaceX"]}')
parts.append("```\n")
parts.append("四盏灯全 true = 全绿 = 放行；任何一盏 false = 红灯 = 丢"
             "（`not_repeat=false` 时 `repeat_of` 写清单里的编号）。\n")
parts.append("`score` 是 AI 价值分 0~100（看板显示「AI 分」，存 `items.ai_score`）；"
             "想让它参与门控就配 `[gate] \"AI分下限\" = 40`。\n")

out = REPO / "提示词.md"
out.write_text("\n".join(parts), encoding="utf-8")
print("written:", out, len("\n".join(parts)), "chars")
