# -*- coding: utf-8 -*-
"""
临时定时推送（测试用，别当正式程序）
====================================
就是个"通不通"的测试：每 30 分钟，让艾莉森发一条【最新的、还没发给本群】的新闻到群。
"重复"（本群已发过）就自动跳到次新、次次新……直到找到没发过的。

运行（在 艾莉森网关 目录下）：
    venv\\Scripts\\python.exe 临时定时推送.py             每30分钟一次（常驻，Ctrl+C 停）
    venv\\Scripts\\python.exe 临时定时推送.py --once      只发一次（测试）
    venv\\Scripts\\python.exe 临时定时推送.py --interval 600   自定义间隔秒数
"""
import sys
import time
import datetime

import 推送 as core

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

INTERVAL = 1800  # 30 分钟
KIND = core.TARGET_KIND  # group_YOUR_GROUP_ID_2


def _ts_of(it) -> float:
    """取条目的时间戳：优先 ts，其次解析 date 字符串。"""
    v = it.get("ts")
    if v:
        try:
            return float(v)
        except Exception:
            pass
    d = str(it.get("date") or "").strip()
    if d:
        try:
            return datetime.datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return 0.0


def pick_newest_unsent(items: list, kind: str):
    """按时间倒序，取第一条"还没发给本群"的。"""
    for it in sorted(items, key=_ts_of, reverse=True):
        sent = it.get("sent_to")
        if not isinstance(sent, list):
            sent = []
        if kind not in sent:
            return it
    return None


def send_one() -> bool:
    """发一条：最新的、没发给本群的。返回是否真发了。"""
    env = core.load_env()
    appid = env.get("APPID", "").strip()
    secret = env.get("SECRET", "").strip()
    sandbox = str(env.get("SANDBOX", "0")).strip().lower() in ("1", "true", "yes", "on")
    host = "https://sandbox.api.sgroup.qq.com" if sandbox else "https://api.sgroup.qq.com"

    state = core.load_state()
    goid = state.get("group_openid", "").strip()
    if not goid:
        core.log("没有 group_openid，跳过（先在群里 @ 一下艾莉森让网关记录）")
        return False

    items = core.load_json(core.PENDING_FILE, [])
    item = pick_newest_unsent(items, KIND)
    if item is None:
        core.log(f"没有'没发给本群'的新闻了（共 {len(items)} 条）")
        return False

    text = core.build_text(item)
    images = core.item_images(item)
    core.log(f"选中 @{item.get('account')} date={item.get('date')} 图={len(images)}")
    core.log("-" * 40)
    core.log(text)
    core.log("-" * 40)

    token = core.get_token(host, appid, secret)
    hdrs = core.auth_headers(token, appid)

    file_info = ""
    if images:
        try:
            file_info = core.upload_group_image(host, hdrs, goid, images[0])
        except Exception as e:
            core.log(f"[图] 上传失败: {e}")
    ok = core.send_group_rich(host, hdrs, goid, text, file_info)
    if not ok:
        core.log("发送失败，未标记（下轮重试）")
        return False

    # 标记已发本群 + 记已发记录
    sent = item.get("sent_to")
    if not isinstance(sent, list):
        sent = []
    if KIND not in sent:
        sent.append(KIND)
    item["sent_to"] = sent
    core.save_json(core.PENDING_FILE, items)
    records = core.load_json(core.SENT_EVER_FILE, [])
    records.append(item)
    core.save_json(core.SENT_EVER_FILE, records[-500:])
    core.log(f"已发送并标记 {KIND}")
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

    core.log("=" * 50)
    core.log(f"临时定时推送（测试）启动 | 间隔 {interval} 秒 | 目标 {KIND}")

    if once:
        send_one()
        return

    while True:
        try:
            send_one()
        except Exception as e:
            core.log(f"本轮出错: {e}")
        core.log(f"睡 {interval} 秒，下一轮 {datetime.datetime.now().strftime('%H:%M')} 后...")
        time.sleep(interval)


if __name__ == "__main__":
    main()
