"""独立旧图整理工具。默认只预览；--apply 暂停服务、备份台账并清理重复文件。"""
import argparse
import contextlib
import json
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from collect.image_store import ImageStore, features, sync_refs, atomic_replace
from collect.logging_util import make_log, setup_console_encoding

log=make_log(ROOT/'logs'/'compact_images.log')


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    atomic_replace(temporary,path)


def pause_services(root,base):
    """只停止采集、处理及渠道；不启动任何服务，也不关闭看板。"""
    token=(root/'data/state/dashboard_token.txt').read_text(encoding='utf-8').strip()
    def request(path,payload=None):
        data=None if payload is None else json.dumps(payload).encode()
        req=urllib.request.Request(base+path,data=data,headers={'X-HX-Token':token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:
            result=json.load(response)
        if not result.get('ok'): raise RuntimeError('看板拒绝暂停服务')
        return result['data']
    state=request('/api/state')
    if any(c.get('connected') and c.get('adapter')=='plugin' and 'inflight' not in c for c in state['channels']):
        raise RuntimeError('请先更新看板或停止外部插件，以便确认在途消息已完成')
    paused=[]
    for name,service in state['services'].items():
        if service.get('running'):
            request('/api/service/toggle',{'service':name,'enabled':False});paused.append(name)
    for channel in state['channels']:
        if channel.get('enabled') or channel.get('running'):
            request('/api/channels/toggle',{'channel':channel['id'],'enabled':False});paused.append(channel['id'])
    time.sleep(2)
    current=request('/api/state')
    if any(s.get('running') for s in current['services'].values()) or any(c.get('running') or c.get('enabled') for c in current['channels']):
        raise RuntimeError('仍有服务运行，未开始整理')
    deadline=time.monotonic()+30
    while any(c.get('inflight',0) for c in current['channels']):
        if time.monotonic()>=deadline: raise RuntimeError('外部插件仍有在途消息，请等待完成或停止插件后重试')
        time.sleep(.5);current=request('/api/state')
    return paused


def compact(root,apply=False):
    root=Path(root).resolve(); image_root=(root/'data/images').resolve(); database=root/'data/hx.sqlite3'
    manifest_path=root/'data/state/image_compaction.json'
    before_bytes=sum(p.stat().st_size for p in image_root.rglob('*') if p.is_file())
    temporary=contextlib.ExitStack()
    if apply:
        manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'mapping':{},'deleted':[],'source_hashes':{}}
        manifest.setdefault('source_hashes',{})
        # 备份与恢复都只针对台账，不接触采集 baseline 或游标。
        if not manifest.get('backup'):
            backup=root/'data/backups'/('images-'+time.strftime('%Y%m%d-%H%M%S'))/'hx.sqlite3'
            backup.parent.mkdir(parents=True,exist_ok=True)
            with contextlib.closing(sqlite3.connect(database)) as source,contextlib.closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            manifest['backup']=str(backup);atomic_json(manifest_path,manifest)
        store=ImageStore(database,image_root)
    else:
        scratch=Path(temporary.enter_context(tempfile.TemporaryDirectory(prefix='hx-image-preview-')))
        store=ImageStore(scratch/'index.sqlite3',scratch/'images')
        manifest={'mapping':{},'deleted':[]}
    saved=0;changed=0;groups={};skipped=[]
    try:
        # 先读现有共享库，预览和执行的候选优先顺序一致。
        files=sorted((p for p in image_root.rglob('*') if p.is_file() and p.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.avif','.gif','.svg','.img')),key=lambda p:('library' not in p.parts,str(p)))
        for path in files:
            path=path.resolve();path.relative_to(image_root)
            if apply and path.parent==store.library: continue
            data=path.read_bytes()
            metadata=features(data)
            previous=manifest['mapping'].get(str(path))
            if previous and Path(previous).is_file() and manifest['source_hashes'].get(str(path))==metadata['sha256']: target=previous
            else:
                if not metadata['phash']: skipped.append(str(path));continue
                target=store.store(data,prepared=metadata)
                if not target: skipped.append(str(path));continue
                manifest['mapping'][str(path)]=target
                manifest.setdefault('source_hashes',{})[str(path)]=metadata['sha256']
                if apply: atomic_json(manifest_path,manifest)
            groups.setdefault(target,[]).append(str(path))
        with contextlib.closing(sqlite3.connect(database)) as conn:
            conn.row_factory=sqlite3.Row
            rows=conn.execute('SELECT id,images FROM items').fetchall()
            for row in rows:
                try: old=json.loads(row['images'] or '[]')
                except ValueError: continue
                if not isinstance(old,list): continue
                new=list(dict.fromkeys(manifest['mapping'].get(str(Path(p).resolve()),p) for p in old))
                if new!=old:
                    changed+=1
                    if apply:
                        for p in new:
                            if not Path(p).is_file(): raise RuntimeError('引用目标缺失，停止删除')
                        conn.execute('UPDATE items SET images=?,rev=rev+1 WHERE id=? AND images=?',(json.dumps(new,ensure_ascii=False),row['id'],row['images']))
                        sync_refs(conn,row['id'],new)
            if apply: conn.commit()
            references={str(Path(p).resolve()) for row in conn.execute('SELECT images FROM items') for p in json.loads(row[0] or '[]')}
        if apply:
            # 先提交并核对台账，再删旧文件；中断后重复执行也不会产生悬空引用。
            for old,new in manifest['mapping'].items():
                path=Path(old);path.resolve().relative_to(image_root)
                if path.is_file() and old!=new and old not in references:
                    data=Path(new).read_bytes()
                    if not features(data)['phash']: raise RuntimeError('目标图片无法识别，未删除旧文件')
                    saved+=path.stat().st_size;path.unlink();manifest['deleted'].append(old)
                    atomic_json(manifest_path,manifest)
        else:
            original=sum(Path(p).stat().st_size for p in manifest['mapping'])
            final=sum(Path(p).stat().st_size for p in groups)
            saved=max(0,original-final)
        if apply:
            after_bytes=sum(p.stat().st_size for p in image_root.rglob('*') if p.is_file())
            saved=max(0,before_bytes-after_bytes)
        winners=[]
        with contextlib.closing(store.connect()) as conn:
            for path,sources in groups.items():
                asset=conn.execute('SELECT * FROM image_assets WHERE path=?',(path,)).fetchone()
                if asset:
                    original=next((source for source in sources if manifest['source_hashes'].get(source)==asset['sha256']),None)
                    winners.append({'path':path,'preferred_source':original,'sources':sources,'width':asset['width'],'height':asset['height'],'quality':asset['quality'],'bytes':asset['byte_size']})
        report={'apply':apply,'scanned':len(files),'groups':len(groups),'duplicates':sum(max(0,len(paths)-1) for paths in groups.values()),'affected_items':changed,'bytes_freed' if apply else 'estimated_bytes_freed':saved,'skipped':skipped,'mapping':manifest['mapping'],'winners':winners}
        if not apply:
            # 报告显示拟使用的库路径，不泄露已清理的临时预览目录。
            report['mapping']={old:str(image_root/'library'/Path(new).name) for old,new in manifest['mapping'].items()}
            for winner in winners: winner['path']=str(image_root/'library'/Path(winner['path']).name)
            report['paths_are_proposals']=True
        atomic_json(root/'logs/image_compaction_report.json',report)
        log(f'整理报告：扫描{len(files)}，重复{report["duplicates"]}，影响新闻{changed}，{"释放" if apply else "预计释放"}{saved}字节')
        return report
    finally: temporary.close()


def main():
    setup_console_encoding()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--dashboard-url',default='http://127.0.0.1:8787')
    parser.add_argument('--offline',action='store_true',help='看板及所有采集/处理/分发进程已手动停止时使用')
    args=parser.parse_args()
    if args.apply and not args.offline:
        paused=pause_services(ROOT,args.dashboard_url.rstrip('/'))
        log('已暂停服务：'+','.join(paused)+'；整理后不自动启动，避免真实抓取或发送')
    if args.apply:
        import subprocess
        # 只读核对本工作区进程；无法确认停机时拒绝执行。
        if sys.platform=='win32':
            result=subprocess.run(['powershell','-NoProfile','-Command',"[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],capture_output=True,text=True,encoding='utf-8',check=True)
            processes=json.loads(result.stdout or '[]');processes=processes if isinstance(processes,list) else [processes]
            names=('service_runner.py','analyze_db.py','main_flow.py','news_fetch.py','channel.py')+ (('server.py',) if args.offline else ())
            if any(str(ROOT).lower() in str(p.get('CommandLine','')).replace('/','\\').lower() and any(name in str(p.get('CommandLine','')) for name in names) for p in processes):
                raise RuntimeError('本工作区仍有服务进程，请先停止')
        else: raise RuntimeError('此环境请通过看板暂停服务')
    compact(ROOT,args.apply)


if __name__=='__main__': main()
