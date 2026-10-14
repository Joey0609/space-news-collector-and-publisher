# -*- coding: utf-8 -*-
"""临时定时推送（测试用，别当正式程序）
====================================
就是个"通不通"的测试：每 30 分钟，让艾莉森发一条【最新的、还没发给本群】的新闻到群。
队列在数据库里（stage='processed'、decision='发' 且本通道 source 不是 true），发过就置 true、下次不会再取到。

运行（在 艾莉森网关 目录下）：
    venv\\Scripts\\python.exe 临时定时推送.py             每30分钟一次（常驻，Ctrl+C 停）
    venv\\Scripts\\python.exe 临时定时推送.py --once      只发一次（测试）
    venv\\Scripts\\python.exe 临时定时推送.py --interval 600   自定义间隔秒数
"""
import datetime
from ali_core.delivery import deliver_all_images
import sys
import time

from ali_core.env import api_host, credentials, is_sandbox, load_env
from ali_core.groups import get_group, list_groups
from ali_core.jsonio import load_json, save_json  # noqa: F401  (留作外部引用)
from ali_core.log import log, setup_console_encoding
from ali_core.paths import DB_FILE, MAX_SENT_RECORDS, PENDING_FILE, SENT_EVER_FILE
import ali_core.push_queue as queue
from ali_core.qq_api import auth_headers, get_token, send_group_rich, upload_group_image
from ali_core.render import build_text, item_images, pick_newest, ts_of  # noqa: F401

INTERVAL = 1800  # 30 分钟


def send_one(group_openid: str = "") -> bool:
    """发一条：最新的、没发给本群的。返回是否真发了。"""
    env = load_env()
    appid, secret = credentials(env)
    if not appid or not secret:
        log("❌ 未配置 APPID/SECRET，跳过（见 艾莉森网关/.env.example）")
        return False
    host = api_host(env)

    available = list_groups(enabled_only=True)
    goid = group_openid or (available[0]["group_openid"] if available else "")
    group = get_group(goid) if goid else {}
    if not group or not group["is_member"] or not group["push_enabled"]:
        log("没有启用推送的群；请在看板启用群，或用 --group <已启用群OpenID> 指定。")
        return False

    items = queue.pending(goid)
    item = pick_newest(items)
    if item is None:
        log("没有'没发给本群'的新闻了（待发 0 条）")
        return False

    text = build_text(item)
    images = item_images(item)
    log(f"选中 @{item.get('account')} date={item.get('date')} 图={len(images)}")
    log("-" * 40)
    log(text)
    log("-" * 40)

    token = get_token(host, appid, secret)
    hdrs = auth_headers(token, appid)

    ok = deliver_all_images(item, queue.source_for(goid), text, images,
                            lambda path: upload_group_image(host, hdrs, goid, path),
                            lambda body, info: send_group_rich(host, hdrs, goid, body, info))

    if not ok:
        log("发送失败，未标记（下轮重试）")
        return False

    queue.mark([item["id"]], goid)
    log(f"已发送并标记 {queue.source_for(goid)}")
    return True


def main():
    args = sys.argv[1:]
    once = "--once" in args
    group = ""
    if "--group" in args:
        try:
            group = str(args[args.index("--group") + 1]).strip()
        except IndexError:
            log("--group 后面缺少群 OpenID")
            return
    interval = INTERVAL
    if "--interval" in args:
        try:
            interval = int(args[args.index("--interval") + 1])
        except Exception:
            interval = INTERVAL

    log("=" * 50)
    log(f"临时定时推送（测试）启动 | 间隔 {interval} 秒 | "
        f"目标 {group or '已启用群目录的第一项'}")

    if once:
        send_one(group)
        return

    while True:
        try:
            send_one(group)
        except Exception as e:
            log(f"本轮出错: {e}")
        log(f"睡 {interval} 秒，下一轮 {datetime.datetime.now().strftime('%H:%M')} 后...")
        time.sleep(interval)


if __name__ == "__main__":
    setup_console_encoding()
    main()
