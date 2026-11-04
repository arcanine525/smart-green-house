"""Recoverable batch workflow. Spark prepares artifacts; only psycopg publishes.

Call with an idle connection in autocommit mode: each persisted transition and
publication has its own explicit transaction. Hooks receive that SAME connection.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from greenhouse.db.loader import CORE_KINDS, digest_file, load_core, safe_child, staging_records
from greenhouse.db.migrate import sql
from greenhouse.ingestion.parser import PARSER_VERSION
from greenhouse.ingestion.run import ingest_snapshot

CODE_VERSION = 'pipeline-v4-climate'
STEPS = ('ingest', 'load-core', 'spark', 'publish')



def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def require_idle(conn):
    from psycopg.pq import TransactionStatus
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError('Workflow requires an idle autocommit connection')


def lock_scope(conn, scope):
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('greenhouse:publish:' + scope,))


def fail(point, selected):
    if point == selected:
        raise RuntimeError('Injected failure: ' + point)


def config(conn, scope, policy):
    """Frozen workflow identity: parser/policy/basis + bundle versions, so a
    corrected bundle cannot be silently reused through a no-change checksum."""
    quality = conn.execute('SELECT definition_hash FROM config.quality_policy_version WHERE policy_version_id=%s', (policy,)).fetchone()
    if not quality:
        raise ValueError('Unknown quality policy')
    bases = conn.execute('SELECT basis_version_id,analysis_context FROM config.selected_basis WHERE data_scope_id=%s AND applied ORDER BY basis_version_id,analysis_context', (scope,)).fetchall()
    # Lifecycle note: on THIS release the export identity adds bundle versions.
    from greenhouse.db.harvest_mart import BUNDLE_VERSION as HARVEST_BUNDLE
    from greenhouse.db.irrigation_mart import BUNDLE_VERSION as IRRIGATION_BUNDLE
    from greenhouse.db.climate_mart import BUNDLE_VERSION as CLIMATE_BUNDLE
    return {'parser': PARSER_VERSION, 'quality_policy': policy, 'quality_hash': quality[0],
            'basis': [list(b) for b in bases], 'code': CODE_VERSION,
            'marts': [IRRIGATION_BUNDLE, HARVEST_BUNDLE, CLIMATE_BUNDLE]}


def artifact_files(folder):
    """Explicit files only, no directory glob accepted as a consumption contract."""
    return [{'path': p.relative_to(folder).as_posix(), 'sha256': digest_file(p), 'bytes': p.stat().st_size}
            for p in sorted(folder.rglob('*')) if p.is_file() and p.name != 'manifest.json']


def verify_artifact(artifact):
    folder = Path(artifact['folder'])
    if digest_file(folder / 'manifest.json') != artifact['checksum']:
        raise ValueError('Artifact manifest checksum mismatch')
    manifest = json.loads((folder / 'manifest.json').read_text())
    for entry in manifest['files']:
        path = safe_child(folder, entry['path'])
        if path.stat().st_size != entry['bytes'] or digest_file(path) != entry['sha256']:
            raise ValueError('Artifact file checksum mismatch: ' + entry['path'])
    return manifest


def save_artifact(folder, payload):
    payload = payload | {'files': artifact_files(folder)}
    (folder / 'manifest.json').write_text(encode(payload) + '\n')
    return {'folder': str(folder), 'checksum': digest_file(folder / 'manifest.json')}


def log_event(folder, **event):
    with (folder / 'steps.jsonl').open('a') as stream:
        stream.write(encode({'at': datetime.now(timezone.utc).isoformat(), **event}) + '\n')


def ingestion_artifact(folder, raw, target):
    manifest = json.loads((folder / 'manifest.json').read_text())
    original = json.loads((raw / 'manifest.json').read_text())
    if manifest['status'] not in ('completed', 'completed_with_issues') or manifest['parser_version'] != PARSER_VERSION:
        raise ValueError('Ingestion is incomplete or parser version differs')
    if manifest['snapshot_id'] != original['snapshot_id'] or manifest['snapshot_sha'] != original['archive']['sha256']:
        raise ValueError('Raw snapshot identity mismatch')
    entries = {f['path']: f for f in original['files']}
    files = {f['source_file']: f for f in manifest['files']}
    if len(entries) != len(original['files']) or len(files) != len(manifest['files']) or entries.keys() != files.keys():
        raise ValueError('Ingestion file set mismatch')
    checks = []
    for path, file in files.items():
        entry = entries[path]
        if file['file_checksum'] != entry['sha256'] or file.get('expected_rows') != entry.get('csv', {}).get('data_rows'):
            raise ValueError('Ingestion file metadata mismatch')
        if digest_file(safe_child(raw, path)) != entry['sha256']:
            raise ValueError('Raw file checksum mismatch')
        if file['read'] != sum(file[k] for k in ('accepted', 'quarantine', 'duplicate', 'conflict')):
            raise ValueError('Ingestion counts unbalanced')
        # Status/outputs_consumable are authoritative; never open deferred/failed output.
        if file['status'] in ('loaded', 'loaded_with_quarantine') and file.get('outputs_consumable', True):
            for relative in file['outputs'].values():
                checks.append({'path': relative, 'sha256': digest_file(safe_child(folder, relative))})
    if manifest['counts'] != {k: sum(f[k] for f in files.values()) for k in ('read', 'accepted', 'quarantine', 'duplicate', 'conflict')}:
        raise ValueError('Ingestion run counts unbalanced')
    target.mkdir()
    (target / 'ingestion.json').write_text(encode({'folder': str(folder), 'manifest_checksum': digest_file(folder / 'manifest.json'), 'outputs': checks}) + '\n')
    return save_artifact(target, {'rows_in': manifest['counts']['read'], 'rows_out': manifest['counts']['accepted'], 'run_id': manifest['run_id']})


def verify_ingestion(artifact, raw):
    verify_artifact(artifact)
    receipt = json.loads((Path(artifact['folder']) / 'ingestion.json').read_text())
    folder = Path(receipt['folder'])
    if digest_file(folder / 'manifest.json') != receipt['manifest_checksum']:
        raise ValueError('Ingestion manifest changed')
    for entry in receipt['outputs']:
        if digest_file(safe_child(folder, entry['path'])) != entry['sha256']:
            raise ValueError('Ingestion output changed')
    manifest = json.loads((folder / 'manifest.json').read_text())
    for entry in json.loads((raw / 'manifest.json').read_text())['files']:
        if digest_file(safe_child(raw, entry['path'])) != entry['sha256']:
            raise ValueError('Raw file checksum mismatch')
    return folder, manifest


def decimal_key(value):
    if value is None:
        return None
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError('Nonfinite candidate')
    if not d:
        return '0'
    sign, digits, exponent = d.as_tuple()
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop(); exponent += 1
    return ('-' if sign else '') + ''.join(map(str, digits)) + 'e' + str(exponent)


class Fingerprint:
    """Order independent exact-value multiset fingerprint, constant memory."""
    def __init__(self):
        self.total = self.count = 0

    def add(self, revision, key, raw_hash, code, value):
        self.total = (self.total + int(digest([revision, key, raw_hash, code, decimal_key(value)]), 16)) % (1 << 256)
        self.count += 1

    def result(self):
        return f'{self.total:064x}', self.count


def baseline_expected(conn, scope, folder, raw, manifest):
    fields = defaultdict(list)
    for kind, code in conn.execute('SELECT record_type,measure_code FROM config.measure_definition'):
        fields[kind].append(code)
    mappings = dict(conn.execute('SELECT source_folder,cycle_id FROM meta.source_folder_mapping WHERE snapshot_id=%s', (manifest['snapshot_id'],)))
    fp = Fingerprint()
    for file in manifest['files']:
        kind = Path(file['source_file']).stem
        if kind not in CORE_KINDS or file['status'] not in ('loaded', 'loaded_with_quarantine') or not file.get('outputs_consumable', True):
            continue
        cycle = mappings[Path(file['source_file']).parent.as_posix()]
        types = {'Resources': ('resource',), 'Production': ('harvest',), 'GreenhouseClimate': ('climate', 'drain')}[kind]
        for row in staging_records(folder, raw, manifest, file):
            eligible = row['disposition'] == 'accepted' or (row['disposition'] == 'quarantine' and row['logical_key'] is not None and row['issues'] and all(i['code'] == 'invalid_numeric' for i in row['issues']))
            if not eligible:
                continue
            for record_type in types:
                revision = hashlib.md5(f"{scope}:{row['source_record_id']}:{record_type}:1".encode()).hexdigest()
                key = cycle + ':' + (row['source_record_id'] if record_type == 'harvest' else row['logical_key'])
                for code in fields[record_type]:
                    fp.add(revision, key, row['raw_hash'], code, row['values'].get(code))
    return fp.result()


def baseline_actual(conn, scope):
    fp = Fingerprint()
    with conn.cursor(name='baseline_' + uuid4().hex) as cursor:
        cursor.itersize = 1000
        cursor.execute(sql('pipeline_baseline'), (scope,))
        for revision, key, raw_hash, code, value in cursor:
            fp.add(revision, key, raw_hash, code, value)
    return fp.result()


def membership(conn, workflow):
    h, count = hashlib.sha256(), 0
    with conn.cursor(name='members_' + uuid4().hex) as cursor:
        cursor.itersize = 1000
        cursor.execute('SELECT revision_id FROM meta.workflow_candidate WHERE workflow_id=%s ORDER BY revision_id', (workflow,))
        for (revision,) in cursor:
            h.update((revision + '\n').encode()); count += 1
    return {'membership_checksum': h.hexdigest(), 'member_count': count}


def load_or_reuse(conn, scope, policy, workflow, folder, raw, manifest, target):
    with conn.transaction():
        lock_scope(conn, scope)
        identity = conn.execute(sql('load_scope'), (scope,)).fetchone()
        if identity != (manifest['snapshot_id'], manifest['snapshot_sha']):
            raise ValueError('Different snapshot requires its own data_scope')
        expected = baseline_expected(conn, scope, folder, raw, manifest)
        receipt = conn.execute('SELECT parser_version,quality_policy_version,fingerprint,measure_rows FROM meta.core_baseline_receipt WHERE data_scope_id=%s', (scope,)).fetchone()
        exists = conn.execute('SELECT 1 FROM meta.pipeline_run WHERE data_scope_id=%s AND status=\'core_loaded\' LIMIT 1', (scope,)).fetchone()
        reused = bool(exists)
        if receipt:
            if receipt != (PARSER_VERSION, policy, *expected):
                raise ValueError('Baseline differs: use explicit meta.correction, never mutate raw')
        else:
            if not exists:
                result = load_core(conn, folder, raw, scope, policy)
                if result['status'] != 'core_loaded':
                    raise ValueError('Core load blocked')
            if baseline_actual(conn, scope) != expected:
                raise ValueError('Existing core does not match verified baseline')
            conn.execute('INSERT INTO meta.core_baseline_receipt VALUES (%s,%s,%s,%s,%s,%s)', (scope, PARSER_VERSION, policy, *expected, 'Verified source/parser/values by workflow ' + workflow))
        run = manifest['run_id']
        old = conn.execute('SELECT data_scope_id,quality_policy_version,source_manifest FROM meta.pipeline_run WHERE run_id=%s', (run,)).fetchone()
        if old and old != (scope, policy, manifest):
            raise ValueError('Ingestion run identity changed')
        if not old:
            conn.execute("INSERT INTO meta.pipeline_run VALUES (%s,%s,%s,'core_loaded',%s,%s,%s)", (run, scope, manifest['snapshot_id'], PARSER_VERSION, policy, Jsonb(manifest)))
            # Baseline identity/value receipt verified above; explicit metadata-only reuse.
            for file in manifest['files']:
                fid = hashlib.sha256((manifest['snapshot_sha'] + ':' + file['source_file']).encode()).hexdigest()
                oldfile = conn.execute('SELECT checksum,expected_rows FROM meta.source_file WHERE source_file_id=%s', (fid,)).fetchone()
                if oldfile != (file['file_checksum'], file.get('expected_rows')):
                    raise ValueError('Reused file identity mismatch')
                allowed = file['status'] in ('loaded', 'loaded_with_quarantine') and file.get('outputs_consumable', True)
                status = file['status'] if Path(file['source_file']).stem in CORE_KINDS and allowed else 'deferred'
                conn.execute('INSERT INTO meta.file_ingestion VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (run, fid, file['status'], status, allowed, *(file[k] for k in ('read','accepted','quarantine','duplicate','conflict')), Jsonb(file | {'cross_run': 'reused'})))
        conn.execute('UPDATE meta.workflow_run SET ingestion_run_id=%s WHERE workflow_id=%s', (run, workflow))
        conn.execute('DELETE FROM meta.workflow_candidate WHERE workflow_id=%s', (workflow,))
        conn.execute(sql('pipeline_candidates'), (workflow, scope))
        frozen = membership(conn, workflow)
        target.mkdir()
        (target / 'core.json').write_text(encode(frozen | {'reused': reused, 'baseline_fingerprint': expected[0]}) + '\n')
        return save_artifact(target, frozen | {'rows_in': manifest['counts']['read'], 'rows_out': frozen['member_count'], 'reused': reused})


def read_workflow(conn, workflow):
    with conn.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute('SELECT * FROM meta.workflow_run WHERE workflow_id=%s', (workflow,)).fetchone()
    if row is None:
        raise ValueError('Unknown workflow')
    return row


def pin_publication(conn, scope):
    row = conn.execute('SELECT publication_id FROM meta.current_publication WHERE data_scope_id=%s', (scope,)).fetchone()
    if not row:
        raise ValueError('Scope has no publication')
    return row[0]


def pinned_values(conn, publication_id, measure, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError('Limit must be 1–1000')
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(sql('pinned_values'), (publication_id, measure, limit)).fetchall()


def publish(conn, workflow, hook=None, fault=None):
    """Hook(conn, publication_id, workflow_id) must use conn, never commit/open JDBC.

    Called after membership and config pins, before the seal and pointer. Failures
    roll back hook writes AND all publication writes. A no-change rerun skips hook.
    """
    require_idle(conn)
    with conn.transaction():
        run = read_workflow(conn, workflow)
        lock_scope(conn, run['data_scope_id'])
        if run['publication_id']:
            return {'publication_id': run['publication_id'], 'reused': True}
        steps = dict(conn.execute("SELECT step,artifact FROM meta.workflow_step WHERE workflow_id=%s AND status='completed'", (workflow,)))
        if any(step not in steps for step in STEPS[:-1]):
            raise ValueError('Upstream steps not completed')
        for artifact in steps.values():
            verify_artifact(artifact)
        verify_ingestion(steps['ingest'], Path(run['input_spec']['snapshot']))
        if config(conn, run['data_scope_id'], run['versions']['quality_policy']) != run['versions']:
            raise ValueError('Configuration changed; start a new workflow')
        if conn.execute(sql('candidate_stale'), (run['data_scope_id'], workflow)).fetchone()[0]:
            raise ValueError('Candidate is stale; start a new workflow')
        candidate = membership(conn, workflow)
        stage = verify_artifact(steps['spark'])
        if any(stage[k] != candidate[k] for k in candidate):
            raise ValueError('Spark candidate membership mismatch')
        if conn.execute(sql('publish_validate'), (workflow, run['versions']['quality_policy'], run['data_scope_id'])).fetchone()[0]:
            raise ValueError('Candidate subtype or quality incomplete')
        current = conn.execute('SELECT p.publication_id,p.config_bundle_hash,s.membership_checksum FROM meta.current_publication c JOIN meta.publication p USING(publication_id) JOIN meta.publication_seal s USING(publication_id) WHERE c.data_scope_id=%s', (run['data_scope_id'],)).fetchone()
        config_hash = digest(run['versions'])
        reused = bool(current and current[1:] == (config_hash, candidate['membership_checksum']))
        publication = current[0] if reused else uuid4().hex
        if not reused:
            conn.execute("INSERT INTO meta.publication SELECT %s,%s,%s,%s,%s,max(r.observed_at),now(),'provisional' FROM meta.workflow_candidate w JOIN core.record_revision r USING(revision_id) WHERE w.workflow_id=%s", (publication, run['data_scope_id'], run['ingestion_run_id'], run['versions']['quality_policy'], config_hash, workflow))
            conn.execute('INSERT INTO meta.publication_member SELECT %s,r.data_scope_id,r.record_type,r.logical_record_key,r.revision_id FROM meta.workflow_candidate w JOIN core.record_revision r USING(revision_id) WHERE w.workflow_id=%s', (publication, workflow))
            conn.execute('INSERT INTO meta.publication_basis SELECT %s,b.data_scope_id,b.compartment_id,b.measure_code,w.analysis_context,b.basis_version_id FROM meta.workflow_basis w JOIN config.area_basis_version b USING(basis_version_id) WHERE w.workflow_id=%s', (publication, workflow))
            fail('publish-members', fault)
            from greenhouse.db.irrigation_mart import materialize_irrigation
            from greenhouse.db.harvest_mart import materialize_harvest
            from greenhouse.db.climate_mart import materialize_climate
            materialize_irrigation(conn, publication, workflow)
            materialize_harvest(conn, publication, workflow)
            materialize_climate(conn, publication, workflow)
            if hook:
                hook(conn, publication, workflow)
            fail('publish-hook', fault)
            conn.execute('INSERT INTO meta.publication_seal(publication_id,workflow_id,membership_checksum,member_count,artifact_checksum) VALUES (%s,%s,%s,%s,%s)', (publication, workflow, candidate['membership_checksum'], candidate['member_count'], steps['spark']['checksum']))
            conn.execute('INSERT INTO meta.current_publication VALUES (%s,%s) ON CONFLICT(data_scope_id) DO UPDATE SET publication_id=excluded.publication_id', (run['data_scope_id'], publication))
            fail('publish-pointer', fault)
        result = {'publication_id': publication, 'reused': reused, **candidate}
        conn.execute("UPDATE meta.workflow_run SET status='completed',publication_id=%s,finished_at=clock_timestamp() WHERE workflow_id=%s", (publication, workflow))
        # This transition shares the publication transaction (crash after commit is resumable).
        conn.execute("UPDATE meta.workflow_step SET status='completed',finished_at=clock_timestamp(),rows_in=%s,rows_out=%s,artifact=%s,artifact_checksum=%s,error=NULL WHERE workflow_id=%s AND step='publish'", (candidate['member_count'], candidate['member_count'], Jsonb(steps['spark']), steps['spark']['checksum'], workflow))
        return result


def run_pipeline(conn, snapshot, output, scope, policy, *, workflow=None, resume=False, ingestion_run=None, fault=None, hook=None, stage_function=None):
    require_idle(conn)
    snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
    if snapshot == output or snapshot in output.parents or output in snapshot.parents:
        raise ValueError('Snapshot and output must be disjoint')
    workflow = workflow or uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', workflow):
        raise ValueError('Invalid workflow id')
    root = output / 'pipeline' / workflow
    # Session lock also serializes recovery attempts for the same workflow. Scope
    # transaction locks serialize loaders, corrections and publishers separately.
    conn.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', ('greenhouse:workflow:' + workflow,))
    try:
        if not resume:
            root.mkdir(parents=True, exist_ok=False)
            original = json.loads((snapshot / 'manifest.json').read_text())
            versions = config(conn, scope, policy)
            with conn.transaction():
                conn.execute("INSERT INTO meta.workflow_run(workflow_id,data_scope_id,snapshot_id,input_spec,versions,status) VALUES (%s,%s,%s,%s,%s,'running')", (workflow, scope, original['snapshot_id'], Jsonb({'snapshot': str(snapshot), 'output': str(output), 'raw_manifest_checksum': digest_file(snapshot / 'manifest.json'), 'ingestion_run': str(Path(ingestion_run).resolve()) if ingestion_run else None}), Jsonb(versions)))
                for basis, context in versions['basis']:
                    conn.execute('INSERT INTO meta.workflow_basis VALUES (%s,%s,%s)', (workflow, basis, context))
        run = read_workflow(conn, workflow)
        if run['input_spec']['snapshot'] != str(snapshot) or run['input_spec']['output'] != str(output) or run['data_scope_id'] != scope or run['versions'] != config(conn, scope, policy):
            raise ValueError('Resume input/configuration differs')
        if digest_file(snapshot / 'manifest.json') != run['input_spec']['raw_manifest_checksum']:
            raise ValueError('Raw manifest changed')
        for step in STEPS:
            state = conn.execute('SELECT status,attempt,artifact FROM meta.workflow_step WHERE workflow_id=%s AND step=%s', (workflow, step)).fetchone()
            if state and state[0] == 'completed':
                verify_artifact(state[2])
                if step == 'ingest':
                    verify_ingestion(state[2], snapshot)
                log_event(root, workflow=workflow, step=step, status='verified_skip')
                continue
            attempt = state[1] + 1 if state else 1
            with conn.transaction():
                conn.execute("UPDATE meta.workflow_run SET status='running',finished_at=NULL WHERE workflow_id=%s", (workflow,))
                conn.execute("INSERT INTO meta.workflow_step(workflow_id,step,status,attempt,started_at,versions) VALUES (%s,%s,'running',%s,clock_timestamp(),%s) ON CONFLICT(workflow_id,step) DO UPDATE SET status='running',attempt=excluded.attempt,started_at=excluded.started_at,finished_at=NULL,error=NULL,artifact=NULL,artifact_checksum=NULL", (workflow, step, attempt, Jsonb(run['versions'])))
            log_event(root, workflow=workflow, step=step, attempt=attempt, status='running', versions=run['versions'])
            target = root / f'{step}-{attempt}-{uuid4().hex}'
            try:
                fail('before-' + step, fault)
                if step == 'ingest':
                    folder = run['input_spec']['ingestion_run']
                    if folder is None:
                        folder, _ = ingest_snapshot(snapshot, json.loads((snapshot / 'manifest.json').read_text()), output)
                    artifact = ingestion_artifact(Path(folder), snapshot, target)
                elif step == 'load-core':
                    upstream = conn.execute("SELECT artifact FROM meta.workflow_step WHERE workflow_id=%s AND step='ingest'", (workflow,)).fetchone()[0]
                    folder, manifest = verify_ingestion(upstream, snapshot)
                    artifact = load_or_reuse(conn, scope, policy, workflow, folder, snapshot, manifest, target)
                elif step == 'spark':
                    if stage_function is None:
                        from greenhouse.db.spark_stage import stage_candidates
                        stage_function = stage_candidates
                    artifact = stage_function(conn, workflow, target, fault=fault)
                else:
                    result = publish(conn, workflow, hook=hook, fault=fault)
                    log_event(root, workflow=workflow, step=step, status='completed', **result)
                    continue
                fail('after-' + step, fault)
                data = verify_artifact(artifact)
                with conn.transaction():
                    conn.execute("UPDATE meta.workflow_step SET status='completed',finished_at=clock_timestamp(),rows_in=%s,rows_out=%s,artifact=%s,artifact_checksum=%s WHERE workflow_id=%s AND step=%s", (data['rows_in'], data['rows_out'], Jsonb(artifact), artifact['checksum'], workflow, step))
                log_event(root, workflow=workflow, step=step, status='completed', rows_in=data['rows_in'], rows_out=data['rows_out'], artifact=artifact)
            except Exception as error:
                # Log even when the engine disappeared. Do not turn a committed
                # step/publication into failed because its post-commit log failed.
                try:
                    log_event(root, workflow=workflow, step=step, status='error', error_type=type(error).__name__)
                except OSError:
                    pass
                if not conn.closed:
                    state = conn.execute('SELECT status FROM meta.workflow_step WHERE workflow_id=%s AND step=%s', (workflow, step)).fetchone()
                    if state != ('completed',):
                        with conn.transaction():
                            conn.execute("UPDATE meta.workflow_step SET status='failed',finished_at=clock_timestamp(),error=%s WHERE workflow_id=%s AND step=%s", (Jsonb({'type': type(error).__name__, 'injected': fault}), workflow, step))
                            conn.execute("UPDATE meta.workflow_run SET status='failed',finished_at=clock_timestamp() WHERE workflow_id=%s", (workflow,))
                raise
        final = read_workflow(conn, workflow)
        return {'workflow_id': workflow, 'status': final['status'], 'publication_id': final['publication_id'], 'output': str(root)}
    finally:
        if not conn.closed:
            conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', ('greenhouse:workflow:' + workflow,))
