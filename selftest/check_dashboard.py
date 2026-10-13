# -*- coding: utf-8 -*-
"""Dashboard selftest: 服务标签/输出转发 + 停止服务杀整棵树 + 分页与增量推送
+ HTTP API 契约（统一 {ok,data} 信封 / 渠道接口）。
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard.server as server  # noqa: E402


def test_service_labels():
    """分发不再是"一个服务"：它是若干渠道（见 /api/channels）。"""
    assert server.SERVICE_NAMES == {"collect": "采集", "process": "处理"}, server.SERVICE_NAMES
    assert callable(server.pump_service_output)
    assert callable(server.stop_service) and callable(server.stop_channel_process)
    assert callable(server.toggle_channel)


def test_api_envelope():
    """HTTP 层：所有 /api/* 都是 {"ok":true,"data":...} / {"ok":false,"error":...}。"""
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    original_data = server.DATA
    original_processes = dict(server.PROCESSES)
    tmp = Path(__file__).resolve().parent / ".sandbox" / "api_case"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    server.DATA = tmp
    # 渠道模块和分发队列各自持有库路径：不隔离的话这个测试会写到真实 data/hx.sqlite3
    from distribute import push_db  # noqa: E402
    original_channel_db = server.channel_hub.DB_FILE
    original_queue_db = push_db.DB_FILE
    server.channel_hub.DB_FILE = tmp / "hx.sqlite3"
    push_db.DB_FILE = tmp / "hx.sqlite3"
    from distribute.alison.ali_core import env as alison_env, groups as alison_groups
    alison_saved = (server.alison_config.DB_FILE, alison_env.ENV_FILE,
                    alison_groups.DB_FILE, alison_groups.STATE_FILE)
    server.alison_config.DB_FILE = tmp / "hx.sqlite3"
    alison_groups.DB_FILE = tmp / "hx.sqlite3"
    alison_groups.STATE_FILE = tmp / "alison_state.json"
    alison_env.ENV_FILE = tmp / "alison.env"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path, token=True):
        request = urllib.request.Request(base + path)
        if token:
            request.add_header("X-HX-Token", server.token())
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(path, payload, token=True):
        request = urllib.request.Request(
            base + path, data=json.dumps(payload).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("X-HX-Token", server.token())
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    try:
        status, body = get("/api/state")
        assert status == 200 and body["ok"] is True, body
        data = body["data"]
        assert set(data) >= {"services", "channels", "stats", "env_ready", "ws_url"}, list(data)
        assert set(data) >= {"process_progress"} and data["process_progress"]["remaining_count"] == 0, data
        assert set(data["services"]) == {"collect", "process"}, list(data["services"])
        assert set(data["stats"]) >= {"item_count", "pending_count", "latest_id", "latest_rev"}
        assert any(c["id"] == "alison" for c in data["channels"]), data["channels"]

        status, body = get("/api/channels")
        assert status == 200 and body["ok"] and "channels" in body["data"], body

        status, body = get("/api/channels/endpoint")
        assert body["ok"] and body["data"]["ws_url"].startswith("ws://127.0.0.1:"), body
        assert body["data"]["token"] == server.token(), body
        status, body = get("/api/channels/endpoint", token=False)
        assert status == 401 and body["ok"] is False and "token" not in body, body

        status, body = get("/api/pushes")
        assert body["ok"] and isinstance(body["data"]["pushes"], list), body

        status, body = get("/api/snapshot?offset=0&limit=5")
        assert body["ok"] and isinstance(body["data"]["items"], list), body
        status, body = get("/api/process/backlog")
        assert status == 200 and body["ok"] and body["data"]["remaining_count"] == 0, body

        status, body = get("/api/config/alison", token=False)
        assert status == 401 and body["ok"] is False, body
        settings = {"APPID": "demo-id", "SECRET": "demo-secret", "SANDBOX": "true",
                    "ALISON_WITH_IMAGE": False, "ALISON_DISCOVER_GROUPS": True}
        status, body = post("/api/config/alison", {"values": settings})
        assert status == 200 and body["ok"] and "SECRET" in body["data"]["keys"], body
        assert "demo-secret" not in str(body), "配置回包不能带 SECRET"
        status, body = get("/api/config/alison")
        fields = {field["key"]: field["value"] for field in body["data"]["fields"]}
        assert status == 200 and fields["SECRET"] == "demo-secret" and fields["SANDBOX"] == "1", fields
        request = urllib.request.Request(base + "/api/config/alison",
                                         headers={"X-HX-Token": server.token()})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers["Cache-Control"] == "no-store", response.headers
        assert body["data"]["groups"] == [], body["data"]["groups"]
        status, body = post("/api/config/alison/groups/add", {"group_openid": "SAMPLE_GROUP"})
        assert status == 200 and not body["data"]["group"]["push_enabled"], body
        status, body = post("/api/config/alison/groups/toggle",
                            {"group_openid": "SAMPLE_GROUP", "enabled": True})
        assert status == 200 and body["data"]["group"]["push_enabled"], body
        status, body = get("/api/config/alison")
        assert len(body["data"]["groups"]) == 1 and body["data"]["groups"][0]["push_enabled"], body
        status, body = post("/api/config/alison/groups/add", {"group_openid": "bad/relative"})
        assert status == 400 and not body["ok"], body
        status, body = post("/api/config/alison/groups/toggle",
                            {"group_openid": "SAMPLE_GROUP", "enabled": False}, token=False)
        assert status == 401 and not body["ok"], body

        status, body = post("/api/channels/toggle", {"channel": "alison", "enabled": False})
        assert status == 200 and body["ok"] is True, body
        assert body["data"]["enabled"] is False and body["data"]["adapter"] == "process", body
        server.PROCESSES.pop(server.channel_key("alison"), None)   # 别留下假进程

        status, body = post("/api/channels/toggle", {"channel": "nope", "enabled": True})
        assert status == 400 and body["ok"] is False and "未知渠道" in body["error"], body

        status, body = post("/api/service/toggle", {"service": "distribute", "enabled": True})
        assert status == 400 and body["ok"] is False and "未知服务" in body["error"], body

        status, body = post("/api/channels/toggle", {"channel": "alison", "enabled": True},
                            token=False)
        assert status == 401 and body["ok"] is False, body

        status, body = get("/api/does-not-exist")
        assert status == 404 and body["ok"] is False, body
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.DATA = original_data
        server.channel_hub.DB_FILE = original_channel_db
        push_db.DB_FILE = original_queue_db
        (server.alison_config.DB_FILE, alison_env.ENV_FILE,
         alison_groups.DB_FILE, alison_groups.STATE_FILE) = alison_saved
        server.PROCESSES.clear()
        server.PROCESSES.update(original_processes)
        shutil.rmtree(tmp, ignore_errors=True)


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


def test_process_backlog():
    """唯一完整分析队列涵盖缺灯/缺元数据，且已达上限单独计数。"""
    tmp = Path(__file__).resolve().parent / ".sandbox" / "process_backlog_case"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    original_data = server.DATA
    original_attempts = server._max_process_attempts
    server.DATA = tmp
    server._max_process_attempts = lambda: 3
    try:
        conn = sqlite3.connect(tmp / "hx.sqlite3")
        conn.execute("""CREATE TABLE items(
            id INTEGER PRIMARY KEY, origin_id TEXT, account TEXT, raw TEXT, zh TEXT,
            stage TEXT, ai_title TEXT, ai_score INTEGER, ai_tags TEXT,
            is_news INTEGER, not_repeat INTEGER, not_comment INTEGER, not_sensitive INTEGER,
            translate_attempts INTEGER, translate_error TEXT, meta_attempts INTEGER,
            meta_error TEXT, ts INTEGER)""")
        fields = ("id, origin_id, account, raw, zh, stage, ai_title, ai_score, ai_tags, "
                  "is_news, not_repeat, not_comment, not_sensitive, translate_attempts, "
                  "translate_error, meta_attempts, meta_error, ts")
        conn.execute(f"INSERT INTO items({fields}) VALUES(1,'2109939184257695951','Redwire','raw','译文',"
                     "'processed','已有标题',65,'[\"领域:发射\"]',1,NULL,1,1,1,'',2,'',100)")
        conn.execute(f"INSERT INTO items({fields}) VALUES(2,'done','NASA','raw','译文',"
                     "'processed','标题',80,'[\"领域:任务\"]',1,1,1,1,1,'',0,'',90)")
        conn.execute(f"INSERT INTO items({fields}) VALUES(3,'stuck','NASA','raw','译文',"
                     "'processed','标题',NULL,'[\"领域:任务\"]',1,1,1,1,3,'格式无效',0,'',80)")
        conn.execute(f"INSERT INTO items({fields}) VALUES(4,'untranslated','X','raw','',"
                     "'collected','',NULL,'',NULL,NULL,NULL,NULL,0,'',0,'',70)")
        conn.commit()
        conn.close()

        result = server.process_backlog()
        assert result["remaining_count"] == 3, result
        assert result["ready_count"] == 2 and result["stuck_count"] == 1, result
        by_id = {item["id"]: item for item in result["items"]}
        assert "重复" in by_id[1]["missing"] and by_id[1]["phase"] == "待完整分析" and by_id[1]["attempts"] == 1, by_id[1]
        assert by_id[3]["stuck"] and "AI分" in by_id[3]["missing"], by_id[3]
        assert by_id[4]["phase"] == "待完整分析", by_id[4]
        summary = server.process_backlog(include_items=False)
        assert summary == {"items": [], "remaining_count": 3, "ready_count": 2, "stuck_count": 1}, summary
    finally:
        server.DATA = original_data
        server._max_process_attempts = original_attempts
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
    assert "--ws-port" in text and "$wsPort = $Port + 1" in text, "启动器没有同步启动渠道 WS 端口"
    assert "& $python @serverArgs" in text and '"--open-browser"' in text, \
        "服务器必须在当前 BAT 终端前台运行，浏览器应在端口绑定后打开"
    assert "Start-Process -FilePath $python" not in text and "$env:ComSpec" not in text, \
        "不要分离服务器进程：关闭 BAT 终端必须能停止它"
    assert "[Console]::OutputEncoding" in text and 'PYTHONIOENCODING = "utf-8"' in text, \
        "PowerShell/Python 输出编码必须固定为 UTF-8"
    assert "Start-Process $url" in text, "已在运行时未自动打开网址"

    bat_raw = bat.read_bytes()
    assert bat_raw and all(byte < 128 for byte in bat_raw), "run_dashboard.bat 必须是纯 ASCII"
    assert b"\r\n" in bat_raw and bat_raw.count(b"\n") == bat_raw.count(b"\r\n"), \
        "run_dashboard.bat 必须使用 CRLF；LF-only 会让 cmd.exe 把多行粘在一起执行"
    bat_text = bat_raw.decode("ascii")
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run_dashboard.ps1" %*' in bat_text
    assert "chcp 65001 >nul" in bat_text, "启动器没有切换到 UTF-8 代码页"

    for old in ("run_main_flow.bat", "run_main_flow.ps1", "run_main_flow.vbs"):
        assert not (root / old).exists(), f"旧的 {old} 应该已经删掉"


if __name__ == "__main__":
    test_service_labels()
    test_api_envelope()
    test_stop_kills_whole_tree()
    test_pagination_and_changes()
    test_process_backlog()
    test_launchers()
    print("dashboard 服务标签 + API 契约 + 停止服务杀整棵树 + 分页/增量推送 + 启动器: OK")
