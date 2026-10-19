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
CREATE TABLE IF NOT EXISTS image_crop_signatures(asset_id TEXT NOT NULL,phash TEXT NOT NULL,
 PRIMARY KEY(asset_id,phash));
CREATE INDEX IF NOT EXISTS image_crop_signatures_hash ON image_crop_signatures(phash,asset_id);
CREATE TABLE IF NOT EXISTS image_crop_buckets(band INTEGER,value INTEGER,phash TEXT,asset_id TEXT,
 PRIMARY KEY(band,value,phash,asset_id));
CREATE INDEX IF NOT EXISTS image_crop_buckets_lookup ON image_crop_buckets(band,value,asset_id);
CREATE TABLE IF NOT EXISTS image_local_features(asset_id TEXT PRIMARY KEY,points BLOB NOT NULL,
 descriptors BLOB NOT NULL,point_count INTEGER NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL);
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


def _phash(gray):
    dct = cv2.dct(cv2.resize(gray, (32,32), interpolation=cv2.INTER_AREA).astype(np.float32))[:8,:8]
    bits = dct.flatten() > np.median(dct.flatten()[1:])
    bits[0] = False
    return ''.join(f'{byte:02x}' for byte in np.packbits(bits))


def _crop_hashes(gray):
    height, width = gray.shape[:2]
    hashes = {_phash(gray)}
    for fraction in (.70, .90):
        crop_w=max(32,round(width*fraction)); crop_h=max(32,round(height*fraction))
        xs=sorted({0,max(0,(width-crop_w)//2),max(0,width-crop_w)})
        ys=sorted({0,max(0,(height-crop_h)//2),max(0,height-crop_h)})
        for y in ys:
            for x in xs:
                hashes.add(_phash(gray[y:y+crop_h,x:x+crop_w]))
    return sorted(hashes)


def _pack_features(points, descriptors):
    if points is None or descriptors is None or not len(points):
        return '', ''
    packed_points=zlib.compress(np.asarray(points,dtype='<f4').tobytes(),6)
    packed_descriptors=zlib.compress(np.asarray(descriptors,dtype=np.uint8).tobytes(),6)
    return packed_points.hex(),packed_descriptors.hex()


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
    phash = _phash(gray)
    crop_hashes = _crop_hashes(gray)
    scale=min(1.0,1200.0/max(height,width))
    feature_gray=cv2.resize(gray,(max(1,round(width*scale)),max(1,round(height*scale))),interpolation=cv2.INTER_AREA) if scale<1 else gray
    detector=cv2.ORB_create(nfeatures=1200,scaleFactor=1.2,nlevels=8,fastThreshold=8)
    keypoints,descriptors=detector.detectAndCompute(feature_gray,None)
    if keypoints and descriptors is not None:
        points=np.asarray([point.pt for point in keypoints],dtype=np.float32)
    else:
        points=np.empty((0,2),dtype=np.float32);descriptors=np.empty((0,32),dtype=np.uint8)
    local_points,local_descriptors=_pack_features(points,descriptors)
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
    result.update(width=width,height=height,quality=quality,phash=phash,crop_hashes=crop_hashes,
                  local_points=local_points,local_descriptors=local_descriptors,
                  local_width=feature_gray.shape[1],local_height=feature_gray.shape[0],
                  preview=(b'HXI1'+zlib.compress(preview.tobytes(),6)).hex())
    return result


def _feature_arrays(metadata):
    points=metadata.get('local_points')
    descriptors=metadata.get('local_descriptors')
    if not points or not descriptors: return None,None
    try:
        points_blob=bytes.fromhex(points) if isinstance(points,str) else bytes(points)
        descriptors_blob=bytes.fromhex(descriptors) if isinstance(descriptors,str) else bytes(descriptors)
        points=np.frombuffer(zlib.decompress(points_blob),dtype='<f4').reshape(-1,2)
        descriptors=np.frombuffer(zlib.decompress(descriptors_blob),dtype=np.uint8).reshape(-1,32)
        if len(points)!=len(descriptors): return None,None
        return points,descriptors
    except (ValueError,zlib.error):
        return None,None


def _nonflat_mask(image):
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
    smooth=cv2.GaussianBlur(gray,(7,7),1.4)
    gx=cv2.Sobel(smooth,cv2.CV_32F,1,0,ksize=3)
    gy=cv2.Sobel(smooth,cv2.CV_32F,0,1,ksize=3)
    mask=(cv2.magnitude(gx,gy)>18).astype(np.uint8)
    return cv2.dilate(mask,np.ones((9,9),np.uint8),iterations=1)


def _preview_image(metadata):
    value=metadata.get('preview')
    if not value:return None
    try:
        data=bytes.fromhex(value) if isinstance(value,str) else bytes(value)
        data=zlib.decompress(data[4:]) if data.startswith(b'HXI1') else data
        return np.frombuffer(data,np.uint8).reshape(128,128,3)
    except (ValueError,zlib.error):
        return None


def _near_full_frame_match(new,old):
    if not new.get('phash') or not old.get('phash'):return None
    if (int(new['phash'],16)^int(old['phash'],16)).bit_count()>8:return None
    old_ratio=old['width']/old['height']
    if abs(new['width']/new['height']-old_ratio)/old_ratio>.005:return None
    a=_preview_image(new);b=_preview_image(old)
    if a is None or b is None:return None
    a=cv2.cvtColor(a,cv2.COLOR_BGR2GRAY).astype(np.float32)
    b=cv2.cvtColor(b,cv2.COLOR_BGR2GRAY).astype(np.float32)
    ma,mb=float(a.mean()),float(b.mean());va,vb=float(a.var()),float(b.var())
    covariance=float(((a-ma)*(b-mb)).mean())
    ssim=((2*ma*mb+6.5025)*(2*covariance+58.5225))/((ma*ma+mb*mb+6.5025)*(va+vb+58.5225))
    if ssim<.88:return None
    area=max(1.0,float(min(np.count_nonzero(_nonflat_mask(a.astype(np.uint8))),
                           np.count_nonzero(_nonflat_mask(b.astype(np.uint8))))))
    return {'new_area':area,'old_area':area,'inliers':0,'distance':1-ssim}


def picture_match(new,old):
    """Compression/crop resistant local match with RANSAC and common-canvas area."""
    if not new.get('phash') or not old.get('phash'): return None
    new_points,new_desc=_feature_arrays(new);old_points,old_desc=_feature_arrays(old)
    if new_points is None or old_points is None or len(new_desc)<12 or len(old_desc)<12:
        return _near_full_frame_match(new,old)
    matcher=cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs=matcher.knnMatch(new_desc,old_desc,k=2)
    good=[]
    for pair in pairs:
        if len(pair)==2 and pair[0].distance < .78*pair[1].distance:
            good.append(pair[0])
    if len(good)<16:return _near_full_frame_match(new,old)
    source=np.float32([new_points[m.queryIdx] for m in good]).reshape(-1,1,2)
    target=np.float32([old_points[m.trainIdx] for m in good]).reshape(-1,1,2)
    scale=max(float(new.get('local_width') or 1),float(new.get('local_height') or 1),
              float(old.get('local_width') or 1),float(old.get('local_height') or 1))/1000.0
    homography,inlier_mask=cv2.findHomography(source,target,cv2.RANSAC,max(3.0,4.0*scale))
    if homography is None or inlier_mask is None:return _near_full_frame_match(new,old)
    inliers=int(inlier_mask.sum());ratio=inliers/max(1,len(good))
    if inliers<16 or ratio<.18:return _near_full_frame_match(new,old)
    nw,nh=int(new.get('local_width') or new['width']),int(new.get('local_height') or new['height'])
    ow,oh=int(old.get('local_width') or old['width']),int(old.get('local_height') or old['height'])
    corners=np.float32([[0,0],[nw,0],[nw,nh],[0,nh]]).reshape(-1,1,2)
    projected=cv2.perspectiveTransform(corners,homography)
    old_corners=np.float32([[0,0],[ow,0],[ow,oh],[0,oh]]).reshape(-1,1,2)
    new_poly=projected.reshape(-1,2).astype(np.float32)
    old_poly=old_corners.reshape(-1,2).astype(np.float32)
    if not np.isfinite(new_poly).all() or np.max(np.abs(new_poly))>max(ow,oh)*20:return None
    new_area=abs(float(cv2.contourArea(new_poly)));old_area=float(ow*oh)
    if new_area<=0 or old_area<=0:return None
    overlap=abs(float(cv2.intersectConvexConvex(new_poly,old_poly)[0]))
    if overlap/min(new_area,old_area)<.16:return None
    minimum=np.minimum(new_poly.min(axis=0),old_poly.min(axis=0))
    maximum=np.maximum(new_poly.max(axis=0),old_poly.max(axis=0))
    canvas_w=max(1,int(np.ceil(maximum[0]-minimum[0])));canvas_h=max(1,int(np.ceil(maximum[1]-minimum[1])))
    if canvas_w*canvas_h>1200*1200*16:return None
    canvas_scale=min(1.0,1400.0/max(canvas_w,canvas_h))
    shift=np.array([[1,0,-minimum[0]],[0,1,-minimum[1]],[0,0,1]],dtype=np.float64)
    resize=np.diag([canvas_scale,canvas_scale,1.0])
    transform=resize@shift
    h_new=transform@homography
    h_old=transform
    out_w=max(1,int(round(canvas_w*canvas_scale)));out_h=max(1,int(round(canvas_h*canvas_scale)))
    new_preview=_preview_image(new);old_preview=_preview_image(old)
    if new_preview is not None and old_preview is not None:
        new_mask=_nonflat_mask(new_preview);old_mask=_nonflat_mask(old_preview)
        to_new=np.diag([nw/128.0,nh/128.0,1.0])
        to_old=np.diag([ow/128.0,oh/128.0,1.0])
        a=cv2.warpPerspective(new_mask,transform@homography@to_new,(out_w,out_h),flags=cv2.INTER_NEAREST)
        b=cv2.warpPerspective(old_mask,transform@to_old,(out_w,out_h),flags=cv2.INTER_NEAREST)
        new_coverage=float(np.count_nonzero(a));old_coverage=float(np.count_nonzero(b))
    else:
        new_coverage=new_area*canvas_scale*canvas_scale
        old_coverage=old_area*canvas_scale*canvas_scale
    if min(new_coverage,old_coverage)<=0:return None
    return {'new_area':new_coverage,'old_area':old_coverage,'inliers':inliers,'distance':float(np.mean([m.distance for m in good]))}


def same_picture(new, old):
    return picture_match(new,old) is not None


def better_picture(new,old):
    match=picture_match(new,old)
    if match:
        new_area,old_area=match['new_area'],match['old_area']
        if new_area>old_area*1.05:return True
        if old_area>new_area*1.05:return False
        if new['quality']>old['quality']*1.02:return True
        if old['quality']>new['quality']*1.02:return False
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

    def _signature(self,conn,ident,metadata,replace_features=True):
        conn.execute('INSERT OR IGNORE INTO image_signatures VALUES(?,?,?)',(metadata['sha256'],ident,metadata['phash']))
        for band,value in enumerate(bytes.fromhex(metadata['phash'])):
            conn.execute('INSERT OR IGNORE INTO image_hash_buckets VALUES(?,?,?)',(band,value,ident))
        crop_hashes=metadata.get('crop_hashes') or ([metadata['phash']] if metadata.get('phash') else [])
        for phash in crop_hashes:
            if not phash:continue
            conn.execute('INSERT OR IGNORE INTO image_crop_signatures VALUES(?,?)',(ident,phash))
            for band,value in enumerate(bytes.fromhex(phash)):
                conn.execute('INSERT OR IGNORE INTO image_crop_buckets VALUES(?,?,?,?)',(band,value,phash,ident))
        points=metadata.get('local_points');descriptors=metadata.get('local_descriptors')
        if points and descriptors:
            point_blob=bytes.fromhex(points);descriptor_blob=bytes.fromhex(descriptors)
            count=len(zlib.decompress(point_blob))//8
            values=(ident,point_blob,descriptor_blob,count,int(metadata.get('local_width') or metadata['width']),
                    int(metadata.get('local_height') or metadata['height']))
            if replace_features:
                conn.execute('''INSERT INTO image_local_features(asset_id,points,descriptors,point_count,width,height)
                  VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET points=excluded.points,
                  descriptors=excluded.descriptors,point_count=excluded.point_count,width=excluded.width,height=excluded.height''',values)
            else:
                conn.execute('''INSERT OR IGNORE INTO image_local_features(asset_id,points,descriptors,point_count,width,height)
                  VALUES(?,?,?,?,?,?)''',values)

    def recover(self,conn):
        for row in conn.execute('SELECT * FROM image_pending').fetchall():
            metadata=json.loads(row['metadata'])
            temporary=Path(row['temporary']); target=self.library/(row['id']+'.img')
            temporary.resolve().relative_to(self.library)
            if temporary.exists(): atomic_replace(temporary,target)
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=metadata['sha256']:
                raise RuntimeError('图片升级尚未恢复，停止写入以保护原图')
            self._finalize(conn,row['id'],metadata)

    def _asset_features(self,conn,asset):
        row=conn.execute('SELECT * FROM image_local_features WHERE asset_id=?',(asset['id'],)).fetchone()
        if row:
            asset=dict(asset)
            asset.update(local_points=bytes(row['points']).hex(),local_descriptors=bytes(row['descriptors']).hex(),
                         local_width=row['width'],local_height=row['height'])
            return asset
        path=Path(asset['path'])
        if not path.is_file():return asset
        try:
            prepared=features(path.read_bytes())
            self._signature(conn,asset['id'],prepared)
            conn.commit()
            asset=dict(asset);asset.update({key:prepared[key] for key in ('local_points','local_descriptors','local_width','local_height','crop_hashes')})
        except (OSError,ValueError,cv2.error):
            pass
        return asset

    def index_existing(self,path):
        """Refresh crop hashes and local descriptors without replacing a managed file."""
        path=Path(path).resolve()
        with self.locked(),contextlib.closing(self.connect()) as conn:
            row=conn.execute('SELECT * FROM image_assets WHERE path=?',(str(path),)).fetchone()
            if row is None or not path.is_file():return False
            metadata=features(path.read_bytes())
            self._signature(conn,row['id'],metadata)
            conn.commit()
            return True

    def candidate(self,conn,metadata):
        exact=conn.execute('SELECT a.* FROM image_signatures s JOIN image_assets a ON a.id=s.asset_id WHERE s.sha256=?',(metadata['sha256'],)).fetchone()
        if exact and (Path(exact['path']).is_file() or exact['sha256']==metadata['sha256']): return exact
        if not metadata['phash']: return None
        votes={}
        probes=metadata.get('crop_hashes') or [metadata['phash']]
        for phash in probes:
            for band,value in enumerate(bytes.fromhex(phash)):
                for row in conn.execute('SELECT phash,asset_id FROM image_crop_buckets WHERE band=? AND value=?',(band,value)):
                    key=(row['asset_id'],row['phash'])
                    votes[key]=votes.get(key,0)+1
                # Existing libraries have only full-image hash buckets until refreshed.
                for row in conn.execute('SELECT asset_id FROM image_hash_buckets WHERE band=? AND value=?',(band,value)):
                    key=(row['asset_id'],phash)
                    votes[key]=votes.get(key,0)+1
        probe_ints=[int(value,16) for value in probes]
        candidate_ids={ident for (ident,stored_hash),count in votes.items()
                       if count>=2 and stored_hash and min((int(stored_hash,16)^value).bit_count() for value in probe_ints)<=28}
        matches=[]
        for ident in candidate_ids:
            old=conn.execute('SELECT * FROM image_assets WHERE id=?',(ident,)).fetchone()
            if not old or not Path(old['path']).is_file():continue
            old=self._asset_features(conn,old)
            match=picture_match(metadata,old)
            if match:matches.append((match,old))
        if not matches:return None
        # Maximize geometric confidence first; coverage comparison runs only after confirmation.
        return max(matches,key=lambda pair:(pair[0]['inliers'],pair[0]['new_area']+pair[0]['old_area']))[1]

    def store(self,data, fallback_path=None, prepared=None):
        metadata=prepared or features(data)
        with self.locked(),contextlib.closing(self.connect()) as conn:
            self.recover(conn)
            old=self.candidate(conn,metadata)
            if old and Path(old['path']).is_file() and (old['sha256']==metadata['sha256'] or not better_picture(metadata,old)):
                self._signature(conn,old['id'],metadata,replace_features=False); conn.commit()
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
                        local=conn.execute('SELECT * FROM image_local_features WHERE asset_id=?',(row['id'],)).fetchone()
                        if local:
                            metadata.update(local_points=bytes(local['points']).hex(),local_descriptors=bytes(local['descriptors']).hex(),
                                            local_width=local['width'],local_height=local['height'])
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
