"""共享图片库：数据库特征检索、稳定路径、可恢复的原子升级。"""
import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
import zlib
from pathlib import Path
from functools import lru_cache

import cv2
import numpy as np
from collect.paths import DB_FILE, IMAGE_DIR, LOG_DIR
from collect.logging_util import make_log

log = make_log(LOG_DIR / 'image_store.log')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS image_assets(
 id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,sha256 TEXT NOT NULL,mime TEXT NOT NULL,
 width INTEGER,height INTEGER,byte_size INTEGER,quality REAL,phash TEXT,preview BLOB,
 version INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS image_signatures(sha256 TEXT PRIMARY KEY,asset_id TEXT NOT NULL,phash TEXT);
CREATE INDEX IF NOT EXISTS image_signatures_asset ON image_signatures(asset_id);
CREATE TABLE IF NOT EXISTS image_hash_buckets(band INTEGER,value INTEGER,asset_id TEXT,
 PRIMARY KEY(band,value,asset_id));
CREATE TABLE IF NOT EXISTS image_refs(item_id INTEGER,asset_id TEXT,PRIMARY KEY(item_id,asset_id));
CREATE INDEX IF NOT EXISTS image_refs_asset ON image_refs(asset_id,item_id);
CREATE TABLE IF NOT EXISTS image_pending(id TEXT PRIMARY KEY,temporary TEXT NOT NULL,metadata TEXT NOT NULL);
'''
_thread_lock = threading.RLock()


def atomic_replace(source,target):
    """Windows 索引或杀毒软件短暂占用文件时，保留原文件并重试替换。"""
    for attempt in range(30):
        try: os.replace(source,target);return
        except PermissionError:
            if attempt==29: raise
            time.sleep(.1)


def encoded_type(data):
    if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg', '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png', '.png'
    if data.startswith((b'GIF87a', b'GIF89a')): return 'image/gif', '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'image/webp', '.webp'
    if b'ftyp' in data[:16] and b'avif' in data[:40]: return 'image/avif', '.avif'
    if b'<svg' in data[:1024]: return 'image/svg+xml', '.svg'
    return 'application/octet-stream', '.bin'


def features(data):
    mime, _ = encoded_type(data)
    result = dict(sha256=hashlib.sha256(data).hexdigest(), mime=mime, width=0, height=0,
                  byte_size=len(data), quality=0.0, phash='', preview='')
    # 动画、矢量图只按原始字节去重，避免第一帧相同就丢失后续内容。
    if mime in ('image/gif', 'image/svg+xml', 'application/octet-stream') or (mime=='image/webp' and b'ANIM' in data[:1024]) or (mime=='image/png' and b'acTL' in data[:1024]):
        return result
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None: return result
    result.update(height=image.shape[0],width=image.shape[1])
    if image.dtype != np.uint8: return result
    if image.ndim == 2: image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    # 透明图不参与近似合并，避免忽略透明度的区别。
    if image.shape[2] == 4: return result
    image = cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dct = cv2.dct(cv2.resize(gray, (32,32), interpolation=cv2.INTER_AREA).astype(np.float32))[:8,:8]
    bits = dct.flatten() > np.median(dct.flatten()[1:])
    bits[0] = False
    phash = ''.join(f'{byte:02x}' for byte in np.packbits(bits))
    preview = cv2.resize(image, (128,128), interpolation=cv2.INTER_AREA)
    normalized = cv2.resize(gray, (1024,1024), interpolation=cv2.INTER_AREA if min(width,height)>=1024 else cv2.INTER_LINEAR)
    normalized = cv2.GaussianBlur(normalized, (5,5), 1.0)
    laplacian=cv2.Laplacian(normalized,cv2.CV_32F)
    smooth=cv2.GaussianBlur(normalized,(0,0),4)
    gradient=cv2.magnitude(cv2.Sobel(smooth,cv2.CV_32F,1,0),cv2.Sobel(smooth,cv2.CV_32F,0,1))
    flat=gradient<=np.percentile(gradient,20)
    # 平坦区域的高频变化主要是噪声，不能把它当作新增细节。
    noise_energy=float(np.mean(laplacian[flat]**2))
    quality = max(0.0,float(laplacian.var())-1.5*noise_energy)
    result.update(width=width,height=height,quality=quality,phash=phash,preview=(b'HXI1'+zlib.compress(preview.tobytes(),6)).hex())
    return result


def same_picture(new, old):
    if not new['phash'] or not old['phash']: return False
    if (int(new['phash'],16)^int(old['phash'],16)).bit_count()>6: return False
    if abs(new['width']/new['height'] - old['width']/old['height']) / (old['width']/old['height']) > .005: return False
    def unpack(value): return zlib.decompress(value[4:]) if value.startswith(b'HXI1') else value
    a=np.frombuffer(unpack(bytes.fromhex(new['preview'])),np.uint8).reshape(128,128,3)
    b=np.frombuffer(unpack(old['preview']),np.uint8).reshape(128,128,3)
    # 降噪后比较每个局部块；文字、水印、裁剪变化不能被全图平均值掩盖。
    a=cv2.GaussianBlur(a,(5,5),1).astype(np.float32)
    b=cv2.GaussianBlur(b,(5,5),1).astype(np.float32)
    if float(np.abs(a-b).mean())>2.0: return False
    for y in range(0,128,16):
        for x in range(0,128,16):
            aa=a[y:y+16,x:x+16]; bb=b[y:y+16,x:x+16]
            if float(np.abs(aa-bb).mean())>5.0: return False
            ma,mb=float(aa.mean()),float(bb.mean())
            va,vb=float(aa.var()),float(bb.var())
            covariance=float(((aa-ma)*(bb-mb)).mean())
            ssim=((2*ma*mb+6.5025)*(2*covariance+58.5225))/((ma*ma+mb*mb+6.5025)*(va+vb+58.5225))
            if ssim < .96: return False
    return True


def better_picture(new,old):
    pixels=new['width']*new['height']; previous=old['width']*old['height']
    if not previous or not pixels: return False
    return ((pixels>=previous and new['quality']>max(old['quality']*1.10,old['quality']+.05))
            or (pixels>=previous*1.3 and new['quality']>max(old['quality']*1.05,old['quality']+.05)))


class ImageStore:
    def __init__(self, database=None, image_root=None):
        self.database=Path(database or DB_FILE)
        self.root=Path(image_root or IMAGE_DIR).resolve()
        self.library=self.root/'library'
        self.library.mkdir(parents=True,exist_ok=True)
        self.database.parent.mkdir(parents=True,exist_ok=True)
        with contextlib.closing(self.connect()) as conn: conn.executescript(SCHEMA); conn.commit()
        with self.locked(),contextlib.closing(self.connect()) as conn: self.recover(conn)

    def connect(self):
        conn=sqlite3.connect(self.database,timeout=30)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    @contextlib.contextmanager
    def locked(self):
        with _thread_lock:
            with (self.library/'.write.lock').open('a+b') as handle:
                handle.seek(0,2)
                if not handle.tell(): handle.write(b'0'); handle.flush()
                deadline=time.monotonic()+60
                while True:
                    try:
                        handle.seek(0)
                        if os.name=='nt':
                            import msvcrt
                            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic()>=deadline: raise TimeoutError('图片库写锁等待超时')
                        time.sleep(.05)
                try: yield
                finally:
                    handle.seek(0)
                    if os.name=='nt': msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                    else: fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    def _finalize(self,conn,ident,metadata):
        old=conn.execute('SELECT version FROM image_assets WHERE id=?',(ident,)).fetchone()
        preview=bytes.fromhex(metadata['preview'])
        path=str(self.library/(ident+'.img'))
        conn.execute('''INSERT INTO image_assets(id,path,sha256,mime,width,height,byte_size,quality,phash,preview,version,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sha256=excluded.sha256,mime=excluded.mime,
          width=excluded.width,height=excluded.height,byte_size=excluded.byte_size,quality=excluded.quality,
          phash=excluded.phash,preview=excluded.preview,version=excluded.version,updated_at=excluded.updated_at''',
          (ident,path,metadata['sha256'],metadata['mime'],metadata['width'],metadata['height'],metadata['byte_size'],
           metadata['quality'],metadata['phash'],preview,old['version']+1 if old else 1,time.time()))
        self._signature(conn,ident,metadata)
        if old and conn.execute("SELECT 1 FROM sqlite_master WHERE name='items'").fetchone():
            conn.execute('UPDATE items SET rev=rev+1 WHERE id IN(SELECT item_id FROM image_refs WHERE asset_id=?)',(ident,))
        conn.execute('DELETE FROM image_pending WHERE id=?',(ident,))
        conn.commit()

    def _signature(self,conn,ident,metadata):
        conn.execute('INSERT OR IGNORE INTO image_signatures VALUES(?,?,?)',(metadata['sha256'],ident,metadata['phash']))
        for band,value in enumerate(bytes.fromhex(metadata['phash'])):
            conn.execute('INSERT OR IGNORE INTO image_hash_buckets VALUES(?,?,?)',(band,value,ident))

    def recover(self,conn):
        for row in conn.execute('SELECT * FROM image_pending').fetchall():
            metadata=json.loads(row['metadata'])
            temporary=Path(row['temporary']); target=self.library/(row['id']+'.img')
            temporary.resolve().relative_to(self.library)
            if temporary.exists(): atomic_replace(temporary,target)
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=metadata['sha256']:
                raise RuntimeError('图片升级尚未恢复，停止写入以保护原图')
            self._finalize(conn,row['id'],metadata)

    def candidate(self,conn,metadata):
        exact=conn.execute('SELECT a.* FROM image_signatures s JOIN image_assets a ON a.id=s.asset_id WHERE s.sha256=?',(metadata['sha256'],)).fetchone()
        if exact and (Path(exact['path']).is_file() or exact['sha256']==metadata['sha256']): return exact
        if not metadata['phash']: return None
        votes={}
        for band,value in enumerate(bytes.fromhex(metadata['phash'])):
            for row in conn.execute('SELECT asset_id FROM image_hash_buckets WHERE band=? AND value=?',(band,value)):
                votes[row[0]]=votes.get(row[0],0)+1
        matches=[]
        for ident,count in votes.items():
            if count<2: continue
            brief=conn.execute('SELECT phash,width,height FROM image_assets WHERE id=?',(ident,)).fetchone()
            if not brief or not brief['phash'] or (int(brief['phash'],16)^int(metadata['phash'],16)).bit_count()>6: continue
            old=conn.execute('SELECT * FROM image_assets WHERE id=?',(ident,)).fetchone()
            if old and Path(old['path']).is_file() and same_picture(metadata,old): matches.append(old)
        # 有多个候选时先选最接近者，避免不同图片组被串联成一组。
        return min(matches,key=lambda r:(int(r['phash'],16)^int(metadata['phash'],16)).bit_count()) if matches else None

    def store(self,data, fallback_path=None, prepared=None):
        metadata=prepared or features(data)
        with self.locked(),contextlib.closing(self.connect()) as conn:
            self.recover(conn)
            old=self.candidate(conn,metadata)
            if old and Path(old['path']).is_file() and (old['sha256']==metadata['sha256'] or not better_picture(metadata,old)):
                self._signature(conn,old['id'],metadata); conn.commit()
                return old['path']
            ident=old['id'] if old else uuid.uuid4().hex
            with tempfile.NamedTemporaryFile(dir=self.library,suffix='.pending',delete=False) as handle:
                temporary=Path(handle.name); handle.write(data); handle.flush(); os.fsync(handle.fileno())
            # 二维码在任何新图进入共享池前过滤；临时文件不会成为新闻引用。
            from collect.image_filter import filter_image_paths
            if not filter_image_paths([str(temporary)]): temporary.unlink(); return ''
            conn.execute('INSERT INTO image_pending VALUES(?,?,?)',(ident,str(temporary),json.dumps(metadata)))
            conn.commit()
            atomic_replace(temporary,self.library/(ident+'.img'))
            self._finalize(conn,ident,metadata)
            log(f'[图片库] {"高清升级" if old else "新增"} {ident} {metadata["width"]}x{metadata["height"]}')
            return str(self.library/(ident+'.img'))


def sync_refs(conn,item_id,paths):
    conn.execute('DELETE FROM image_refs WHERE item_id=?',(item_id,))
    for path in paths:
        conn.execute('INSERT OR IGNORE INTO image_refs SELECT ?,id FROM image_assets WHERE path=?',(item_id,str(path)))


def store_image_bytes(data,fallback_path=None,database=None,image_root=None):
    return ImageStore(database,image_root).store(data,fallback_path)


@lru_cache(maxsize=2048)
def _file_features(path,modified,size):
    return features(Path(path).read_bytes())


def deduplicate_message_images(paths,database=None):
    """同一条消息只保留同图的最佳版本，不保存重复图片文件。"""
    if not isinstance(paths,(list,tuple)): return []
    indexed={}
    # 只有采集/看板端传入数据库；渠道端仅使用已经下发的图片与本地缓存。
    if database is not None and Path(database).is_file():
        with contextlib.closing(sqlite3.connect(database,timeout=10)) as conn:
            conn.row_factory=sqlite3.Row
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='image_assets'").fetchone():
                for value in paths:
                    row=conn.execute('SELECT * FROM image_assets WHERE path=?',(str(value),)).fetchone()
                    if row:
                        metadata=dict(row);metadata['preview']=row['preview'].hex()
                        indexed[str(value)]=metadata
    groups=[]
    for value in paths:
        path=str(value).strip()
        if not path or any(path==entry[0] for entry in groups): continue
        try:
            metadata=indexed.get(path)
            if metadata is None:
                file=Path(path);stat=file.stat()
                metadata=_file_features(str(file.resolve()),stat.st_mtime_ns,stat.st_size)
        except (OSError,ValueError): metadata=None
        match=None
        if metadata:
            for i,(_,previous) in enumerate(groups):
                if previous and (metadata['sha256']==previous['sha256'] or same_picture(metadata,{**previous,'preview':bytes.fromhex(previous['preview'])})):
                    match=i;break
        if match is None: groups.append((path,metadata))
        elif better_picture(metadata,groups[match][1]): groups[match]=(path,metadata)
    return [path for path,_ in groups]
