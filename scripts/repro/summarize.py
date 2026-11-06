"""Validate the complete rehearsal evidence set and emit a compact manifest."""
import hashlib
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
def read(name):
    return json.loads((root/name).read_text())

def test_result(name):
    text = (root/name).read_text()
    matches = re.findall(r'(\d+) passed(?:, \d+ skipped)? in ([0-9.]+)s',text)
    assert matches and not re.search(r'\b[1-9]\d* (?:failed|errors?)\b',text),name
    return {'passed':int(matches[-1][0]),'seconds':float(matches[-1][1])}

before, after = read('database-before.json'),read('database-after.json')
assert before == after
assert read('raw-before.json') == read('raw-after.json')
assert read('import-before.json') == read('import-after.json')
volumes = ['outputs','broker_state','simulator_outbox','collector_spool','raw_ready','spark_checkpoints']
for name in volumes:
    assert read('before-'+name+'.json') == read('after-'+name+'.json'),name
    assert read('match-'+name+'.json')['match']
ui_before,ui_after = read('ui-before.json'),read('ui-after.json')
def comparable_ui(value):
    return {'publication':value['publication_id'],'oracle':value['raw_water_oracle'],
        'screens':[{k:v for k,v in screen.items() if k!='elapsed_seconds'} for screen in value['screens']],
        'configured_tasks':value['configured_rule_and_scenario_tasks']}
assert comparable_ui(ui_before) == comparable_ui(ui_after)
assert len(ui_after['screens'])==8
for filename in ('pipeline.log','pipeline-resume.log'):
    text=(root/filename).read_text()
    results=[json.loads(line) for line in text.splitlines() if line.startswith('{"workflow_id"')]
    assert results[-1]['status']=='completed' and results[-1]['publication_id']==ui_after['publication_id']
for filename in ('app-health.txt','app-restored-health.txt'):
    assert (root/filename).read_text().strip()=='ok'
for filename in ('egress.txt','egress-restored.txt'):
    code=int((root/filename).read_text().strip().split()[-1])
    assert code!=0
sensor_ui=read('sensor-ui.json')
assert sensor_ui['status']=='passed' and sensor_ui['accepted_events']==30
sensor=read('sensor-restore.json')
assert sensor['unique_events']==30 and sensor['complete'] and sensor['checksum_verified']
assert sensor['original_envelopes_exact'] and sensor['queued_rows_received_before_retry']>0
steps=[json.loads(line) for line in (root/'workflow-steps.jsonl').read_text().splitlines()]
assert {s['step'] for s in steps}=={'ingest','load-core','spark','publish'}
assert all(s['status']=='completed' for s in steps)
manifest=[]
for path in sorted(root.glob('*')):
    if path.is_file() and path.name not in ('summary.json','restore-run.log','summary-output.log'):
        manifest.append({'file':path.name,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
result={'status':'passed','source_head':(root/'source-head.txt').read_text().strip(),
    'images':(root/'runtime-images.txt').read_text().splitlines(),
    'scope':'Full locked AGC historical snapshot; 30-row Automatoes MQTT replay; isolated container runtime, host may remain online',
    'publication':ui_after['publication_id'],'snapshot':ui_after['snapshot_id'],
    'backend_tests':test_result('backend-tests.log'),'ui_tests':test_result('ui-tests.log'),
    'database_bytes_before_backup':int((root/'database-bytes.txt').read_text()),
    'tables_verified':len([r for r in before if 'table' in r]),
    'table_rows_verified':sum(r.get('rows',0) for r in before),
    'sequences_verified':len([r for r in before if 'sequence' in r]),
    'raw_files_verified':len(read('raw-before.json')), 'import_files_verified':len(read('import-before.json')),
    'volumes':{name:len(read('before-'+name+'.json')) for name in volumes},
    'screens_verified_before_and_after':len(ui_after['screens']),
    'configured_rule_and_scenario_tasks_match':True,'pipeline_resume_same_publication':True,
    'networks':(root/'network.txt').read_text().splitlines(),'egress_denied_before_and_after':True,
    'sensor':sensor,'sensor_ui':sensor_ui,'workflow_steps':[{k:s[k] for k in ('step','status','started_at','finished_at','rows_in','rows_out')} for s in steps],
    'evidence_files':manifest, 'harness_files':[{'file':str(p.relative_to(root/'harness')), 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted((root/'harness').rglob('*')) if p.is_file()]}
(root/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k not in ('evidence_files','workflow_steps')},ensure_ascii=False,indent=2))
