# -*- coding: utf-8 -*-
"""看板托管的服务循环。分发只观察队列，绝不自动触发 QQ 发送。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PROJECT_PYTHON.is_file():
    raise RuntimeError(f"项目虚拟环境不存在: {PROJECT_PYTHON}；请先创建并安装依赖")
COMMANDS = {
    "collect": [str(PROJECT_PYTHON), str(ROOT / "main_flow.py"), "--collect-only"],
    "process": [str(PROJECT_PYTHON), str(ROOT / "main_flow.py"), "--process-only"],
}
INTERVALS = {"collect": 600, "process": 60}


def configured_interval(name: str) -> int:
    key = "COLLECT_INTERVAL_SECONDS" if name == "collect" else "PROCESS_INTERVAL_SECONDS"
    try:
        values = {}
        env_file = ROOT / ".env"
        for line in env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []:
            field, separator, value = line.partition("=")
            if separator:
                values[field.strip()] = value.strip()
        return max(10, int(values.get(key, INTERVALS[name])))
    except (ValueError, OSError):
        return INTERVALS[name]


def run_command(name: str) -> int:
    command = COMMANDS[name]
    print(f"[{name}] 开始一轮", flush=True)
    # 看板按 UTF-8 读本进程输出；本进程也要让子进程按 UTF-8 写、并且不缓冲。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    result = subprocess.run(command, cwd=ROOT, env=env)
    print(f"[{name}] 本轮结束，退出码 {result.returncode}", flush=True)
    return result.returncode


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in {"collect", "process", "distribute"}:
        raise SystemExit("usage: service_runner.py collect|process|distribute")
    if name == "distribute":
        queue = ROOT / "data" / "queue" / "待推送.json"
        while True:
            try:
                rows = json.loads(queue.read_text(encoding="utf-8")) if queue.exists() else []
                print(f"[distribute] 队列 {len(rows) if isinstance(rows, list) else 0} 条；默认不执行真实发送", flush=True)
            except Exception as exc:
                print(f"[distribute] 队列读取失败: {exc}", flush=True)
            time.sleep(10)
    while True:
        run_command(name)
        time.sleep(configured_interval(name))


if __name__ == "__main__":
    main()
