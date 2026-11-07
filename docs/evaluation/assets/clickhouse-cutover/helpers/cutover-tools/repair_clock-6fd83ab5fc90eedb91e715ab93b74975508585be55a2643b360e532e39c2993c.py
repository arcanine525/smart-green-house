"""Recreate ONLY the agent-owned failed rollback fixture from its baseline.

The failed attempt has a separately verified full dump and retained artifacts.
No original PostgreSQL container, user database or Docker volume is removed.
"""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

workspace=Path(__file__).resolve().parents[2]
setup=json.loads((workspace/'tmp/clickhouse-cutover/current.json').read_text())
root=Path(setup['root']);container=setup['project']+'-rollback-db-1'
if (setup['project'],setup['database'],setup['purpose'])!=('gh-cutover-pg-202611070931','greenhouse_rollback','isolated_clickhouse_cutover_rollback'):
    raise ValueError('Explicitly owned temporary rollback fixture required')
info=json.loads(subprocess.check_output(['docker','inspect',container]))[0]
if info['Config']['Labels']['com.docker.compose.project']!=setup['project']:
    raise ValueError('Refuse another project')
if not any(m.get('Name')==setup['project']+'_rollback_pg' and m['Destination']=='/var/lib/postgresql/data' for m in info['Mounts']):
    raise ValueError('Refuse another PostgreSQL data volume')
backup=root/'failed-clock-attempt.dump'
receipt=json.loads((root/'failed-clock-backup-verified.json').read_text())
with backup.open('rb') as stream:hashed=hashlib.file_digest(stream,'sha256').hexdigest()
if receipt['status']!='passed' or not receipt['all_archive_members_read'] or hashed!=receipt['sha256'] or backup.stat().st_size!=receipt['bytes']:
    raise ValueError('Verified forensic backup required before resetting the private fixture')
baseline=Path(setup['source_backup'])
with baseline.open('rb') as stream:hashed=hashlib.file_digest(stream,'sha256').hexdigest()
if hashed!=setup['backup_sha256']:raise ValueError('Original baseline backup changed')
state=json.loads((root/'post-cutover.json').read_text())['archive']['journal']
current=json.loads((root/'journal-backup-receipt.json').read_text())
if current['head']!=state['head'] or current['cursor']!=len(state['records']):
    raise ValueError('New acknowledged native writes require a new delta export')


def phase(name):
    value=dict(phase=name,at=datetime.now(timezone.utc).isoformat(),monotonic_seconds=time.monotonic())
    with (root/'phase-times.jsonl').open('a') as stream:stream.write(json.dumps(value)+'\n')
    print(json.dumps(value),flush=True)


failed=root/'failed-clock-attempt'
failed.mkdir(exist_ok=False)
for name in ('baseline-map.json','postgres-baseline-business.json','rollback-inputs.json','rollback-verification.json',
        'scenario-mappings.json','tariff-map.json','postgres-corrected-publication.json',
        'postgres-rollback-business.json','final-six-br-parity.json','postgres-output',
        'references-current','full-parity'):
    path=root/name
    if path.exists():shutil.move(path,failed/name)
phase('clock_fix_private_fixture_reset_started_after_verified_backup')
with (root/'clock-fix-reset-restore.log').open('x') as log:
    for command in (
        ['psql','-U','greenhouse','-d','postgres','-v','ON_ERROR_STOP=1','-c','DROP DATABASE greenhouse_rollback WITH (FORCE)'],
        ['createdb','-U','greenhouse','greenhouse_rollback'],
        ['pg_restore','--username','greenhouse','--dbname','greenhouse_rollback','--no-owner','--no-acl','--exit-on-error','/backups/database.dump'],
        ['psql','-U','greenhouse','-d','greenhouse_rollback','-v','ON_ERROR_STOP=1','-c','ANALYZE']):
        subprocess.run(['docker','exec',container,*command],stdout=log,stderr=subprocess.STDOUT,check=True)
phase('clock_fix_private_baseline_restored_and_analyzed')
for label,script,mode in (
        ('clock-fix-baseline','state.py','baseline'),('clock-fix-inputs','rollback.py','inputs'),
        ('clock-fix-rebuild','rollback.py','rebuild'),('clock-fix-verification','rollback.py','verify'),
        ('clock-fix-verification-retry','rollback.py','verify')):
    phase(label+'_started')
    subprocess.run([sys.executable,str(workspace/'scripts/clickhouse_cutover/run.py'),'--label',label,
        '--','python','/cutover-tools/'+script,mode],cwd=workspace,check=True)
    phase(label+'_passed')
phase('clock-fix-full-parity_started')
subprocess.run([sys.executable,str(workspace/'scripts/clickhouse_cutover/run.py'),
    '--label','clock-fix-full-parity','--','python','/cutover-tools/parity.py'],cwd=workspace,check=True)
phase('clock_fix_all_metadata_and_six_br_parity_passed_waiting_for_main_ui')
