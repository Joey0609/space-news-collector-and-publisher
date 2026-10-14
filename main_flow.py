# -*- coding: utf-8 -*-
"""航小麦 · 主流程 (模块5 · main_flow.py)
=======================================
作用：把整条推送线串起来：
      增量采集 → 一次完整 AI 分析 → 最终结果逐条入库
      → 判定"发"的结果原地留在 SQLite（items.decision）
      → 分发端（麦麦插件 / 艾莉森）直接读库取自己还没发过的条目推送。

看板处理服务启动时完整扫描一次；后续由 collect.db 的 processing_events 订阅增量条目。

运行：
    <venv>/Scripts/python.exe main_flow.py
    （venv 需装 twscrape/openai/dotenv；也可由计划任务定时调用）

说明：
  - 子模块用麦麦 venv 的 pythonw.exe 跑（无控制台黑窗）；找不到就退回当前解释器。
  - 每个子模块独立进程：一个崩了不影响其它模块，日志见 logs/*.log。
  - **不再写 待推送.json**：队列就是 items 表里 stage='processed' 且 decision='发' 的行，
    每条内容发给了哪些通道记在 items.pushed 里（见 distribute/push_db.py）。
"""
import os
import subprocess
import sys
import argparse
import json
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


def run_processing(item_ids=None) -> bool:
    """一个分析程序处理整批输入，内部逐条提交最终结果。"""
    if item_ids == []:
        return True
    args = [] if item_ids is None else ["--item-ids", ",".join(str(v) for v in dict.fromkeys(item_ids))]
    return run_module("analyze_db.py", *args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect-only", action="store_true", help="只执行采集")
    parser.add_argument("--sources", help="本轮采集源，逗号分隔；不改变保存的配置")
    parser.add_argument("--process-only", action="store_true", help="只执行处理阶段")
    parser.add_argument("--item-ids", help="处理订阅收到的逗号分隔条目 ID")
    args = parser.parse_args()
    item_ids = None
    if args.item_ids is not None:
        item_ids = []
        for value in args.item_ids.split(","):
            if value.strip().isdigit():
                item_ids.append(int(value.strip()))
        item_ids = list(dict.fromkeys(item_ids))
        if not item_ids:
            parser.error("--item-ids 没有有效条目 ID")
        if not args.process_only or args.collect_only:
            parser.error("--item-ids 只能和 --process-only 一起使用")
    log("=" * 55)
    log("航小麦 · 主流程")
    if not args.process_only:
        collect_ok = run_module("news_fetch.py", *(["--sources", args.sources] if args.sources is not None else []))
        if not collect_ok:
            log("[错误] 采集阶段失败，停止本轮处理")
            raise SystemExit(1)
    if not args.collect_only:
        process_ok = run_processing(item_ids)
        if process_ok is False:
            raise SystemExit(1)
    log("=== 主流程结束 ===")

if __name__ == "__main__":
    setup_console_encoding()
    main()

