# -*- coding: utf-8 -*-
"""旧手动调试脚本使用的 SQLite 队列入口；群由已启用的 OpenID 指定。正常分发走 WS。"""
import sys

from .paths import DB_FILE, HX_ROOT

if str(HX_ROOT) not in sys.path:
    sys.path.insert(0, str(HX_ROOT))

from distribute import push_db  # noqa: E402

CHANNEL = "alison"


def source_for(group_openid: str) -> str:
    """每个 QQ 群独立跟踪已发状态（`alison:group_<group_openid>`）。"""
    goid = str(group_openid or "").strip()
    if not goid:
        raise ValueError("请选择启用推送的群 OpenID")
    return push_db.source_of(CHANNEL, f"group_{goid}")


def register(group_openid: str) -> list:
    return push_db.register_sources([source_for(group_openid)], db_file=DB_FILE)


def pending(group_openid: str) -> list:
    """该群还未发过的条目（带图优先由 render.pick_item 决定）。"""
    register(group_openid)
    return push_db.pending(source=source_for(group_openid), db_file=DB_FILE)


def mark(item_ids, group_openid: str) -> int:
    """该群发送成功后才标记。"""
    return push_db.mark_pushed(item_ids, source_for(group_openid), db_file=DB_FILE)


def pending_count(group_openid: str) -> int:
    return push_db.pending_count(db_file=DB_FILE, sources=[source_for(group_openid)])
