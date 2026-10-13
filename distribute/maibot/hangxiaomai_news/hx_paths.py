# -*- coding: utf-8 -*-
"""hx_paths.py · 插件路径与内置默认值（"文件在哪、默认怎么配"的唯一出处）
=====================================================================
⚠️ 目录层级是**硬约定**：本插件必须放在  <麦麦项目根>/plugins/hangxiaomai_news/ ，
   因为"航小麦根目录"是靠向上数两层推出来的：
       <麦麦项目根>/plugins/hangxiaomai_news/hx_paths.py
       PLUGIN_DIR   = .../plugins/hangxiaomai_news
       PROJECT_ROOT = .../<麦麦项目根>        (= MaiBot-main)
       HX_ROOT      = .../<航小麦>            (= PROJECT_ROOT 的上一级)
   换部署位置时要么保持这个层级，要么改这里的三个常量。
"""
from pathlib import Path

# ── 目录层级 ──
PLUGIN_DIR = Path(__file__).resolve().parent          # .../plugins/hangxiaomai_news
PROJECT_ROOT = PLUGIN_DIR.parent.parent               # 麦麦项目根（MaiBot-main）
HX_ROOT = PROJECT_ROOT.parent                         # 航小麦根目录

# ── 航小麦 配置/数据文件（与 collect/process/alison 共用）──
CONFIG_DIR = HX_ROOT / "config"
DATA_DIR = HX_ROOT / "data"
QUEUE_DIR = DATA_DIR / "queue"
STATE_DIR = DATA_DIR / "state"
IMAGE_DIR = DATA_DIR / "images"
# 分发队列由看板持有（dashboard/channels.py）：插件作为 WebSocket 渠道连过去，
# 按自己的节奏 want 一批、发完回报 —— 插件不再直接读数据库、也不再碰旧队列 JSON。
# 连接凭据：看板 token；端口见 HX_WS_PORT 或默认 8788。
TOKEN_FILE = STATE_DIR / "dashboard_token.txt"
SWITCH_FILE = CONFIG_DIR / "功能开关.toml"
PUSH_DEFAULT_FILE = CONFIG_DIR / "推送默认.toml"
ARCHIVE_FILE = QUEUE_DIR / "内容留档.json"
REMINDER_FILE = CONFIG_DIR / "提醒待发.txt"
STATE_FILE = STATE_DIR / "推送状态.json"

# ── 麦麦本体配置（读取人设用）──
MAIBOT_CONFIG = PROJECT_ROOT / "config" / "bot_config.toml"

# ── 插件内置默认（推送默认.toml 缺失时兜底；应与 配置模板/推送默认.toml 一致）──
BUILTIN_PUSH_DEFAULTS: dict = {
    "新闻推送": True,
    "活跃时段": "09:00-21:00",
    "间隔最小分钟": 20,
    "间隔最大分钟": 40,
    "一次发几条": 1,
    "带图": True,
    "一天最多发几次": 0,
    "闲聊": True,
}

# 单条消息最多带几张图（QQ 限制）；超过则另凑一条消息
MAX_IMG_PER_MSG: int = 9


def config_path(name: str) -> Path:
    """取 配置/ 下某文件（避免各处拼字符串）。"""
    return CONFIG_DIR / name
