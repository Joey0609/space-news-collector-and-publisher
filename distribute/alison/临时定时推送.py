# -*- coding: utf-8 -*-
"""临时定时推送（测试用，别当正式程序）
====================================
就是个"通不通"的测试：每 30 分钟，让艾莉森发一条【最新的、还没发给本群】的新闻到群。
"重复"（本群已发过）就自动跳到次新、次次新……直到找到没发过的。

运行（在 艾莉森网关 目录下）：
    venv\\Scripts\\python.exe 临时定时推送.py             每30分钟一次（常驻，Ctrl+C 停）
    venv\\Scripts\\python.exe 临时定时推送.py --once      只发一次（测试）
    venv\\Scripts\\python.exe 临时定时推送.py --interval 600   自定义间隔秒数
"""
import datetime
import sys
import time

from ali_core.env import api_host, credentials, group_openid, is_sandbox, load_env
from ali_core.jsonio import load_json, save_json
from ali_core.log import log, setup_console_encoding
from ali_core.paths import MAX_SENT_RECORDS, PENDING_FILE, SENT_EVER_FILE, TARGET_KIND
from ali_core.qq_api import auth_headers, get_token, send_group_rich, upload_group_image
from ali_core.render import (
    build_text,
    item_images,
    mark_sent_to,
    pick_newest_unsent,
    ts_of,  # noqa: F401  (留作调试/外部引用)
)

INTERVAL = 1800  # 30 分钟
KIND = TARGET_KIND


def send_one() -> bool:
    """发一条：最新的、没发给本群的。返回是否真发了。"""
    env = load_env()
    appid, secret = credentials(env)
    if not appid or not secret:
        log("❌ 未配置 APPID/SECRET，跳过（见 艾莉森网关/.env.example）")
        return False
    host = api_host(env)

    goid = group_openid(log)
    if not goid:
        return False

    items = load_json(PENDING_FILE, []) or []
    item = pick_newest_unsent(items, KIND)
    if item is None:
        log(f"没有'没发给本群'的新闻了（共 {len(items)} 条）")
        return False

    text = build_text(item)
    images = item_images(item)
    log(f"选中 @{item.get('account')} date={item.get('date')} 图={len(images)}")
    log("-" * 40)
    log(text)
    log("-" * 40)

    token = get_token(host, appid, secret)
    hdrs = auth_headers(token, appid)

    file_info = ""
    if images:
        try:
            file_info = upload_group_image(host, hdrs, goid, images[0])
        except Exception as e:
            log(f"[图] 上传失败: {e}")
    ok = send_group_rich(host, hdrs, goid, text, file_info)
    if not ok:
        log("发送失败，未标记（下轮重试）")
        return False

    mark_sent_to(item, KIND)
    save_json(PENDING_FILE, items)
    records = load_json(SENT_EVER_FILE, []) or []
    records.append(item)
    save_json(SENT_EVER_FILE, records[-MAX_SENT_RECORDS:])
    log(f"已发送并标记 {KIND}")
    return True


def main():
    args = sys.argv[1:]
    once = "--once" in args
    interval = INTERVAL
    if "--interval" in args:
        try:
            interval = int(args[args.index("--interval") + 1])
        except Exception:
            interval = INTERVAL

    log("=" * 50)
    log(f"临时定时推送（测试）启动 | 间隔 {interval} 秒 | 目标 {KIND}")

    if once:
        send_one()
        return

    while True:
        try:
            send_one()
        except Exception as e:
            log(f"本轮出错: {e}")
        log(f"睡 {interval} 秒，下一轮 {datetime.datetime.now().strftime('%H:%M')} 后...")
        time.sleep(interval)


if __name__ == "__main__":
    setup_console_encoding()
    main()
