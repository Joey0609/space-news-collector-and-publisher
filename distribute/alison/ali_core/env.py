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
    env = load_env_file()
    for key in ("APPID", "SECRET", "SANDBOX", "SANDBOX_GROUP"):
        if os.getenv(key):
            env[key] = os.getenv(key)
    return env


def load_env_file() -> dict:
    """只读同目录 .env（不叠加进程环境变量），供 Dashboard 展示/编辑其实际持久配置。"""
    env: dict = {}
    try:
        if ENV_FILE.exists():
            for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def save_env_updates(updates: dict, allowed=None) -> list:
    """原样保留其它键，只更新允许的 .env 字段；返回实际更新的 key，不返回值。"""
    allowed = set(allowed or ("APPID", "SECRET", "SANDBOX"))
    clean = {}
    for key, value in (updates or {}).items():
        if key not in allowed or not isinstance(value, (str, int, float, bool)):
            continue
        text = str(value).strip()
        if "\n" in text or "\r" in text:
            raise ValueError("配置值不能包含换行")
        clean[key] = text
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.exists() else []
    seen = set()
    out = []
    for line in lines:
        key, sep, _ = line.partition("=")
        name = key.strip()
        if sep and name in clean:
            out.append(f"{name}={clean[name]}")
            seen.add(name)
        else:
            out.append(line)
    for key, value in clean.items():
        if key not in seen:
            out.append(f"{key}={value}")
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = ENV_FILE.with_suffix(ENV_FILE.suffix + ".tmp")
    temp.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    temp.replace(ENV_FILE)
    return sorted(clean)


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
