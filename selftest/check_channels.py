# -*- coding: utf-8 -*-
"""自检：分发渠道（WebSocket 通道）—— 真 socket 往返，不联网。

跑法：python selftest/check_channels.py（或统一跑 selftest/run_all.py）

覆盖：
  1. 注册：`hello` → `register_sources` → `channels` 表落库 → `welcome`
  2. 推送模式：服务端逐条推 `item`，渠道回 `result ok` → `items.pushed` 置 true + `pushes` 审计
  3. 失败/延后：回 `result ok=false` 或 `defer` → 条目**保持未发**（下次还会推）
  4. 拉取模式：`want` / `pending` → 渠道按自己节奏拿内容，回执照样写库
  5. 鉴权：token 不对连不上；内置渠道登记
  6. 插件侧镜像客户端 `hx_ws.py` 与仓库 SDK 跑通同一套协议
"""
import json
import shutil
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SANDBOX = REPO / "selftest" / ".sandbox" / "channel_case"

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


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
DB = SANDBOX / "hx.sqlite3"

import collect.db as cdb          # noqa: E402
from distribute import push_db    # noqa: E402

push_db.DB_FILE = DB
# 关掉"旧 JSON 一次性迁移"：否则会读到真实仓库的 data/queue/待推送.json，
# 把 id 比它小的沙箱条目全标成"已发过"（迁移规则本身没错，但自检里不能这么脏）
push_db.LEGACY_QUEUE_FILES = ()
push_db.LEGACY_SENT_FILES = ()

import dashboard.channels as ch   # noqa: E402
from dashboard.ws import ChannelWSServer   # noqa: E402
from distribute.ws_channel import ChannelClient  # noqa: E402

ch.DB_FILE = DB

# 旧部署的 pushes 只有 sent_ts/detail，QQ 发成功后曾因缺 source 写库失败。
legacy_db = SANDBOX / "legacy_pushes.sqlite3"
legacy = sqlite3.connect(legacy_db)
legacy.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,decision TEXT,pushed TEXT,rev INTEGER,stage TEXT)")
legacy.execute("INSERT INTO items VALUES(1,'发','{}',1,'processed')")
legacy.execute("INSERT INTO items VALUES(2,'发','{}',1,'processed')")
legacy.execute("CREATE TABLE pushes(id INTEGER PRIMARY KEY,item_id INTEGER,channel TEXT,"
               "kind TEXT,sent_ts INTEGER,detail TEXT)")
legacy.execute("INSERT INTO pushes(item_id,channel,kind,sent_ts,detail)"
               " VALUES(1,'alison','group_abc',123,'旧记录')")
legacy.commit()
legacy.close()
check("旧 pushes 表补齐 source/sent_at 并可标记已发",
      push_db.mark_pushed([1, 2], "alison:group_abc", db_file=legacy_db) == 2 and
      push_db.pending("alison:group_abc", db_file=legacy_db) == [])
legacy = sqlite3.connect(legacy_db)
check("旧分发记录保留并迁移 source",
      legacy.execute("SELECT source,sent_at FROM pushes WHERE item_id=1").fetchone() ==
      ("alison:group_abc", "123"))
check("旧表的必填 sent_ts 也写入新成功记录",
      legacy.execute("SELECT sent_ts FROM pushes WHERE source='alison:group_abc'"
                     " AND item_id=2").fetchone() is not None)
legacy.close()

conn = sqlite3.connect(DB)
cdb.ensure_schema(conn)
for item_id, account, zh in ((1, "SpaceX", "星舰第十次试飞成功"), (2, "NASA", "月球轨道站进展"),
                             (3, "路人", "今天天气不错")):
    conn.execute("INSERT INTO items(id, account, zh, raw, decision, ai_score, stage)"
                 " VALUES(?,?,?,?,'发',80,'processed')", (item_id, account, zh, f"raw-{item_id}"))
conn.execute("UPDATE items SET decision='丢' WHERE id=3")
conn.commit()
conn.close()

TOKEN = "selftest-token"
PORT = free_port()
server = ChannelWSServer("127.0.0.1", PORT, TOKEN,
                         handle_message=ch.handle_message,
                         on_disconnect=ch.unregister_connection)
print("[0] 起 WS 服务端")
check("WS 服务端已监听", server.start() is True, server.error)


def pushed_of(item_id: int) -> dict:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("SELECT pushed FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0] or "{}") if row else {}


def scalar(sql, args=()):
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def add_item(item_id: int, account: str, zh: str, score: int = 70, stage: str = "processed"):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO items(id, account, zh, raw, decision, ai_score, stage)"
                 " VALUES(?,?,?,?,'发',?,?)", (item_id, account, zh, f"raw-{item_id}", score, stage))
    conn.commit()
    conn.close()


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        time.sleep(0.05)
    return bool(predicate())


def make_client(channel, targets, pull=False, adapter="process"):
    return ChannelClient(channel, channel.title(), targets, host="127.0.0.1", port=PORT,
                         token=TOKEN, adapter=adapter, pull=pull, logger=lambda *a: None)


class FakeConnection:
    def __init__(self):
        self.sent = []

    def send_json(self, message):
        self.sent.append(message)
        return True


print("[1] 注册：hello → register_sources → channels 表 → welcome（推送模式）")
seen = []


def alison_send(item, source):
    """假艾莉森：全部发成功，并记下收到的 source。"""
    seen.append((source, item["id"]))
    return {"ok": True}


client = make_client("alison", [{"kind": "group_999", "label": "测试群"}])
thread = threading.Thread(target=client.run_forever, args=(alison_send,),
                          kwargs={"max_rounds": 2}, daemon=True)
thread.start()
wait_for(lambda: len(seen) >= 2)
wait_for(lambda: pushed_of(1).get("alison:group_999") is True
         and pushed_of(2).get("alison:group_999") is True)
thread.join(timeout=5)

check("渠道收到了两条待发内容", sorted(item_id for _, item_id in seen) == [1, 2], seen)
check("来源都是自己的 source", {source for source, _ in seen} == {"alison:group_999"}, seen)
check("发成功后 pushed 置 true", pushed_of(1).get("alison:group_999") is True, pushed_of(1))
check("pushes 审计表记了 2 条", scalar("SELECT COUNT(*) FROM pushes") == 2,
      scalar("SELECT COUNT(*) FROM pushes"))
check("pushes 记了 channel/kind", scalar("SELECT channel || '/' || kind FROM pushes LIMIT 1")
      == "alison/group_999", scalar("SELECT channel || '/' || kind FROM pushes LIMIT 1"))
check("decision='丢' 的不会被推", pushed_of(3).get("alison:group_999") is not True, pushed_of(3))
check("source 进了注册表", push_db.registry(db_file=DB) == ["alison:group_999"],
      push_db.registry(db_file=DB))
check("已发完 → 待发 0", push_db.pending(source="alison:group_999", db_file=DB) == [])

rego = ch.channel_row("alison", db_file=DB)
check("channels 表登记了渠道与 targets",
      rego.get("adapter") == "process"
      and rego.get("targets") == [{"kind": "group_999", "label": "测试群"}], rego)

print("[1b] 拉取模式：want / pending（麦麦插件那种「按自己节奏要内容」）")
add_item(6, "Pull", "拉取测试条目", 90)
pull = make_client("pull_probe", [{"kind": "group_555", "label": "拉取群"}], pull=True)
pulled = {}
done = threading.Event()


def run_pull():
    try:
        if not pull.connect():
            pulled["error"] = pull.last_error
            return
        pulled["pending"] = pull.pending("pull_probe:group_555")
        items = pull.want("pull_probe:group_555", 5)
        pulled["ids"] = [it["id"] for it in items]
        # 只把最后一条当"发成功"（前面几条故意不回执，模拟"拿了还没发"）
        if items:
            pull.send_result("pull_probe:group_555", items[-1]["id"], True)
        time.sleep(0.5)                     # 等服务端落库
    finally:
        pull.stopped = True
        pull.close()
        done.set()


thread_pull = threading.Thread(target=run_pull, daemon=True)
thread_pull.start()
done.wait(timeout=15)
thread_pull.join(timeout=5)

check("拉取模式连上并拿到条目", pulled.get("ids") == [6], pulled)
check("pending 查询能问到未发条数", pulled.get("pending") == 1, pulled)
wait_for(lambda: pushed_of(6).get("pull_probe:group_555") is True)
check("拉取模式的回执照样写库", pushed_of(6).get("pull_probe:group_555") is True, pushed_of(6))
check("老渠道发过的条目不会灌给新通道（回填规则）",
      pushed_of(1).get("pull_probe:group_555") is True, pushed_of(1))

print("[1c] 已连接渠道轮询处理输出池")
add_item(13, "Pipeline", "处理输出订阅测试", stage="judged")
output_source = "process_output_probe:group_333"
push_db.register_sources([output_source], db_file=DB)
push_db.mark_pushed([6], output_source, db_file=DB)  # 隔离上一段留下的历史样例
output_client = make_client("process_output_probe", [{"kind": "group_333", "label": "输出测试"}])
output_seen = []
output_thread = threading.Thread(
    target=output_client.run_forever,
    args=(lambda item, source: (output_seen.append((source, item["id"])), {"ok": True})[1],),
    kwargs={"max_rounds": 1}, daemon=True)
output_thread.start()
time.sleep(0.3)
check("stage=judged 时输出池不提前暴露", output_seen == [], output_seen)
conn = sqlite3.connect(DB)
conn.execute("UPDATE items SET stage='processed' WHERE id=13")
conn.commit()
conn.close()
wait_for(lambda: bool(output_seen))
output_thread.join(timeout=5)
check("处理完成后已连接渠道下一轮扫描收到输出",
      output_seen == [("process_output_probe:group_333", 13)], output_seen)
conn = sqlite3.connect(DB)
conn.execute("UPDATE items SET decision='丢' WHERE id=13")
conn.commit()
conn.close()

print("[2] 失败 / 延后就保持未发（下次还会推）")
add_item(4, "Blue", "新一条")
fail_client = make_client("napcat_probe", [{"kind": "group_888", "label": "联调群"}],
                          adapter="plugin")
seen_fail = []
thread2 = threading.Thread(
    target=fail_client.run_forever,
    args=(lambda item, source: (seen_fail.append(item["id"]),
                                {"ok": False, "detail": "沙箱里当然发不出去"})[1],),
    kwargs={"max_rounds": 1}, daemon=True)
thread2.start()
wait_for(lambda: bool(seen_fail))
thread2.join(timeout=5)
time.sleep(0.3)

check("渠道收到了那条", seen_fail == [4], seen_fail)
check("回执 ok=false → 条目保持未发",
      pushed_of(4).get("napcat_probe:group_888") is not True, pushed_of(4))
check("失败不写 pushes",
      scalar("SELECT COUNT(*) FROM pushes WHERE source='napcat_probe:group_888'") == 0)
check("失败尝试写入分发记录并标明失败",
      scalar("SELECT COUNT(*) FROM push_attempts WHERE source='napcat_probe:group_888'"
             " AND status='failed'") == 1)

defer_client = make_client("defer_probe", [{"kind": "group_777", "label": "x"}])
seen_defer = []
thread3 = threading.Thread(
    target=defer_client.run_forever,
    args=(lambda item, source: (seen_defer.append(item["id"]),
                                {"defer": 600, "reason": "静默时段"})[1],),
    kwargs={"max_rounds": 1}, daemon=True)
thread3.start()
wait_for(lambda: bool(seen_defer))
thread3.join(timeout=5)
time.sleep(0.3)
check("defer 的条目也保持未发",
      seen_defer == [4] and pushed_of(4).get("defer_probe:group_777") is not True, pushed_of(4))

print("[3] 鉴权 / 内置渠道 / 看板口径")
bad = make_client("bad", [{"kind": "group_1", "label": "x"}])
bad.token = "wrong-token"
check("token 不对连不上", bad.connect() is False)
check("连不上时不报错崩溃", bad.connected is False)

added = ch.ensure_builtin_channels(db_file=DB)
check("内置渠道都在表里（alison 是自检里先连上来的，所以只补命令）",
      set(added) <= {"alison", "maibot", "napcat"}
      and all(ch.channel_row(cid, db_file=DB) for cid in ("alison", "maibot", "napcat")), added)
check("内置渠道幂等（再调不再新增）", ch.ensure_builtin_channels(db_file=DB) == [])
builtin_alison = ch.channel_row("alison", db_file=DB)
check("内置艾莉森是看板托管渠道且带启动命令",
      builtin_alison["adapter"] == "process"
      and json.loads(builtin_alison["command"])[1] == "distribute/alison/channel.py",
      builtin_alison)
check("内置麦麦插件是外部渠道（看板只控制启用）",
      ch.channel_row("maibot", db_file=DB)["adapter"] == "plugin",
      ch.channel_row("maibot", db_file=DB))

channels = {c["id"]: c for c in ch.list_channels(db_file=DB)}
check("看板能看到内置渠道与自检渠道",
      set(channels) >= {"alison", "maibot", "napcat", "pull_probe", "napcat_probe", "defer_probe"},
      sorted(channels))
check("渠道带 target 细节（source/待发/累计发送）",
      channels["alison"]["targets"][0]["source"] == "alison:group_999"
      and channels["alison"]["targets"][0]["sent_total"] == 2
      and channels["alison"]["targets"][0]["pending"] >= 1, channels["alison"])
check("最近分发记录能读出来",
      any(row["channel"] == "alison" for row in ch.recent_pushes(10, db_file=DB)),
      ch.recent_pushes(3, db_file=DB))

print("[4] 看板开关（enabled）与离线状态")
ch.set_enabled("alison", False, db_file=DB)
check("停用写库", ch.channel_row("alison", db_file=DB)["enabled"] is False)
ch.set_enabled("alison", True, db_file=DB)
check("重新启用写库", ch.channel_row("alison", db_file=DB)["enabled"] is True)
check("离线渠道 connected=false",
      [c for c in ch.list_channels(db_file=DB) if c["id"] == "alison"][0]["connected"] is False)

print("[4a] 回执必须匹配当前 source 与 item_id")
ack_source = "ack_probe:target"
pull_source = "pull_guard:target"
push_db.register_sources([ack_source, pull_source], db_file=DB)
add_item(11, "Ack Guard", "回执匹配测试")
add_item(12, "Pull Guard", "拉取回执匹配测试")
ack_session = ch.ChannelSession("ack_probe", "Ack Probe", ch.ADAPTER_PROCESS,
                                [{"kind": "target", "label": "测试目标"}], FakeConnection())
wrong_item_reply = {"type": "result", "source": ack_source, "item_id": 12, "ok": True}
wrong_source_reply = {"type": "result", "source": "other:target", "item_id": 11, "ok": True}
ack_session._handle_reply(ack_source, {"id": 11, "account": "Ack Guard"}, wrong_item_reply)
check("推送模式错误 item_id 的成功回执不会误标已发",
      pushed_of(11).get(ack_source) is not True, pushed_of(11))
ack_session.acks.put(wrong_source_reply)
ack_session.acks.put(wrong_item_reply)
expected_reply = {"type": "result", "source": ack_source, "item_id": 11, "ok": True}
ack_session.acks.put(expected_reply)
matched_reply = ack_session._wait_for_reply(ack_source, {"id": 11}, timeout=1)
check("推送线程跳过 source/item_id 不匹配的迟到回执",
      matched_reply == expected_reply, matched_reply)
ack_session._handle_reply(ack_source, {"id": 11, "account": "Ack Guard"}, matched_reply)
check("正确回执仍能标记当前条目已发",
      pushed_of(11).get(ack_source) is True, pushed_of(11))

pull_guard_session = ch.ChannelSession(
    "pull_guard", "Pull Guard", ch.ADAPTER_PLUGIN,
    [{"kind": "target", "label": "测试目标"}], FakeConnection(), pull=True)
pull_guard_session.inflight[pull_source] = {12: time.time()}
pull_guard_session.handle_result(pull_source, 11, True)
check("拉取模式非在途 item_id 的回执不会误标已发",
      pushed_of(12).get(pull_source) is not True, pushed_of(12))
pull_guard_session.handle_result(pull_source, "12", True)
check("拉取模式在途条目的有效回执正常记账",
      pushed_of(12).get(pull_source) is True, pushed_of(12))

print("[4b] 空目标渠道可连接，新增群后不用断线即可重注册")
empty = make_client("empty_probe", [], pull=True)
try:
    check("零个启用目标时仍可注册渠道", empty.connect() is True)
    welcome = empty._read_reply(lambda msg: msg.get("type") == "welcome", 4)
    check("欢迎消息允许空 source", bool(welcome) and welcome["sources"] == [], welcome)
    check("服务端登记空目标", ch.channel_row("empty_probe", db_file=DB)["targets"] == [])
    existing_session = ch.SESSIONS.get("empty_probe")
    check("添加目标发送第二次 hello", empty.update_targets([{"kind": "group_NEW", "label": "新群"}]))
    wait_for(lambda: len(ch.channel_row("empty_probe", db_file=DB)["targets"]) == 1)
    check("同一 WS 更新目标保留原会话和推送线程",
          ch.SESSIONS.get("empty_probe") is existing_session and existing_session.thread.is_alive())
    check("动态注册新群 source", ch.channel_row("empty_probe", db_file=DB)["targets"]
          == [{"kind": "group_NEW", "label": "新群"}]
          and "empty_probe:group_NEW" in push_db.registry(db_file=DB))
finally:
    empty.stopped = True
    empty.close()

print("[5] 插件侧镜像客户端 hx_ws.py 也能跑通同一套协议")
PLUGIN_DIR = REPO / "distribute" / "maibot" / "hangxiaomai_news"
sys.path.insert(0, str(PLUGIN_DIR))
from hx_ws import ChannelClient as MirrorClient  # noqa: E402

add_item(7, "Mirror", "镜像客户端条目", 60)
# 先做一次回填（= 镜像客户端 hello 时会做的事），再算"该它发的"
# —— 老渠道已发完的条目会被回填成 true，不会灌给新通道
push_db.register_sources(["maibot:private"], db_file=DB)
expected = [it["id"] for it in push_db.pending(source="maibot:private", db_file=DB)][:2]
mirror = MirrorClient("maibot", "麦麦插件", [{"kind": "private", "label": "私聊"}],
                      host="127.0.0.1", port=PORT, token=TOKEN, adapter="plugin",
                      logger=lambda *a: None)
mirror_seen = []
mirror_result = {}
mirror_done = threading.Event()


def mirror_run():
    try:
        mirror.run_forever(lambda item, source: (mirror_seen.append(item["id"]), {"ok": True})[1],
                           max_rounds=2)
        mirror_result["error"] = mirror.last_error
    except Exception as exc:                      # 镜像客户端不该抛
        mirror_result["error"] = f"{type(exc).__name__}: {exc}"
    mirror_done.set()


thread4 = threading.Thread(target=mirror_run, daemon=True)
thread4.start()
wait_for(lambda: len(mirror_seen) >= 2, timeout=15)
mirror_done.wait(timeout=10)
check("镜像客户端按 id 升序拿到未发的两条", mirror_seen == expected, (mirror_seen, expected))
wait_for(lambda: all(pushed_of(i).get("maibot:private") is True for i in expected))
check("镜像客户端发的回执被服务端采纳（写库）",
      all(pushed_of(i).get("maibot:private") is True for i in expected),
      [pushed_of(i) for i in expected])
check("镜像客户端没抛异常", not mirror_result.get("error"), mirror_result)

# 收尾：把客户端和服务端都干净停掉（不然守护线程会在解释器退出时抢 stdout）
for probe in (client, fail_client, defer_client, pull, mirror):
    probe.stopped = True
    probe.close()
for worker in (thread, thread2, thread3, thread_pull, thread4):
    worker.join(timeout=3)
server.stop()
time.sleep(0.3)

print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 分发渠道（WS 通道 + 拉取模式）自检全部通过")
