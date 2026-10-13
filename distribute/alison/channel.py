# -*- coding: utf-8 -*-
"""艾莉森 QQ Bot 的 WebSocket 分发渠道。

渠道 targets = 已知群目录中已加入且打开推送的群，source = alison:group_<group_openid>。
每个目标独立得到一条新闻；发送成功后向看板回 result，只有看板会写 items.pushed。
群目录新增/开关变化后每 3 秒更新 hello.targets，不必重启渠道。
同一个 BotPy 长连接负责群事件和消息发送；未连接或发送失败时不回报成功。
"""
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from distribute.alison.ali_core.env import api_host, credentials, load_env  # noqa: E402
from distribute.alison.ali_core.groups import get_group, list_groups  # noqa: E402
from distribute.alison.ali_core.log import log, setup_console_encoding  # noqa: E402
from distribute.alison.ali_core.qq_api import (auth_headers, get_token,
                                              upload_group_image)  # noqa: E402
from distribute.alison.ali_core.render import build_text, item_images  # noqa: E402
from distribute.alison.gateway import BotRuntime  # noqa: E402
from distribute.ws_channel import ChannelClient  # noqa: E402

CHANNEL = "alison"


def enabled_targets() -> list:
    """Only enabled member groups are registered as distribution targets."""
    return [{"kind": f"group_{row['group_openid']}",
             "label": row["group_name"] or "群资料查询中…"}
            for row in list_groups(enabled_only=True)]


class AlisonSender:
    """把一条新闻发到该 source 对应的群；失败绝不谎报成功。"""

    def __init__(self, runtime: BotRuntime, no_image: bool = False):
        self.runtime = runtime
        self.no_image = no_image

    def send(self, item: dict, source: str) -> dict:
        prefix = f"{CHANNEL}:group_"
        if not source.startswith(prefix):
            return {"ok": False, "detail": "来源不是艾莉森群 source"}
        group_openid = source[len(prefix):]
        group = get_group(group_openid)
        if not group or not group["is_member"] or not group["push_enabled"]:
            return {"ok": False, "detail": "该群不在目录中或已关闭推送"}
        settings = load_env()  # 图文开关立即生效；凭据或环境改动需重启 QQ 长连接
        appid, secret = credentials(settings)
        if not appid or not secret:
            return {"ok": False, "detail": "未配置艾莉森 APPID/SECRET"}
        text = build_text(item)
        if not text.strip():
            return {"ok": False, "detail": "条目没有可发正文"}
        images = item_images(item) if not self.no_image and settings.get("ALISON_WITH_IMAGE", "1") == "1" else []
        try:
            file_info = ""
            if images:
                try:
                    host = api_host(settings)
                    token = get_token(host, appid, secret)
                    file_info = upload_group_image(host, auth_headers(token, appid),
                                                   group_openid, images[0])
                except Exception:
                    log(f"[图] 上传失败，改发纯文字 id={item.get('id')} group_openid={group_openid}")
            if self.runtime.send(group_openid, text, file_info):
                log(f"[已发] @{item.get('account')} id={item.get('id')} group_openid={group_openid}")
                return {"ok": True}
            return {"ok": False, "detail": self.runtime.last_error or "QQ Bot 发送返回失败"}
        except Exception as exc:
            # Never log exception bodies from credential/token exchanges.
            log(f"[发送失败] item={item.get('id')} group_openid={group_openid} 类型={type(exc).__name__}")
            return {"ok": False, "detail": f"QQ Bot 请求失败（{type(exc).__name__}）"}


def parse_args(argv: list) -> dict:
    once = None
    if "--once" in argv:
        try:
            once = int(argv[argv.index("--once") + 1])
        except (IndexError, ValueError):
            once = 1
    return {"once": once, "no_image": "--no-image" in argv}


def _watch_groups(client: ChannelClient, stop: threading.Event,
                  runtime: BotRuntime) -> None:
    rounds = 0
    while not stop.wait(3):
        try:
            targets = enabled_targets()
            if targets != client.targets:
                if client.update_targets(targets):
                    log(f"[群配置] 已同步 {len(targets)} 个启用群："
                        f"{', '.join(target['label'] for target in targets) if targets else '暂无，等待看板配置'}")
            rounds += 1
            if rounds % 20 == 0:
                known = list_groups()
                log(f"[运行] {'已连接' if client.connected else '正在重连看板'}；"
                    f"已知群 {len(known)}，启用推送 {len(targets)}，"
                    f"QQ 长连接 {'已就绪' if runtime.ready.is_set() else '未就绪'}")
        except Exception as exc:
            log(f"[群配置] 同步失败：{type(exc).__name__}")


def main() -> int:
    args = parse_args(sys.argv[1:])
    settings = load_env()
    appid, secret = credentials(settings)
    log("=" * 50)
    known = list_groups()
    targets = enabled_targets()
    log(f"艾莉森渠道启动 | 已知群={len(known)} | 启用推送={len(targets)} | "
        f"凭据={'已配置' if appid and secret else '未配置'}")
    if not targets:
        log("[等待] 没有启用推送的群；渠道可正常连接，但不会收到新闻。"
            "请在看板「分发 → 艾莉森 → 配置」补录群 OpenID，并打开该群的推送开关。")
    client = ChannelClient(CHANNEL, "艾莉森 QQ Bot", targets=targets,
                           adapter="process", pull=False, logger=lambda message: log(str(message)))
    runtime = BotRuntime(settings)
    sender = AlisonSender(runtime, no_image=args["no_image"])
    stop = threading.Event()
    if args["once"] != 0:
        runtime.start()
    watcher = threading.Thread(target=_watch_groups, args=(client, stop, runtime), daemon=True)
    watcher.start()
    try:
        client.run_forever(sender.send, max_rounds=args["once"])
    finally:
        stop.set()
        client.close()
    return 0


if __name__ == "__main__":
    setup_console_encoding()
    sys.exit(main())
