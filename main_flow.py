# -*- coding: utf-8 -*-
"""航小麦 · 主流程 (模块5 · main_flow.py)
=======================================
作用：把整条推送线串起来，建议每 10 分钟跑一遍：
      模块1(增量抓取) → 模块2(打标打分) → 模块3(AI兜底)
      → 判定"发"的结果原地留在 SQLite（items.decision）
      → 分发端（麦麦插件 / 艾莉森）直接读库取自己还没发过的条目推送。

运行：
    <venv>/Scripts/python.exe main_flow.py
    （venv 需装 twscrape/openai/dotenv；也可由计划任务定时调用）

说明：
  - 子模块用麦麦 venv 的 pythonw.exe 跑（无控制台黑窗）；找不到就退回当前解释器。
  - 每个子模块独立进程：一个崩了不影响其它模块，日志见 logs/*.log。
  - **不再写 待推送.json**：队列就是 items 表里 decision='发' 的行，
    每条内容发给了哪些通道记在 items.pushed 里（见 distribute/push_db.py）。
"""
import os
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
from distribute.push_db import (
    describe as queue_describe,
    pending as queue_pending,
    pending_count as queue_pending_count,
)
OUTPUT_DIR = RAW_DIR

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
            # 我们按 UTF-8 读子进程输出，就必须让子进程按 UTF-8 写（管道下 Windows 默认 GBK）；
            # 同时禁用缓冲，否则子模块日志会卡在缓冲区、看板终端看不到实时输出。
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
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


def should_send_count() -> int:
    """库里判定为"发"的条数。

    分发端读的就是这些行（外加 items.pushed 里自己那个 source 还不是 true），
    所以这里只统计、不再生成任何队列文件。
    """
    return len(queue_pending())


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
    should = should_send_count()
    log(f"[结果] 库里判定'发'共 {should} 条；{queue_describe()}")
    log(f"[结果] 待分发 {queue_pending_count()} 条（分发端直接读库，不写队列文件）")
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

