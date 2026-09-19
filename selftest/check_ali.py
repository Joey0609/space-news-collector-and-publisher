# -*- coding: utf-8 -*-
"""重构后自检：艾莉森网关 侧的导入 + 排版/挑选逻辑冒烟测试。

跑法：python 自检/check_ali.py（或统一跑 自检/run_all.py）
"""
import importlib.util
import sys
from pathlib import Path

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
check("TARGET_KIND 由群号推出", paths.TARGET_KIND == f"group_{paths.TARGET_GROUP_NUMBER}")
check("host 沙箱/正式切换", aenv.api_host({"SANDBOX": "1"}).startswith("https://sandbox")
      and aenv.api_host({}).startswith("https://api."))
check("is_sandbox 各种写法", aenv.is_sandbox({"SANDBOX": "true"}) and not aenv.is_sandbox({"SANDBOX": "0"}))
check("credentials 去空格", aenv.credentials({"APPID": " 1 ", "SECRET": "2"}) == ("1", "2"))
check("auth_headers 形状", qq.auth_headers("tok", "app")["Authorization"] == "QQBot tok")

print("[2] 入口脚本可导入 + 兼容名还在")
push = load_by_path(ALI / "推送.py", "push_mod")
for attr in ("main", "log", "load_env", "load_state", "load_json", "save_json", "PENDING_FILE",
             "SENT_EVER_FILE", "TARGET_KIND", "build_text", "item_images", "get_token",
             "auth_headers", "upload_group_image", "send_group_rich"):
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
check("sent_to 标记与读取", render.item_sent_to({}) == []
      and (lambda it: (render.mark_sent_to(it, "group_x"), render.item_sent_to(it))[1])({}) == ["group_x"])
# 挑选：带图优先、分数次之、跳过已发
items = [
    {"account": "a", "score": 99, "images": [], "sent_to": []},
    {"account": "b", "score": 10, "images": ["x.jpg"], "sent_to": []},
    {"account": "c", "score": 100, "images": ["y.jpg"], "sent_to": ["group_G"]},
]
idx, picked = render.pick_item(items, "group_G")
check("挑选=带图且未发过、分数最高", idx == 1 and picked["account"] == "b", (idx, picked))
check("指定 index 直取", render.pick_item(items, "group_G", 0)[1]["account"] == "a")
check("全部发过则无候选", render.pick_item([{"sent_to": ["group_G"]}], "group_G") == (None, None))
newest = render.pick_newest_unsent(
    [{"account": "old", "ts": 100, "sent_to": []}, {"account": "new", "ts": 200, "sent_to": []}], "group_G")
check("定时推送取最新未发", newest["account"] == "new", newest)
check("ts 缺失时回退 date", render.ts_of({"date": "2026-09-18T12:00:00+00:00"}) > 0)

print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 艾莉森网关 侧自检全部通过")
