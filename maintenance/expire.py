"""Cloud cleanup: public opaque IDs/expiry only; no Mac dependency.

GitHub schedules are best effort, not an exact-time access control. Removal
withdraws the live static directory; Git history may retain old public bytes.
"""
import base64, hashlib, json, math, os, pathlib, re, shutil, subprocess, time
from datetime import datetime, timezone

ROOT=pathlib.Path(__file__).resolve().parents[1]
OPAQUE=re.compile(r'[a-f0-9]{32}\Z')
SHA=re.compile(r'[a-f0-9]{40}\Z')
REPO=os.environ.get('GH_REPO','')
if not re.fullmatch(r'[a-z0-9-]+/[a-z0-9-]+\.github\.io',REPO):
    raise SystemExit('Invalid repository')

def api(method,suffix,payload=None):
    args=['gh','api','--hostname','github.com','--method',method,
          '-H','Accept: application/vnd.github+json',
          '-H','X-GitHub-Api-Version: 2026-03-10','repos/'+REPO+'/'+suffix]
    if payload is not None:args+=['--input','-']
    result=subprocess.run(args,input=None if payload is None else json.dumps(payload).encode(),
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30,check=False)
    if result.returncode:raise RuntimeError('GitHub cleanup request failed; no forced update')
    return json.loads(result.stdout)

head=api('GET','git/ref/heads/main')['object']['sha']
if not SHA.fullmatch(head):raise SystemExit('Invalid main commit')
# checkout@v6 may have run before a concurrent authorized publication. Build
# from the exact current commit rather than uploading an older workspace.
for args in (['git','fetch','--no-tags','--depth=1','origin',head],['git','reset','--hard',head]):
    result=subprocess.run(args,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        timeout=30,check=False)
    if result.returncode:raise SystemExit('Could not synchronize current public tree')
commit=api('GET','git/commits/'+head)
tree=api('GET','git/trees/'+commit['tree']['sha']+'?recursive=1')
if tree.get('truncated'):raise SystemExit('Incomplete repository tree')
rows={row['path']:row for row in tree['tree'] if row['type']=='blob'}
entry=rows.get('public-lifecycle.json')
if not entry:raise SystemExit('Missing public lifecycle')
blob=api('GET','git/blobs/'+entry['sha'])
manifest=json.loads(base64.b64decode(blob['content']))
if set(manifest) not in ({'version','sites'}, {'version','sites','expired_sites'}) or manifest['version']!=1 or not isinstance(manifest['sites'],dict):
    raise SystemExit('Invalid public lifecycle')
prior_expired=manifest.get('expired_sites',[])
if not isinstance(prior_expired,list) or len(set(prior_expired))!=len(prior_expired) or any(not isinstance(x,str) or not OPAQUE.fullmatch(x) for x in prior_expired) or set(prior_expired)&set(manifest['sites']):
    raise SystemExit('Invalid expired sites')
now=time.time();expired=[]
for site,value in manifest['sites'].items():
    if not OPAQUE.fullmatch(site) or not isinstance(value,dict) or set(value)!={'expires_at'}:
        raise SystemExit('Invalid lifecycle record')
    expiry=value['expires_at']
    if expiry is not None and (type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=0):
        raise SystemExit('Invalid lifecycle expiry')
    if expiry is not None and expiry<=now:expired.append(site)
if expired:
    prefixes=tuple('dist/s/'+site+'/' for site in expired)
    changes=[{'path':name,'mode':'100644','type':'blob','sha':None}
             for name in rows if name.startswith(prefixes) and name not in {'dist/s/'+site+'/index.html' for site in expired}]
    for site in expired:del manifest['sites'][site]
    manifest['expired_sites']=sorted(set(prior_expired)|set(expired))
    landing='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>网页已到期</title><style>body{font:17px/1.7 system-ui;max-width:34em;margin:15vh auto;padding:24px;color:#263c32}</style><h1>网页已到期</h1><p>原网址和作品仍保留。创建者可回到自己的 Kai 原会话，发送“续期这个网页”并附上此链接。</p><p>续期后可继续使用原网址，无需重新制作。</p></html>\n'.encode()
    landing_blob=api('POST','git/blobs',{'encoding':'base64','content':base64.b64encode(landing).decode()})['sha']
    changes += [{'path':'dist/s/'+site+'/index.html','mode':'100644','type':'blob','sha':landing_blob} for site in expired]
    body=(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n').encode()
    manifest_blob=api('POST','git/blobs',{'encoding':'base64','content':base64.b64encode(body).decode()})['sha']
    changes.append({'path':'public-lifecycle.json','mode':'100644','type':'blob','sha':manifest_blob})
    new_tree=api('POST','git/trees',{'base_tree':commit['tree']['sha'],'tree':changes})['sha']
    stamp=datetime.fromtimestamp(int(now),timezone.utc).isoformat().replace('+00:00','Z')
    identity={'name':'Static Publisher','email':'static-publisher@users.noreply.github.com','date':stamp}
    new_commit=api('POST','git/commits',{'message':'Expire static previews\n','tree':new_tree,
        'parents':[head],'author':identity,'committer':identity})['sha']
    if api('GET','git/ref/heads/main')['object']['sha']!=head:
        raise SystemExit('Main changed; preserve it and let the next run retry cleanup')
    api('PATCH','git/refs/heads/main',{'sha':new_commit,'force':False})
    if api('GET','git/ref/heads/main')['object']['sha']!=new_commit:
        raise SystemExit('Main changed after cleanup; do not deploy stale content')
    # This run deploys directly: GITHUB_TOKEN commits do not trigger a second
    # push workflow. A fresh standard workflow checks the current head again.
    for site in expired:
        target=ROOT/'dist'/'s'/site
        if target.is_symlink():raise SystemExit('Unsafe static directory')
        if target.exists():shutil.rmtree(target)
        target.mkdir(parents=True)
        (target/'index.html').write_bytes(landing)
    (ROOT/'public-lifecycle.json').write_bytes(body)
    head=new_commit
if api('GET','git/ref/heads/main')['object']['sha']!=head:
    raise SystemExit('Main changed before upload; preserve it and retry from the new head')
print('Expired public static directories:',len(expired))
