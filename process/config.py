# -*- coding: utf-8 -*-
"""处理阶段的配置：通用"模型 API" + 翻译/处理参数。

模型 API 支持三种协议（MODEL_PROTOCOL）：
    openai     OpenAI 兼容的 /chat/completions
               —— OpenAI、通义、Moonshot、vLLM、Ollama、one-api/new-api 网关都走这个
    responses  OpenAI Responses API 的 /responses
    anthropic  Anthropic 兼容的 /messages
               —— Claude 官方，以及各家中转网关

**没有内置服务商默认值**：MODEL_API_BASE / MODEL_NAME / MODEL_API_KEY 三项必须自己配，
缺任一项就不发请求（`is_configured()` 为假），错误信息里会列出缺哪几项。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env', encoding='utf-8')
load_dotenv(ROOT / 'collect' / '.env', encoding='utf-8')

PROTOCOLS = ('openai', 'responses', 'anthropic')


def _env(*names: str) -> str:
    """按顺序取第一个非空环境变量。"""
    for name in names:
        value = os.getenv(name, '').strip()
        if value:
            return value
    return ''


# ---- 通用模型 API ----
MODEL_API_BASE = _env('MODEL_API_BASE').rstrip('/')
MODEL_NAME = _env('MODEL_NAME')
MODEL_API_KEY = _env('MODEL_API_KEY')
MODEL_PROTOCOL = _env('MODEL_PROTOCOL').lower() or 'openai'
if MODEL_PROTOCOL not in PROTOCOLS:      # 写错就给个安全默认值，不要静默乱发请求
    MODEL_PROTOCOL = 'openai'
MODEL_TIMEOUT = float(_env('MODEL_TIMEOUT') or 90)
# 单次输出上限：0 = 不显式传，交给服务端默认（避免某些模型/网关不接受 max_tokens）。
# Anthropic 协议必填，为 0 时用 4096 兜底。
MODEL_MAX_TOKENS = int(_env('MODEL_MAX_TOKENS') or 0)
ANTHROPIC_VERSION = _env('ANTHROPIC_VERSION') or '2023-06-01'


def missing_config() -> list:
    """列出还没配的必需项，给日志/报错用（不含 key 的值）。"""
    missing = []
    if not MODEL_API_KEY:
        missing.append('MODEL_API_KEY')
    if not MODEL_API_BASE:
        missing.append('MODEL_API_BASE')
    if not MODEL_NAME:
        missing.append('MODEL_NAME')
    return missing


# ---- 翻译 ----
# 每轮开始时会把"没翻译过 / 翻译失败"的条目补齐；失败的单条最多重试这么多次。
TRANSLATE_MAX_ATTEMPTS = int(_env('TRANSLATE_MAX_ATTEMPTS') or 3)
TRANSLATE_PAUSE_SECONDS = float(_env('TRANSLATE_PAUSE_SECONDS') or 0.5)
# 免费/共享模型经常 429（该模型访问量过大）或超时，这类"临时失败"当场等一会儿重试，
# 且不消耗上面的重试次数，避免把整批条目一次性判死。
TRANSLATE_INLINE_RETRIES = int(_env('TRANSLATE_INLINE_RETRIES') or 2)
TRANSLATE_RATE_LIMIT_WAIT = float(_env('TRANSLATE_RATE_LIMIT_WAIT') or 20)

# 判"第 2 盏灯（不重复）"时，给模型看多少条"最近已入库标题"用来对比；0 = 不给清单（只看文本线索）
AI_REPEAT_CONTEXT = int(_env('AI_REPEAT_CONTEXT') or 40)

DEFAULT_TARGET_PRIVATE = 'YOUR_QQ_ID'
DEFAULT_TARGET_GROUP = 'YOUR_GROUP_ID_1'
