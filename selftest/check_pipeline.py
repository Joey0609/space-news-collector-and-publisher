# -*- coding: utf-8 -*-
"""重构后自检：抓取脚本 侧的导入 + 关键逻辑冒烟测试。

跑法：python 自检/check_pipeline.py（或统一跑 自检/run_all.py）
（需要 自检/stubs 里的 openai/twscrape 桩模块）
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "collect"
PROCESS = REPO / "process"
SANDBOX = REPO / "selftest" / ".sandbox"
sys.path.insert(0, str(REPO / "selftest" / "stubs"))   # 桩模块
sys.path.insert(0, str(REPO))                         # collect package
sys.path.insert(0, str(SEED))                         # collect source imports
sys.path.insert(0, str(PROCESS))                      # process modules
SANDBOX.mkdir(parents=True, exist_ok=True)
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


def raises(fn) -> bool:
    """调用应当抛异常（用来验证"坏输出不能被当成成功"）。"""
    try:
        fn()
    except Exception:
        return True
    return False


def load_by_path(path: Path, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("[1] collect core + X source 子模块可导入")
import collect.paths as paths          # noqa: E402
import collect.env as env              # noqa: E402
import collect.jsonio as jsonio        # noqa: E402
import collect.store as store          # noqa: E402
import sources.x.tweet_utils as tu     # noqa: E402
import sources.x.images as images      # noqa: E402
import rules as scoring                # noqa: E402
import ai as llm                       # noqa: E402
import sources.x.x_client as xc        # noqa: E402
import sources.x.scan as scan          # noqa: E402
from sources.x import ADAPTER          # noqa: E402
import sources as registry             # noqa: E402

check("统一 X adapter", ADAPTER.name == "x" and callable(ADAPTER.collect))

print("[1b] 源注册表：自动发现 + 遍历")
check("注册表发现了 x 源", [s.name for s in registry.all_sources()] == ["x"],
      [getattr(s, "name", "?") for s in registry.all_sources()])
check("enabled_sources() 默认返回全部源",
      [s.name for s in registry.enabled_sources()] == ["x"])
check("enabled_sources(['x']) 能按名字挑",
      [s.name for s in registry.enabled_sources(["x"])] == ["x"])
check("未知源名不炸、只是跳过", registry.enabled_sources(["nope"]) == [])
check("每个源都有 name/platform/collect", all(
    isinstance(s.name, str) and isinstance(s.platform, str) and callable(s.collect)
    for s in registry.all_sources()))
check("collect_all 可调用", callable(registry.collect_all))
check("CollectResult 契约可用",
      registry.CollectResult(source="x").items == []
      and registry.CollectResult(source="x").error == "")
check("注册表跳过下划线开头的模板", "_template" not in [s.name for s in registry.all_sources()])
import importlib.util as _ilu             # noqa: E402
check("已删除被同名包遮蔽的 sources/x.py",
      _ilu.find_spec("sources.x") is not None
      and not (SEED / "sources" / "x.py").exists())

check("空 WATCH_ACCOUNT 有默认值", bool(env.WATCH_ACCOUNT))
check("空 COOKIES_PATH 不等于当前目录", str(env.COOKIES_PATH) != ".")
check("paths.ROOT 指到仓库根", paths.ROOT == REPO, f"{paths.ROOT} != {REPO}")
check("paths.SEED_DIR 指到抓取脚本", paths.SEED_DIR == SEED)
check("路径常量齐全", all(str(p).endswith(".json") for p in
                          [paths.PENDING_FILE, paths.SENT_FILE, paths.QUEUE_ARCHIVE_FILE,
                           paths.BASELINE_FILE, paths.CURSOR_FILE]))

print("[2] 入口脚本可导入 + 兼容别名还在")
nf = load_by_path(SEED / "news_fetch.py", "news_fetch")
for attr in ("SEED_DIR", "OUTPUT_DIR", "PROXY_URL", "COOKIES_PATH",
             "load_cookie_header", "get_media_urls", "download_image", "log", "main"):
    check(f"news_fetch.{attr}", hasattr(nf, attr))
entries = [
    (REPO / "process" / "judge.py", "judge"),
    (REPO / "process" / "ai_fallback.py", "ai_fallback"),
    (REPO / "main_flow.py", "main_flow"),
]
for path, name in entries:
    mod = load_by_path(path, name)
    check(f"{path.name}.main", hasattr(mod, "main"))
dl = load_by_path(SEED / "sources" / "x" / "download_images.py", "download_pic")
check("下载图片.main", hasattr(dl, "main"))
check("下载图片.extract_id", dl.extract_id("https://x.com/SpaceX/status/2099237998294454546?s=20")
      == "2099237998294454546")
check("下载图片.extract_id 纯ID", dl.extract_id("2099237998294454546") == "2099237998294454546")

print("[3] 打标打分规则与旧版一致")
item = {"account": "SpaceX", "orig_account": "", "zh": "星舰发射", "raw": "Starship launch",
        "images": ["a.jpg"]}
labels = scoring.mechanical_labels(item)
check("机械标签=来源+带图", labels == ["来源:SpaceX", "带图"], labels)
rules = {"weights": {"发射": 50, "带图视频": 20}, "keywords": {}}
score, reason = scoring.score_with_rules(labels + ["领域:发射"], item, rules, {})
check("打分=50+20=70", score == 70, f"{score} {reason}")
check("decide 高阈值", scoring.decide(70, 15, 0) == "发")
check("decide 低阈值", scoring.decide(0, 15, 0) == "丢")
check("decide 中间=AI兜底", scoring.decide(8, 15, 0) == "AI兜底")
check("白名单空=不限", scoring.in_allowlist(item, labels, {}) is True)
check("白名单挡非航天", scoring.in_allowlist(item, ["领域:动漫"], {"可发送领域": ["发射"]}) is False)
check("排除关键词命中", scoring.hit_exclude(item, labels, {"exclude": {"排除关键词": ["starship"]}}) is True)
check("排除关键词不命中", scoring.hit_exclude(item, labels, {"exclude": {"排除关键词": ["nasa"]}}) is False)

print("[4] 新条目构造（_build_new_item）")


class _Photo:
    def __init__(self, url):
        self.url = url


class _Media:
    def __init__(self, photos=None, videos=None):
        self.photos = photos or []
        self.videos = videos or []


class _Tweet:
    def __init__(self, tid=1, text="hello world", date=None, media=None, rt=None, qt=None,
                 author=None):
        self.id = tid
        self.rawContent = text
        self.date = date
        self.media = media
        self.retweetedTweet = rt
        self.quotedTweet = qt
        self.author = author


scan.download_image = lambda tid, url, i=0, log=print, day="": f"/tmp/{tid}_{i}.jpg"
ok_img = _Photo("https://pbs.twimg.com/media/AAA.jpg?name=small")
avatar = _Photo("https://pbs.twimg.com/profile_images/BBB.jpg")
t = _Tweet(tid=99, text="Starship launch", date=None,
           media=_Media(photos=[ok_img, avatar]))
out = scan._build_new_item("SpaceX", t, 1234, {}, print)
check("普通原始图文条目", out and out["account"] == "SpaceX" and out["raw"] == "Starship launch" and out["zh"] == "", out)
check("只收真图(pbs/media)", out and out["images"] == ["/tmp/99_0.jpg"], out and out.get("images"))
check("内容类型=图片", out and out["content_type"] == "图片", out and out.get("content_type"))
video_t = _Tweet(tid=100, text="video", media=_Media(videos=[1]))
check("视频推文跳过", scan._build_new_item("SpaceX", video_t, 1234, {}, print) is None)
rt_missing = _Tweet(tid=101, text="RT @NASA: something")
check("原帖缺失的转推跳过", scan._build_new_item("x", rt_missing, 1234, {}, print) is None)
empty_t = _Tweet(tid=102, text="")
check("无文本跳过", scan._build_new_item("x", empty_t, 1234, {}, print) is None)

print("[4b] 引用（quote）：被引用内容随条目入库")
quote_logs = []
_Author = type("_Author", (), {"username": "NASA"})
qpic = _Photo("https://pbs.twimg.com/media/QQQ.jpg?name=small")
quoted_t = _Tweet(tid=500, text="被引用的正文内容", media=_Media(photos=[qpic]), author=_Author())
quote_t = _Tweet(tid=103, text="我的一句话感想", media=_Media(), qt=quoted_t, author=_Author())
qout = scan._build_new_item("elonmusk", quote_t, 1234, {}, quote_logs.append)
check("引用：被引用作者入库", bool(qout) and qout["quoted_account"] == "NASA",
      qout and qout.get("quoted_account"))
check("引用：被引用正文入库", bool(qout) and qout["quoted_raw"] == "被引用的正文内容")
check("引用：被引用链接入库", bool(qout) and qout["quoted_url"] == "https://x.com/NASA/status/500",
      qout and qout.get("quoted_url"))
check("引用：被引用图片一并下载并合并进 images",
      bool(qout) and qout["images"] == ["/tmp/103_0.jpg"], qout and qout.get("images"))
check("引用：quoted_zh 交给翻译阶段，初始为空", bool(qout) and qout["quoted_zh"] == "")
check("引用：内容类型仍是引用", bool(qout) and qout["content_type"] == "引用")
check("引用：采集日志有 [引用] 行",
      any("[引用]" in line for line in quote_logs), quote_logs)
check("引用：日志里被引用正文不截断",
      any("被引用的正文内容" in line for line in quote_logs))
check("无引用时 quoted_* 全是空",
      bool(out) and out["quoted_account"] == "" and out["quoted_raw"] == ""
      and out["quoted_url"] == "")

print("[5] JSON 读写工具")
tmp = SANDBOX / "_t.json"
jsonio.save_json(tmp, {"a": 1})
check("save/load json", jsonio.load_json(tmp, {}) == {"a": 1})
check("读不存在的文件给默认值", jsonio.load_json(SANDBOX / "nope.json", "d") == "d")
check("deep_merge 递归", jsonio.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}})
tmp.unlink(missing_ok=True)

print("[6] 翻译+四盏灯+标签：一次调用的输出解析与门控")
plain = llm.parse_analysis('{"zh":"星舰第 11 次试飞成功","is_news":true,"not_repeat":true,"repeat_of":"",'
                           '"not_comment":true,"not_sensitive":true,"title":"星舰第 11 次试飞成功",'
                           '"summary":"SpaceX 完成第 11 次试飞，飞船海上溅落。","importance":"高",'
                           '"tags":["领域:发射","对象:星舰"]}')
check("纯 JSON 解析", plain["zh"] == "星舰第 11 次试飞成功" and plain["is_news"] is True
      and plain["tags"] == ["领域:发射", "对象:星舰"], plain)
check("四盏灯齐了", (plain["is_news"], plain["not_repeat"], plain["not_comment"],
                 plain["not_sensitive"]) == (True, True, True, True), plain)
check("新闻三件套：标题", plain["title"] == "星舰第 11 次试飞成功", plain)
check("新闻三件套：要点", plain["summary"].startswith("SpaceX"), plain)
check("新闻三件套：重要度", plain["importance"] == "高", plain)
fenced = ('好的：\n```json\n{"zh":"译文","is_news":"是","not_repeat":"否","repeat_of":"#56",'
          '"not_comment":"是","not_sensitive":false,"tags":"领域:故障, 对象:猎鹰9"}\n```')
parsed = llm.parse_analysis(fenced)
check("容忍代码块与前后废话", parsed["zh"] == "译文", parsed)
check("is_news 中文归一", parsed["is_news"] is True, parsed)
check("not_repeat 中文归一", parsed["not_repeat"] is False, parsed)
check("repeat_of 保留编号", parsed["repeat_of"] == "#56", parsed)
check("not_sensitive 布尔解析", parsed["not_sensitive"] is False, parsed)
check("价值分解析", llm.parse_analysis('{"zh":"x","score":82}')["score"] == 82)
check("价值分：带「分」字/小数/越界都归一", (
    llm.parse_analysis('{"zh":"x","score":"77分"}')["score"] == 77
    and llm.parse_analysis('{"zh":"x","score":81.6}')["score"] == 82
    and llm.parse_analysis('{"zh":"x","score":180}')["score"] == 100
    and llm.parse_analysis('{"zh":"x","score":-5}')["score"] == 0
), "")
check("价值分判不出 -> None", llm.parse_analysis('{"zh":"x","score":"很好"}')["score"] is None)
check("标签字符串归一", parsed["tags"] == ["领域:故障", "对象:猎鹰9"], parsed)
check("敏感政治中文键也认", llm.parse_analysis('{"zh":"x","不敏感":false}')["not_sensitive"] is False)
check("非新闻时三件套为空", llm.parse_analysis('{"zh":"x","is_news":false}')["importance"] == "")
check("重要度英文归一", llm.parse_analysis('{"zh":"x","is_news":true,"importance":"High"}')["importance"] == "高")
parsed = llm.parse_analysis('{"zh":"x","is_news":"看不懂","tags":["a","a","b"]}')
check("is_news 判不出 -> None", parsed["is_news"] is None, parsed)
check("标签去重", parsed["tags"] == ["a", "b"], parsed)
check("缺译文要抛（不能写空译文进库）", raises(lambda: llm.parse_analysis('{"is_news":true}')), "")
check("非 JSON 要抛", raises(lambda: llm.parse_analysis("这条是新闻")), "")
check("空输出要抛", raises(lambda: llm.parse_analysis("")), "")
check("提示词带来源账号", "@SpaceX" in llm.build_analysis_prompt("Starship", "SpaceX"))
check("提示词无账号时不残留占位符", "{source}" not in llm.build_analysis_prompt("Starship"))
check("提示词带已入库清单", "#56 星舰试飞" in llm.build_analysis_prompt("x", "a", ["#56 星舰试飞"]))
check("清单为空时不留占位符", "{known}" not in llm.build_analysis_prompt("x", "a"))
check("只补标提示词带译文", "译文：\n已有译文" in llm.build_meta_prompt("raw text", "已有译文"))
check("只补标提示词无残留占位符", "{raw}" not in llm.build_meta_prompt("raw text", "已有译文"))

print("[6b] 补标解析（存量条目：只点灯不翻译）")
meta = llm.parse_meta('{"is_news":false,"not_repeat":true,"not_comment":true,'
                      '"title":"","summary":"","importance":"","tags":["领域:其他"]}')
check("补标：非新闻", meta["is_news"] is False and meta["tags"] == ["领域:其他"], meta)
meta = llm.parse_meta('{"is_news":true,"not_repeat":false,"repeat_of":"#12","not_comment":true,'
                      '"not_sensitive":false,"title":"长征十二号首飞","summary":"成功入轨。",'
                      '"importance":"高","tags":[]}')
check("补标：四盏灯 + 三件套", meta["is_news"] is True and meta["not_repeat"] is False
      and meta["repeat_of"] == "#12" and meta["title"] == "长征十二号首飞"
      and meta["importance"] == "高" and meta["not_sensitive"] is False, meta)
check("补标：四盏灯和标签都空要抛", raises(lambda: llm.parse_meta('{"title":"x"}')), "")

print("[6c] 四盏灯门控与打分")
check("四绿", scoring.light_state({"is_news": 1, "not_repeat": 1, "not_comment": 1,
                                "not_sensitive": 1}) == ("green", ""))
state, detail = scoring.light_state({"is_news": 1, "not_repeat": 0, "not_comment": 0, "not_sensitive": 1})
check("有红灯并说明哪盏", state == "red" and "重复" in detail and "评价" in detail, (state, detail))
state, detail = scoring.light_state({"is_news": 1, "not_repeat": 1, "not_comment": 1, "not_sensitive": 0})
check("敏感政治是红灯", state == "red" and "敏感政治" in detail, (state, detail))
check("未点灯", scoring.light_state({}) == ("unknown", ""))
check("只有一盏绿 -> 未点齐", scoring.light_state({"is_news": 1})[0] == "unknown")
check("非新闻门控（单灯函数仍在）", scoring.is_non_news({"is_news": 0}) is True)
check("ai_tags 读 JSON 串", scoring.ai_tags({"ai_tags": '["领域:发射","对象:星舰"]'}) == ["领域:发射", "对象:星舰"])
check("ai_tags 读 list", scoring.ai_tags({"ai_tags": ["领域:发射"]}) == ["领域:发射"])
check("ai_tags 坏数据返回空", scoring.ai_tags({"ai_tags": "not-json"}) == [])
check("ai_tags 缺失返回空", scoring.ai_tags({}) == [])
check("重要度标签", scoring.importance_label({"importance": "高"}) == "重要:高")
check("重要度未知 -> 空", scoring.importance_label({}) == "")
check("AI分下限=0（默认）不拦", scoring.score_floor_ok(10, 0) is True and scoring.score_floor_ok(10, None) is True)
check("AI分下限：低于下限拦", scoring.score_floor_ok(39, 40) is False)
check("AI分下限：等于/高于下限放行", scoring.score_floor_ok(40, 40) is True and scoring.score_floor_ok(88, 40) is True)
check("AI分下限：模型没给分不拦", scoring.score_floor_ok(None, 40) is True)
imp_item = {"account": "SpaceX", "zh": "星舰发射", "raw": "Starship launch", "importance": "高"}
score0, _ = scoring.score_with_rules(["领域:发射"], imp_item, {"weights": {"发射": 50}}, {})
check("重要度不配权重时不影响打分", score0 == 50, score0)
score1, reason1 = scoring.score_with_rules(["领域:发射"], imp_item,
                                           {"weights": {"发射": 50, "重要高": 20}}, {})
check("配了 重要高 权重才加分", score1 == 70 and "重要高" in reason1, f"{score1} {reason1}")

print("[7] 数据库迁移：旧库要能补出 AI 判定/标签/重要度 相关列")
import collect.db as cdb  # noqa: E402

NEW_COLUMNS = {"ai_tags", "is_news", "not_repeat", "not_comment", "not_sensitive", "repeat_of",
               "ai_title", "ai_summary", "importance", "meta_attempts", "meta_error", "rev",
               # 引用（quote）：被引用那条也随条目存下来
               "quoted_account", "quoted_raw", "quoted_zh", "quoted_url"}
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, raw TEXT, zh TEXT DEFAULT '')")
cdb.ensure_schema(conn)
cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
check("旧表补出新列", NEW_COLUMNS <= cols, sorted(NEW_COLUMNS - cols))
check("原有数据不丢", conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0)
# 精简掉的列不能再被加回来（2026-09 那次瘦身）
REMOVED_COLUMNS = {"tweet_id", "score", "labels", "final_decision",
                   "published_ts", "published_date", "title", "url", "has_media"}
check("删掉的列不会被重新建出来", not (cols & REMOVED_COLUMNS), sorted(cols & REMOVED_COLUMNS))
# 老库（还带着这些列）跑 ensure_schema：不能把已删的空列又建出来，也不该动已存在的旧列
# （清掉旧列是 tools/drop_columns.py 的活，ensure_schema 只做"加列"）
legacy = sqlite3.connect(":memory:")
legacy.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, raw TEXT, zh TEXT DEFAULT '',"
               " tweet_id TEXT, score INTEGER, labels TEXT, final_decision TEXT)")
cdb.ensure_schema(legacy)
legacy_cols = {row[1] for row in legacy.execute("PRAGMA table_info(items)")}
check("ensure_schema 不会把删掉的空列建出来",
      not (legacy_cols & {"published_ts", "published_date", "title", "url", "has_media"}),
      sorted(legacy_cols & {"published_ts", "published_date", "title", "url", "has_media"}))
check("老库已有的列原样保留（等 drop_columns 处理）",
      {"tweet_id", "score", "labels", "final_decision"} <= legacy_cols)
check("唯一键还在（防重复入库）", "UNIQUE" in cdb.SCHEMA.upper() and "dedup_key" in cdb.SCHEMA)
legacy.close()
conn.close()

print("[8] 翻译队列判定：哪些要翻、哪些绝不重翻")
import translate_db as tdb  # noqa: E402

case = SANDBOX / "translate_case"
case.mkdir(parents=True, exist_ok=True)
tdb_conn = sqlite3.connect(":memory:")
tdb_conn.row_factory = sqlite3.Row      # pending_rows 要 dict(row)
cdb.ensure_schema(tdb_conn)
# (id, raw, zh, translate_attempts, meta_attempts, 四盏灯是否齐, 是否有 AI 分)
ROWS = [
    (1, "r1", "", 0, 0, False, False),            # 缺译文 → 完整重做
    (2, "r2", "人工改过的译文", 0, 0, False, False),  # 有译文（下面还会给它塞个旧错误）→ 绝不重翻
    (3, "r3", "", 3, 0, False, False),            # 失败已达上限 → 跳过
    (4, "r4", "[未配置 key]", 1, 0, False, False),  # 旧占位符 → 完整重做
    (5, "r5", "已有译文", 0, 0, True, True),        # 译文 + 四灯 + 分数都齐 → 两遍都不碰
    (6, "r6", "已有译文", 0, 0, False, True),       # 有译文但缺灯 → 补标
    (7, "r7", "已有译文", 0, 0, True, False),       # 灯齐但缺 AI 分 → 也要补
]
for item_id, raw, zh, attempts, meta_attempts, lit, scored in ROWS:
    tdb_conn.execute(
        "INSERT INTO items(id, raw, zh, translate_attempts, meta_attempts, stage, collected_at) "
        "VALUES(?,?,?,?,?,'translated','t')",
        (item_id, raw, zh, attempts, meta_attempts),
    )
    if lit:
        tdb_conn.execute(
            "UPDATE items SET is_news=1, not_repeat=1, not_comment=1, not_sensitive=1,"
            " ai_tags='[\"领域:发射\"]' WHERE id=?", (item_id,))
    else:
        tdb_conn.execute("UPDATE items SET is_news=1, ai_tags='[]' WHERE id=?", (item_id,))
    if scored:
        tdb_conn.execute("UPDATE items SET ai_score=80 WHERE id=?", (item_id,))
tdb_conn.execute("UPDATE items SET translate_error='HTTP 500 boom' WHERE id=2")
tdb_conn.commit()

picked = {r["id"] for r in tdb.pending_rows(tdb_conn, 3)}
check("缺译文才完整重做（zh 空 + 旧占位符）", picked == {1, 4}, sorted(picked))
check("有译文的绝不被重翻（留了旧错误也一样）", 2 not in picked, sorted(picked))
check("达到重试上限的不再翻", 3 not in picked, sorted(picked))
meta = {r["id"] for r in tdb.meta_pending_rows(tdb_conn, 3)}
check("缺灯 / 缺 AI 分 / 缺标签 → 补标", meta == {2, 6, 7}, sorted(meta))
check("没译文的（含达上限的失败条目）不该进补标", not (meta & {1, 3}), sorted(meta))
check("译文+四灯+分数都齐的不补标", 5 not in meta, sorted(meta))


class _Recorder:
    """假连接：只记下发出去的 SQL，用来证明补标不碰 zh。"""

    def __init__(self):
        self.sql = []
        self.params = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(params)
        return self


recorder = _Recorder()
tdb.save_meta_ok(recorder, {"id": 6, "zh": "已有译文"},
                 {"is_news": True, "not_repeat": True, "not_comment": True, "not_sensitive": True,
                  "repeat_of": "", "title": "t", "summary": "s", "importance": "低",
                  "tags": ["领域:发射"]}, 1)
check("补标的 UPDATE 里没有 zh", "zh=?" not in recorder.sql[0], recorder.sql[0][:90])
check("补标会 +1 rev（看板才能实时刷新）", "rev=COALESCE(rev, 0) + 1" in recorder.sql[0], recorder.sql[0][:120])

print("[8b] 引用放行：有引用时第 3 盏灯强制绿灯")
check("has_quote：有链接 → 真", scoring.has_quote({"quoted_url": "https://x.com/a/status/1"}) is True)
check("has_quote：没引用 → 假",
      scoring.has_quote({}) is False and scoring.has_quote({"quoted_raw": "只有正文没链接"}) is False)
check("有引用：AI 判红也变绿灯", tdb._not_comment_flag({"quoted_url": "u"}, False) == 1)
check("有引用：AI 没判也是绿灯", tdb._not_comment_flag({"quoted_url": "u"}, None) == 1)
check("无引用：照常听 AI 的",
      tdb._not_comment_flag({}, False) == 0 and tdb._not_comment_flag({}, True) == 1
      and tdb._not_comment_flag({}, None) is None)
check("放行只影响 not_comment，其它灯不看引用",
      tdb._flag(False) == 0 and tdb._flag(None) is None)

# 落库层面：save_ok 的参数顺序是 zh, quoted_zh, tags, is_news, not_repeat, not_comment, ...
_ANALYSIS = {"zh": "译文", "quoted_zh": "被引用正文的翻译", "is_news": True, "not_repeat": True,
             "not_comment": False, "not_sensitive": True, "repeat_of": "", "title": "t",
             "summary": "s", "importance": "中", "score": 80, "tags": ["领域:发射"]}
q_save = _Recorder()
tdb.save_ok(q_save, {"id": 9, "stage": "collected", "quoted_url": "https://x.com/a/status/1"},
            _ANALYSIS, 1)
check("save_ok 写了 quoted_zh", "quoted_zh=?" in q_save.sql[0])
check("save_ok：引用条目 not_comment 落库为 1（AI 判的是 False）",
      q_save.params[0][5] == 1, f"not_comment={q_save.params[0][5]}")
check("save_ok：quoted_zh 参数正确", q_save.params[0][1] == "被引用正文的翻译")
plain_save = _Recorder()
tdb.save_ok(plain_save, {"id": 10, "stage": "collected"}, _ANALYSIS, 1)
check("save_ok：无引用时 not_comment 照抄 AI（False → 0）",
      plain_save.params[0][5] == 0, f"not_comment={plain_save.params[0][5]}")

print("[8c] 引用内容进提示词 + quoted_zh 解析")
check("无引用时提示词不残留 {quoted}",
      "{quoted}" not in llm.build_analysis_prompt("x", "a"))
q_prompt = llm.build_analysis_prompt("我的一句话感想", "elonmusk", None, "NASA", "被引用的正文内容")
check("提示词带【被引用的推文】与被引用正文",
      "【被引用的推文】" in q_prompt and "@NASA" in q_prompt and "被引用的正文内容" in q_prompt)
check("补标提示词同样带引用",
      "被引用的正文内容" in llm.build_meta_prompt("x", "y", "a", None, "NASA", "被引用的正文内容"))
parsed_q = llm.parse_analysis('{"zh":"译文","quoted_zh":"被引用的正文翻译","is_news":true}')
check("解析 quoted_zh", parsed_q["quoted_zh"] == "被引用的正文翻译", parsed_q)
check("没有 quoted_zh 时给空串", llm.parse_analysis('{"zh":"译文","is_news":true}')["quoted_zh"] == "")

tdb_conn.close()
import shutil as _shutil  # noqa: E402
_shutil.rmtree(case, ignore_errors=True)

print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 抓取脚本 侧自检全部通过")
