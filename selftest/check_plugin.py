# -*- coding: utf-8 -*-
"""重构后自检：麦麦插件 侧（装配 + 一轮调度 + Hook + 配置落盘）。

跑法：python selftest/check_plugin.py（或统一跑 selftest/run_all.py）
要点：
  - 用"按文件路径加载、且插件目录**不在** sys.path 上"的最坏情况验证导入
  - 所有数据文件都重定向到 自检/.sandbox/plugin_config/，不碰真实 配置/ 目录
  - 插件现在**不读数据库**：它作为渠道连看板（`hx_ws.ChannelClient`，拉取模式）。
    这里用替身客户端供内容，验证"按节奏 want → 发 → 回报结果"；
    真实 WS 往返（hello/item/result/写库）见 selftest/check_channels.py。
"""
import asyncio
import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "distribute" / "maibot" / "hangxiaomai_news"
SANDBOX = REPO / "selftest" / ".sandbox" / "plugin_config"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(REPO / "selftest" / "stubs"))   # 桩模块（openai / maibot_sdk）
assert str(PLUGIN_DIR) not in sys.path, "本测试故意不把插件目录放进 sys.path"

failures = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond:
        failures.append(name)


# ────────────────────────── 准备沙箱配置目录 ──────────────────────────
if SANDBOX.exists():
    shutil.rmtree(SANDBOX)
SANDBOX.mkdir(parents=True)


def w(path: Path, text: str):
    path.write_text(text, encoding="utf-8")


w(SANDBOX / "推送默认.toml", '[push]\n"活跃时段" = "00:00-23:59"\n"间隔最小分钟" = 0\n'
                             '"间隔最大分钟" = 0\n"一次发几条" = 1\n"带图" = true\n'
                             '"一天最多发几次" = 0\n"闲聊" = true\n"新闻推送" = true\n')
w(SANDBOX / "功能开关.toml", "新闻推送 = true\n")
w(SANDBOX / "内容留档.json", "[]")
w(SANDBOX / "已发记录.json", "[]")

# ────────────────────────── 按文件路径加载插件（最坏情况）──────────────────────────
spec = importlib.util.spec_from_file_location("plugin", PLUGIN_DIR / "plugin.py")
plugin_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_mod)

print("[1] 插件加载与类装配")
check("create_plugin 可调用", callable(plugin_mod.create_plugin))
check("插件目录已被加载器加入 sys.path（兄弟模块才 import 得到）",
      str(PLUGIN_DIR) in sys.path)
Plugin = plugin_mod.HangxiaomaiNewsPlugin
for meth in ("_poll_loop", "_maybe_push", "_is_active_hours", "_next_random_interval",
             "_push_targets", "_resolve_stream_id", "_extract_stream_id", "_send_text_and_images",
             "_speak_news", "_send_text", "_do_daily_first_push", "_send_summary",
             "_send_one_archive_to", "_send_casual_to", "_send_reminder_if_pending",
             "_read_toml", "_effective_settings_for", "_bool_setting", "_user_pref_path",
             "_start_channel", "_target_label", "_channel_ready", "_channel_want",
             "_channel_pending", "_channel_result",
             "handle_push_pref", "on_load", "on_unload", "on_config_update"):
    check(f"方法 {meth}", hasattr(Plugin, meth), "Mixin 没装齐？")
check("Hook 元信息挂在 handle_push_pref 上",
      getattr(Plugin.handle_push_pref, "__hook_event__", "") == "chat.receive.after_process")
check("钩子模式=BLOCKING / EARLY",
      getattr(Plugin.handle_push_pref, "__hook_mode__", "") == "blocking"
      and getattr(Plugin.handle_push_pref, "__hook_order__", None) == 0)

# ────────────────────────── 把路径重定向到沙箱 ──────────────────────────
import hx_config, hx_llm, hx_paths, hx_schedule, hx_store, hx_ws  # noqa: E402

paths = {
    "TOKEN_FILE": SANDBOX / "dashboard_token.txt",
    "ARCHIVE_FILE": SANDBOX / "内容留档.json",
    "SWITCH_FILE": SANDBOX / "功能开关.toml",
    "REMINDER_FILE": SANDBOX / "提醒待发.txt",
    "STATE_FILE": SANDBOX / "推送状态.json",
    "PUSH_DEFAULT_FILE": SANDBOX / "推送默认.toml",
    "CONFIG_DIR": SANDBOX,
}
for mod in (hx_paths, hx_store, hx_schedule, hx_config, hx_ws):
    for k, v in paths.items():
        if hasattr(mod, k):
            setattr(mod, k, v)
hx_llm.MAIBOT_CONFIG = SANDBOX / "bot_config.toml"       # 不存在 → 走兜底人设
plugin_mod.STATE_FILE = SANDBOX / "推送状态.json"


# ────────────────────────── 渠道客户端替身 ──────────────────────────
class FakeChannelClient:
    """替身：不连 WS，按测试塞进来的清单供内容，并记下回报的结果。"""

    instances = []

    def __init__(self, channel="maibot", label="麦麦插件", targets=None, **kwargs):
        self.channel = channel
        self.label = label
        self.targets = targets or []
        self.pull = bool(kwargs.get("pull"))
        self.connected = True
        self.paused = False
        self.host = "127.0.0.1"
        self.port = 8788
        self.sources = [f"{channel}:{t['kind']}" for t in self.targets]
        self.items = {}            # source -> [item, ...]
        self.results = []          # (source, item_id, ok, detail)
        self.pending_calls = []
        self.stopped = False
        FakeChannelClient.instances.append(self)

    # 测试用
    def feed(self, source, items):
        self.items[source] = list(items)

    def want(self, source, count=1):
        queue_ = self.items.get(source, [])
        taken, self.items[source] = queue_[:count], queue_[count:]
        return taken

    def pending(self, source):
        self.pending_calls.append(source)
        return len(self.items.get(source, []))

    def send_result(self, source, item_id, ok, detail=""):
        self.results.append((source, item_id, bool(ok), detail))

    def run_forever(self, on_item=None, max_rounds=None):
        return

    def close(self):
        self.connected = False


plugin_mod.ChannelClient = FakeChannelClient

print("[2] 生效设置 / 活跃时段 / 间隔")


class Logger:
    def __init__(self):
        self.lines = []

    def _p(self, lvl, msg):
        self.lines.append(f"{lvl}: {msg}")

    def info(self, m):
        self._p("INFO", m)

    def warning(self, m):
        self._p("WARN", m)

    def debug(self, m):
        self._p("DEBUG", m)

    def error(self, m):
        self._p("ERROR", m)


logger = Logger()

cfg = plugin_mod.HangxiaomaiNewsPluginConfig()
cfg.plugin.enabled = True
cfg.push.target_qq = "10001"
cfg.push.target_groups = ["20002"]
cfg.push.auto_switch = True
plugin = Plugin(config=cfg)
plugin.ctx = type("Ctx", (), {"logger": logger})()

st = plugin._effective_settings_for("private")
check("生效设置读到了沙箱基线(活跃时段 00:00-23:59)", st.get("活跃时段") == "00:00-23:59", st)
check("布尔设置解析", plugin._bool_setting({"带图": "true"}, "带图", False) is True
      and plugin._bool_setting({"带图": "false"}, "带图", True) is False
      and plugin._bool_setting({}, "带图", True) is True)
check("全天活跃", plugin._is_active_hours({"活跃时段": "00:00-23:59"}) is True)
check("跨零点(11:00-01:00) 由当前时刻决定",
      isinstance(plugin._is_active_hours({"活跃时段": "11:00-01:00"}), bool))
check("不活跃时段", plugin._is_active_hours({"活跃时段": "03:00-03:01"}) is False)
check("畸形时段=宽松放行", plugin._is_active_hours({"活跃时段": "乱写"}) is True)
gap = plugin._next_random_interval({"间隔最小分钟": 0, "间隔最大分钟": 0})
check("间隔写 0 会被当成没配置→兜底 20~40 分钟（旧行为，别改）", 1200 <= gap <= 2400, gap)
check("正常间隔区间生效", plugin._next_random_interval(
    {"间隔最小分钟": 5, "间隔最大分钟": 5}) == 300)
check("间隔区间写反也能纠正", 60 <= plugin._next_random_interval(
    {"间隔最小分钟": 40, "间隔最大分钟": 20}) <= 2400)
check("target 显示名", plugin._target_label("private") == "私聊"
      and plugin._target_label("group_20002") == "群 20002")

print("[3] 一轮调度：给私聊/群各发一条，并回报结果")


class FakeChat:
    async def get_stream_by_user_id(self, uid, platform="qq"):
        return {"stream_id": f"s_priv_{uid}"}

    async def get_stream_by_group_id(self, gid, platform="qq"):
        return {"stream_id": f"s_grp_{gid}"}

    async def open_session(self, **kw):
        return {"stream_id": "s_opened"}


class FakeSend:
    def __init__(self):
        self.calls = []

    async def text(self, text, stream_id, **kw):
        self.calls.append(("text", stream_id, text))
        return {"sent": True}

    async def hybrid(self, segs, stream_id, **kw):
        self.calls.append(("hybrid", stream_id, segs))
        return {"sent": True}


class FakeMessage:
    async def get_recent(self, sid, limit=6):
        return [{"text": "在吗"}]

    async def build_readable(self, recent):
        return "在吗"


send = FakeSend()
plugin.ctx = type("Ctx", (), {"logger": logger, "chat": FakeChat(), "send": send,
                              "message": FakeMessage()})()

SAMPLE = {"id": 1, "account": "SpaceX", "zh": "星舰第十次试飞成功", "raw": "Starship",
          "score": 90, "images": [], "date": "2026-09-18 12:00:00", "ts": 1789000000}
channel = FakeChannelClient(targets=[{"kind": "private", "label": "私聊"},
                                     {"kind": "group_20002", "label": "群 20002"}])
channel.feed("maibot:private", [SAMPLE])
channel.feed("maibot:group_20002", [SAMPLE])
plugin._channel_client = channel

asyncio.run(plugin._maybe_push(plugin_mod.STATE_FILE, 0.0))   # 第一轮：建立前态
asyncio.run(plugin._maybe_push(plugin_mod.STATE_FILE, 0.0))   # 第二轮：真正推送
targets_hit = {c[1] for c in send.calls}
check("私聊和群都发了", "s_priv_10001" in targets_hit and "s_grp_20002" in targets_hit, targets_hit)
check("消息里有来源行", any("来源：@SpaceX" in c[2] for c in send.calls if c[0] == "text"),
      [c[2][:40] for c in send.calls if c[0] == "text"])
check("发成功后回报 ok=True（看板据此写库）",
      sorted(channel.results) == [("maibot:group_20002", 1, True, ""),
                                  ("maibot:private", 1, True, "")], channel.results)
check("发过了就不再要（want 的清单已空）",
      channel.want("maibot:private", 1) == [] and channel.want("maibot:group_20002", 1) == [])
state = json.loads((SANDBOX / "推送状态.json").read_text(encoding="utf-8"))
check("目标 kind 命名：private + group_<群号>", "last_push_ts_private" in state
      and "last_push_ts_group_20002" in state, sorted(state))
check("状态写了 next_gap（每个目标各自一份）", "next_gap_private" in state
      and "next_gap_group_20002" in state)
check("每日计数累加", int(state.get("daily_push_count", 0)) >= 2, state.get("daily_push_count"))
check("不再写 已发记录.json（内容原样没动）",
      (SANDBOX / "已发记录.json").read_text(encoding="utf-8") == "[]")
check("不再写 待推送.json（沙箱里根本没这个文件）", not (SANDBOX / "待推送.json").exists())

print("[3b] 发送失败 → 回报 ok=False（条目保持未发）")
channel2 = FakeChannelClient(targets=[{"kind": "private", "label": "私聊"}])
channel2.feed("maibot:private", [{"id": 7, "account": "NASA", "zh": "会失败的条目",
                                  "raw": "x", "score": 10, "images": []}])
plugin._channel_client = channel2
state["last_push_ts_private"] = 0
state["next_gap_private"] = 0
(SANDBOX / "推送状态.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


class FailingSend(FakeSend):
    """两条都发不出去（hybrid 与 text 都返回 sent=False）。

    `_speak_news` 的语义是"正文或评论有一条成功就算成功"，所以这里必须两条都失败，
    才能逼出 False —— 也就才能验证"失败不会谎报成功"。
    """

    async def hybrid(self, segs, stream_id, **kw):
        return {"sent": False}

    async def text(self, text, stream_id, **kw):
        return {"sent": False}


plugin.ctx = type("Ctx", (), {"logger": logger, "chat": FakeChat(), "send": FailingSend(),
                              "message": FakeMessage()})()
asyncio.run(plugin._maybe_push(plugin_mod.STATE_FILE, 0.0))
check("发送失败（_speak_news 返回 False）→ 回报 ok=False",
      channel2.results == [("maibot:private", 7, False, "麦麦发送返回失败")], channel2.results)
check("失败的那条不会被算成已发（状态里没有它）",
      int(json.loads((SANDBOX / "推送状态.json").read_text(encoding="utf-8"))
          .get("daily_push_count", 0)) == int(state.get("daily_push_count", 0)),
      (SANDBOX / "推送状态.json").read_text(encoding="utf-8"))
plugin.ctx = type("Ctx", (), {"logger": logger, "chat": FakeChat(), "send": send,
                              "message": FakeMessage()})()

print("[3c] 没连上看板 → 什么都不做")
channel3 = FakeChannelClient(targets=[{"kind": "private", "label": "私聊"}])
channel3.connected = False
channel3.feed("maibot:private", [{"id": 8, "account": "X", "zh": "不该被发", "raw": "x"}])
plugin._channel_client = channel3
before = len(send.calls)
asyncio.run(plugin._maybe_push(plugin_mod.STATE_FILE, 0.0))
check("断线时不发也不回报", len(send.calls) == before and channel3.results == [])

print("[4] 没有待发内容 + 有留档 → 发旧闻兜底")
channel4 = FakeChannelClient(targets=[{"kind": "private", "label": "私聊"},
                                      {"kind": "group_20002", "label": "群 20002"}])
plugin._channel_client = channel4
w(SANDBOX / "内容留档.json", json.dumps(
    [{"account": "旧账号", "zh": "旧闻一条", "raw": "old", "score": 10, "images": []}],
    ensure_ascii=False))
for kind in ("private", "group_20002"):
    state[f"last_push_ts_{kind}"] = 0
    state[f"next_gap_{kind}"] = 0
(SANDBOX / "推送状态.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
before = len(send.calls)
asyncio.run(plugin._maybe_push(plugin_mod.STATE_FILE, 0.0))
archive_now = json.loads((SANDBOX / "内容留档.json").read_text(encoding="utf-8"))
check("留档被消费掉一条", len(archive_now) == 0, archive_now)
check("兜底确实发了消息", len(send.calls) > before, (before, len(send.calls)))

print("[4b] 有内容但节奏没到点 → 不做兜底（别把旧闻插进来）")
channel6 = FakeChannelClient(targets=[{"kind": "private", "label": "私聊"}])
channel6.feed("maibot:private", [{"id": 10, "account": "X", "zh": "还没到点发的内容",
                                  "raw": "x", "images": []}])
plugin._channel_client = channel6
w(SANDBOX / "内容留档.json", json.dumps(
    [{"account": "旧账号", "zh": "不该发的旧闻", "raw": "old", "images": []}], ensure_ascii=False))
state["last_push_ts_private"] = time.time()      # 刚发过 → 间隔没到
state["next_gap_private"] = 3600
(SANDBOX / "推送状态.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
before = len(send.calls)
asyncio.run(plugin._maybe_push(plugin_mod.STATE_FILE, 0.0))
check("到点前不推内容", channel6.results == [])
check("到点前也不发留档旧闻（内容还在等着发）",
      len(json.loads((SANDBOX / "内容留档.json").read_text(encoding="utf-8"))) == 1)
check("问过看板「还有多少条未发」", channel6.pending_calls != [], channel6.pending_calls)
check("没多发消息", len(send.calls) == before)

print("[5] 功能开关 off → 一条都不发")
w(SANDBOX / "功能开关.toml", "新闻推送 = false\n")
channel5 = FakeChannelClient(targets=[{"kind": "private", "label": "私聊"}])
channel5.feed("maibot:private", [{"id": 9, "account": "X", "zh": "内容", "raw": "c"}])
plugin._channel_client = channel5
before = len(send.calls)
asyncio.run(plugin._maybe_push(plugin_mod.STATE_FILE, 0.0))
check("总闸关闭时静默", len(send.calls) == before and channel5.results == [])
w(SANDBOX / "功能开关.toml", "新闻推送 = true\n")

print("[6] 私聊自然语言改偏好（Hook）")
fake_parsed = {"action": "set", "schedule": {"活跃时段": "12:00-23:00", "一次发几条": 2},
               "scoring": {"高阈值": 30}, "preference": {"少发": ["猎鹰"]}}


async def fake_parse(text, context="", logger=None):
    return fake_parsed


plugin_mod.parse_push_pref = fake_parse


async def fake_reply(parsed, logger=None):
    return "好嘞，记下了。"


plugin_mod.build_setting_reply = fake_reply
res = asyncio.run(plugin.handle_push_pref(message={
    "processed_plain_text": "以后 12 点开始推，一次发两条，少发猎鹰",
    "session_id": "s_priv_10001",
    "message_info": {"user_info": {"user_id": "10001"}},
}))
check("Hook 返回 abort（拦截这条消息）", res == {"action": "abort"}, res)
push_pref = SANDBOX / "推送偏好_10001.toml"
check("写下了 推送偏好_10001.toml", push_pref.exists())
if push_pref.exists():
    txt = push_pref.read_text(encoding="utf-8")
    check("中文 key 带引号（TOML 必须）", '"活跃时段" = "12:00-23:00"' in txt, txt)
    check("数值字段写对", '"一次发几条" = 2' in txt, txt)
scoring = SANDBOX / "推送规则_10001.toml"
check("写下了 推送规则_10001.toml（阈值钳制）", scoring.exists()
      and '"高阈值" = 30' in scoring.read_text(encoding="utf-8"))
pref = SANDBOX / "偏好_10001.toml"
check("写下了 偏好_10001.toml（少发→dislike）", pref.exists()
      and "猎鹰" in pref.read_text(encoding="utf-8"))
grp_res = asyncio.run(plugin.handle_push_pref(message={
    "processed_plain_text": "少发猎鹰",
    "session_id": "s_grp_20002",
    "message_info": {"group_info": {"group_id": "20002"}, "user_info": {"user_id": "9"}},
}))
check("群消息不处理（只私聊生效）", grp_res == {"action": "continue"}, grp_res)
other = asyncio.run(plugin.handle_push_pref(message={"processed_plain_text": "今天天气不错",
                                                    "message_info": {"user_info": {"user_id": "9"}}}))
check("与推送无关的话放行", other == {"action": "continue"}, other)

print("[7] 非法配置不落盘")
bad = plugin._user_pref_path("10001")
before_txt = bad.read_text(encoding="utf-8")
hx_prefs = sys.modules["hx_prefs"]
hx_prefs.apply_schedule(plugin, "10001", {"活跃时段": "乱写", "一次发几条": -5, "间隔最小分钟": "abc"})
after_txt = bad.read_text(encoding="utf-8")
check("畸形时段被丢弃", '"乱写"' not in after_txt, after_txt)
check("负数被钳到 0", '"一次发几条" = 0' in after_txt, after_txt)
check("非数字被丢弃", '"间隔最小分钟" = abc' not in after_txt, after_txt)
check("已有关键字段没被清掉", '"活跃时段" = "12:00-23:00"' in after_txt, after_txt)

print("[8] on_load 会起渠道 + 任务并归零节奏")
w(SANDBOX / "推送状态.json", json.dumps({"last_push_ts": 1, "empty_cycles_private": 9}))
FakeChannelClient.instances.clear()


async def run_load():
    await plugin.on_load()
    await asyncio.sleep(0.05)
    await plugin.on_unload()


asyncio.run(run_load())
launched = FakeChannelClient.instances[-1] if FakeChannelClient.instances else None
check("on_load 建了渠道客户端（拉取模式 + 每目标一个 target）",
      launched is not None and launched.pull is True and len(launched.targets) == 2,
      [c.targets for c in FakeChannelClient.instances])
check("on_unload 把渠道关掉", launched is not None and launched.connected is False)
st2 = json.loads((SANDBOX / "推送状态.json").read_text(encoding="utf-8"))
check("last_push_ts 被重置为当前时间", st2.get("last_push_ts", 0) > 1000, st2)
check("空内容计数被清零", st2.get("empty_cycles_private") == 0, st2)

print("[9] 按包方式加载也能 import（宿主的另一种加载方式）")
sys.path.insert(0, str(REPO / "distribute" / "maibot"))
pkg_mod = importlib.import_module("hangxiaomai_news.plugin")
check("hangxiaomai_news.plugin 作为包可导入", hasattr(pkg_mod, "create_plugin"))
check("包内的 hx_config 走的是相对导入",
      hasattr(sys.modules.get("hangxiaomai_news.hx_config"), "ConfigMixin"))

print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 麦麦插件 侧自检全部通过")
