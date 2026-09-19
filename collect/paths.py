# -*- coding: utf-8 -*-
"""Canonical repository/data paths for collect, process and distribute."""
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent   # collect/
ROOT = SEED_DIR.parent                       # repository root
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
WORK_DIR = DATA_DIR / "work"
IMAGE_DIR = DATA_DIR / "images"
QUEUE_DIR = DATA_DIR / "queue"
STATE_DIR = DATA_DIR / "state"
MARKDOWN_DIR = DATA_DIR / "markdown"
OUTPUT_DIR = RAW_DIR                         # compatibility name for old collect code
CONFIG_DIR = ROOT / "config"

PENDING_FILE = QUEUE_DIR / "待推送.json"
SENT_FILE = QUEUE_DIR / "已发记录.json"
QUEUE_ARCHIVE_FILE = QUEUE_DIR / "队列档案.json"
ARCHIVE_FILE = QUEUE_DIR / "内容留档.json"
BASELINE_FILE = STATE_DIR / "抓取基线.json"
CURSOR_FILE = STATE_DIR / "扫描游标.json"
ACCOUNTS_DB = STATE_DIR / "accounts.db"
RUN_STATUS_FILE = STATE_DIR / "run_status.json"
AUDIT_FILE = STATE_DIR / "audit.jsonl"
DASHBOARD_TOKEN_FILE = STATE_DIR / "dashboard_token.txt"
DB_FILE = DATA_DIR / "hx.sqlite3"

MAX_PENDING = 100
MAX_SENT = 500


def config_file(name: str) -> Path:
    return CONFIG_DIR / name


def image_path(name: str, day: str = "") -> Path:
    day = day or __import__('datetime').datetime.now().strftime('%Y%m%d')
    path = IMAGE_DIR / day
    path.mkdir(parents=True, exist_ok=True)
    return path / name


def ensure_data_dirs() -> None:
    for path in (RAW_DIR, WORK_DIR, IMAGE_DIR, QUEUE_DIR, STATE_DIR, MARKDOWN_DIR, LOG_DIR, CONFIG_DIR):
        path.mkdir(parents=True, exist_ok=True)
