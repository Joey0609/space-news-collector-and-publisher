# -*- coding: utf-8 -*-
"""AI 接入点独立保存到本地 JSON；仅首次迁移时读取旧 .env。"""
import hashlib
import json
import math
import re
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

MODEL_FIELDS = ("MODEL_PROTOCOL", "MODEL_API_BASE", "MODEL_NAME", "MODEL_API_KEY",
                "MODEL_TIMEOUT", "MODEL_MAX_TOKENS", "ANALYSIS_PAUSE_SECONDS", "ANALYSIS_CONCURRENCY")
DEFAULTS = {"MODEL_PROTOCOL": "openai", "MODEL_API_BASE": "", "MODEL_NAME": "",
            "MODEL_API_KEY": "", "MODEL_TIMEOUT": "90", "MODEL_MAX_TOKENS": "0",
            "ANALYSIS_PAUSE_SECONDS": "0.5", "ANALYSIS_CONCURRENCY": "1"}


def read_env(path):
    return {key: value or "" for key, value in dotenv_values(path, encoding="utf-8-sig", interpolate=False).items()}


def settings_path(env_path):
    return Path(env_path).parent / 'config' / 'ai_endpoints.json'


def read_settings(path):
    destination = settings_path(path)
    values = {} if destination.exists() else read_env(path)
    raw = values.get('MODEL_ENDPOINTS', '')
    if destination.exists():
        try:
            payload = json.loads(destination.read_text(encoding='utf-8-sig'))
            endpoints, active = payload['endpoints'], payload['active_id']
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('AI 配置 JSON 格式错误，请检查 config/ai_endpoints.json') from exc
    elif raw:
        try:
            endpoints = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("AI 接入点配置格式错误") from exc
        if not isinstance(endpoints, list) or not endpoints:
            raise ValueError("至少保留一个 AI 接入点")
        active = values.get("MODEL_ACTIVE_ENDPOINT", "")
    else:
        endpoints = [{"id": "legacy", "name": "默认接入点",
                      **{key: values.get(key) or DEFAULTS[key] for key in MODEL_FIELDS}}]
        active = "legacy"
    if not isinstance(endpoints, list) or not endpoints or not all(isinstance(item, dict) for item in endpoints):
        raise ValueError('AI 配置必须包含非空的接入点列表')
    if not any(item.get("id") == active for item in endpoints):
        raise ValueError("当前使用的 AI 接入点不存在")
    for item in endpoints:
        for key in MODEL_FIELDS:
            item.setdefault(key, values.get(key) or DEFAULTS[key])
    return endpoints, active


def version(path):
    destination = settings_path(path)
    if destination.exists():
        return hashlib.sha256(destination.read_bytes()).hexdigest()
    # 首次保存仅比较旧 AI 配置；其他服务修改 .env 不应导致冲突。
    endpoints, active = read_settings(path)
    return hashlib.sha256(json.dumps({'endpoints': endpoints, 'active_id': active}, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def public_settings(path):
    endpoints, active = read_settings(path)
    return {"endpoints": [{**{key: value for key, value in item.items() if key != "MODEL_API_KEY"},
                            "MODEL_API_KEY": "", "key_configured": bool(item.get("MODEL_API_KEY"))}
                           for item in endpoints], "active_id": active, "version": version(path)}


def validate_endpoints(raw, old, active, activating=False):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
        raise ValueError("请保留 1 至 50 个 AI 接入点")
    previous = {item["id"]: item for item in old}
    result, ids = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("接入点格式错误")
        ident = str(item.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ident) or ident in ids:
            raise ValueError("接入点 ID 无效或重复")
        ids.add(ident)
        name = str(item.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("请填写接入点名称（最多 80 字）")
        row = {"id": ident, "name": name}
        for key in MODEL_FIELDS:
            row[key] = str(item.get(key, DEFAULTS[key])).strip()
            if "\n" in row[key] or "\r" in row[key]:
                raise ValueError("配置值不能包含换行")
        if not row["MODEL_API_KEY"]:
            source = previous.get(ident) or previous.get(str(item.get("copy_from", "")), {})
            row["MODEL_API_KEY"] = source.get("MODEL_API_KEY", "")
        if row["MODEL_PROTOCOL"] not in ("openai", "responses", "anthropic"):
            raise ValueError("不支持的接口协议")
        address = row["MODEL_API_BASE"]
        if address and (urlsplit(address).scheme not in ("http", "https") or not urlsplit(address).hostname):
            raise ValueError("API 地址必须是完整的 HTTP / HTTPS 地址")
        try:
            timeout = float(row["MODEL_TIMEOUT"])
            maximum = int(row["MODEL_MAX_TOKENS"])
            pause = float(row['ANALYSIS_PAUSE_SECONDS'])
            concurrency = int(row['ANALYSIS_CONCURRENCY'])
            if not math.isfinite(timeout) or timeout <= 0 or maximum < 0 or not math.isfinite(pause) or pause < 0 or not 1 <= concurrency <= 32:
                raise ValueError()
        except ValueError as exc:
            raise ValueError("请求超时必须大于0，最大输出Token与处理间隔不能为负，并行数量必须为1至32的整数") from exc
        result.append(row)
    if active not in ids:
        raise ValueError("不能删除当前使用的接入点，请先切换到其他接入点")
    if activating:
        row = next(item for item in result if item["id"] == active)
        if not all(row[key] for key in ("MODEL_API_BASE", "MODEL_NAME", "MODEL_API_KEY")):
            raise ValueError("使用前请配置 API 地址、模型名称和 API 密钥")
    return result


def save_settings(path, endpoints, active):
    """UTF-8 原子替换，只写 AI 文件，密钥不经过 dotenv 转义。"""
    destination = settings_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {'active_id': active, 'endpoints': endpoints}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent, prefix='.ai-endpoints-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
