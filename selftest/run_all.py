# -*- coding: utf-8 -*-
"""航小麦 · 一键自检（改完代码先跑这个）
====================================
跑法（在任意目录都行）：
    python 自检/run_all.py

做什么：分别在**独立子进程**里跑几份自检，汇总结果。
    check_pipeline.py   采集/处理侧：导入 + 打标打分 + 新条目构造 + 分发队列口径
    check_channels.py   分发渠道：真 socket 的 WS 往返（注册/推送/拉取/回报/写库）
    check_ali.py        艾莉森网关侧：导入 + 排版/候选挑选
    check_plugin.py     麦麦插件侧：装配 + 一轮调度（渠道替身）+ Hook 落盘
    check_dashboard.py  看板：服务标签 + HTTP API 契约 + 停止服务 + 分页

为什么用子进程：几份自检会各自往 sys.path 加桩模块、也会 import 同名顶层模块
（hx_paths / 推送 等），同进程跑会互相串味。
"""
import subprocess
import sys
from pathlib import Path

try:  # 避免 Windows GBK 控制台打印符号/中文崩溃
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

CHECKS = [
    ("collect", HERE / "check_pipeline.py"),
    ("web sources", HERE / "check_web_sources.py"),
    ("channels", HERE / "check_channels.py"),
    ("Alison", HERE / "check_ali.py"),
    ("MaiBot plugin", HERE / "check_plugin.py"),
    ("dashboard", HERE / "check_dashboard.py"),
    ("AI config", HERE / "check_ai_config.py"),
    ("retry rotation", HERE / "check_retry_rotation.py"),
    ("data reset", HERE / "check_data_reset.py"),
    ("queue progress", HERE / "check_queue_progress.py"),
    ("image storage", HERE / "check_image_store.py"),
]


def main() -> int:
    results = []
    logs = HERE / ".sandbox" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name, script in CHECKS:
        print("=" * 60)
        print(f"[跑] {name}：{script.name}")
        print("=" * 60)
        # 输出重定向到文件（不用管道：某些受限环境不允许创建管道捕获子进程输出）
        log_file = logs / f"{script.stem}.log"
        with open(log_file, "w", encoding="utf-8", errors="replace") as f:
            r = subprocess.run([sys.executable, str(script)], cwd=str(REPO),
                               stdout=f, stderr=subprocess.STDOUT)
        try:
            print(log_file.read_text(encoding="utf-8", errors="replace").rstrip())
        except Exception as e:
            print(f"(读日志失败: {e})")
        ok = r.returncode == 0
        results.append((name, ok))
        print(f"→ 退出码 {r.returncode}，日志: {log_file}")
        print()

    print("=" * 60)
    print("汇总")
    print("=" * 60)
    for name, ok in results:
        print(f"  {'[通过]' if ok else '[失败]'}  {name}")
    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"\n共 {len(failed)} 项失败：{'、'.join(failed)}")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
