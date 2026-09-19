# -*- coding: utf-8 -*-
"""配置与状态：读 艾莉森网关/.env、读写 state.json。

.env（模板见 .env.example）：
    APPID / SECRET    QQ 开放平台机器人凭据
    SANDBOX=1         走沙箱域名（默认正式环境）

state.json：网关在群里被 @ 时记录下来的 group_openid / user_openid。
"""
import datetime
import os
from typing import Callable

from .jsonio import load_json, save_json
from .paths import ENV_FILE, STATE_FILE


def load_env() -> dict:
    """读 .env；同一键优先用进程环境变量（便于临时覆盖）。文件不存在返回 {}。"""
    env: dict = {}
    try:
        if ENV_FILE.exists():
            for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
                s = raw.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    for k in ("APPID", "SECRET", "SANDBOX", "SANDBOX_GROUP"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    return env


def is_sandbox(env: dict) -> bool:
    """SANDBOX=1/true/yes/on → 走沙箱环境。"""
    return str(env.get("SANDBOX", "0")).strip().lower() in ("1", "true", "yes", "on")


def api_host(env: dict) -> str:
    """按环境返回 OpenAPI 域名。"""
    return "https://sandbox.api.sgroup.qq.com" if is_sandbox(env) else "https://api.sgroup.qq.com"


def credentials(env: dict) -> tuple[str, str]:
    """取 (appid, secret)，都是去空格后的字符串。"""
    return str(env.get("APPID", "")).strip(), str(env.get("SECRET", "")).strip()


def load_state() -> dict:
    return load_json(STATE_FILE, {}) or {}


def save_state(d: dict) -> None:
    save_json(STATE_FILE, d)


def update_state(**fields) -> dict:
    """合并写入 state.json（保留已有字段），返回写入后的内容。"""
    st = load_state()
    st.update(fields)
    st["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_state(st)
    return st


def group_openid(log: Callable[[str], None] = print) -> str:
    """取群里发消息用的 group_openid；没有则记一条提示日志并返回 ''。"""
    goid = str(load_state().get("group_openid", "")).strip()
    if not goid:
        log("❌ state.json 里没有 group_openid：先在群里 @ 一下艾莉森让 gateway.py 记录")
    return goid
