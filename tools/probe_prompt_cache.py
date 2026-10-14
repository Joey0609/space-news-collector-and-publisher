"""真实接口缓存对比；只读内容，不写分析结果或触发分发。"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'process'), str(ROOT)]
import ai
from process.logging_util import make_log, setup_console_encoding


def main():
    setup_console_encoding()
    log = make_log(ROOT / 'logs' / 'prompt_cache_probe.log')
    if not ai.is_configured() or ai.MODEL_PROTOCOL != 'openai':
        raise RuntimeError('本对比需要已配置的 OpenAI Chat Completions 接口')
    with sqlite3.connect((ROOT / 'data' / 'hx.sqlite3').as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE length(trim(raw))>80 ORDER BY length(raw),id LIMIT 2")]
        known = [f"#{r['id']} {r['ai_title']} | {r['ai_summary']}" for r in conn.execute("SELECT id,ai_title,ai_summary FROM items WHERE stage='processed' ORDER BY id DESC LIMIT 40")]
    if len(items) != 2:
        raise RuntimeError('需要两条已有内容进行对比')
    client = ai.get_client().with_options(max_retries=0, timeout=90)
    results = []
    destination = ROOT / 'logs' / 'prompt_cache_probe.json'
    payload = {'started_at': datetime.now().isoformat(), 'model': ai.MODEL_NAME, 'base': ai.MODEL_API_BASE, 'results': results}
    for repeat in range(2):
        for item in items:
            prompt = ai.build_analysis_prompt(item['raw'], item['account'], known, item['quoted_account'], item['quoted_raw'])
            # 第二轮反转顺序，减少固定先后顺序对耗时比较的影响。
            for variant in (('before', 'after') if repeat == 0 else ('after', 'before')):
                instructions, content, key = ai._cache_input(prompt, ai.MODEL_NAME)
                if variant == 'before':
                    instructions, content = ai.SYSTEM_PROMPT, prompt
                kwargs = dict(model=ai.MODEL_NAME, temperature=0.0, max_tokens=1600,
                              messages=[{'role': 'system', 'content': instructions}, {'role': 'user', 'content': content}])
                row = {'repeat': repeat + 1, 'item_id': item['id'], 'variant': variant}
                start = time.perf_counter()
                try:
                    response = ai._cached_request(client.chat.completions.create, kwargs, key) if variant == 'after' else client.chat.completions.create(**kwargs)
                    row['usage'] = response.usage.model_dump() if response.usage is not None else None
                    row['finish_reason'] = response.choices[0].finish_reason
                    row['cache_key_rejected'] = (ai.MODEL_API_BASE, ai.MODEL_PROTOCOL, ai.MODEL_NAME) in ai._cache_key_unsupported if variant == 'after' else False
                    ai._record_cache(response.usage, ai.MODEL_NAME)
                except Exception as exc:
                    row.update(error_type=type(exc).__name__, status_code=getattr(exc, 'status_code', None))
                row['seconds'] = round(time.perf_counter() - start, 3)
                results.append(row)
                temporary = destination.with_suffix('.tmp')
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(destination)
                details = (row.get('usage') or {}).get('prompt_tokens_details') or {}
                log(f"{len(results)}/8 {variant} item={item['id']} 耗时={row['seconds']}秒 缓存token={details.get('cached_tokens', '未提供')} 错误={row.get('error_type', '无')}")
    client.close()


if __name__ == '__main__':
    main()
