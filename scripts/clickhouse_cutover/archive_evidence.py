"""Keep bounded cutover evidence in Git; retain dumps/raw/journal SQL locally."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

workspace=Path(__file__).resolve().parents[2]
setup=json.loads((workspace/'tmp/clickhouse-cutover/current.json').read_text())
root=Path(setup['root']);dest=workspace/'docs/evaluation/assets/clickhouse-cutover'
if dest.exists():raise ValueError('Preserve existing evidence; review explicitly before replacing')
def read(name):return json.loads((root/name).read_text())
def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
native=read('native-default-browser/result.json');pg=read('postgres-rollback-browser-clock-fixed-final/result.json')
returned=read('native-return-browser/result.json');parity=read('final-six-br-parity.json')
inputs=read('rollback-inputs.json');scenarios=read('rollback-verification.json');backup=read('accepted-rollback-backup.json')
for gate in (native,pg,returned,parity,inputs,scenarios,backup):
    if gate['status']!='passed':raise ValueError('All final gates must pass before archiving')
if len(native['checks'])!=9 or len(pg['checks'])!=9 or len(returned['checks'])!=1:
    raise ValueError('Native initial / PG full / native return smoke scopes differ')
if len(inputs['replays'])!=7 or not all(p['all_metadata_fields_exact'] for p in inputs['replays']):
    raise ValueError('Replay clock/identity/state must all match, including old partial pins')
if scenarios['scenario_versions']!=178 or not parity['all_six_brs'] or sum(p['episodes'] for p in parity['threshold_groups'])!=67829:
    raise ValueError('Scenario or six-BR parity gate incomplete')
dest.mkdir(parents=True)
sources=[]
def copy(name,target=None):
    source=root/name;target=dest/(target or name)
    target.parent.mkdir(parents=True,exist_ok=True)
    if source.suffix=='.log':
        text=source.read_text().replace('\r\n','\n').replace('\r','\n')
        target.write_text('\n'.join(line.rstrip() for line in text.splitlines())+'\n')
    else:shutil.copy2(source,target)
    sources.append(dict(source=name,source_sha256=sha(source),artifact=target.relative_to(dest).as_posix(),
        artifact_sha256=sha(target),log_normalized=source.suffix=='.log'))

for name in ('cutoff-receipt.json','post-cutover-receipt.json','baseline-map.json',
        'native-commands.json','native-correction.json','native-corrected-publication.json',
        'rollback-inputs.json','rollback-verification.json','postgres-corrected-publication.json',
        'final-six-br-parity.json','journal-backup-receipt.json','accepted-rollback-backup.json',
        'failed-clock-backup-verified.json','phase-times.jsonl',
        'baseline-before-config-import-differences.json','docker-build-cache-cleanup.txt'):
    copy(name)
for folder in ('native-default-browser','postgres-rollback-browser-clock-fixed-final','native-return-browser'):
    copy(folder+'/result.json')
    for image in sorted((root/folder).glob('*.png')):copy(image.relative_to(root).as_posix())
copy('postgres-rollback-browser-v2/replay.png','failed-clock-attempt/replay-caption-before-fix.png')
for name in ('packaged-native-suite.log','default-routing-tests.log','postgres-cost-regression.log',
        'freeze-cutoff.log','baseline-parity.log','baseline-parity-with-config.log','baseline-parity-alias-map.log',
        'native-new-commands.log','native-new-correction.log','native-new-sensor.log',
        'native-corrected-rebuild.log','rollback-verification.log','rollback-verification-mapping.log',
        'clock-fix-inputs.log','clock-fix-rebuild.log','clock-fix-verification.log',
        'clock-fix-verification-retry.log','clock-fix-full-parity.log','clock-fix-reset-restore.log',
        'native-default-browser.log','postgres-rollback-browser.log','clock-fix-pg-ui-browser-final.log',
        'native-default-cli-setup.log','native-return-browser.log','resume-native-final.log'):
    copy(name,'logs/'+name)
for name in sorted(root.glob('archive-retention-*.json')):copy(name.relative_to(root).as_posix())

# Deduplicate exact helper revisions by content, retaining per-stage source maps.
for label in ('native-new-correction','rollback-inputs','rollback-verification-mapping',
        'clock-fix-inputs','clock-fix-full-parity','resume-native-final'):
    folder=root/'helper-revisions'/label;manifest=json.loads((folder/'manifest.json').read_text());references={}
    for name,expected in manifest['helper_hashes'].items():
        source=folder/name
        if sha(source)!=expected:raise ValueError('Executed helper snapshot changed')
        target=dest/'helpers'/Path(name).parent/(Path(name).stem+'-'+expected+'.py')
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():shutil.copy2(source,target)
        references[name]=dict(sha256=expected,artifact=target.relative_to(dest).as_posix())
    stage=dest/'stages'/(label+'.json');stage.parent.mkdir(exist_ok=True)
    stage.write_text(json.dumps(dict(manifest,archived_helper_references=references),indent=2)+'\n')

local=[]
for name in ('cutoff.json','post-cutover.json','journal-backup/cutover-journal.sqlite',
        'journal-backup-clock-fix/cutover-journal.sqlite','failed-clock-attempt.dump','accepted-rollback.dump'):
    p=root/name;local.append(dict(path=name,bytes=p.stat().st_size,sha256=sha(p),kept_locally=True))
runtime={}
for name in ('gh-ch-repro-20261107032510-rebuilt-ch-app-1',
        'gh-ch-repro-20261107032510-rebuilt-ch-web-1','gh-ch-repro-20261107032510-rebuilt-clickhouse-1',
        'gh-cutover-pg-202611070931-rollback-db-1','smart-greenhouse-db-1','smart-greenhouse-app-1','smart-greenhouse-broker-1'):
    value=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    runtime[name]=dict(status=value['State']['Status'],container_id=value['Id'],image_id=value['Image'],
        started_at=value['State']['StartedAt'],data_volumes=[m.get('Name') for m in value['Mounts'] if m['Type']=='volume'])
    if name.endswith('rebuilt-ch-app-1'):
        env=dict(v.split('=',1) for v in value['Config']['Env'] if '=' in v)
        runtime[name].update(backend=env.get('GREENHOUSE_BACKEND'),pg_environment_keys=[k for k in env if k.startswith('PG')],
            required_journal=env.get('GREENHOUSE_CH_REQUIRE_CUTOVER_JOURNAL'))
        if env.get('GREENHOUSE_BACKEND')!='clickhouse' or any(k.startswith('PG') for k in env):raise ValueError('Default app must be native without PG credentials')
for name in ('gh-cutover-pg-202611070931-rollback-db-1','smart-greenhouse-db-1'):
    if runtime[name]['status']!='exited':raise ValueError('Project PostgreSQL must be stopped at final native smoke')
phases=[json.loads(line) for line in (root/'phase-times.jsonl').read_text().splitlines()]
by_name={v['phase']:v for v in phases}
switch=by_name['return_to_native_switch_started']['monotonic_seconds']
timing=dict(return_http_ready_seconds=by_name['native_return_http_healthy']['monotonic_seconds']-switch,
    return_kpi_ready_seconds=by_name['native_return_kpi_ready']['monotonic_seconds']-switch,
    native_nine_screen_seconds=native['elapsed_seconds'],pg_nine_screen_seconds=pg['elapsed_seconds'],
    scope='Measured rehearsal, includes validation/repairs; HTTP readiness and KPI readiness are distinct. Not a zero-downtime or warm-query benchmark.')
summary=dict(status='passed',recorded_at=datetime.now(timezone.utc).isoformat(),parent_source_head=setup['source_head'],
    current_native_publication=parity['clickhouse_publication'],restored_pg_publication=parity['postgres_publication'],
    configuration_versions=inputs['native_configurations_preserved'],scenario_versions=scenarios['scenario_versions'],
    scenario_collision_families=scenarios['collision_families'],original_pg_scenarios_unchanged=True,
    source_namespace_lookup_and_retry_exact=True,replay_pins=7,replay_scopes=4,all_replay_metadata_fields_exact=True,
    post_cutover_correction=read('native-correction.json'),six_brs_business_csv_lineage_and_raw_oracle_exact=True,
    threshold_groups=18,episodes=67829,journal_cursor=inputs['journal_cursor'],journal_head=inputs['journal_head'],
    journal_backup_exact=True,old_publication_unchanged=True,accepted_pg_backup=backup,
    tests=dict(native_suite='100 total, 91 run, 9 gated heavy skips',routing='8 passed, includes two new default-backend tests',pg_cost='24 passed, includes audit timestamp/author import'),
    timing=timing,runtime=runtime,local_archives=local,
    limits=['Single host/local POSIX lock; arbitrary DBA bypass is outside the writer protocol.',
        'Rehearsal covers the locked AGC snapshot, one audited resource correction, profiles/tariff/scenario and finite replay.',
        'New snapshot/basis/config families require verified translators and parity before opening PG.',
        'Legacy native timestamps originally NULL remain unknown in the source archive; PG import time is labeled separately.',
        'Initial preparation stop was not fully timestamped; recorded return readiness is not total historical outage.'])
(dest/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
(dest/'source-artifacts.json').write_text(json.dumps(sources,indent=2)+'\n')
files=[dict(path=p.relative_to(dest).as_posix(),bytes=p.stat().st_size,sha256=sha(p))
    for p in sorted(dest.rglob('*')) if p.is_file()]
(dest/'manifest.json').write_text(json.dumps(dict(status='passed',files=files),indent=2)+'\n')
print(json.dumps({'status':'passed','files':len(files),'output':str(dest)}))
