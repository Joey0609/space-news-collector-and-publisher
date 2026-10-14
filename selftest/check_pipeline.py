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
    (REPO / "process" / "analyze_db.py", "analyze_db"),
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

print("[6] 单次完整分析输出与原子处理")
from unittest.mock import patch
from collect import db as cdb
from process import analyze_db as adb
from process.analysis_contract import missing_analysis_fields
from distribute import push_db
import tempfile

analysis = dict(zh="NASA完成任务",quoted_zh="",title="NASA完成任务",summary="NASA任务完成。",importance="中",score=70,tags=["领域:任务"],repeat_of="",decision="发",invalid_reason="")
with patch.object(llm,'chat',return_value=json.dumps(analysis,ensure_ascii=False)) as call:
    parsed=llm.analyze_item("NASA completed the mission.")
    check("一次成功请求返回完整最终结果",call.call_count==1 and parsed==analysis)
for key in analysis:
    bad=dict(analysis);bad.pop(key)
    check("缺字段必须失败："+key,raises(lambda:llm.parse_analysis(json.dumps(bad))))
for changes in ({"decision":"AI兜底"},{"decision":"丢"},{"score":True},{"score":101},{"importance":""},{"tags":[]},{"invalid_reason":"无效"}):
    bad=dict(analysis,**changes)
    check("无效输出不能成功",raises(lambda:llm.parse_analysis(json.dumps(bad))))
rejected=dict(analysis,decision="丢",invalid_reason="内容为购物广告，无独立航天事实。")
check("丢弃内容也返回完整归档",llm.parse_analysis(json.dumps(rejected))==rejected)
check("事实污染校验仍保留",raises(lambda:llm.validate_analysis_grounding(dict(analysis,title="星舰第11次试飞"),"I captured the ISS!")))
with tempfile.TemporaryDirectory() as directory:
    database=Path(directory)/'test.sqlite3'
    conn=sqlite3.connect(database);conn.row_factory=sqlite3.Row;cdb.ensure_schema(conn)
    columns={r[1] for r in conn.execute('PRAGMA table_info(items)')}
    check("新内容表恰好34字段",len(columns)==34)
    check("旧字段彻底删除",not columns & {'is_news','not_repeat','not_comment','not_sensitive','rules_score','reason','translated_at','translate_attempts','translate_error'})
    check("事件表无重复尝试计数",len(conn.execute('PRAGMA table_info(processing_events)').fetchall())==4)
    for ident in (1,2):
        conn.execute("INSERT INTO items(id,raw,account,ts) VALUES(?,'NASA completed the mission.','NASA',?)",(ident,int(time.time())))
        cdb.ensure_processing_event(conn,ident)
    conn.commit()
    def model(text,*args,**kwargs):
        if model.calls==1:
            other=sqlite3.connect(database)
            check("下一条开始前上一条已可分发",other.execute("SELECT stage,decision FROM items WHERE id=1").fetchone()==('processed','发'))
            check("上一条事件已原子确认",other.execute('SELECT COUNT(*) FROM processing_events WHERE item_id=1').fetchone()[0]==0)
            other.close()
            raise TimeoutError('offline timeout')
        model.calls+=1
        return dict(analysis)
    model.calls=0
    with patch.object(adb,'analyze_item',side_effect=model),patch.object(adb,'ANALYSIS_MAX_ATTEMPTS',1),patch.object(adb,'ANALYSIS_INLINE_RETRIES',0),patch.object(adb,'ANALYSIS_PAUSE_SECONDS',0),patch.object(adb,'write_progress'):
        result=adb.analysis_pass(conn,[1,2])
    check("失败项不阻塞成功提交",result=={'ok':1,'failed':1,'skipped':0})
    check("超时保留未完成事件且进入冷却",conn.execute('SELECT available_at FROM processing_events WHERE item_id=2').fetchone()[0]>time.time())
    conn.execute("UPDATE items SET raw='new version' WHERE id=2")
    cdb.enqueue_processing_event(conn,2);conn.commit()
    stale=dict(conn.execute('SELECT * FROM items WHERE id=2').fetchone())
    generation=conn.execute('SELECT generation FROM processing_events WHERE item_id=2').fetchone()[0]
    cdb.enqueue_processing_event(conn,2);conn.commit()
    check("旧事件版本不能覆盖新版本",not adb.save_ok(conn,stale,analysis,1,generation))
    check("新事件仍存在",conn.execute('SELECT generation FROM processing_events WHERE item_id=2').fetchone()[0]==generation+1)
    conn.execute("INSERT INTO items(id,stage,decision,ts) VALUES(3,'processed','发',?)",(int(time.time())-5*86400-1,));conn.commit()
    check("分发统一过滤5天前内容",[i['id'] for i in push_db.pending(db_file=database)]==[1])
    push_db.register_sources(['maibot:private','maibot:group_test'],db_file=database)
    push_db.mark_pushed([1],'maibot:private',db_file=database)
    check("分发按目标独立记账",push_db.pending('maibot:private',db_file=database)==[] and [i['id'] for i in push_db.pending('maibot:group_test',db_file=database)]==[1])
    cdb.ensure_processing_event(conn,1);conn.commit()
    with patch.object(adb,'analyze_item') as unused:
        zero=adb.analysis_pass(conn,[1,99999])
        check('完整记录不再调用模型',unused.call_count==0 and zero['ok']==0)
    check('完整记录的残留事件已清理',conn.execute('SELECT COUNT(*) FROM processing_events WHERE item_id=1').fetchone()[0]==0)
    from dashboard import service_runner as runner
    with patch.object(runner,'ROOT',Path(directory)):
        first=runner.acquire_instance_lock('analysis_worker')
        check('完整分析程序只允许运行一份',first is not None and runner.acquire_instance_lock('analysis_worker') is None)
        first.close()
        released=runner.acquire_instance_lock('analysis_worker')
        check('关闭后释放运行锁',released is not None)
        released.close()
    conn.close()
if failures:
    raise SystemExit("失败："+str(failures))
print("采集与单次完整分析自检全部通过")
