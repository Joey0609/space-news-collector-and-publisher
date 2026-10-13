# -*- coding: utf-8 -*-
"""看板托管的服务循环（只跑"采集"和"处理"两个循环）。
=====================================================
分发**不再是一个服务**：它是若干"渠道"（麦麦插件 / 艾莉森 / NapCat），各自连看板的
渠道 WebSocket，由 `dashboard/channels.py` 调度、看板"分发"页启停。见 dashboard/PROTOCOL.md。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

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


def acquire_instance_lock(name: str):
    """Prevent two dashboard instances from running the same database loop concurrently."""
    lock_dir = ROOT / "data" / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"service_{name}.lock").open("a+b")
    try:
        if os.name == "nt":
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


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
    if name not in {"collect", "process"}:
        raise SystemExit("usage: service_runner.py collect|process"
                         "（分发已改为渠道，见 dashboard/PROTOCOL.md）")
    lock = acquire_instance_lock(name)
    if lock is None:
        print(f"[{name}] 已有同类服务在运行，本实例退出以避免重复处理", flush=True)
        return
    while True:
        run_command(name)
        time.sleep(configured_interval(name))


if __name__ == "__main__":
    main()
