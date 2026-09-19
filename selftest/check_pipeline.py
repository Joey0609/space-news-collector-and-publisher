# -*- coding: utf-8 -*-
"""重构后自检：抓取脚本 侧的导入 + 关键逻辑冒烟测试。

跑法：python 自检/check_pipeline.py（或统一跑 自检/run_all.py）
（需要 自检/stubs 里的 openai/twscrape 桩模块）
"""
import importlib.util
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

check("统一 X adapter", ADAPTER.name == "x" and callable(ADAPTER.collect))

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
    def __init__(self, tid=1, text="hello world", date=None, media=None, rt=None, qt=None):
        self.id = tid
        self.rawContent = text
        self.date = date
        self.media = media
        self.retweetedTweet = rt
        self.quotedTweet = qt


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

print("[5] JSON 读写工具")
tmp = SANDBOX / "_t.json"
jsonio.save_json(tmp, {"a": 1})
check("save/load json", jsonio.load_json(tmp, {}) == {"a": 1})
check("读不存在的文件给默认值", jsonio.load_json(SANDBOX / "nope.json", "d") == "d")
check("deep_merge 递归", jsonio.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}})
tmp.unlink(missing_ok=True)

print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 抓取脚本 侧自检全部通过")
