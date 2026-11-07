"""Continue the private rollback only after the actual native browser gate."""
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import time

workspace=Path(__file__).resolve().parents[2]
setup=json.loads((workspace/'tmp/clickhouse-cutover/current.json').read_text())
root=Path(setup['root']);expected='gh-ch-repro-20261107032510-rebuilt'
project=json.loads(subprocess.check_output(['docker','compose','config','--format','json'],cwd=workspace))
if setup['project']!='gh-cutover-pg-202611070931' or project['name']!=expected:
    raise ValueError('This continuation addresses only the named private cutover fixture')


def phase(name,**fields):
    event=dict(at=datetime.now(timezone.utc).isoformat(),monotonic_seconds=time.monotonic(),phase=name,**fields)
    with (root/'phase-times.jsonl').open('a') as stream:stream.write(json.dumps(event)+'\n')
    print(json.dumps(event),flush=True)


gate=root/'native-default-browser/result.json';deadline=time.monotonic()+1800
phase('awaiting_native_browser_gate')
while not gate.is_file():
    if (root/'native-default-browser/failed.png').exists():raise ValueError('Native browser gate failed; do not start rollback')
    if time.monotonic()>deadline:raise TimeoutError('Native browser gate did not finish; no rollback mutation attempted')
    time.sleep(.5)
result=json.loads(gate.read_text())
if result['status']!='passed' or result['backend']!='clickhouse' or len(result['checks'])!=9:
    raise ValueError('All nine actual native screens must pass')
pg=setup['project']+'-rollback-db-1'
for name in (pg,'smart-greenhouse-db-1'):
    status=subprocess.check_output(['docker','inspect','--format','{{.State.Status}}',name],text=True).strip()
    if status!='exited':raise ValueError('Project PostgreSQL must remain stopped during the native smoke')
phase('native_browser_passed_with_project_postgres_stopped',screens=9,elapsed_seconds=result['elapsed_seconds'])
# Releasing the read process also releases its reconstructible verified-row
# spools, leaving headroom for the PostgreSQL/Spark rebuild.
phase('rollback_downtime_start')
subprocess.run(['docker','compose','stop','--timeout','30','ch-app'],cwd=workspace,check=True)
phase('native_app_stopped_for_rollback')
subprocess.run(['docker','start',pg],check=True)
deadline=time.monotonic()+90
while subprocess.check_output(['docker','inspect','--format','{{.State.Health.Status}}',pg],text=True).strip()!='healthy':
    if time.monotonic()>deadline:raise TimeoutError('Private PostgreSQL restore fixture is not healthy')
    time.sleep(1)
phase('rollback_postgres_healthy')
for label,mode in (('rollback-inputs','inputs'),('rollback-rebuild','rebuild'),('rollback-verification','verify')):
    phase(label+'_started')
    subprocess.run([sys.executable,str(workspace/'scripts/clickhouse_cutover/run.py'),'--label',label,'--',
        'python','/cutover-tools/rollback.py',mode],cwd=workspace,check=True)
    phase(label+'_passed')
phase('rollback_data_verified_waiting_for_main_ui_review')
