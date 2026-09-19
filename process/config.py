# -*- coding: utf-8 -*-
"""Processing configuration and DeepSeek environment."""
import os
from pathlib import Path
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parents[1]
load_dotenv(ROOT/'.env', encoding='utf-8')
load_dotenv(ROOT/'collect'/'.env', encoding='utf-8')
DEEPSEEK_KEY=os.getenv('DEEPSEEK_KEY','').strip()
DEEPSEEK_MODEL=os.getenv('DEEPSEEK_MODEL','deepseek-chat').strip()
DEEPSEEK_BASE='https://api.deepseek.com/v1'
DEFAULT_TARGET_PRIVATE='YOUR_QQ_ID'
DEFAULT_TARGET_GROUP='YOUR_GROUP_ID_1'
