"""按顺序发送全部配图；只有正文及每张图均成功才返回成功。"""
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

STATE = Path(__file__).resolve().parents[3] / 'data' / 'state' / 'alison_delivery_progress.json'
LOCK = threading.Lock()


def deliver_all_images(item, source, text, images, upload, send):
    from collect.image_filter import filter_image_paths
    images = filter_image_paths(images)
    identity = json.dumps([source, item.get('id'), item.get('rev'), text, images], ensure_ascii=False)
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    with LOCK:
        try:
            progress = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
        except (OSError, ValueError):
            progress = {}
        entry = progress.get(key, {'count': 0, 'text_sent': False})

        def checkpoint():
            entry['updated_at'] = time.time()
            progress[key] = entry
            if len(progress) > 1000:
                for old in sorted(progress, key=lambda k: progress[k].get('updated_at', 0))[:len(progress)-1000]:
                    del progress[old]
            STATE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=STATE.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(progress, handle, ensure_ascii=False)
            try: os.replace(temporary, STATE)
            finally: temporary.unlink(missing_ok=True)

        try:
            if not images:
                if not entry['text_sent']:
                    if not send(text, ''): return False
                    entry['text_sent'] = True
                    checkpoint()
                return True
            for index in range(entry['count'], len(images)):
                file_info = upload(images[index])
                if not file_info: return False
                # 第一条包含正文和日期，其后的消息只追加配图。
                if not send(text if not entry['text_sent'] else '', file_info): return False
                entry.update(text_sent=True, count=index+1)
                checkpoint()
            return entry['text_sent'] and entry['count'] == len(images)
        except Exception:
            return False
