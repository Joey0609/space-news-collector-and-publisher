# -*- coding: utf-8 -*-
"""重构后自检：艾莉森网关 侧的导入 + 排版/挑选逻辑冒烟测试。

跑法：python 自检/check_ali.py（或统一跑 自检/run_all.py）
"""
import asyncio
import importlib.util
import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
ALI = REPO / "distribute" / "alison"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ALI))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

failures = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond:
        failures.append(name)


def load_by_path(path: Path, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("[1] ali_core 可导入")
import ali_core.paths as paths      # noqa: E402
import ali_core.env as aenv         # noqa: E402
import ali_core.jsonio as ajson     # noqa: E402
import ali_core.qq_api as qq        # noqa: E402
import ali_core.render as render    # noqa: E402

check("paths.HX_ROOT=仓库根", paths.HX_ROOT == REPO, paths.HX_ROOT)
check("旧的固定目标群配置已移除", not hasattr(paths, "TARGET_GROUP_NUMBER")
      and not hasattr(paths, "TARGET_KIND"))
check("host 沙箱/正式切换", aenv.api_host({"SANDBOX": "1"}).startswith("https://sandbox")
      and aenv.api_host({}).startswith("https://api."))
check("is_sandbox 各种写法", aenv.is_sandbox({"SANDBOX": "true"}) and not aenv.is_sandbox({"SANDBOX": "0"}))
check("credentials 去空格", aenv.credentials({"APPID": " 1 ", "SECRET": "2"}) == ("1", "2"))
check("auth_headers 形状", qq.auth_headers("tok", "app")["Authorization"] == "QQBot tok")

print("[2] 入口脚本可导入 + 兼容名还在")
push = load_by_path(ALI / "推送.py", "push_mod")
for attr in ("main", "log", "load_env", "load_state", "load_json", "save_json", "PENDING_FILE",
             "SENT_EVER_FILE", "DB_FILE", "build_text", "item_images", "get_token",
             "auth_headers", "upload_group_image", "send_group_rich", "queue"):
    check(f"推送.{attr}", hasattr(push, attr))
timer = load_by_path(ALI / "临时定时推送.py", "timer_mod")
check("临时定时推送.send_one/main", hasattr(timer, "send_one") and hasattr(timer, "main"))
gw = load_by_path(ALI / "gateway.py", "gateway_mod")
check("gateway.main/AlisonClient", hasattr(gw, "main") and hasattr(gw, "AlisonClient"))
for name in ("send_test.py", "send_botpy.py", "try_variants.py"):
    m = load_by_path(ALI / name, name.replace(".py", ""))
    check(f"{name}.main", hasattr(m, "main"))

print("[3] 排版与挑选")
item = {"account": "SpaceX", "orig_account": "NASA", "zh": "星舰发射成功", "raw": "Starship",
        "date": "2026-09-18 12:00:00", "ts": 1789000000, "score": 70,
        "images": ["a.jpg", "b.jpg"], "image": "a.jpg", "forward_comment": "值得一看"}
src = render.build_source_line(item)
check("来源行用原帖作者+北京时间", src.startswith("来源：@NASA · X · UTC+8-"), src)
text = render.build_text(item)
check("正文含来源/译文/转发评论", src in text and "星舰发射成功" in text and "转发@SpaceX：值得一看" in text, text)
check("images 优先列表", render.item_images(item) == ["a.jpg", "b.jpg"])
check("images 回退单图", render.item_images({"image": "c.jpg"}) == ["c.jpg"])
# 挑选：候选已经是"本通道还没发过的"（见 push_queue.pending），这里只按 带图/分数 选
items = [
    {"account": "a", "score": 99, "images": []},
    {"account": "b", "score": 10, "images": ["x.jpg"]},
    {"account": "c", "score": 100, "images": ["y.jpg"]},
]
idx, picked = render.pick_item(items)
check("挑选=带图优先、分数最高", idx == 2 and picked["account"] == "c", (idx, picked))
check("指定 index 直取", render.pick_item(items, 0)[1]["account"] == "a")
check("没有候选时返回空", render.pick_item([]) == (None, None))
newest = render.pick_newest(
    [{"account": "old", "ts": 100}, {"account": "new", "ts": 200}])
check("定时推送取最新", newest["account"] == "new", newest)
check("ts 缺失时回退 date", render.ts_of({"date": "2026-09-18T12:00:00+00:00"}) > 0)

print("[4] 分发队列入口（SQLite，按 source 记已发）")
import ali_core.push_queue as pq    # noqa: E402

check("source = 通道:目标（按群 OpenID 动态计算）",
      pq.source_for("GROUP_A") == "alison:group_GROUP_A" and not hasattr(pq, "SOURCE"))
check("队列四件套就绪", callable(pq.pending) and callable(pq.mark)
      and callable(pq.register) and callable(pq.pending_count))
check("队列库指向仓库 data/hx.sqlite3", paths.DB_FILE == REPO / "data" / "hx.sqlite3",
      paths.DB_FILE)

print("[5] QQ Bot 凭据配置、群目录和逐群推送开关（全沙箱）")
from distribute.alison.ali_core import env as config_env, groups, qq_api as config_qq  # noqa: E402
import dashboard.alison_config as alison_config  # noqa: E402

case = REPO / "selftest" / ".sandbox" / "alison_case"
shutil.rmtree(case, ignore_errors=True)
case.mkdir(parents=True, exist_ok=True)
old_env = config_env.ENV_FILE
old_state = groups.STATE_FILE
old_env_state = config_env.STATE_FILE
old_db = groups.DB_FILE
old_config_db = alison_config.DB_FILE
old_info = config_qq.get_group_info
old_token = config_qq.get_token
old_interval = alison_config.INFO_INTERVAL
try:
    config_env.ENV_FILE = case / ".env"
    groups.STATE_FILE = case / "state.json"
    config_env.STATE_FILE = case / "state.json"
    groups.DB_FILE = case / "hx.sqlite3"
    alison_config.DB_FILE = groups.DB_FILE
    alison_config.INFO_INTERVAL = 0
    groups.STATE_FILE.write_text(json.dumps({"group_openid": "LEGACY"}), encoding="utf-8")

    initial = alison_config.config()
    check("配置提示明确区分沙箱与正式机器人",
          "沙箱与正式机器人" in initial["discovery_note"])
    check("旧 state.json 已知群只补录一次且默认关闭推送",
          [row["group_openid"] for row in initial["groups"]] == ["LEGACY"]
          and initial["groups"][0]["push_enabled"] is False, initial["groups"])
    result = alison_config.save({"APPID": "demo-id", "SECRET": "demo-secret",
                                 "SANDBOX": "true", "ALISON_WITH_IMAGE": False,
                                 "ALISON_DISCOVER_GROUPS": True})
    fields = {entry["key"]: entry["value"] for entry in alison_config.config()["fields"]}
    check("配置弹窗可读取明文凭据，沙箱布尔规范化为 0/1",
          fields["APPID"] == "demo-id" and fields["SECRET"] == "demo-secret"
          and fields["SANDBOX"] == "1" and fields["ALISON_WITH_IMAGE"] == "0"
          and fields["ALISON_DISCOVER_GROUPS"] == "1", {k: v for k, v in fields.items() if k != "SECRET"})
    check("审计只需要键名，不返回密钥值", "SECRET" in result["keys"] and "demo-secret" not in str(result))

    first = alison_config.add_group("GROUP_A")
    second = alison_config.add_group("GROUP_B")
    check("新增群默认关闭推送", not first["push_enabled"] and not second["push_enabled"])
    alison_config.toggle_group("GROUP_A", True)
    groups.upsert_group("GROUP_A", {"group_name": "航天群", "group_member_num": 42})
    check("刷新信息不会重置群推送开关",
          groups.get_group("GROUP_A")["push_enabled"] and groups.get_group("GROUP_A")["group_name"] == "航天群")
    check("只注册已加入且已启用的群", [row["group_openid"] for row in
          groups.list_groups(enabled_only=True)] == ["GROUP_A"])

    channel = load_by_path(ALI / "channel.py", "alison_channel_case")
    check("渠道 target 使用各群 OpenID 而不是旧占位群号",
          [entry["kind"] for entry in channel.enabled_targets()] == ["group_GROUP_A"])
    check("手动预览仍可按已启用群 OpenID 选源",
          pq.source_for("GROUP_A") == "alison:group_GROUP_A"
          and push.parse_args(["--group", "GROUP_A"])["group"] == "GROUP_A")
    observed = []
    class FakeRuntime:
        last_error = ""

        def send(self, goid, text, file_info=""):
            observed.append((goid, text, file_info))
            return True

    sender = channel.AlisonSender(FakeRuntime(), no_image=True)
    item = {"id": 7, "account": "NASA", "zh": "试验成功", "raw": "test", "images": []}
    check("开启群发送成功", sender.send(item, "alison:group_GROUP_A")["ok"] is True and
          len(observed) == 1 and observed[0][0] == "GROUP_A")
    check("关闭群不会发送也不会回报成功",
          sender.send(item, "alison:group_GROUP_B")["ok"] is False and len(observed) == 1)
    alison_config.toggle_group("GROUP_A", False)
    check("运行中关闭群立即拦截发送", sender.send(item, "alison:group_GROUP_A")["ok"] is False)
    check("全关群后渠道仍可连接，但注册目标为空", channel.enabled_targets() == [])
    logs = []
    original_channel_log = channel.log
    try:
        channel.log = logs.append
        class IdleClient:
            def __init__(self, *args, **kwargs):
                self.targets = kwargs.get("targets", [])
                self.sources = []
                self.connected = False
                self.paused = False
                self.host = "127.0.0.1"
                self.port = 8788
            def run_forever(self, *args, **kwargs):
                return None
            def close(self):
                return None
        original_client = channel.ChannelClient
        original_argv = sys.argv
        channel.ChannelClient = IdleClient
        sys.argv = ["channel.py", "--once", "0"]
        try:
            channel.main()
        finally:
            channel.ChannelClient = original_client
            sys.argv = original_argv
        check("0 个启用群时日志说明不会收到新闻和下一步操作",
              any("没有启用推送的群" in line and "不会收到新闻" in line
                  for line in logs), logs)
    finally:
        channel.log = original_channel_log

    # 旧手动推送脚本也走这个 QQ API 发送函数：必须遵守同一逐群开关。
    old_post = config_qq.httpx.post
    old_log = config_qq.log
    requests = []
    proxy_flags = []
    class FakeResponse:
        status_code = 200
        text = ""
    try:
        config_qq.httpx.post = lambda *a, **kw: (requests.append(a[0]), proxy_flags.append(kw.get("trust_env")), FakeResponse())[-1]
        config_qq.log = lambda message: None
        assert config_qq.send_group_rich("https://fake.qq", {}, "GROUP_A", "x") is False
        check("手动推送不能绕过关群开关", requests == [], requests)
        alison_config.toggle_group("GROUP_A", True)
        check("手动推送已启用群才调用 QQ API",
              config_qq.send_group_rich("https://fake.qq", {}, "GROUP_A", "x") is True
              and requests == ["https://fake.qq/v2/groups/GROUP_A/messages"] and proxy_flags == [False], (requests, proxy_flags))
        alison_config.toggle_group("GROUP_A", False)
    finally:
        config_qq.httpx.post = old_post
        config_qq.log = old_log

    config_qq.get_token = lambda *args: "fake-token"
    config_qq.get_group_info = lambda host, headers, goid: {
        "group_openid": goid, "group_name": f"群 {goid}", "group_finger_memo": "已知群",
        "group_class_text": "科技", "group_tags": ["航天"], "group_member_num": 99}
    status = alison_config.start_refresh()
    deadline = time.time() + 4
    while alison_config.config()["refresh"]["running"] and time.time() < deadline:
        time.sleep(.02)
    status = alison_config.config()
    check("只显式点击刷新才向 QQ 查已知群信息", status["refresh"]["done"] == 3
          and status["refresh"]["error"] == 0 and not status["refresh"]["running"],
          status["refresh"])
    refreshed = groups.get_group("GROUP_B")
    check("群信息展示字段齐全", refreshed["group_member_num"] == 99
          and refreshed["group_tags"] == ["航天"] and refreshed["group_class_text"] == "科技")
    alison_config.toggle_group("GROUP_A", True)
    def deny_group_info(host, headers, goid):
        raise RuntimeError("11253 - white list only")
    config_qq.get_group_info = deny_group_info
    alison_config.start_refresh()
    deadline = time.time() + 4
    while alison_config.config()["refresh"]["running"] and time.time() < deadline:
        time.sleep(.02)
    failed_refresh = alison_config.config()["refresh"]
    check("官方群信息接口无白名单权限时标注失败，群开关不被关闭",
          failed_refresh["error"] == 3 and groups.get_group("GROUP_A")["push_enabled"]
          and groups.get_group("GROUP_A")["last_error"] != "", failed_refresh)
    try:
        alison_config.add_group("https://unsafe/abc")
        check("手动添加拒绝 URL 和斜杠", False)
    except ValueError:
        check("手动添加拒绝 URL 和斜杠", True)

    gw = load_by_path(ALI / "gateway.py", "alison_gateway_case")
    class FakeBotAPI:
        def __init__(self):
            self.calls = []
            self.response = {"id": "qq-message-1"}

        async def post_group_message(self, **kwargs):
            self.calls.append(kwargs)
            return self.response

    api = FakeBotAPI()
    runtime = gw.BotRuntime({"APPID": "demo-id", "SECRET": "demo-secret"})
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
                                   daemon=True)
    loop_thread.start()
    deadline = time.time() + 2
    while not loop.is_running() and time.time() < deadline:
        time.sleep(.01)
    try:
        runtime.client = SimpleNamespace(loop=loop, api=api,
                                         _alison_send_lock=asyncio.Lock(), _alison_last_send=0.0)
        runtime.thread = SimpleNamespace(is_alive=lambda: True)
        runtime.ready.set()
        check("长连接 BotPy API 返回非空结果后才算发送成功",
              runtime.send("GROUP_A", "新闻正文") is True
              and api.calls == [{"group_openid": "GROUP_A", "msg_type": 0,
                                 "content": "新闻正文"}])
        runtime.client._alison_last_send = 0.0
        api.response = {}
        check("QQ 返回空结果不回报成功", runtime.send("GROUP_A", "第二条") is False)
        runtime.client._alison_last_send = 0.0
        alison_config.toggle_group("GROUP_A", False)
        calls_before = len(api.calls)
        check("等待 QQ 发送期间关群会再次拦截",
              runtime.send("GROUP_A", "第三条") is False and len(api.calls) == calls_before)
        runtime.ready.clear()
        runtime.ready.wait = lambda timeout=None: False
        check("QQ 断线时不调用发送接口", runtime.send("GROUP_A", "第四条") is False
              and len(api.calls) == calls_before)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()
    alison_config.toggle_group("GROUP_A", True)
    ready = threading.Event()
    ready.set()
    asyncio.run(gw.AlisonClient.on_connection_lost(SimpleNamespace(_alison_ready=ready)))
    check("BotPy 断线事件撤销发送就绪状态", not ready.is_set())
    import botpy  # noqa: E402
    from botpy.connection import ConnectionState
    from botpy.gateway import BotWebSocket
    old_gateway_log = gw.log
    observed_messages = []
    try:
        gw.log = observed_messages.append
        gateway_client = object.__new__(gw.AlisonClient)
        gateway_client.api = None
        delivered = []
        gateway_client.ws_dispatch = lambda event, message: delivered.append((event, message))
        state = ConnectionState(gateway_client.ws_dispatch, None)
        check("项目内置 BotPy 和群事件位生效",
              Path(botpy.__file__).resolve().parent == ALI / "botpy"
              and botpy.Intents(group_and_c2c_event=True).value == 1 << 25
              and botpy.Intents(public_messages=True).value == 1 << 25)
        check("内置 BotPy 原生解析 GROUP_MESSAGE_CREATE",
              "group_message_create" in state.parsers)
        async def dispatch_raw_frame(event_name, group_id):
            gateway_client.loop = asyncio.get_running_loop()
            frame_state = ConnectionState(botpy.Client.ws_dispatch.__get__(gateway_client), None)
            frame_session = {"last_seq": 0}
            socket = BotWebSocket(frame_session,
                                  SimpleNamespace(parser=frame_state.parsers, loop=gateway_client.loop,
                                                  dispatch=botpy.Client.ws_dispatch.__get__(gateway_client)))
            frame = {"op": 0, "t": event_name, "s": 1, "d": {
                "id": "frame-1", "group_openid": group_id, "content": "收到事件",
                "author": {"member_openid": "MEMBER"},
                "message_scene": {"ext": ["auth_token=SHOULD_NOT_LOG"]}}}
            await socket.on_message(None, json.dumps(frame))
            await asyncio.sleep(0)
            return frame_session["last_seq"]
        seq = asyncio.run(dispatch_raw_frame("GROUP_AT_MESSAGE_CREATE", "AUTO_FRAME_AT"))
        check("原始 WS 群@帧经 SDK 调度到网关并自动登记",
              seq == 1 and groups.get_group("AUTO_FRAME_AT")["push_enabled"] is False
              and any("AUTO_FRAME_AT" in line and "收到事件" in line for line in observed_messages)
              and "SHOULD_NOT_LOG" not in "".join(observed_messages))
        seq = asyncio.run(dispatch_raw_frame("GROUP_MESSAGE_CREATE", "AUTO_FRAME_ALL"))
        check("原始 WS 全量群帧经 SDK 调度到网关并自动登记",
              seq == 1 and groups.get_group("AUTO_FRAME_ALL")["push_enabled"] is False
              and any("AUTO_FRAME_ALL" in line and "收到事件" in line for line in observed_messages))
        async def dispatch_bad_frames():
            gateway_client.loop = asyncio.get_running_loop()
            handler = botpy.Client.ws_dispatch.__get__(gateway_client)
            frame_state = ConnectionState(handler, None)
            socket = BotWebSocket({"last_seq": 0}, SimpleNamespace(
                parser=frame_state.parsers, loop=gateway_client.loop, dispatch=handler))
            await socket.on_message(None, json.dumps({"op": 0, "s": 2, "t": "UNKNOWN_GROUP_EVENT",
                                                       "d": {"auth_token": "SHOULD_NOT_LOG"}}))
            await socket.on_message(None, json.dumps({"op": 0, "s": 3, "t": "GROUP_AT_MESSAGE_CREATE",
                                                       "d": {"author": 123, "auth_token": "SHOULD_NOT_LOG"}}))
            await asyncio.sleep(0)
        asyncio.run(dispatch_bad_frames())
        check("未知及解析失败事件可见且不泄漏载荷、不终止监听",
              any("UNKNOWN_GROUP_EVENT：unregistered" in line for line in observed_messages)
              and any("GROUP_AT_MESSAGE_CREATE：AttributeError" in line for line in observed_messages)
              and "SHOULD_NOT_LOG" not in "".join(observed_messages))
        payload = {"id": "evt1", "d": {"id": "msg-id-1", "group_openid": "AUTO_NEW", "content": "晚上好\n看看新闻",
                                      "author": {"member_openid": "USER_1"},
                                      "message_scene": {"ext": ["auth_token=SHOULD_NOT_LOG"]}}}
        state.parsers["group_message_create"](payload)
        check("全量事件分发为群消息且保留正文",
              len(delivered) == 1 and delivered[0][0] == "group_message_create"
              and delivered[0][1].content == "晚上好\n看看新闻")
        asyncio.run(gateway_client.on_group_message_create(delivered[0][1]))
        check("同一群消息重复事件只打印一次",
              sum("晚上好\\n看看新闻" in line for line in observed_messages) == 1)
        added = groups.get_group("AUTO_NEW")
        check("从全量消息自动加入群列表，新群默认不推送",
              added["group_openid"] == "AUTO_NEW" and added["is_member"]
              and added["push_enabled"] is False)
        check("每条消息都打印，但已知群消息不反复写群目录",
              any("晚上好\\n看看新闻" in line for line in observed_messages))
        original_upsert = gw.upsert_group
        observed_upserts = []
        gw.upsert_group = lambda goid: observed_upserts.append(goid)
        try:
            asyncio.run(gateway_client.on_group_message_create(delivered[0][1]))
            later = gw.GroupMessage(None, None, {"id": "later-id", "group_openid": "AUTO_NEW",
                                                  "content": "新的独立消息", "author": {"member_openid": "USER_1"}})
            asyncio.run(gateway_client.on_group_message_create(later))
        finally:
            gw.upsert_group = original_upsert
        check("重复事件去重；独立新消息仍打印且不重复写群目录",
              observed_upserts == [] and sum("晚上好\\n看看新闻" in line
                                             for line in observed_messages) == 1
              and sum("新的独立消息" in line for line in observed_messages) == 1,
              (observed_upserts, observed_messages))
        check("终端显示群ID/发送者/正文、不泄漏事件鉴权元数据",
              any("AUTO_NEW" in line and "USER_1" in line and "晚上好\\n看看新闻" in line
                  for line in observed_messages)
              and "SHOULD_NOT_LOG" not in "".join(observed_messages), observed_messages)
        at_message = SimpleNamespace(group_openid="GROUP_A", content="@消息内容",
                                     author=SimpleNamespace(member_openid="USER_2"), attachments=[])
        asyncio.run(gateway_client.on_group_at_message_create(at_message))
        check("收到 @ 消息也打印正文，已开启群的开关不被覆盖",
              groups.get_group("GROUP_A")["push_enabled"]
              and any("[群@消息]" in line and "@消息内容" in line for line in observed_messages))
        state.parse_group_message_create({"id": "evt2", "d": {
            "group_openid": "AUTO_NEXT", "content": " ", "author": {"member_openid": "USER_3"},
            "msg_elements": [{"content": "被引用正文"}]}})
        asyncio.run(gateway_client.on_group_message_create(delivered[-1][1]))
        check("群消息元素可提取正文，并自动发现第二个群",
              groups.get_group("AUTO_NEXT")["group_openid"] == "AUTO_NEXT"
              and any("被引用正文" in line for line in observed_messages))
        state.parse_group_message_create({"id": "evt3", "d": {
            "id": "image-msg", "group_openid": "AUTO_IMG", "content": "",
            "author": None, "mentions": None, "attachments": None}})
        asyncio.run(gateway_client.on_group_message_create(delivered[-1][1]))
        check("全量事件空 author/附件也能登记群并打印空消息",
              groups.get_group("AUTO_IMG")["group_openid"] == "AUTO_IMG"
              and any("AUTO_IMG" in line and "[空消息]" in line for line in observed_messages))
    finally:
        gw.log = old_gateway_log

    asyncio.run(gw.AlisonClient.on_group_add_robot(None,
                SimpleNamespace(group_openid="JOINED", timestamp=123456789)))
    check("入群事件登记群但默认关闭推送", groups.get_group("JOINED")["push_enabled"] is False)
    asyncio.run(gw.AlisonClient.on_group_del_robot(None,
                SimpleNamespace(group_openid="JOINED")))
    check("退群事件取消成员状态和推送", not groups.get_group("JOINED")["is_member"]
          and not groups.get_group("JOINED")["push_enabled"])
finally:
    config_env.ENV_FILE = old_env
    groups.STATE_FILE = old_state
    config_env.STATE_FILE = old_env_state
    groups.DB_FILE = old_db
    alison_config.DB_FILE = old_config_db
    config_qq.get_group_info = old_info
    config_qq.get_token = old_token
    alison_config.INFO_INTERVAL = old_interval
    shutil.rmtree(case, ignore_errors=True)

print("[6] 正式环境登录失败能安全提示根因")
from botpy.errors import AuthenticationFailedError  # noqa: E402
original_client = gw.AlisonClient
original_load_env = gw.load_env
original_log = gw.log
startup_logs = []
class RejectedClient:
    def __init__(self, **kwargs):
        pass
    def run(self, **kwargs):
        raise AuthenticationFailedError("接口访问源IP不在白名单 token=SENSITIVE")
try:
    gw.AlisonClient = RejectedClient
    gw.load_env = lambda: {"APPID": "test", "SECRET": "private", "SANDBOX": "0"}
    gw.log = startup_logs.append
    check("正式接口 IP 白名单 401 会报告到终端且不泄漏响应",
          gw.main() == 1 and any("[启动失败]" in entry and "白名单" in entry
                                 for entry in startup_logs)
          and not any("SENSITIVE" in entry or "private" in entry for entry in startup_logs),
          startup_logs)
finally:
    gw.AlisonClient = original_client
    gw.load_env = original_load_env
    gw.log = original_log

print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 艾莉森网关 侧自检全部通过")
