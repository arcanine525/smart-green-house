"""Read-only post-recovery source/history and receipt assertions."""
import json
from datetime import datetime, timezone
from pathlib import Path
import psycopg
from greenhouse.db.pipeline import read_workflow
from greenhouse.settings import Settings

old_publications = {
    '11d0e3523c024107936a2091e99ddec8', '3459db897ead45a28177c60000bd0561',
    '50c3485dd763453cb6c4395016d7554d', '804770b6ac534b308ad4e449487db50a',
    'adf2a02900864da2bf0e41b86516f6ef', 'dbc3d6cbf9fa4914b37137455e85576e'}
settings = Settings.from_env()
assert settings.pg_database == 'greenhouse_t07_195889f1a3b1'
with psycopg.connect(**settings.connection_kwargs(), autocommit=True,
                    options='-c default_transaction_read_only=on') as conn:
    run = read_workflow(conn, 'drain-br03-recovered-20261105')
    assert run['status'] == 'completed' and run['publication_id']
    pub = run['publication_id']
    old = read_workflow(conn, 'drain-br03')
    assert old['publication_id'] is None and old['status'] == 'running'
    assert old['input_spec']['raw_manifest_checksum'] == 'ae95152e42dd51ff6b28497250d40eb51537eb61dab66932dd7c5657b6f7dc0f'
    pubs = {row[0] for row in conn.execute('SELECT publication_id FROM meta.publication')}
    assert pubs == old_publications | {pub} and pub not in old_publications
    assert conn.execute('SELECT publication_id FROM meta.current_publication WHERE data_scope_id=%s',
                        (run['data_scope_id'],)).fetchone() == (pub,)
    counts = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
        for table in ('core.record_revision', 'core.climate_observation',
                      'core.drain_observation', 'core.measure_quality', 'meta.correction')}
    assert tuple(counts.values()) == (574843, 286854, 286854, 2874794, 0)
    expected = {'irrigation': 'irrigation-mart-v1', 'harvest': 'harvest-mart-v3',
                'climate': 'climate-mart-v1', 'drain': 'drain-mart-v1'}
    receipts = {}
    for kind, version in expected.items():
        row = conn.execute('SELECT to_jsonb(p) FROM mart.' + kind + '_publication p WHERE publication_id=%s',
                            (pub,)).fetchone()[0]
        assert row['bundle_version'] == version and row['cycle_count'] == 6
        receipts[kind] = row
    steps = conn.execute('SELECT step,status,attempt FROM meta.workflow_step WHERE workflow_id=%s ORDER BY step',
                          (run['workflow_id'],)).fetchall()
    assert {step: (status, attempt) for step, status, attempt in steps} == {
        'ingest': ('completed', 1), 'load-core': ('completed', 1),
        'spark': ('completed', 1), 'publish': ('completed', 2)}
    result = {'generated_at': datetime.now(timezone.utc).isoformat(), 'database': settings.pg_database,
        'workflow': run['workflow_id'], 'publication': pub, 'versions': run['versions'],
        'new_raw_manifest_checksum': run['input_spec']['raw_manifest_checksum'],
        'old_workflow_unchanged': True, 'old_publications': sorted(old_publications),
        'all_publications': sorted(pubs), 'exactly_one_new_publication': True,
        'source_counts': counts, 'receipts': receipts, 'steps': steps}
    Path('/evidence/br03-publication.json').write_text(json.dumps(result, indent=2, default=str) + '\n')
    print(json.dumps({'publication': pub, 'publication_count': len(pubs), 'source_counts': counts,
                      'receipt_versions': expected}))
