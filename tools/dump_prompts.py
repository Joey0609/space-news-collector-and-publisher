"""导出代码中实际使用的唯一完整分析提示词。"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT/'process'): sys.path.insert(0,str(p))
from ai import build_analysis_prompt, SYSTEM_PROMPT
text = '# 完整 AI 分析提示词\n\n一次输出全部内容字段、最终发/丢操作码与无效原因。\n\n## 系统提示\n```text\n'+SYSTEM_PROMPT+'\n```\n\n## 分析请求\n```text\n'+build_analysis_prompt('{实际原文}','{实际账号}',[], '', '')+'\n```\n'
(ROOT/'提示词.md').write_text(text,encoding='utf-8')
print('提示词已导出')
