# -*- coding: utf-8 -*-
"""
航小麦 · 主流程 (模块5 · main_flow.py)
=======================================
作用：把整条推送线串起来，每 10 分钟跑一遍：
      模块1(增量抓取) → 模块2(打标打分) → 模块3(AI兜底)
      → 汇总"判定=发"的新闻 → 写入 配置\待推送.json
      → 麦麦插件(模块4)检测到后以口吻推送。

运行：
    <venv>/Scripts/python.exe main_flow.py
    （venv 需装 twscrape/openai/dotenv）

也可定时(计划任务)每10分钟调用本脚本。
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent          # 航小麦
SEED_DIR = Path(__file__).parent            # 抓取脚本
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = SEED_DIR / "output"
CONFIG_DIR = ROOT / "配置"
PENDING_FILE = CONFIG_DIR / "待推送.json"
LOG_FILE = LOG_DIR / "main_flow.log"
SENT_FILE = CONFIG_DIR / "已发记录.json"
MAX_PENDING = 100  # 待推送队列上限
MAX_SENT = 500     # 已发记录上限


def load_sent_keys() -> set:
    """读已发记录，返回 {(account, zh)} 集合；失败返回空。"""
    try:
        if SENT_FILE.exists():
            data = json.loads(SENT_FILE.read_text(encoding="utf-8-sig"))
            return {(str(x.get("account", "")), str(x.get("zh", ""))) for x in data}
    except Exception:
        pass
    return set()


def save_sent_records(records: list) -> None:
    """写已发记录（保留最近 MAX_SENT 条）。"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SENT_FILE.write_text(json.dumps(records[-MAX_SENT:], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# 用麦麦 venv 的 pythonw.exe 跑子模块（无控制台，不弹黑窗口）
VENV_PY = ROOT / "MaiBot-main" / ".venv" / "Scripts" / "pythonw.exe"
if not VENV_PY.exists():
    VENV_PY = ROOT / "MaiBot-main" / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_module(script: str, *args: str) -> None:
    """依次运行给定脚本，可传额外参数（如 judge 吃本轮 new_news 文件路径）。"""
    log(f"[运行] {script} {' '.join(args)}")
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            [str(VENV_PY), str(SEED_DIR / script), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, creationflags=no_window,
        )
        if r.returncode != 0:
            log(f"[警告] {script} 退出码 {r.returncode}\n{r.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        log(f"[警告] {script} 超时")
    except Exception as e:
        log(f"[警告] {script} 异常: {e}")


def latest_file(pattern: str) -> Path | None:
    files = sorted(OUTPUT_DIR.glob(pattern))
    return files[-1] if files else None


def collect_should_send() -> list:
    """从最新的 fallback 结果里收集 final_decision=='发' 的新闻。"""
    fb = latest_file("fallback_*.json")
    if not fb:
        log("[信息] 无 fallback 结果(可能无本轮判定)")
        return []
    data = json.loads(fb.read_text(encoding="utf-8"))
    should = []
    for n in data:
        if str(n.get("final_decision") or n.get("decision")) == "发":
            # 只保留有效的推送字段
            should.append({
                "account": n.get("account", ""),
                "orig_account": n.get("orig_account", ""),
                "forward_comment": n.get("forward_comment", ""),
                "zh": n.get("zh", ""),
                "raw": n.get("raw", ""),
                "images": n.get("images", []) or ([] if not n.get("image") else [n.get("image")]),
                "image": n.get("image", ""),
                "score": n.get("score", 0),
                "date": n.get("date", ""),
                "content_type": n.get("content_type", ""),
            })
    return should


def append_pending(items: list) -> None:
    """把应发的新闻追加到 待推送.json（队列上限 100 条，超出留档到档案；按账号+内容去重防重复）。"""
    old = []
    if PENDING_FILE.exists():
        try:
            old = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        except Exception:
            old = []
    # 去重：按 (account, zh) 去重，避免同一新闻重复追加；且跳过已在"已发记录"里的（永不重推）
    seen = {(str(x.get("account", "")), str(x.get("zh", ""))) for x in old}
    sent = load_sent_keys()
    add = []
    for it in items:
        key = (str(it.get("account", "")), str(it.get("zh", "")))
        if key in seen or key in sent:
            continue
        seen.add(key)
        add.append(it)
    merged = old + add
    # 队列上限 100：超出的部分存到"队列档案"，防丢失
    if len(merged) > MAX_PENDING:
        overflow = merged[:MAX_PENDING]
        archived = merged[MAX_PENDING:]
        try:
            archive_file = CONFIG_DIR / "队列档案.json"
            exist = []
            if archive_file.exists():
                exist = json.loads(archive_file.read_text(encoding="utf-8"))
            archive_file.write_text(json.dumps(exist + archived, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        merged = overflow
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[待推送] 新增 {len(add)} 条，当前队列 {len(merged)}（上限{MAX_PENDING}）")


def main() -> None:
    log("=" * 55)
    log("航小麦 · 主流程")
    # 模块1 增量抓取 → 模块2 打标打分（只吃本轮生成的 new_news，杜绝读旧文件）→ 模块3 AI兜底
    run_module("news_fetch.py")
    latest = latest_file("new_news_*.json")
    run_module("judge.py", str(latest)) if latest else run_module("judge.py")
    run_module("ai_fallback.py")
    # 收集"该发"的 → 写待推送
    should = collect_should_send()
    log(f"[结果] 本轮判定'发'共 {len(should)} 条")
    if should:
        append_pending(should)
    # 清理旧输出文件（保留最近若干，防堆积）
    for pattern in ["new_news_*.json", "judged_*.json", "fallback_*.json"]:
        for f in sorted(OUTPUT_DIR.glob(pattern))[:-3]:
            try:
                f.unlink()
            except Exception:
                pass
    log("=== 主流程结束 ===")


if __name__ == "__main__":
    main()
