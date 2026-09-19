# -*- coding: utf-8 -*-
"""航小麦 · 主流程 (模块5 · main_flow.py)
=======================================
作用：把整条推送线串起来，建议每 10 分钟跑一遍：
      模块1(增量抓取) → 模块2(打标打分) → 模块3(AI兜底)
      → 汇总"判定=发"的新闻 → 写入 配置/待推送.json
      → 麦麦插件(模块4) 检测到后以麦麦口吻推送。

运行：
    <venv>/Scripts/python.exe main_flow.py
    （venv 需装 twscrape/openai/dotenv；也可由计划任务定时调用）

说明：
  - 子模块用麦麦 venv 的 pythonw.exe 跑（无控制台黑窗）；找不到就退回当前解释器。
  - 每个子模块独立进程：一个崩了不影响其它模块，日志见 logs/*.log。
   - 待推送队列与去重规则在 process/queue.py。
"""
import json
import subprocess
import sys
import argparse
from pathlib import Path

_REPO = Path(__file__).resolve().parent
_COLLECT_DIR = _REPO / "collect"
_PROCESS_DIR = _REPO / "process"
for _path in (_REPO, _COLLECT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from collect.jsonio import latest_file
from collect.logging_util import make_log, setup_console_encoding
from collect.paths import LOG_DIR, RAW_DIR, WORK_DIR, ROOT  # noqa: F401
OUTPUT_DIR = RAW_DIR

from collect.store import append_pending

LOG_FILE = LOG_DIR / "main_flow.log"
log = make_log(LOG_FILE)

# Project-local interpreter is mandatory for the pipeline.
PROJECT_PY = ROOT / ".venv" / "Scripts" / "python.exe"
VENV_PY = PROJECT_PY if PROJECT_PY.is_file() else Path(sys.executable)


def run_module(script: str, *args: str) -> bool:
    """运行子模块并返回是否成功；输出和错误都写入主流程日志。"""
    log(f"[运行] {script} {' '.join(args)} | python={VENV_PY}")
    module_dir = _COLLECT_DIR if script == "news_fetch.py" else _PROCESS_DIR
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.Popen(
            [str(VENV_PY), str(module_dir / script), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            creationflags=no_window,
        )
        for line in r.stdout:
            line=line.rstrip("\r\n")
            print(f"[{script}] {line}", flush=True)
            try:
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                with LOG_FILE.open("a", encoding="utf-8") as stream:
                    stream.write(f"[{script}] {line}\n")
            except Exception:
                pass
        returncode=r.wait(timeout=600)
        if returncode != 0:
            log(f"[警告] {script} 退出码 {returncode}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log(f"[警告] {script} 超时")
        return False
    except Exception as e:
        log(f"[警告] {script} 异常: {e}")
        return False


def collect_should_send() -> list:
    """从 SQLite 读取 final_decision='发' 的内容，生成分发队列字段。"""
    from process.db import rows as db_rows
    should=[]
    for n in db_rows():
        if str(n.get("final_decision") or n.get("decision")) != "发": continue
        images=n.get("images") or []
        if isinstance(images,str):
            try: images=json.loads(images)
            except Exception: images=[]
        should.append({"id":n.get("id"),"account":n.get("account",""),"orig_account":n.get("orig_account",""),"forward_comment":n.get("forward_comment",""),"zh":n.get("zh",""),"raw":n.get("raw",""),"images":images,"ori_url":n.get("ori_url",""),"score":n.get("score",0),"date":n.get("date",""),"content_type":n.get("content_type","")})
    return should


def cleanup_old_outputs(keep: int = 3) -> None:
    """清理旧输出文件（每类保留最近 keep 个），防 output/ 无限堆积。"""
    for directory, pattern in ((RAW_DIR, "new_news_*.json"), (WORK_DIR, "judged_*.json"), (WORK_DIR, "fallback_*.json")):
        for f in sorted(directory.glob(pattern))[:-keep]:
            try:
                f.unlink()
            except Exception:
                pass


def run_processing() -> bool:
    """只处理最近一次采集结果，不重复触发采集。"""
    success = True
    run_module("translate_db.py")
    success = run_module("judge.py") and success
    success = run_module("ai_fallback.py") and success
    if not success:
        log("[错误] 处理阶段存在失败")
        return False
    should = collect_should_send()
    log(f"[结果] 本轮判定'发'共 {len(should)} 条")
    if should:
        append_pending(should, log)
    cleanup_old_outputs()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect-only", action="store_true", help="只执行采集")
    parser.add_argument("--process-only", action="store_true", help="只处理最近采集结果")
    args = parser.parse_args()
    log("=" * 55)
    log("航小麦 · 主流程")
    if not args.process_only:
        collect_ok = run_module("news_fetch.py")
        if not collect_ok:
            log("[错误] 采集阶段失败，停止本轮处理")
            raise SystemExit(1)
    if not args.collect_only:
        process_ok = run_processing()
        if process_ok is False:
            raise SystemExit(1)
    log("=== 主流程结束 ===")

if __name__ == "__main__":
    setup_console_encoding()
    main()

