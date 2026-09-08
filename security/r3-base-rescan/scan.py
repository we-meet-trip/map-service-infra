#!/usr/bin/env python3
"""One leased hosted-runner exact OCI rescan. Never builds or deploys images."""
import datetime as dt
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile

REPO='we-meet-trip/map-service-infra'
SCANNER='aquasec/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969'
HERE=Path(__file__).resolve().parent
DEADLINE=None
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def read(p):return json.loads(Path(p).read_text())
def write(p,j):Path(p).write_text(json.dumps(j,indent=2)+'\n')
def require(ok,msg):
    if not ok:raise ValueError(msg)
def remaining(maximum):
    if DEADLINE is None:return maximum
    require(time.monotonic()<DEADLINE,'40minute total budget exceeded')
    return min(maximum,max(0.1,DEADLINE-time.monotonic()))
def gh_json(api):return json.loads(subprocess.check_output(['gh','api',api],timeout=remaining(45)))

class RemoteZip(io.RawIOBase):
    """Seekable ZIP reader; no full archive fallback; 4MiB bounded response each."""
    def __init__(self,artifact):
        self.artifact=artifact;self.size=artifact['size_in_bytes'];self.pos=0;self.bytes=0;self.cache=[]
    def seekable(self):return True
    def readable(self):return True
    def tell(self):return self.pos
    def seek(self,n,whence=0):
        pos=n if whence==0 else self.pos+n if whence==1 else self.size+n
        require(0<=pos<=self.size,'remote ZIP seek bounds');self.pos=pos;return pos
    def chunk(self,start,n):
        remaining(45)
        for off,data in self.cache:
            if off<=start and start+n<=off+len(data):return data[start-off:start-off+n]
        args=['gh','api','-H',f'Range: bytes={start}-{start+n-1}',f'repos/{REPO}/actions/artifacts/{self.artifact["id"]}/zip']
        with subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as proc:
            # communicate would collect a whole ignored-range response. Bounded
            # reads enforce the no-repeat-large-wrapper contract instead.
            import threading
            timer=threading.Timer(remaining(45),proc.kill);timer.start()
            try:
                data=proc.stdout.read(n+1)
                if len(data)!=n:
                    proc.terminate();raise ValueError('range ignored/truncated; no full-download fallback')
                require(proc.wait(timeout=remaining(5))==0,'range HTTP request failed')
            finally:
                timer.cancel()
                if proc.poll() is None:proc.kill();proc.wait(timeout=5)
        self.bytes+=len(data);require(self.bytes<=230*1024*1024,'per-artifact range budget exceeded')
        # Only central-directory/header-sized reads are cached; layer bytes stream.
        if n<=128*1024:self.cache.append((start,data))
        return data
    def read(self,n=-1):
        n=self.size-self.pos if n<0 else min(n,self.size-self.pos);chunks=[]
        while n:
            take=min(n,4*1024*1024);chunks.append(self.chunk(self.pos,take));self.pos+=take;n-=take
        return b''.join(chunks)

def extract_oci(archive,layout):
    layout.mkdir();count=0;total=0
    with tarfile.open(archive) as t:
        for member in t:
            name=PurePosixPath(member.name)
            require(not name.is_absolute() and '..' not in name.parts,'unsafe tar path')
            if member.isdir():continue
            require(member.isfile(),'nonregular image archive member')
            total+=member.size;require(total<=1024*1024*1024,'extracted input exceeds1GiB')
            p=layout/name;p.parent.mkdir(parents=True,exist_ok=True)
            with t.extractfile(member) as src,p.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
            if name.parts[:2]==('blobs','sha256'):
                require(sha(p)==name.name,'OCI blob SHA mismatch');count+=1
    # Classic docker-save remains a supported Trivy input. Validate its config
    # before scanning the tar directly, instead of requiring a rebuild.
    if (layout/'index.json').exists() and (layout/'oci-layout').exists():
        return layout,count
    require((layout/'manifest.json').exists(),'unknown image archive format')
    manifest=read(layout/'manifest.json');require(len(manifest)==1,'ambiguous Docker archive')
    config=manifest[0]['Config'];require(sha(layout/config)==Path(config).stem,'Docker config SHA mismatch')
    return archive,count

def image_identity(reference):
    raw=subprocess.check_output(['docker','image','inspect','--format',
        '{{json .Id}} {{json .RepoDigests}} {{json .Os}} {{json .Architecture}}',reference],timeout=30).decode()
    # Decode the four JSON values without inspecting Env or credentials.
    dec=json.JSONDecoder();values=[]
    while raw.strip():
        raw=raw.lstrip();value,n=dec.raw_decode(raw);values.append(value);raw=raw[n:]
    identity=dict(zip(['Id','RepoDigests','Os','Architecture'],values))
    require(identity['Os']=='linux' and identity['Architecture']=='amd64','unexpected image platform')
    require(reference in identity['RepoDigests'],'exact pinned reference absent')
    return identity

def layout_config(layout,reference=None):
    if not (layout/'index.json').exists():
        c=read(layout/'manifest.json')[0]['Config'];return 'sha256:'+sha(layout/c)
    def visit(desc):
        path=layout/'blobs'/'sha256'/desc['digest'].split(':')[1]
        require(path.exists() and sha(path)==desc['digest'].split(':')[1],'descriptor integrity')
        j=read(path)
        if 'config' in j:
            config=j['config'];cj=read(layout/'blobs'/'sha256'/config['digest'].split(':')[1])
            if cj.get('os')=='linux' and cj.get('architecture')=='amd64':return config['digest']
            return None
        matches=[v for d in j.get('manifests',[]) if (v:=visit(d))]
        require(len(set(matches))<=1,'ambiguous platform image');return matches[0] if matches else None
    index=read(layout/'index.json');descs=index['manifests']
    if reference:
        pin=reference.rsplit('@',1)[1]
        pinned=layout/'blobs'/'sha256'/pin.split(':')[1]
        require(pinned.exists(),'pinned reference blob absent')
        return visit({'digest':pin})
    values=[x for d in descs if (x:=visit(d))]
    require(len(set(values))==1,'missing/ambiguous config');return values[0]

def main():
    global DEADLINE
    DEADLINE=time.monotonic()+40*60
    require(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
            and sys.platform=='linux','hosted Linux workflow only; local memory gate is HOLD')
    plan=read(HERE/'plan.json');trigger=read(HERE/'EXECUTE.json')
    require(trigger['plan_sha256']==sha(HERE/'plan.json'),'lease trigger plan mismatch')
    require(re.fullmatch(r'[0-9a-f]{32}',trigger.get('r0_lease_id','')) is not None,'R0 lease ID required')
    out=Path(os.environ['RUNNER_TEMP'])/'r3-base-rescan';out.mkdir()
    results=Path(os.environ.get('R3_RESULTS','r3-base-results'));results.mkdir()
    cache=out/'cache';cache.mkdir();deadline=DEADLINE
    session='r3-base-'+uuid.uuid4().hex[:12]
    receipt={'schema':'map-r3-base-rescan-v1','at_utc':now(),'source_sha':os.environ['GITHUB_SHA'],
             'run_id':os.environ['GITHUB_RUN_ID'],'lease':trigger,'scanner':SCANNER,
             'plan_sha256':sha(HERE/'plan.json'),'script_sha256':sha(Path(__file__)),
             'disk_free_bytes_before':shutil.disk_usage(out).free,
             'commands':[],'targets':[],'status':'RUNNING','live_changes':0,'image_builds':0}
    write(results/'receipt.json',receipt)
    def run(cmd,timeout=600):
        require(time.monotonic()<deadline,'40minute total budget exceeded')
        stamp=now();timeout=min(timeout,max(1,deadline-time.monotonic()))
        timed_out=False
        try:p=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True;p=subprocess.CompletedProcess(cmd,124,'','command timeout')
            # Stop only this session's labeled scanner, never name-match another.
            check=subprocess.run(['docker','inspect','--format','{{index .Config.Labels "map.session"}}',session],capture_output=True,text=True,timeout=15)
            if check.returncode==0 and check.stdout.strip()==session:
                subprocess.run(['docker','stop','--time','10',session],capture_output=True,timeout=30)
        n=len(receipt['commands']);(results/f'command-{n}.stdout').write_text(p.stdout);(results/f'command-{n}.stderr').write_text(p.stderr)
        receipt['commands'].append({'argv':cmd,'started_at':stamp,'completed_at':now(),'exit_code':p.returncode,'timed_out':timed_out})
        write(results/'receipt.json',receipt);return p
    def scan(args,network='none'):
        return run(['docker','run','--rm','--pull=never','--name',session,'--label','map.owner=R3',
            '--label','map.session='+session,'--label','map.ttl=2400','--cpus','2','--memory','2g','--memory-swap','2g',
            '--pids-limit','128','--cap-drop','ALL','--security-opt','no-new-privileges:true','--read-only',
            '--tmpfs','/tmp:rw,noexec,nosuid,size=512m','--log-opt','max-size=10m','--log-opt','max-file=3',
            '--network',network,'--mount',f'type=bind,src={out},dst=/work',
            '--mount',f'type=bind,src={results.resolve()},dst=/results',SCANNER]+args)
    try:
        require(shutil.disk_usage(out).free>=(3*1024**3*5+3)//4+10*1024**3,'runner3GiB peak times1.25 plus10GiB floor')
        for image in [SCANNER]+[t['reference'] for t in plan['targets'] if t['kind']=='registry_exact']:
            # New runner: pull once only when absent. Never retry a complete pull.
            if subprocess.run(['docker','image','inspect',image],capture_output=True).returncode:
                require(run(['docker','pull','--platform','linux/amd64',image]).returncode==0,'pinned image pull failed')
            image_identity(image)
        require(scan(['image','--download-db-only','--cache-dir','/work/cache'],network='bridge').returncode==0,'DB download failed')
        db=read(cache/'db/metadata.json');db['db_sha256']=sha(cache/'db/trivy.db');write(results/'scanner-db-metadata.json',db)
        for target in plan['targets']:
            require(shutil.disk_usage(out).free>=10*1024**3,'runner disk protection floor')
            service=target['service'];print('START',service,flush=True)
            row={'service':service,'target':target,'started_at':now()}
            with tempfile.TemporaryDirectory(prefix=service+'-',dir=out) as td:
                temp=Path(td);archive=temp/'image.tar'
                if target['kind']=='artifact_member':
                    api=f'repos/{REPO}/actions/artifacts/{target["artifact"]["id"]}'
                    meta=gh_json(api)
                    for key in ['id','size_in_bytes','digest']:require(meta[key]==target['artifact'][key],'artifact identity changed')
                    require(not meta['expired'],'artifact expired');remote=RemoteZip(meta)
                    with zipfile.ZipFile(remote) as z,z.open(target['member']) as src,archive.open('xb') as dst:
                        require(z.getinfo(target['member']).file_size==target['bytes'],'member size changed')
                        shutil.copyfileobj(src,dst,1024*1024)
                    row['range_bytes_downloaded']=remote.bytes;row['whole_wrapper_redownloaded']=False
                elif target['kind']=='release_asset':
                    meta=gh_json(f'repos/{REPO}/releases/assets/{target["asset_id"]}')
                    require(meta['size']==target['bytes'] and meta['digest']=='sha256:'+target['sha256'],'release asset changed')
                    with archive.open('xb') as dst:
                        subprocess.run(['gh','api','-H','Accept: application/octet-stream',f'repos/{REPO}/releases/assets/{target["asset_id"]}'],stdout=dst,check=True,timeout=remaining(180))
                else:
                    row['local_identity']=image_identity(target['reference'])
                    require(run(['docker','image','save','--output',str(archive),target['reference']],180).returncode==0,'exact image save failed')
                row['archive_sha256']=sha(archive);row['archive_bytes']=archive.stat().st_size
                require(row['archive_bytes']<=target.get('bytes',512*1024**2),'archive size bound')
                if target.get('sha256'):require(row['archive_sha256']==target['sha256'],'archive SHA mismatch')
                layout=temp/'oci';inp,row['verified_blobs']=extract_oci(archive,layout)
                reference=target.get('reference')
                if reference and not (layout/'blobs'/'sha256'/reference.rsplit(':',1)[1]).exists():
                    # Classic Docker save may omit a registry index; the daemon
                    # already bound RepoDigests to this immutable config Id.
                    config=layout_config(layout)
                    require(config==row['local_identity']['Id'],'classic config differs from pinned inspected image Id')
                    row['registry_binding']='exact RepoDigest verified by inspect; saved config equals immutable daemon Id'
                else:
                    config=layout_config(layout,reference)
                    row['registry_binding']='verified pinned OCI descriptor tree' if reference else 'verified artifact config and manifest'
                if target.get('config_digest'):require(config==target['config_digest'],'OCI config does not match candidate')
                row['config_digest']=config
                for key in ['manifest_digest']:
                    if target.get(key):require((layout/'blobs'/'sha256'/target[key].split(':')[1]).exists(),'exact manifest missing')
                relative='/work/'+str(inp.relative_to(out));report=results/(service+'.json')
                p=scan(['image','--input',relative,'--cache-dir','/work/cache','--skip-db-update','--skip-java-db-update',
                    '--offline-scan','--scanners','vuln','--severity','HIGH,CRITICAL','--ignore-unfixed=false',
                    '--ignorefile','/dev/null','--list-all-pkgs','--exit-code','1','--format','json',
                    '--output','/results/'+service+'.json','--timeout','8m'])
                require(p.returncode in (0,1) and report.exists(),'scanner failed without findings report')
                data=read(report);require(data.get('Results'),'empty scanner Results')
                require(data['Metadata']['ImageID']==config,'scanner config identity mismatch')
                counts={s:sum(v.get('Severity')==s for r in data['Results'] for v in r.get('Vulnerabilities',[])) for s in ['HIGH','CRITICAL']}
                require((p.returncode==0)==(not any(counts.values())),'strict exit/count disagreement')
                require(scan(['convert','--format','cyclonedx','--output','/results/'+service+'.cdx.json','/results/'+service+'.json']).returncode==0,'SBOM conversion failed')
                row.update({'status':'PASS' if p.returncode==0 else 'FAIL_FINDINGS','counts':counts,'report_sha256':sha(report),
                            'scan_created_at':data['CreatedAt'],'completed_at':now()})
                receipt['targets'].append(row);write(results/'receipt.json',receipt)
            print('DONE',service,counts,flush=True)
        require(sha(cache/'db/trivy.db')==db['db_sha256'],'frozen DB changed')
        receipt['status']='PASS' if all(r['status']=='PASS' for r in receipt['targets']) else 'FAIL_FINDINGS'
    except Exception as exc:
        receipt['status']='FAIL_EXECUTION';receipt['error']=type(exc).__name__+': '+str(exc);raise
    finally:
        receipt['completed_at']=now();receipt['cleanup']='per-target temporary directories cleaned; ephemeral runner cache retained to job end'
        receipt['disk_free_bytes_after']=shutil.disk_usage(out).free
        write(results/'receipt.json',receipt)
        write(results/'SHA256SUMS.json',{str(p.relative_to(results)):sha(p) for p in sorted(results.rglob('*')) if p.is_file() and p.name!='SHA256SUMS.json'})
    return 0 if receipt['status']=='PASS' else 1

if __name__=='__main__':sys.exit(main())
