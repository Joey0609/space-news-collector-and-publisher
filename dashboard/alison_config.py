# -*- coding: utf-8 -*-
"""Dashboard-managed Alison configuration and known-group info refresh.

Credentials are returned only by the token-authenticated config endpoint, as explicitly
requested; they are never included in channel summaries, audit payloads, or logs.
QQ Bot has an API to inspect a *known* group OpenID, not an API to enumerate every joined
group. Membership is learned from gateway join/leave/message events or manual entry.
"""
from __future__ import annotations

import os
import threading
import time

from distribute.alison.ali_core import env, groups, qq_api

FIELDS = ("APPID", "SECRET", "SANDBOX", "ALISON_WITH_IMAGE", "ALISON_DISCOVER_GROUPS")
DEFAULTS = {"SANDBOX": "0", "ALISON_WITH_IMAGE": "1", "ALISON_DISCOVER_GROUPS": "1"}
DISCOVERY_NOTE = ("QQ 官方没有查询机器人全部已加入群的接口；入群/退群事件和收到的群消息"
                  "会自动维护目录。要接收未 @ 的普通消息，需在每个 QQ 群的机器人设置中"
                  "将『可获取的群消息范围』设为『获取群内全部消息』，且机器人拥有全量事件权限；"
                  "未开启时只能靠入群事件和 @ 消息发现新群。"
                  "历史未记录的群也可手动补录 OpenID。刷新仅查询已知群的信息，可能要求白名单权限。"
                  "新群默认关闭推送。沙箱与正式机器人是不同的消息连接："
                  "要接收正式群消息，请使用该群里的机器人 APPID/SECRET 并选择正式环境；"
                  "艾莉森发送使用同一个 BotPy 长连接；群发现开关只控制事件登记。"
                  "更换凭据、沙箱环境或群发现开关后需重启艾莉森渠道。")
INFO_INTERVAL = 2.1  # /v2/groups/{group_openid}/info: 30 QPM
DB_FILE = groups.DB_FILE
LOCK = threading.Lock()
REFRESH = {"running": False, "total": 0, "done": 0, "error": 0, "message": ""}


def _refresh_state() -> dict:
    with LOCK:
        return dict(REFRESH)


def config() -> dict:
    stored = env.load_env_file()
    fields = []
    for key in FIELDS:
        fields.append({"key": key, "value": stored.get(key, DEFAULTS.get(key, "")),
                       "choices": (["0", "1"] if key in
                                   ("SANDBOX", "ALISON_WITH_IMAGE", "ALISON_DISCOVER_GROUPS") else [])})
    overrides = [key for key in FIELDS if os.getenv(key) and os.getenv(key) != stored.get(key, "")]
    note = DISCOVERY_NOTE
    if overrides:
        note += " 注意：进程环境变量覆盖了以下文件配置，需在进程环境中修改：" + "、".join(overrides) + "。"
    return {"service": "alison", "fields": fields,
            "groups": groups.list_groups(db_file=DB_FILE), "refresh": _refresh_state(),
            "discovery_note": note}


def save(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("艾莉森配置格式错误")
    normalized = {key: value for key, value in values.items() if key in FIELDS}
    for key in ("SANDBOX", "ALISON_WITH_IMAGE", "ALISON_DISCOVER_GROUPS"):
        if key not in normalized:
            continue
        value = str(normalized[key]).strip().lower()
        if value not in ("0", "1", "false", "true"):
            raise ValueError(f"{key} 只能是开或关")
        normalized[key] = "1" if value in ("1", "true") else "0"
    keys = env.save_env_updates(normalized, FIELDS)
    return {"keys": keys}


def add_group(group_openid: str) -> dict:
    goid = str(group_openid or "").strip()
    if not 1 <= len(goid) <= 128 or not all(char.isascii() and
                                             (char.isalnum() or char in "_-") for char in goid):
        raise ValueError("群 OpenID 只允许 1~128 个 ASCII 字母、数字、下划线或连字符")
    return groups.upsert_group(goid, db_file=DB_FILE)


def toggle_group(group_openid: str, enabled: bool) -> dict:
    group = groups.get_group(group_openid, db_file=DB_FILE)
    if not group:
        raise ValueError("群目录里找不到这个 OpenID")
    if enabled and not group["is_member"]:
        raise ValueError("机器人已退出这个群，不能开启推送")
    return groups.set_push_enabled(group_openid, enabled, db_file=DB_FILE)


def start_refresh() -> dict:
    """Only an explicit dashboard POST starts outbound QQ API calls; no QQ messages are sent."""
    stored = env.load_env_file()
    appid, secret = env.credentials(stored)
    if not appid or not secret:
        raise ValueError("先填写并保存艾莉森 APPID 和 SECRET")
    known = groups.list_groups(members_only=True, db_file=DB_FILE)
    with LOCK:
        if REFRESH["running"]:
            return dict(REFRESH)
        REFRESH.update(running=True, total=len(known), done=0, error=0, message="")
    if not known:
        with LOCK:
            REFRESH["running"] = False
        return _refresh_state()
    thread = threading.Thread(target=_refresh_groups,
                              args=(stored, [row["group_openid"] for row in known], DB_FILE),
                              name="alison-group-refresh", daemon=True)
    thread.start()
    return _refresh_state()


def _refresh_groups(settings: dict, openids: list[str], db_file) -> None:
    appid, secret = env.credentials(settings)
    try:
        host = env.api_host(settings)
        token = qq_api.get_token(host, appid, secret)
        headers = qq_api.auth_headers(token, appid)
    except Exception:
        # Do not echo token responses or credentials in dashboard errors/logs.
        with LOCK:
            REFRESH.update(running=False, error=1,
                           message="无法获取 QQ Bot 访问凭证，请检查 APPID/SECRET 与网络")
        return
    try:
        for index, goid in enumerate(openids):
            try:
                info = qq_api.get_group_info(host, headers, goid)
                if info.get("group_openid") and info["group_openid"] != goid:
                    raise ValueError("群信息响应的 OpenID 与请求不一致")
                groups.upsert_group(goid, info, db_file=db_file)
            except Exception:
                # The official info endpoint can be white-list only (11253). Never expose token responses.
                try:
                    groups.record_refresh_error(goid, "群信息不可用（可能未开通该 API 权限）", db_file=db_file)
                except Exception:
                    pass
                with LOCK:
                    REFRESH["error"] += 1
            finally:
                with LOCK:
                    REFRESH["done"] = index + 1
            if index < len(openids) - 1:
                time.sleep(INFO_INTERVAL)
    finally:
        with LOCK:
            REFRESH["running"] = False
