# -*- coding: utf-8 -*-
"""艾莉森推送 v1 · 纯转述图文发送器
================================
作用：从**数据库分发队列**（stage='processed'、decision='发' 且本通道 source 还没发过）取一条真新闻，
      用艾莉森（QQ 官方接口）把「来源行 + 正文 (+图)」**原样转述**到指定群。
      艾莉森不写文案（符合「★ 新架构」）。

不依赖常驻网关：走纯 HTTP OpenAPI（取 token → 上传图 → 发消息）。
队列不再是 待推送.json：发完会把 `items.pushed` 里本通道的 source 置 true（见 ali_core/push_queue.py）。

用法（在 艾莉森网关 目录下）：
    venv\\Scripts\\python.exe 推送.py --list            只列出候选，不发送
    venv\\Scripts\\python.exe 推送.py --dry              演练：只打印要发的内容，不真发
    venv\\Scripts\\python.exe 推送.py --once             取1条（优先带图）真发到群
    venv\\Scripts\\python.exe 推送.py --once --no-image  取1条纯文字真发
    venv\\Scripts\\python.exe 推送.py --once --group <已启用群OpenID>  指定目标群
    venv\\Scripts\\python.exe 推送.py --upload-only <图> 只测图片上传

实现拆分：路径 ali_core/paths.py、日志 ali_core/log.py、.env/state ali_core/env.py、
        OpenAPI ali_core/qq_api.py、排版 ali_core/render.py、队列 ali_core/push_queue.py。
（本文件末尾保留了旧名 re-export：临时定时推送.py 等按 `推送.xxx` 取值也能用。）
"""
import sys

from ali_core.env import api_host, credentials, is_sandbox, load_env, load_state
from ali_core.groups import get_group, list_groups
from ali_core.jsonio import load_json, save_json  # noqa: F401
from ali_core.log import log, setup_console_encoding
from ali_core.paths import (  # noqa: F401
    DB_FILE,
    HTTP_TIMEOUT,
    MAX_SENT_RECORDS,
    PENDING_FILE,
    SENT_EVER_FILE,
    STATE_FILE,
)
import ali_core.push_queue as queue
from ali_core.qq_api import auth_headers, get_token, send_group_rich, upload_group_image
from ali_core.render import (  # noqa: F401
    build_source_line,
    build_text,
    fmt_beijing,
    item_images,
    pick_item,
)


def parse_args(argv: list) -> dict:
    """解析命令行开关。"""
    args = {
        "list": "--list" in argv,
        "dry": "--dry" in argv,
        "once": "--once" in argv,
        "no_image": "--no-image" in argv,
        "index": None,
        "group": "",
        "upload_only": "",
    }
    if "--index" in argv:
        try:
            args["index"] = int(argv[argv.index("--index") + 1])
        except Exception:
            args["index"] = None
    if "--group" in argv:
        try:
            args["group"] = str(argv[argv.index("--group") + 1]).strip()
        except IndexError:
            args["group"] = ""
    if "--upload-only" in argv:
        try:
            args["upload_only"] = argv[argv.index("--upload-only") + 1]
        except Exception:
            args["upload_only"] = ""
    return args


def main():
    args = parse_args(sys.argv[1:])

    env = load_env()
    appid, secret = credentials(env)
    sandbox = is_sandbox(env)
    host = api_host(env)

    if not appid or not secret:
        log("❌ 未配置 APPID/SECRET：请复制 艾莉森网关\\.env.example 为 .env 并填写")
        sys.exit(1)

    candidates = list_groups(enabled_only=True)
    goid = args["group"] or (candidates[0]["group_openid"] if candidates else "")
    group = get_group(goid) if goid else {}
    log("=" * 50)
    if not group or not group["is_member"] or not group["push_enabled"]:
        log("没有启用推送的群。先在看板「分发 → 艾莉森 → 配置」启用群，"
            "或用 --group <已启用群OpenID> 选择目标。")
        return
    log(f"艾莉森手动推送 | 目标群 {group['group_name'] or goid} openid={goid} | "
        f"环境={'沙箱' if sandbox else '正式'}")

    # 只测"上传图片"：推送.py --upload-only <图片路径>
    if args["upload_only"]:
        token = get_token(host, appid, secret)
        hdrs = auth_headers(token, appid)
        try:
            fi = upload_group_image(host, hdrs, goid, args["upload_only"])
            log(f"[upload-only] 成功 file_info={fi[:24]}...")
        except Exception as e:
            log(f"[upload-only] 失败: {e}")
        return

    items = queue.pending(goid)
    log(f"分发队列（本群 source={queue.source_for(goid)}）共 {len(items)} 条待发")
    idx, item = pick_item(items, args["index"])

    if args["list"] or item is None:
        log(f"尚未发到本群的候选：{len(items)} 条")
        if args["list"]:
            for i, it in enumerate(items):
                log(f"  #{i} score={it.get('score')} 图={len(item_images(it))} "
                    f"@{it.get('account')} {str(it.get('zh') or '')[:40]}")
        if item is None and not args["list"]:
            log("没有可发候选（都已发过本群）")
        return

    text = build_text(item)
    images = [] if args["no_image"] else item_images(item)
    log(f"选中 #{idx} @{item.get('account')} score={item.get('score')} 图={len(images)}")
    log("-" * 40)
    log(text)
    log("-" * 40)
    if args["dry"]:
        log("[dry] 演练模式，未真发")
        return
    if not args["once"]:
        log("未指定 --once，仅预览。加 --once 真发。")
        return

    token = get_token(host, appid, secret)
    hdrs = auth_headers(token, appid)

    # 图文一条：上传第一张图 → msg_type=7 带 content + media
    file_info = ""
    if images:
        try:
            file_info = upload_group_image(host, hdrs, goid, images[0])
        except Exception as e:
            log(f"[图] 上传失败({images[0]}): {e}")
    ok = send_group_rich(host, hdrs, goid, text, file_info)

    if ok:
        # 发完才标记：写数据库（items.pushed 里本通道 source = true），不再动任何 JSON 队列
        queue.mark([item["id"]], goid)
        log(f"✅ 已发送并标记 {queue.source_for(goid)}")
    else:
        log("❌ 发送失败（文字和图都没成功），未标记")


if __name__ == "__main__":
    setup_console_encoding()
    main()
