"""Logged tools on the explicitly owned cutover fixture, never the source PG DB."""
import argparse
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--setup',type=Path,default=Path('tmp/clickhouse-cutover/current.json'))
parser.add_argument('--label',required=True)
parser.add_argument('--image',default='greenhouse-cutover-processing:20261107')
parser.add_argument('--env',action='append',default=[])
parser.add_argument('command',nargs=argparse.REMAINDER)
args=parser.parse_args()
workspace=Path(__file__).resolve().parents[2]
setup=json.loads(args.setup.read_text());root=Path(setup['root'])
ch=json.loads((workspace/'tmp/clickhouse-repro/current.json').read_text())
project=ch['project']+'-rebuilt';state=Path(ch['root'])/'rebuilt/state'
data=Path(ch['root'])/'rebuilt/data'
if not re.fullmatch('gh-cutover-pg-[0-9]+',setup['project']) or setup['database']!='greenhouse_rollback':
    raise ValueError('Isolated rollback fixture identity required')
if not re.fullmatch('[a-zA-Z0-9_-]+',args.label):raise ValueError('Simple label required')
tail=args.command[1:] if args.command[:1]==['--'] else args.command
if not tail:raise ValueError('Explicit command required')
minimum=6 if 'rebuild' in tail else 3
free=shutil.disk_usage(root).free
if free<minimum*1024**3:raise ValueError('Need at least '+str(minimum)+' GiB free before cutover helper')
log=root/(args.label+'.log')
if log.exists():raise ValueError('Preserve prior evidence; choose a new label')
snapshot=root/'helper-revisions'/args.label
snapshot.mkdir(parents=True,exist_ok=False)
helpers={}
for name,folder in (('cutover-tools','scripts/clickhouse_cutover'),('repro','scripts/clickhouse_repro'),
        ('migration','scripts/clickhouse'),('walkthrough','tools/walkthrough')):
    target=snapshot/name;target.mkdir()
    for path in sorted((workspace/folder).glob('*.py')):
        shutil.copy2(path,target/path.name)
        helpers[name+'/'+path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
image_id=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',args.image],text=True).strip()
source_hashes={p.relative_to(workspace).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted((workspace/'src').rglob('*')) if p.is_file() and p.suffix in ('.py','.sql','.json')}
command=['docker','run','--rm','--pull','never','--name',setup['project']+'-'+args.label,
    '--network',project+'_backend','--cpus','1','--memory','1536m',
    '-e','CH_HOST=clickhouse','-e','CH_DATABASE=greenhouse_repro','-e','CH_USER=greenhouse_repro',
    '-e','CH_PASSWORD=rehearsal-local-only','-e','GREENHOUSE_BACKEND=clickhouse',
    '-e','GREENHOUSE_CH_COORDINATOR=/state/coordinator','-e','GREENHOUSE_RAW_DIR=/data/raw',
    '-e','PGHOST=rollback-db','-e','PGPORT=5432','-e','PGDATABASE=greenhouse_rollback',
    '-e','PGUSER=greenhouse','-e','PGPASSWORD=cutover-local-only',
    '-e','GREENHOUSE_REHEARSAL_PROJECT='+project,'-e','SPARK_LOCAL_IP=127.0.0.1',
    '-e','PYSPARK_SUBMIT_ARGS=--driver-memory 768m pyspark-shell']
for source,target,access in ((root,'/cutover','rw'),(state,'/state','rw'),
        (data/'raw','/data/raw','ro'),(snapshot/'cutover-tools','/cutover-tools','ro'),
        (snapshot/'migration','/migration','ro'),(snapshot/'repro','/repro','ro'),
        (snapshot/'walkthrough','/walkthrough','ro'),
        (Path(ch['root'])/'rebuilt/references','/references','ro')):
    if not source.exists():raise ValueError('Required bind is missing: '+str(source))
    command+=['-v',str(source)+':'+target+':'+access]
for value in args.env:command+=['-e',value]
root.chmod(0o777)
with log.open('x') as stream:
    receipt={'label':args.label,'image':args.image,'image_id':image_id,'source_head':setup['source_head'],
        'helper_hashes':helpers,'working_source_hashes':source_hashes,
        'preflight_host_free_bytes':free,'minimum_free_gib':minimum}
    (snapshot/'manifest.json').write_text(json.dumps(receipt,indent=2)+'\n')
    stream.write(json.dumps({k:receipt[k] for k in ('label','image','image_id','source_head')}|
        {'manifest_sha256':hashlib.sha256((snapshot/'manifest.json').read_bytes()).hexdigest()})+'\n');stream.flush()
    result=subprocess.run(command+[args.image,*tail],stdout=stream,stderr=subprocess.STDOUT,cwd=workspace)
print(json.dumps({'exit_code':result.returncode,'log':str(log)}),flush=True)
raise SystemExit(result.returncode)
