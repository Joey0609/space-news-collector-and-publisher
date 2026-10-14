# -*- coding: utf-8 -*-
"""隔离 .env 的多接入点 HTTP 与热切换自检，不调用真实模型。"""
import json
import sys
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard import server
from process import model_endpoints


def fixture(directory):
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath('.env').write_text(
        'MODEL_PROTOCOL=openai\nMODEL_API_BASE=https://example.invalid/v1\n'
        'MODEL_NAME=model-a\nMODEL_API_KEY=offline-key\nKEEP_THIS=keep\n', encoding='utf-8')
    server.ROOT = directory
    server.STATE = directory / 'state'
    server.TOKEN_FILE = server.STATE / 'token.txt'
    server.AUDIT = server.STATE / 'audit.jsonl'
    server.DATA = directory / 'data'
    server.PROCESS_PROGRESS_FILE = server.STATE / 'progress.json'
    server.channel_hub.DB_FILE = server.DATA / 'hx.sqlite3'
    from distribute import push_db
    push_db.DB_FILE = server.DATA / 'hx.sqlite3'
    server.DATA.mkdir(exist_ok=True)
    from distribute.alison.ali_core import env as alison_env, groups as alison_groups
    alison_env.ENV_FILE = directory / 'alison.env'
    alison_groups.DB_FILE = server.DATA / 'hx.sqlite3'
    alison_groups.STATE_FILE = server.STATE / 'alison-state.json'
    server.alison_config.DB_FILE = server.DATA / 'hx.sqlite3'


def run_checks():
    with tempfile.TemporaryDirectory(prefix='hx-ai-config-') as directory:
        fixture(Path(directory))
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{httpd.server_port}'

        def request(path, payload=None, auth=True):
            headers = {'X-HX-Token': server.token()} if auth else {}
            req = urllib.request.Request(base + path, headers=headers,
                data=json.dumps(payload).encode() if payload is not None else None)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)

        try:
            assert request('/api/config/ai', auth=False)[0] == 401
            assert request('/api/config/ai', {}, auth=False)[0] == 401
            status, body = request('/api/config/ai')
            data = body['data']
            assert status == 200 and data['active_id'] == 'legacy'
            assert 'offline-key' not in json.dumps(body)
            first = data['endpoints'][0]
            first['name'] = '原接入点'
            second = {**first, 'id': 'second', 'name': "备用'模型", 'MODEL_NAME': 'model-b',
                      'MODEL_API_BASE': 'https://second.invalid/v1',
                      'MODEL_API_KEY': r"secret\path' # ${VALUE}中文"}
            data['endpoints'].append(second)
            status, body = request('/api/config/ai', data)
            assert status == 200, body
            saved = body['data']
            assert saved['active_id'] == 'legacy', '保存编辑不得切换'
            rows, active = model_endpoints.read_settings(server.ROOT / '.env')
            assert rows[0]['MODEL_API_KEY'] == 'offline-key'
            assert rows[1]['MODEL_API_KEY'] == second['MODEL_API_KEY'], '特殊字符密钥必须无损保存'
            assert rows[1]['name'] == second['name']
            assert model_endpoints.read_env(server.ROOT / '.env')['KEEP_THIS'] == 'keep'
            assert request('/api/config/ai', data)[0] == 400, '旧版本不能覆盖配置'

            sys.path.insert(0, str(ROOT / 'process'))
            import config
            import ai
            config.ROOT = server.ROOT
            config._model_stamp = None
            seen = []
            ai._HANDLERS['openai'] = lambda prompt, temperature, model: seen.append((model, ai.MODEL_API_BASE)) or 'offline'
            assert ai.chat('offline') == 'offline'
            assert seen[-1][0] == 'model-a'
            class CachedClient:
                closed = False

                def close(self):
                    self.closed = True

            cached = CachedClient()
            ai._openai_client = cached
            status, body = request('/api/config/ai/use', {**saved, 'id': 'second'})
            assert status == 200, body
            saved = body['data']
            assert saved['active_id'] == 'second'
            ai.chat('offline')
            assert seen[-1] == ('model-b', 'https://second.invalid/v1'), '同一进程的下一请求必须切换'
            assert cached.closed and ai._openai_client is None, '切换时必须释放旧模型客户端'
            assert config.MODEL_API_KEY == second['MODEL_API_KEY']
            active_key = second['MODEL_API_KEY']

            third = {**saved['endpoints'][1], 'id': 'third', 'name': '复制项', 'copy_from': 'second'}
            saved['endpoints'].append(third)
            status, body = request('/api/config/ai', saved)
            assert status == 200, body
            saved = body['data']
            rows, _ = model_endpoints.read_settings(server.ROOT / '.env')
            assert rows[2]['MODEL_API_KEY'] == active_key
            assert active_key not in json.dumps(body)
            assert 'copy_from' not in saved['endpoints'][2]
            deleted = {**saved, 'endpoints': [row for row in saved['endpoints'] if row['id'] != 'second']}
            assert request('/api/config/ai', deleted)[0] == 400
            blank = {**saved['endpoints'][0], 'id': 'blank', 'name': '未配置', 'MODEL_API_BASE': '', 'MODEL_NAME': ''}
            assert request('/api/config/ai/use', {**saved, 'id': 'blank', 'endpoints': [*saved['endpoints'], blank]})[0] == 400
            assert request('/api/config/ai/use', {**saved, 'id': 'missing'})[0] == 400
            bad = {**saved, 'endpoints': [{**row, 'MODEL_TIMEOUT': 'nan'} for row in saved['endpoints']]}
            assert request('/api/config/ai', bad)[0] == 400
            assert request('/api/config', {'service': 'process', 'values': {'ANALYSIS_MAX_ATTEMPTS': '0'}})[0] == 400
            assert request('/api/config', {'service': 'process', 'values': {'ANALYSIS_PAUSE_SECONDS': '0'}})[0] == 200
            assert 'secret' not in server.AUDIT.read_text(encoding='utf-8')
            fields = request('/api/config/process')[1]['data']['fields']
            assert len(fields) == 4 and not any(row['key'].startswith('MODEL_') or row['key'] == 'ANALYSIS_PAUSE_SECONDS' for row in fields)
            print('通过：鉴权、旧配置迁移、保存不切换、密钥无损保留/复制、版本冲突、使用切换及同进程刷新、删除与参数校验。')
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    if '--serve' in sys.argv:
        preview = ROOT / 'selftest' / '.sandbox' / 'ai-preview'
        fixture(preview)
        server.TOKEN_FILE.parent.mkdir(exist_ok=True)
        server.TOKEN_FILE.write_text('offline-ui-preview', encoding='utf-8')
        # 仅 HTTP 界面与隔离配置，不启动采集、处理、渠道进程。
        ThreadingHTTPServer(('127.0.0.1', 8799), server.Handler).serve_forever()
    else:
        run_checks()
