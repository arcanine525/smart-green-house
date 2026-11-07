"""Archive/restore only stopped, disposable ClickHouse rehearsal volumes.

Host runner. No volume pruning or source-database mutations. Restore refuses a
nonempty target and validates the archive SHA before extraction. The fixed
client image runs without networking, with only the named volume and archive
folder mounted. The image must already exist locally; pulls are disabled.
"""
import argparse,hashlib,json,re,subprocess
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('mode',choices=('backup','restore'));p.add_argument('--project',required=True)
p.add_argument('--volume',required=True);p.add_argument('--archive',type=Path,required=True)
p.add_argument('--image',required=True)
a=p.parse_args()
if not re.fullmatch('gh-ch-repro-[a-z0-9-]+',a.project):raise ValueError('Explicit disposable rehearsal project required')
if not a.volume.startswith(a.project+'_'):raise ValueError('Volume does not belong to named rehearsal')
def docker(*arguments):return subprocess.check_output(['docker',*arguments],text=True).strip()
volume=json.loads(docker('volume','inspect',a.volume))[0]
if volume['Labels'].get('com.docker.compose.project')!=a.project:raise ValueError('Volume project label differs')
if docker('ps','--filter','label=com.docker.compose.project='+a.project,'--format','{{.Names}}'):
    raise ValueError('Stop every writer/server/broker in rehearsal project before snapshot or restore')
a.archive=a.archive.resolve();a.archive.parent.mkdir(parents=True,exist_ok=True)
receipt=a.archive.with_suffix(a.archive.suffix+'.sha256.json')
if a.mode=='backup':
    if a.archive.exists() or receipt.exists():raise ValueError('Refuse to overwrite an existing backup')
    script='''import tarfile
with tarfile.open('/archive/'+NAME,'w:gz',compresslevel=3) as target:
    target.add('/volume',arcname='.',recursive=True)
'''
else:
    r=json.loads(receipt.read_text())
    if r['sha256']!=hashlib.file_digest(a.archive.open('rb'),'sha256').hexdigest():raise ValueError('Archive hash differs')
    if r['bytes']!=a.archive.stat().st_size:raise ValueError('Archive size differs')
    script='''from pathlib import Path
import tarfile
if any(Path('/volume').iterdir()):raise ValueError('Restore target is not empty')
def safe(info,destination):
    filtered=tarfile.data_filter(info,destination)
    return None if filtered is None else filtered.replace(uid=info.uid,gid=info.gid)
with tarfile.open('/archive/'+NAME,'r:gz') as source:
    source.extractall('/volume',filter=safe)
'''
script='NAME='+repr(a.archive.name)+'\n'+script
mount='type=volume,source='+a.volume+',target=/volume'+(',readonly' if a.mode=='backup' else '')
subprocess.run(['docker','run','--rm','--pull','never','--network','none','--user','0','--read-only',
    '--mount',mount,'--mount','type=bind,source='+str(a.archive.parent)+',target=/archive'+(',readonly' if a.mode=='restore' else ''),
    '--entrypoint','python',a.image,'-c',script],check=True)
if a.mode=='backup':
    value={'source_project':a.project,'source_volume':a.volume,'bytes':a.archive.stat().st_size,
        'sha256':hashlib.file_digest(a.archive.open('rb'),'sha256').hexdigest()}
    receipt.write_text(json.dumps(value,indent=2)+'\n')
print(json.dumps({'status':'archived' if a.mode=='backup' else 'restored','volume':a.volume,'archive':str(a.archive)}))
