# -*- coding: utf-8 -*-
"""重构后自检：抓取脚本 侧的导入 + 关键逻辑冒烟测试。

跑法：python 自检/check_pipeline.py（或统一跑 自检/run_all.py）
（需要 自检/stubs 里的 openai/twscrape 桩模块）
"""
import importlib.util
import json
import sqlite3
import sys
import time
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
import sources.x.link_preview as link_preview  # noqa: E402
import sources.x.images as images      # noqa: E402
import rules as scoring                # noqa: E402
import ai as llm                       # noqa: E402
import sources.x.x_client as xc        # noqa: E402
import sources.x.scan as scan          # noqa: E402
from sources.x import ADAPTER          # noqa: E402
import sources as registry             # noqa: E402

check("统一 X adapter", ADAPTER.name == "x" and callable(ADAPTER.collect))

print("[1b] 源注册表：自动发现 + 遍历")
check("注册表发现 X 与三个网页采集器", [s.name for s in registry.all_sources()] == ["rss", "sina", "snapi", "x"],
      [getattr(s, "name", "?") for s in registry.all_sources()])
check("显式空列表返回全部已注册源",
      [s.name for s in registry.enabled_sources([])] == ["rss", "sina", "snapi", "x"])
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

check("账号 CSV 名单位于 x 源目录", env.X_ACCOUNTS_FILE == SEED / "sources" / "x" / "x_accounts.csv")
import asyncio
account_fixture = SANDBOX / "x_accounts.csv"
account_fixture.write_text("名字,性质\n@SpaceX,official\nNASA,media\nspacex,\n,\n", encoding="utf-8-sig")
check("CSV 名单保序且允许空分类/忽略空行/重复账号",
      xc.load_account_names(account_fixture) == ["SpaceX", "NASA"])
account_fixture.write_text("名字,性质\nSpaceX,official\nSPACEdotcom,media\njohnkrausphotos,photographer\nMatt_Lowne,enthusiast\nEcho5550,others\n", encoding="utf-8")
check("CSV 支持全部五种账号性质", xc.load_account_names(account_fixture) ==
      ["SpaceX", "SPACEdotcom", "johnkrausphotos", "Matt_Lowne", "Echo5550"])
account_fixture.write_text("名字,性质\nhttps://x.com/SpaceX,others\n", encoding="utf-8")
check("名单中误填网址明确报错", raises(lambda: xc.load_account_names(account_fixture)))
account_fixture.write_text("名字,性质\n", encoding="utf-8")
check("空名单明确报错", raises(lambda: xc.load_account_names(account_fixture)))
account_fixture.write_text("名字,性质\nNASA,unknown\n", encoding="utf-8")
check("不支持的性质明确报错", raises(lambda: xc.load_account_names(account_fixture)))
account_fixture.write_text("名字,性质\nNASA\n", encoding="utf-8")
check("CSV 缺少性质列明确报错", raises(lambda: xc.load_account_names(account_fixture)))
account_fixture.write_text("性质,名字\nofficial,NASA\n", encoding="utf-8")
check("CSV 表头顺序错误明确报错", raises(lambda: xc.load_account_names(account_fixture)))
account_fixture.unlink()
check("名单缺失明确报错", raises(lambda: xc.load_account_names(account_fixture)))
class _NoAccountNetwork:
    def __getattr__(self, name):
        raise AssertionError("读取账号名单不得调用 X 接口：" + name)
original_accounts_file = xc.X_ACCOUNTS_FILE
account_fixture.write_text("名字,性质\nSpaceX,\nNASA,official\n", encoding="utf-8")
xc.X_ACCOUNTS_FILE = account_fixture
try:
    check("账号列表完全来自文件，不查询关注关系",
          asyncio.run(xc.gather_accounts(_NoAccountNetwork(), lambda line: None)) == ["SpaceX", "NASA"])
    account_fixture.write_text("名字,性质\nESA,official\n", encoding="utf-8")
    check("修改名单下一轮自动读取",
          asyncio.run(xc.gather_accounts(_NoAccountNetwork(), lambda line: None)) == ["ESA"])
finally:
    xc.X_ACCOUNTS_FILE = original_accounts_file
    account_fixture.unlink(missing_ok=True)
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

print("[4c] t.co 外链正文附加到原文（离线样例）")
sample_html = ("<html><head><meta property='og:description' content='备用摘要'></head>"
               "<body><nav><p>导航噪声</p></nav><article><p>文章正文第一段。</p>"
               "<p>文章正文第二段。</p><script>不应抓取</script></article></body></html>")
original_fetch = link_preview.fetch_article_data
link_preview.fetch_article_data = lambda url: {
    "text": "文章正文第一段。\n\n文章正文第二段。",
    "images": ["https://example.com/large.jpg"],
    "status": 200, "final_url": "https://example.com/article",
}
original_download = link_preview._download_article_image
link_preview._download_article_image = lambda url, tweet_id, index, day, log: f"/data/images/{tweet_id}_{index}.jpg"
preview_logs = []
preview_images = []
preview_raw = link_preview.append_tco_references(
    "主帖文本 https://t.co/AbC123 和重复链接 https://t.co/AbC123",
    preview_logs.append, tweet_id=777, images=preview_images)
link_preview.fetch_article_data = original_fetch
link_preview._download_article_image = original_download
check("t.co 引用格式保留短链并追加正文",
      "引用1：（链接：https://t.co/AbC123）【文章正文第一段。\n\n文章正文第二段。】" in preview_raw,
      preview_raw)
check("重复 t.co 只抓取一次",
      sum("已获取引用正文" in line for line in preview_logs) == 1, preview_logs)
check("网页大图保存并加入条目附件",
      preview_images == ["/data/images/777_0.jpg"], preview_images)
parser = link_preview._ArticleParser()
parser.feed(sample_html)
check("通用 HTML 提取 article 段落并跳过导航/脚本",
      parser.get_text() == "文章正文第一段。\n\n文章正文第二段。", parser.get_text())
check("支持 t.co 客户端 meta refresh 跳转",
      link_preview._extract_client_redirect(
          '<meta http-equiv="refresh" content="0;URL=https://example.com/news">',
          "https://t.co/example") == "https://example.com/news")
check("支持 JavaScript location.replace 跳转",
      link_preview._extract_client_redirect(
          'location.replace("https:\\/\\/example.com\\/news")',
          "https://t.co/example") == "https://example.com/news")
image_parser = link_preview._ArticleParser()
image_parser.feed('<meta property="og:image" content="/large.jpg">'
                  '<img src="/small.jpg" srcset="/small.jpg 300w, /large.jpg 1200w">')
check("从社交分享图与 srcset 选择大图",
      image_parser.get_image_urls("https://example.com/post")[0] == "https://example.com/large.jpg",
      image_parser.get_image_urls("https://example.com/post"))

print("[5] JSON 读写工具")
tmp = SANDBOX / "_t.json"
jsonio.save_json(tmp, {"a": 1})
check("save/load json", jsonio.load_json(tmp, {}) == {"a": 1})
check("读不存在的文件给默认值", jsonio.load_json(SANDBOX / "nope.json", "d") == "d")
check("deep_merge 递归", jsonio.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}})
tmp.unlink(missing_ok=True)

print("[6] 翻译+四盏灯+标签：一次调用的输出解析与门控")
def _valid_analysis(**changes):
    data = {"zh": "x", "title": "测试标题", "is_news": True, "not_repeat": True, "not_comment": True,
            "not_sensitive": True, "score": 60, "tags": ["领域:任务"],
            "summary": "测试要点", "importance": "中"}
    if changes.get("is_news") is False:
        data["summary"] = ""
        data["importance"] = ""
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)

plain = llm.parse_analysis('{"zh":"星舰第 11 次试飞成功","is_news":true,"not_repeat":true,"repeat_of":"",'
                           '"not_comment":true,"not_sensitive":true,"title":"星舰第 11 次试飞成功",'
                           '"summary":"SpaceX 完成第 11 次试飞，飞船海上溅落。","importance":"高","score":92,'
                           '"tags":["领域:发射","对象:星舰"]}')
check("纯 JSON 解析", plain["zh"] == "星舰第 11 次试飞成功" and plain["is_news"] is True
      and plain["tags"] == ["领域:发射", "对象:星舰"], plain)
check("四盏灯齐了", (plain["is_news"], plain["not_repeat"], plain["not_comment"],
                 plain["not_sensitive"]) == (True, True, True, True), plain)
check("repeat_of 与不重复冲突时以重复编号为准",
      llm.parse_analysis(_valid_analysis(repeat_of="#56"))["not_repeat"] is False)
check("新闻三件套：标题", plain["title"] == "星舰第11次试飞成功", plain)
check("新闻三件套：要点", plain["summary"].startswith("SpaceX"), plain)
check("新闻三件套：重要度", plain["importance"] == "高", plain)
fenced = ('好的：\n```json\n{"zh":"译文","is_news":"是","not_repeat":"否","repeat_of":"#56",'
          '"not_comment":"是","not_sensitive":false,"title":"猎鹰9故障",'
          '"summary":"测试要点","importance":"高","score":67,'
          '"tags":"领域:故障, 对象:猎鹰9"}\n```')
parsed = llm.parse_analysis(fenced)
check("容忍代码块与前后废话", parsed["zh"] == "译文", parsed)
check("is_news 中文归一", parsed["is_news"] is True, parsed)
check("not_repeat 中文归一", parsed["not_repeat"] is False, parsed)
check("repeat_of 保留编号", parsed["repeat_of"] == "#56", parsed)
check("not_sensitive 布尔解析", parsed["not_sensitive"] is False, parsed)
check("价值分解析", llm.parse_analysis(_valid_analysis(score=82))["score"] == 82)
check("价值分：带「分」字/小数/越界都归一", (
    llm.parse_analysis(_valid_analysis(score="77分"))["score"] == 77
    and llm.parse_analysis(_valid_analysis(score=81.6))["score"] == 82
    and llm.parse_analysis(_valid_analysis(score=180))["score"] == 100
    and llm.parse_analysis(_valid_analysis(score=-5))["score"] == 0
), "")
check("价值分判不出 -> 完整分析失败重试",
      raises(lambda: llm.parse_analysis(_valid_analysis(score="很好"))), "")
check("标签字符串归一", parsed["tags"] == ["领域:故障", "对象:猎鹰9"], parsed)
check("敏感政治中文键也认", llm.parse_analysis(
      '{"zh":"x","is_news":true,"not_repeat":true,"not_comment":true,"不敏感":false,"title":"测试标题","summary":"测试要点","importance":"中","score":60,"tags":["领域:任务"]}')["not_sensitive"] is False)
check("非新闻时三件套为空", llm.parse_analysis(_valid_analysis(is_news=False))["importance"] == "")
check("重要度英文归一", llm.parse_analysis(_valid_analysis(importance="High"))["importance"] == "高")
parsed = llm.parse_analysis(_valid_analysis(tags=["a", "a", "b"]))
check("标签去重", parsed["tags"] == ["a", "b"], parsed)
check("缺任意灯的完整分析必须失败", raises(lambda: llm.parse_analysis('{"zh":"x","is_news":true}')), "")
check("完整分析缺 AI 标题必须失败", raises(lambda: llm.parse_analysis(
      '{"zh":"x","is_news":true,"not_repeat":true,"not_comment":true,'
      '"not_sensitive":true,"score":60,"tags":["领域:任务"]}')), "")
check("缺译文要抛（不能写空译文进库）", raises(lambda: llm.parse_analysis('{"is_news":true}')), "")
check("非 JSON 要抛", raises(lambda: llm.parse_analysis("这条是新闻")), "")
malformed_keys = ('{"zh":"译文","quoted_zh":"",is_news":false,"not_repeat":true,'
                  '"repeat_of":"","not_comment":true,"not_sensitive":true,'
                  '"title":"测试标题","summary":"","importance":"低",'
                  '"score":10,"tags":["领域:其他"]}')
check("修复模型漏引号的 JSON 字段名", llm.parse_analysis(malformed_keys)["is_news"] is False,
      llm.parse_analysis(malformed_keys))
check("空输出要抛", raises(lambda: llm.parse_analysis("")), "")
check("提示词带来源账号", "@SpaceX" in llm.build_analysis_prompt("Starship", "SpaceX"))
check("提示词无账号时不残留占位符", "{source}" not in llm.build_analysis_prompt("Starship"))
check("提示词带已入库清单", "#56 星舰试飞" in llm.build_analysis_prompt("x", "a", ["#56 星舰试飞"]))
check("清单为空时不留占位符", "{known}" not in llm.build_analysis_prompt("x", "a"))
check("完整分析格式不再包含具体新闻示例",
      '"title":"星舰第 11 次试飞成功"' not in llm.ANALYSIS_PROMPT
      and '"summary":"SpaceX 完成星舰' not in llm.ANALYSIS_PROMPT)
check("ISS 摄影原文不能变成星舰试飞",
      raises(lambda: llm.validate_analysis_grounding(plain, "I captured the ISS!")))
check("感想不能借用历史标题补造星舰主体",
      raises(lambda: llm.validate_analysis_grounding(plain, "One of the most incredible launches I have ever seen.")))
check("星舰原文没有编号时不能补成第11次试飞",
      raises(lambda: llm.validate_analysis_grounding(plain, "Starship launched successfully.")))
check("原文明确给出的星舰第11次试飞仍可正常处理",
      not raises(lambda: llm.validate_analysis_grounding(plain, "Starship Flight 11 completed its flight.")))
check("引用提供的任务信息也作为校验依据",
      not raises(lambda: llm.validate_analysis_grounding(plain, "An incredible launch!", "Starship's eleventh test flight succeeded.")))
import tempfile
from collect import db as repair_db_module
from tools.repair_analysis_pollution import repair as repair_pollution
with tempfile.TemporaryDirectory() as directory:
    repair_root = Path(directory)
    repair_db = repair_root / "hx.sqlite3"
    with sqlite3.connect(repair_db) as repair_conn:
        repair_db_module.ensure_schema(repair_conn)
        repair_conn.execute("CREATE TABLE push_attempts(item_id INTEGER,status TEXT)")
        repair_conn.execute("INSERT INTO items(id,raw,zh,ai_title,stage,decision,pushed) VALUES(1,?,?,?,?,?,?)",
                            ("I captured the ISS!", plain["zh"], plain["title"], "processed", "发", '{"alison:test":true}'))
        repair_conn.execute("INSERT INTO items(id,raw,zh,ai_title,stage,decision) VALUES(2,?,?,?,?,?)",
                            ("Starship Flight 11 completed its flight.", plain["zh"], plain["title"], "processed", "发"))
        repair_conn.execute("INSERT INTO pushes(item_id,source) VALUES(1,'alison:test')")
        repair_conn.execute("INSERT INTO push_attempts VALUES(1,'success')")
        db_id = 1
        repair_db_module.enqueue_processing_event(repair_conn, db_id)
    repair_conn.close()
    preview = repair_pollution(repair_db, log=lambda text: None)
    result = repair_pollution(repair_db, apply=True, backup_root=repair_root / "backups", log=lambda text: None)
    with sqlite3.connect(repair_db) as repair_conn:
        repaired = repair_conn.execute("SELECT raw,zh,stage,decision,pushed FROM items WHERE id=1").fetchone()
        check("错误条目直接删除并移出发送池", repaired is None)
        check("删除错误条目保留历史发送审计", repair_conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0] == 1
              and repair_conn.execute("SELECT COUNT(*) FROM push_attempts").fetchone()[0] == 1)
        check("删除错误条目清理待处理事件且不改其他新闻", repair_conn.execute("SELECT item_id FROM processing_events").fetchall() == []
              and repair_conn.execute("SELECT stage FROM items WHERE id=2").fetchone()[0] == "processed")
    repair_conn.close()
    check("污染修复先备份且重复执行幂等", preview["item_ids"] == [1]
          and (Path(result["backup"]) / "hx.sqlite3").is_file()
          and repair_pollution(repair_db, apply=True, log=lambda text: None)["affected"] == 0)
check("标题提示词要求每条都有标题且优先复用原题",
      '每条内容都必须有非空的 "title"' in llm.build_analysis_prompt("Starship")
      and "优先复用" in llm.build_analysis_prompt("Starship")
      and "依据正文重写" in llm.build_analysis_prompt("Starship"))
sample_noisy = ("▶️ 直播观看：https://t.co/tnbEHvEX35 → https://t.co/tnMukzRZEK "
                "https://t.co/HKjN3dHh1y 10/13/26 https://t.co/vJxWKEidCF "
                "location: https://t.co/KIadNi37so Information as of 2026-10-11 16:19 UTC")
cleaned_sample = llm.clean_zh_body(sample_noisy)
check("清理直播短链和状态尾巴", not any(x in cleaned_sample.lower() for x in
      ("http", "直播观看", "information as of", "location:", "10/13/26")), cleaned_sample)
with_junk = llm.parse_analysis(_valid_analysis(zh="星舰完成试飞。 ▶️ 直播观看：https://t.co/abc Information as of 2026-10-11 16:19 UTC", is_news=False))
check("清理正文时保留主要事实", with_junk["zh"] == "星舰完成试飞。", with_junk["zh"])
check("缺原题也生成不超过20字的标题",
      bool(with_junk["title"]) and len(with_junk["title"]) <= 20, with_junk["title"])
long_title = llm.parse_analysis(_valid_analysis(zh="正文。", title="这是一个明显超过二十个汉字的特别特别长标题"))
check("标题限制为20字", len(long_title["title"]) <= 20, long_title["title"])

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
               "ai_title", "ai_summary", "importance", "rev",
               # 引用（quote）：被引用那条也随条目存下来
               "quoted_account", "quoted_raw", "quoted_zh", "quoted_url"}
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, raw TEXT, zh TEXT DEFAULT '')")
cdb.ensure_schema(conn)
cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
check("旧表补出新列", NEW_COLUMNS <= cols, sorted(NEW_COLUMNS - cols))
check("新库建立处理增量订阅表",
      conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='processing_events'").fetchone()
      == ("processing_events",))
check("原有数据不丢", conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0)
check("新数据库不再创建补标重试列",
      not ({"meta_attempts", "meta_error"} & cols), sorted({"meta_attempts", "meta_error"} & cols))
# 精简掉的列不能再被加回来（2026-09 那次瘦身）
REMOVED_COLUMNS = {"tweet_id", "score", "labels", "final_decision",
                    "published_ts", "published_date", "title", "url", "has_media",
                    "meta_attempts", "meta_error"}
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

event_case = SANDBOX / "processing_subscription"
event_case.mkdir(parents=True, exist_ok=True)
event_db = event_case / "hx.sqlite3"
event_db.unlink(missing_ok=True)
original_collect_db_file = cdb.DB_FILE
cdb.DB_FILE = event_db
try:
    source_item = {"platform": "probe", "origin_id": "subscription-1", "account": "Probe",
                   "raw": "第一版原文", "quoted_account": "", "quoted_raw": "", "images": []}
    subscribed_id = cdb.upsert_raw(source_item)
    event_conn = sqlite3.connect(event_db)
    first_event = event_conn.execute(
        "SELECT generation FROM processing_events WHERE item_id=?", (subscribed_id,)).fetchone()
    event_conn.execute("UPDATE items SET stage='processed', zh='已处理的译文' WHERE id=?", (subscribed_id,))
    event_conn.commit()
    event_conn.close()
    check("新采集行发布首个处理事件", first_event == (1,), first_event)

    cdb.upsert_raw(source_item)
    event_conn = sqlite3.connect(event_db)
    unchanged_event = event_conn.execute(
        "SELECT generation FROM processing_events WHERE item_id=?", (subscribed_id,)).fetchone()
    unchanged_row = event_conn.execute(
        "SELECT stage,zh FROM items WHERE id=?", (subscribed_id,)).fetchone()
    event_conn.close()
    check("重复采集相同正文不重复发布事件", unchanged_event == (1,), unchanged_event)
    check("重复采集相同正文保留处理结果", unchanged_row == ("processed", "已处理的译文"), unchanged_row)

    changed_item = dict(source_item, raw="更新后的原文")
    cdb.upsert_raw(changed_item)
    event_conn = sqlite3.connect(event_db)
    changed_event = event_conn.execute(
        "SELECT generation FROM processing_events WHERE item_id=?", (subscribed_id,)).fetchone()
    changed_row = event_conn.execute(
        "SELECT stage,zh,decision,translate_attempts FROM items WHERE id=?", (subscribed_id,)).fetchone()
    event_conn.close()
    check("正文变化发布新一代处理事件", changed_event == (2,), changed_event)
    check("正文变化清空旧分析并回到 collected",
          changed_row == ("collected", "", None, 0), changed_row)
finally:
    cdb.DB_FILE = original_collect_db_file

print("[8] 处理池全库扫描：缺字段时统一完整分析")
import translate_db as tdb  # noqa: E402
from process.analysis_contract import missing_analysis_fields, processing_required  # noqa: E402

case = SANDBOX / "translate_case"
case.mkdir(parents=True, exist_ok=True)
tdb_conn = sqlite3.connect(":memory:")
tdb_conn.row_factory = sqlite3.Row      # pending_rows 要 dict(row)
cdb.ensure_schema(tdb_conn)
# (id, raw, zh, translate_attempts, 四盏灯是否齐, 是否有 AI 分)
ROWS = [
    (1, "r1", "", 0, False, False),            # 缺译文 → 完整重做
    (2, "r2", "人工改过的译文", 0, True, True),   # 完整 → 保留人工译文
    (3, "r3", "", 3, False, False),            # 失败已达上限 → 跳过
    (4, "r4", "[未配置 key]", 1, False, False),  # 旧占位符 → 完整重做
    (5, "r5", "已有译文", 0, True, True),        # 完整 → 不重跑
    (6, "r6", "已有译文", 0, False, True),       # 缺灯 → 完整重做
    (7, "r7", "已有译文", 0, True, False),       # 缺 AI 分 → 完整重做
    (8, "r8", "已有译文", 0, True, True),        # 缺标题 → 完整重做
]
for item_id, raw, zh, attempts, lit, scored in ROWS:
    tdb_conn.execute(
        "INSERT INTO items(id, raw, zh, translate_attempts, stage, collected_at) "
        "VALUES(?,?,?,?,'translated','t')",
        (item_id, raw, zh, attempts),
    )
    if lit:
        tdb_conn.execute(
            "UPDATE items SET is_news=1, not_repeat=1, not_comment=1, not_sensitive=1,"
            " ai_tags='[\"领域:发射\"]', ai_title='已有标题',"
            " ai_summary='要点', importance='高' WHERE id=?", (item_id,))
    else:
        tdb_conn.execute("UPDATE items SET is_news=1, ai_tags='[]' WHERE id=?", (item_id,))
    if scored:
        tdb_conn.execute("UPDATE items SET ai_score=80 WHERE id=?", (item_id,))
# 保留已有标题；随后清空第 8 条，验证缺标题也会完整重做。
tdb_conn.execute("UPDATE items SET ai_title='已有标题' WHERE id IN (5, 8)")
tdb_conn.execute("UPDATE items SET ai_title='' WHERE id=8")
tdb_conn.execute("UPDATE items SET translate_error='HTTP 500 boom' WHERE id=2")
tdb_conn.commit()

picked = {r["id"] for r in tdb.pending_rows(tdb_conn, 3)}
check("缺译文/灯/AI分/标题进入同一个完整分析队列", picked == {1, 3, 4, 6, 7, 8}, sorted(picked))
check("增量处理只选订阅提供的 ID",
      {r["id"] for r in tdb.pending_rows(tdb_conn, 3, [6, 8])} == {6, 8})
check("空增量不会退化成全库扫描", tdb.pending_rows(tdb_conn, 3, []) == [])
check("完整记录不因旧错误重跑", 2 not in picked, sorted(picked))
check("累计失败达到旧上限仍会轮转重试", 3 in picked, sorted(picked))
tdb_conn.execute("UPDATE items SET stage='collected' WHERE id=2")
tdb_conn.commit()
collected_complete = dict(tdb_conn.execute("SELECT * FROM items WHERE id=2").fetchone())
check("完整分析的 collected 行仍须进入处理链", processing_required(collected_complete))
check("扫描把完整 collected 行推进到判定阶段",
      tdb.promote_complete_collected(tdb_conn) == 1
      and tdb_conn.execute("SELECT stage FROM items WHERE id=2").fetchone()[0] == "translated")
check("新闻要点缺失需要完整重做",
      "要点" in missing_analysis_fields({"is_news": 1, "ai_summary": "", "importance": "高"}))
check("非新闻允许空要点和重要度",
      "要点" not in missing_analysis_fields({"is_news": 0, "ai_summary": "", "importance": ""}))
check("引用译文缺失需要完整重做",
      "引用译文" in missing_analysis_fields({"quoted_raw": "quoted", "quoted_zh": ""}))
check("新闻完整分析缺要点时拒绝落库",
      raises(lambda: llm.parse_analysis(_valid_analysis(summary=""))))
_original_chat = llm.chat
_original_is_configured = llm.is_configured
llm.chat = lambda *args, **kwargs: json.dumps(_valid_analysis(quoted_zh=""), ensure_ascii=False)
llm.is_configured = lambda: True
try:
    quote_analysis_rejected = raises(lambda: llm.analyze_item("raw", quoted_raw="quoted"))
finally:
    llm.chat = _original_chat
    llm.is_configured = _original_is_configured
check("引用译文缺失时拒绝完整分析结果", quote_analysis_rejected)
check("没有补标或单独语义打标入口",
      not any(hasattr(llm, name) for name in ("build_meta_prompt", "parse_meta", "analyze_meta", "semantic_labels")))

class _Recorder:
    """假连接：记录数据库写入 SQL。"""

    def __init__(self, zh=""):
        self.sql = []
        self.params = []
        self.zh = zh

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(params)
        return self

    def fetchone(self):
        return (self.zh,)


check("没有独立补标队列或补标入口",
      not hasattr(tdb, "meta_pending_rows") and not hasattr(tdb, "meta_pass"))

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
_original_analyze_item = tdb.analyze_item
_repair_calls = []
def _fake_analyze_item(*args, repair_hint="", **kwargs):
    _repair_calls.append(repair_hint)
    if len(_repair_calls) == 1:
        raise ValueError("完整分析缺少必需的灯: not_repeat")
    return dict(_ANALYSIS)
tdb.analyze_item = _fake_analyze_item
try:
    repaired, repair_error, repair_auth = tdb.analyze_with_retry("原文", "测试")
finally:
    tdb.analyze_item = _original_analyze_item
check("格式缺字段时立即重试一次完整分析",
      repaired is not None and repair_error is None and not repair_auth
      and len(_repair_calls) == 2 and "not_repeat" in _repair_calls[1], _repair_calls)

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
check("标题提示词围绕有效正文并排除填充信息",
      "以清理后的 zh 为准" in q_prompt and "直播引导" in q_prompt
      and "账号会由发送格式单独附在标题后" in q_prompt)
parsed_q = llm.parse_analysis(_valid_analysis(zh="译文", quoted_zh="被引用的正文翻译"))
check("解析 quoted_zh", parsed_q["quoted_zh"] == "被引用的正文翻译", parsed_q)
print("[9] 分发队列（SQLite）：source 注册 / 待发判定 / 标记已发")
import json  # noqa: E402
import distribute.push_db as qdb  # noqa: E402

qcase = SANDBOX / "push_case"
qcase.mkdir(parents=True, exist_ok=True)
qdb_file = qcase / "hx.sqlite3"
if qdb_file.exists():
    qdb_file.unlink()
qconn = sqlite3.connect(qdb_file)
cdb.ensure_schema(qconn)          # 真库的建表口径：pushed 列 + pushes 表都由它建
for item_id, account, zh, decision in ((1, "SpaceX", "星舰", "发"), (2, "NASA", "月球", "发"),
                                       (3, "路人", "闲聊", "丢")):
    qconn.execute("INSERT INTO items(id, account, zh, raw, decision, ai_score, stage, ts)"
                  " VALUES(?,?,?,?,?,?, 'processed', ?)",
                  (item_id, account, zh, "raw", decision, 80, int(time.time())))
qconn.commit()
qconn.close()


def _q(sql):
    conn = sqlite3.connect(qdb_file)
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


check("pushed 列由 collect/db.py 的 schema 建出来（不是分发端临时加的）",
      "pushed" in {r[1] for r in sqlite3.connect(qdb_file).execute("PRAGMA table_info(items)")})
check("pushes 审计表同样由 schema 建出来", _q(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='pushes'") == "pushes")
check("未注册 source 时：待发 = 已处理完成且 decision='发'（'丢'的不算）",
      [it["id"] for it in qdb.pending(db_file=qdb_file)] == [1, 2])

gate_case = SANDBOX / "distribution_stage_gate"
gate_case.mkdir(parents=True, exist_ok=True)
gate_db = gate_case / "hx.sqlite3"
gate_db.unlink(missing_ok=True)
gate_conn = sqlite3.connect(gate_db)
cdb.ensure_schema(gate_conn)
gate_conn.execute("INSERT INTO items(id,raw,decision,stage,ts) VALUES(1,'raw','发','judged',?)",
                  (int(time.time()),))
gate_conn.execute("INSERT INTO items(id,raw,decision,stage,ts) VALUES(2,'raw','发','processed',?)",
                  (int(time.time()),))
gate_conn.commit()
gate_conn.close()
check("第三池隐藏尚未完成 AI 兜底的 judged 输出",
      [it["id"] for it in qdb.pending(db_file=gate_db)] == [2])
gate_conn = sqlite3.connect(gate_db)
gate_conn.execute("UPDATE items SET stage='processed' WHERE id=1")
gate_conn.commit()
gate_conn.close()
check("第三池扫描到新完成的 processed 输出",
      [it["id"] for it in qdb.pending(db_file=gate_db)] == [1, 2])

age_case = SANDBOX / "distribution_age_filter"
age_case.mkdir(parents=True, exist_ok=True)
age_db = age_case / "hx.sqlite3"
age_db.unlink(missing_ok=True)
age_conn = sqlite3.connect(age_db)
cdb.ensure_schema(age_conn)
age_now = int(time.time())
for item_id, ts in ((1, age_now), (2, age_now - 5 * 24 * 60 * 60 - 1), (3, None)):
    age_conn.execute("INSERT INTO items(id,decision,stage,ts) VALUES(?,'发','processed',?)",
                     (item_id, ts))
age_conn.commit()
age_conn.close()
check("所有渠道只读取最近 5 天且有发布时间的内容",
      [it["id"] for it in qdb.pending(db_file=age_db)] == [1]
      and qdb.pending_count(db_file=age_db) == 1)

# 这一段只测注册/标记，先把"旧 JSON 迁移"的输入关掉，免得读到真实 data/queue/待推送.json
_real_queue_files, _real_sent_files = qdb.LEGACY_QUEUE_FILES, qdb.LEGACY_SENT_FILES
qdb.LEGACY_QUEUE_FILES, qdb.LEGACY_SENT_FILES = (), ()

check("注册两个 source", qdb.register_sources(["maibot:private", "maibot:group_G1"],
                                          db_file=qdb_file) == ["maibot:private", "maibot:group_G1"])
check("重复注册不重复添加",
      qdb.register_sources(["maibot:private"], db_file=qdb_file) == [])
check("source 进了 meta 注册表",
      sorted(qdb.registry(db_file=qdb_file)) == ["maibot:group_G1", "maibot:private"],
      qdb.registry(db_file=qdb_file))
check("新 source 对已有行默认未发（缺键按 false）",
      [it["id"] for it in qdb.pending(source="maibot:private", db_file=qdb_file)] == [1, 2])

check("标记已发返回改动条数", qdb.mark_pushed([1], "maibot:private", db_file=qdb_file) == 1)
check("重复标记不重复计数", qdb.mark_pushed([1], "maibot:private", db_file=qdb_file) == 0)
check("标记会给 source 置 true",
      json.loads(_q("SELECT pushed FROM items WHERE id=1")).get("maibot:private") is True)
check("私聊发过 ≠ 群发过（粒度到目标，不是到通道）",
      [it["id"] for it in qdb.pending(source="maibot:group_G1", db_file=qdb_file)] == [1, 2])
check("pushes 审计表写入且幂等",
      _q("SELECT COUNT(*) FROM pushes") == 1 and _q("SELECT sent_at FROM pushes"))
check("看板口径：还有 source 没发完就算待分发", qdb.pending_count(db_file=qdb_file) == 2)
check("指定只计启用 source：关掉的群不应计入摘要",
      qdb.pending_count(db_file=qdb_file, sources=["maibot:private"]) == 1
      and qdb.pending_count(db_file=qdb_file, sources=[]) == 0)

# 新通道（如艾莉森）第一次注册：旧 source 全发过的行不能灌给新通道
qdb.mark_pushed([1], "maibot:group_G1", db_file=qdb_file)
qdb.register_sources(["alison:group_A"], db_file=qdb_file)
check("新 source 回填：旧 source 全发过的行 → 新 source 直接 true（不灌历史）",
      json.loads(_q("SELECT pushed FROM items WHERE id=1")).get("alison:group_A") is True)
check("新 source 回填：还没发完的行 → 新 source 记 false（新通道照样能取）",
      json.loads(_q("SELECT pushed FROM items WHERE id=2")).get("alison:group_A") is False)
check("新通道只拿到还没发完的那条",
      [it["id"] for it in qdb.pending(source="alison:group_A", db_file=qdb_file)] == [2])

print("[9b] 旧 待推送.json / 已发记录.json 的状态一次性迁移进 pushed")
seed_case = SANDBOX / "seed_case"
seed_case.mkdir(parents=True, exist_ok=True)
seed_db = seed_case / "hx.sqlite3"
if seed_db.exists():
    seed_db.unlink()
(seed_case / "待推送.json").write_text(json.dumps([
    {"id": 5, "account": "SpaceX", "zh": "星舰", "sent_to": ["private"]},   # 只发过私聊
    {"id": 4, "account": "旧账号", "zh": "旧闻", "sent_to": ["private", "group_G1"]},
], ensure_ascii=False), encoding="utf-8")
(seed_case / "已发记录.json").write_text(json.dumps(
    [{"account": "Blue", "zh": "已发过的"}], ensure_ascii=False), encoding="utf-8")
sconn = sqlite3.connect(seed_db)
cdb.ensure_schema(sconn)
for row in ((3, "老条目", "队列写下之前就在库里"), (4, "旧账号", "旧闻"),
            (5, "SpaceX", "星舰"), (6, "Blue", "已发过的"), (7, "New", "队列之后的新条目")):
    sconn.execute("INSERT INTO items(id, account, zh, raw, decision, ai_score)"
                  " VALUES(?,?,?,'raw','发',50)", row)
sconn.commit()
sconn.close()

_real_queue_files, _real_sent_files = qdb.LEGACY_QUEUE_FILES, qdb.LEGACY_SENT_FILES
qdb.LEGACY_QUEUE_FILES = (seed_case / "待推送.json",)
qdb.LEGACY_SENT_FILES = (seed_case / "已发记录.json",)
qdb.register_sources(["maibot:private", "maibot:group_G1"], db_file=seed_db)


def _seed_pushed(item_id):
    conn = sqlite3.connect(seed_db)
    try:
        row = conn.execute("SELECT pushed FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0] or "{}")


check("迁移：旧队列里 sent_to 只列了 private → 群那份还是未发",
      _seed_pushed(5).get("maibot:private") is True
      and _seed_pushed(5).get("maibot:group_G1") is False, _seed_pushed(5))
check("迁移：旧队列里两个 kind 都发过 → 都标 true",
      _seed_pushed(4).get("maibot:private") is True
      and _seed_pushed(4).get("maibot:group_G1") is True, _seed_pushed(4))
check("迁移：不在旧队列但 id 更小（当年发完被移除）→ 不重推",
      _seed_pushed(3).get("maibot:private") is True, _seed_pushed(3))
check("迁移：在旧已发记录里 → 不重推",
      _seed_pushed(6).get("maibot:private") is True, _seed_pushed(6))
check("迁移：比旧队列更新的条目 → 留给分发端去发",
      not _seed_pushed(7).get("maibot:private"), _seed_pushed(7))
check("迁移只跑一次（第二次注册不再动状态）",
      qdb.seed_if_needed(db_file=seed_db) == 0)
qdb.LEGACY_QUEUE_FILES, qdb.LEGACY_SENT_FILES = _real_queue_files, _real_sent_files

# 注：插件侧不再直接读库（它通过看板的渠道 WebSocket 拉内容，见 selftest/check_channels.py），
# 所以这里没有"插件镜像 vs 仓库实现"的对照用例了。

tdb_conn.close()
import shutil as _shutil  # noqa: E402
_shutil.rmtree(case, ignore_errors=True)

print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 抓取脚本 侧自检全部通过")
