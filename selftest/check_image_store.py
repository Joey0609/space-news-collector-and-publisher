"""共享图片库离线验收，不抓取或发送。"""
import contextlib
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
import numpy as np
from collect.image_store import ImageStore, features, sync_refs, same_picture, better_picture
from collect.logging_util import setup_console_encoding
setup_console_encoding()


def picture(size=512):
    y,x=np.mgrid[0:size,0:size].astype(np.float32)/size
    image=np.stack([50+100*x,70+90*y,80+80*x*y],axis=2).astype(np.uint8)
    cv2.circle(image,(int(size*.65),int(size*.35)),int(size*.17),(90,175,220),-1)
    cv2.rectangle(image,(int(size*.1),int(size*.45)),(int(size*.42),int(size*.83)),(165,90,70),-1)
    cv2.line(image,(int(size*.15),int(size*.2)),(int(size*.8),int(size*.85)),(205,190,170),max(1,int(size*.008)))
    return image


def encoded(image,extension='.jpg',quality=95):
    ok,data=cv2.imencode(extension,image,[cv2.IMWRITE_JPEG_QUALITY,quality] if extension=='.jpg' else [])
    assert ok
    return data.tobytes()


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=self.root/'data/hx.sqlite3';self.images=self.root/'data/images'
        self.store=ImageStore(self.db,self.images)
        with contextlib.closing(self.store.connect()) as c:
            c.execute('CREATE TABLE items(id INTEGER PRIMARY KEY, images TEXT, rev INTEGER DEFAULT 1)');c.commit()
        self.filter=patch('collect.image_filter.filter_image_paths',side_effect=lambda paths:paths);self.filter.start()
    def tearDown(self): self.filter.stop();self.temp.cleanup()
    def asset(self,path):
        with contextlib.closing(self.store.connect()) as c: return dict(c.execute('SELECT * FROM image_assets WHERE path=?',(path,)).fetchone())
    def test_exact_compression_and_local_changes(self):
        image=picture();high=encoded(image);path=self.store.store(high)
        self.assertEqual(path,self.store.store(high))
        self.assertEqual(path,self.store.store(encoded(image,quality=85)))
        changed=image.copy();cv2.putText(changed,'NEW TEXT',(40,140),cv2.FONT_HERSHEY_SIMPLEX,1.5,(0,0,0),4)
        self.assertNotEqual(path,self.store.store(encoded(changed)))
        self.assertNotEqual(path,self.store.store(encoded(image[20:-20,20:-20])))
    def test_upgrade_stable_path_original_encoding_and_refs(self):
        high=picture();low=cv2.resize(high,(128,128),interpolation=cv2.INTER_AREA)
        path=self.store.store(encoded(low,quality=85));initial=self.asset(path)
        with contextlib.closing(self.store.connect()) as c:
            c.executemany('INSERT INTO items(id,images) VALUES(?,?)',[(1,json.dumps([path])),(2,json.dumps([path]))])
            sync_refs(c,1,[path]);sync_refs(c,2,[path]);c.commit()
        data=encoded(high,'.png')
        self.assertEqual(path,self.store.store(data))
        updated=self.asset(path)
        self.assertGreater(updated['version'],initial['version'])
        self.assertEqual(Path(path).read_bytes(),data)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual([r[0] for r in c.execute('SELECT rev FROM items ORDER BY id')],[2,2])
        enlarged=cv2.resize(low,(1024,1024),interpolation=cv2.INTER_LINEAR)
        before=Path(path).read_bytes();self.store.store(encoded(enlarged))
        self.assertEqual(before,Path(path).read_bytes())
    def test_recovery_after_file_replace_before_commit(self):
        path=self.store.store(encoded(cv2.resize(picture(),(128,128),interpolation=cv2.INTER_AREA)));data=encoded(picture(),'.png')
        with patch.object(self.store,'_finalize',side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError): self.store.store(data)
        repaired=ImageStore(self.db,self.images)
        self.assertEqual(Path(path).read_bytes(),data)
        with contextlib.closing(repaired.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM image_pending').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT sha256 FROM image_assets WHERE path=?',(path,)).fetchone()[0],hashlib.sha256(data).hexdigest())
    def test_concurrency_and_no_historical_image_decode(self):
        data=encoded(picture())
        with ThreadPoolExecutor(4) as pool: paths=list(pool.map(lambda _:self.store.store(data),range(8)))
        self.assertEqual(len(set(paths)),1)
        with patch('pathlib.Path.read_bytes',side_effect=AssertionError('historical image read')):
            self.assertEqual(self.store.store(data),paths[0])
            self.assertEqual(self.store.store(encoded(picture(),quality=85)),paths[0])
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0],1)
    def test_process_concurrency(self):
        source=self.root/'input.jpg';source.write_bytes(encoded(picture()))
        processes=[subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--insert',str(self.db),str(self.images),str(source)],stdout=subprocess.DEVNULL) for _ in range(3)]
        self.assertEqual([p.wait(timeout=30) for p in processes],[0,0,0])
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0],1)
    def test_plain_upscale_does_not_replace(self):
        low=cv2.resize(picture(),(128,128),interpolation=cv2.INTER_AREA)
        data=encoded(low);path=self.store.store(data)
        decoded=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
        self.assertEqual(path,self.store.store(encoded(cv2.resize(decoded,(512,512),interpolation=cv2.INTER_LINEAR))))
        self.assertEqual(Path(path).read_bytes(),data)
    def test_noise_does_not_replace(self):
        image=picture();data=encoded(image,'.png');path=self.store.store(data)
        noisy=np.clip(image.astype(np.float32)+np.random.default_rng(7).normal(0,6,image.shape),0,255).astype(np.uint8)
        self.store.store(encoded(noisy,'.png'))
        self.assertEqual(Path(path).read_bytes(),data)
    def test_same_message_best_only_and_low_duplicate_no_file(self):
        from collect.image_store import deduplicate_message_images
        image=picture();low=self.root/'low.jpg';high=self.root/'high.png'
        low.write_bytes(encoded(cv2.resize(image,(128,128),interpolation=cv2.INTER_AREA)))
        high.write_bytes(encoded(image,'.png'))
        self.assertEqual(deduplicate_message_images([str(low),str(high),str(low),str(high)]),[str(high)])
        path=self.store.store(high.read_bytes())
        with patch('collect.image_store.tempfile.NamedTemporaryFile',side_effect=AssertionError('low duplicate written')):
            self.assertEqual(self.store.store(low.read_bytes()),path)
    def test_alison_text_first_images_separate_and_resume(self):
        from distribute.alison.ali_core import delivery
        a=self.root/'a.jpg';b=self.root/'b.jpg';a.write_bytes(b'first');b.write_bytes(b'second')
        sent=[];uploads=[];item={'id':1,'rev':1}
        def upload(path): uploads.append(path);return Path(path).name
        def send(text,info):
            sent.append((text,info));return info!='b.jpg'
        with patch.object(delivery,'STATE',self.root/'progress.json'),patch('collect.image_filter.filter_image_paths',side_effect=lambda paths:paths):
            self.assertFalse(delivery.deliver_all_images(item,'alison:group_test','正文\n日期',[str(a),str(b)],upload,send))
            self.assertEqual(sent,[('正文\n日期',''),('','a.jpg'),('','b.jpg')])
            sent.clear()
            self.assertTrue(delivery.deliver_all_images(item,'alison:group_test','正文\n日期',[str(a),str(b)],upload,lambda text,info:sent.append((text,info)) or True))
            self.assertEqual(sent,[('','b.jpg')])
    def test_http_real_mime_and_cache_change(self):
        import threading,urllib.request
        from http.server import ThreadingHTTPServer
        from dashboard import server
        path=self.store.store(encoded(cv2.resize(picture(),(128,128),interpolation=cv2.INTER_AREA)))
        with patch.object(server,'DATA',self.root/'data'):
            httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            threading.Thread(target=httpd.serve_forever,daemon=True).start()
            try:
                url=f'http://127.0.0.1:{httpd.server_port}/api/image/library/{Path(path).name}'
                with urllib.request.urlopen(url+'?v=1') as r:
                    self.assertEqual(r.headers['Content-Type'],'image/jpeg');etag=r.headers['ETag'];r.read()
                self.store.store(encoded(picture(),'.png'))
                with urllib.request.urlopen(url+'?v=2') as r:
                    self.assertEqual(r.headers['Content-Type'],'image/png');self.assertNotEqual(etag,r.headers['ETag']);r.read()
            finally: httpd.shutdown();httpd.server_close()
    def test_standalone_old_tool_dry_run_apply_resume(self):
        from tools.compact_images import compact
        low=self.images/'a.jpg';high=self.images/'b.png';low.write_bytes(encoded(cv2.resize(picture(),(128,128),interpolation=cv2.INTER_AREA)));high.write_bytes(encoded(picture(),'.png'))
        state=self.root/'data/state';state.mkdir(parents=True);baseline=state/'baseline.json';baseline.write_bytes(b'unchanged')
        with contextlib.closing(self.store.connect()) as c:
            c.execute('INSERT INTO items VALUES(1,?,1)',(json.dumps([str(low),str(high)]),));c.commit()
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        preview=compact(self.root)
        self.assertGreater(preview['duplicates'],0);self.assertTrue(low.exists());self.assertTrue(high.exists())
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())
        report=compact(self.root,True);self.assertGreater(report['bytes_freed'],0)
        self.assertFalse(low.exists());self.assertFalse(high.exists());self.assertEqual(baseline.read_bytes(),b'unchanged')
        with contextlib.closing(self.store.connect()) as c:
            paths=json.loads(c.execute('SELECT images FROM items WHERE id=1').fetchone()[0]);self.assertEqual(len(paths),1);self.assertTrue(Path(paths[0]).is_file())
        compact(self.root,True)


if __name__=='__main__':
    if '--insert' in sys.argv:
        ImageStore(sys.argv[2],sys.argv[3]).store(Path(sys.argv[4]).read_bytes())
    else: unittest.main()
