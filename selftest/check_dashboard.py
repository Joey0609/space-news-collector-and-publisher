# -*- coding: utf-8 -*-
"""Dashboard selftest: 服务标签/输出转发 + 停止服务杀整棵树 + 分页与增量推送。"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard.server as server  # noqa: E402


def test_service_labels():
    assert server.SERVICE_NAMES == {"collect": "采集", "process": "处理", "distribute": "分发"}
    assert callable(server.pump_service_output)


class _FakeProcess:
    """假 Popen：只记录被调用了什么。"""

    def __init__(self, pid=4321, running=True):
        self.pid = pid
        self._running = running
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.terminated = True
        self._running = False

    def kill(self):
        self.killed = True
        self._running = False

    def wait(self, timeout=None):
        self.waited = True
        return 0


def _patch_run(returncode, calls):
    """把 subprocess.run 换掉，模拟 taskkill 成功/被拒。"""

    class _Result:
        def __init__(self):
            self.returncode = returncode
            self.stdout = "SUCCESS: ..." if returncode == 0 else ""
            self.stderr = "" if returncode == 0 else "ERROR: Access is denied."

    def fake_run(command, **kwargs):
        calls.append(command)
        return _Result()

    return fake_run


def test_stop_kills_whole_tree():
    original_run = subprocess.run
    original_processes = dict(server.PROCESSES)
    try:
        # ① taskkill 成功：发的是 /F /T /PID，并且进程被收尸
        calls = []
        subprocess.run = _patch_run(0, calls)
        process = _FakeProcess(pid=1111)
        server.PROCESSES["process"] = process
        assert server.stop_service("process") is True
        assert calls and calls[0] == ["taskkill", "/F", "/T", "/PID", "1111"], calls
        assert process.waited, "停止后应当 wait() 收尸"
        assert not process.terminated, "taskkill 成功就不该再 terminate"

        # ② taskkill 被拒：退回 terminate()，不能"看起来停了其实没停"
        calls = []
        subprocess.run = _patch_run(1, calls)
        process = _FakeProcess(pid=2222)
        server.PROCESSES["process"] = process
        assert server.stop_service("process") is True
        assert calls and calls[0][:2] == ["taskkill", "/F"], calls
        assert process.terminated, "taskkill 失败时必须退回 terminate()"

        # ③ 没在跑：返回 False，不报错
        subprocess.run = _patch_run(0, [])
        server.PROCESSES["process"] = _FakeProcess(running=False)
        assert server.stop_service("process") is False
    finally:
        subprocess.run = original_run
        server.PROCESSES.clear()
        server.PROCESSES.update(original_processes)


def test_pagination_and_changes():
    """分页默认 20；就地更新（翻译写完）必须能被增量推送捞到。"""
    tmp = Path(__file__).resolve().parent / ".sandbox" / "dashboard_case"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    original_data = server.DATA
    server.DATA = tmp
    try:
        db = tmp / "hx.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, ts INTEGER, rev INTEGER DEFAULT 1,"
                     " account TEXT, zh TEXT DEFAULT '', images TEXT DEFAULT '[]')")
        for i in range(1, 46):
            conn.execute("INSERT INTO items(id, ts, rev, account) VALUES(?,?,1,?)", (i, i, f"a{i}"))
        conn.commit()
        conn.close()

        page = server.snapshot(offset=0, limit=20)
        assert len(page["items"]) == 20, len(page["items"])
        assert page["has_more"] is True and page["item_count"] == 45, page
        assert page["latest_id"] == 45 and page["latest_rev"] == 1, page
        assert [r["id"] for r in page["items"]][:3] == [45, 44, 43], "新的必须在前"

        assert len(server.all_items(40, 20)) == 5, "最后一页只应剩 5 条"
        assert server.changed_items(45, 1) == [], "没有任何变化时不该回条目"

        conn = sqlite3.connect(db)
        conn.execute("UPDATE items SET rev=rev+1 WHERE id=7")   # 模拟 translate_db 就地写译文
        conn.commit()
        conn.close()
        changed = server.changed_items(45, 1)
        assert [r["id"] for r in changed] == [7], changed
        assert server.snapshot(since_id=45, since_rev=1)["incremental"] is True

        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO items(id, ts, rev) VALUES(46, 46, 1)")
        conn.commit()
        conn.close()
        assert [r["id"] for r in server.changed_items(45, 2)] == [46], "新条目也要能被捞到"
    finally:
        server.DATA = original_data
        shutil.rmtree(tmp, ignore_errors=True)


def test_launchers():
    """根目录启动器：必须存在、ps1 必须带 BOM（无 BOM 时 PS 5.1 按 GBK 读，中文会变乱码并报语法错）。"""
    root = Path(__file__).resolve().parents[1]
    ps1 = root / "run_dashboard.ps1"
    bat = root / "run_dashboard.bat"
    vbs = root / "run_dashboard.vbs"
    for path in (ps1, bat, vbs):
        assert path.is_file(), f"缺少启动器 {path.name}"
    raw = ps1.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "run_dashboard.ps1 必须存成 UTF-8 带 BOM（Windows PowerShell 5.1 要求）"
    text = raw.decode("utf-8-sig")
    assert "dashboard\\server.py" in text, "启动器没有指向 dashboard\\server.py"
    assert "Start-Process $url" in text, "启动器没有自动打开网址"
    for old in ("run_main_flow.bat", "run_main_flow.ps1", "run_main_flow.vbs"):
        assert not (root / old).exists(), f"旧的 {old} 应该已经删掉"


if __name__ == "__main__":
    test_service_labels()
    test_stop_kills_whole_tree()
    test_pagination_and_changes()
    test_launchers()
    print("dashboard output forwarding + 停止服务杀整棵树 + 分页/增量推送 + 启动器: OK")
