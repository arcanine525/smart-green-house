"""Publish bounded recovery receipts only after every acceptance gate passes.

Full native metadata/DTO exports, raw files, ready envelopes and tar backups
remain local. The manifest binds their checksums without copying their payloads
into Git. Failed attempts are retained as diagnostics, never marked successful.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    setup = json.loads(args.setup.read_text())
    root = Path(setup['root'])
    source = Path(setup['state'])
    restored = root / 'restored/state'
    rebuilt = root / 'rebuilt/state'
    read = lambda p: json.loads(p.read_text())
    fresh, restored_parity, rebuilt_parity = (read(p) for p in (
        source/'parity/parity.json', root/'evidence/physical-inventory-match.json', rebuilt/'parity/parity.json'))
    assert fresh['status'] == rebuilt_parity['status'] == 'passed'
    assert fresh['all_six_brs'] and rebuilt_parity['all_six_brs']
    assert fresh['episodes_compared'] == rebuilt_parity['episodes_compared'] == 67829
    assert restored_parity['status'] == 'passed' and restored_parity['all_tables_exact']
    assert fresh['postgres_publication'] == rebuilt_parity['postgres_publication']
    assert fresh['clickhouse_publication'] != rebuilt_parity['clickhouse_publication']
    for state in (source, restored, rebuilt):
        assert len(read(state/'historical-ui.json')['screens']) == 8
    physical_config = read(restored/'configuration-physical-proof.json')
    rebuilt_config = read(rebuilt/'configuration-rebuilt-proof.json')
    assert physical_config['status'] == rebuilt_config['status'] == 'passed'
    assert physical_config['scenario_versions'] == rebuilt_config['scenario_versions'] == 175
    assert physical_config['exact_configuration_versions'] == 302
    assert rebuilt_config['exact_configuration_versions'] + rebuilt_config['explicitly_mapped_versions'] == 302
    for state in (restored, rebuilt):
        assert read(state/'native-profile-ui.json')['status'] == 'passed'
        assert read(state/'sensor-ui.json')['status'] == 'passed'
    restored_sensor, rebuilt_sensor = read(restored/'sensor/restore-result.json'), read(rebuilt/'sensor/rebuild-result.json')
    for value in (restored_sensor, rebuilt_sensor):
        assert value['unique_events'] == 30 and value['original_envelopes_exact']
        assert value['complete'] and value['checksum_verified']
        assert value['business_fingerprint'] == '7783fdfa2f63909171709dea99b43bb5e6956af13fde3dca15ce4d1030d1f675'
    assert restored_sensor['queued_rows_received_before_retry'] == 28
    assert rebuilt_sensor['old_checkpoint_used'] is False
    assert read(rebuilt/'native-scenario-import.json')['native_business_exact']
    assert read(source/'offline-probe.json')['status'] == 'passed'
    suite = (root/'evidence/clean-tests-fixed.log').read_text()
    assert 'Ran 85 tests' in suite and 'OK (skipped=9)' in suite and 'FAILED' not in suite
    assert not subprocess.check_output(['git','diff',setup['source_head'],'--','src/greenhouse/clickhouse'],text=True)
    if args.destination.exists():
        raise ValueError('Evidence destination exists; preserve the previous generation')
    args.destination.mkdir(parents=True)
    copied = []

    def copy(path, name):
        target = args.destination / name
        shutil.copy2(path, target)
        copied.append(dict(path=name, bytes=target.stat().st_size, sha256=sha(target)))

    def summary(path, name, omit=()):
        value = read(path)
        value = {k:v for k,v in value.items() if k not in omit}
        value['original_local_receipt_sha256'] = sha(path)
        target = args.destination / name
        target.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
        copied.append(dict(path=name,bytes=target.stat().st_size,sha256=sha(target)))

    for label, state in (('source',source),('restored',restored),('rebuilt',rebuilt)):
        copy(state/'historical.json',label+'-historical.json')
        copy(state/'historical-ui.json',label+'-historical-ui.json')
    for label, path in (
        ('source-parity',source/'parity/parity.json'),('rebuilt-parity',rebuilt/'parity/parity.json'),
        ('source-inventory',source/'inventory.json'),('restored-inventory',restored/'restored-inventory.json'),
        ('source-settle',source/'settle.json'),('rebuilt-settle',rebuilt/'settle.json'),
        ('offline-probe',source/'offline-probe.json'),
        ('sensor-restored',restored/'sensor/restore-result.json')):
        copy(path,label+'.json')
    summary(rebuilt/'sensor/rebuild-result.json','sensor-rebuilt.json',('original_config','rebuilt_config'))
    for label,state,mode in (('restored',restored,'physical'),('rebuilt',rebuilt,'rebuilt')):
        summary(state/('configuration-'+mode+'-proof.json'),label+'-configuration.json',('preserved_identities','mapped_identities'))
        copy(state/'native-profile-ui.json',label+'-native-profile-ui.json')
        copy(state/'sensor-ui.json',label+'-sensor-ui.json')
    summary(rebuilt/'native-scenario-import.json','native-scenario-import.json',('scenario_versions',))
    copy(rebuilt/'native-rebuild-input.json','native-rebuild-input.json')
    for name in ('physical-inventory-match.json','runtime-isolation.json','internal-port-observation.json',
        'archive-probe/result.json','host-guard-regression.json','override-path-guard.json','source-fixture-cleanup.json',
        'failed-rebuild-fixture-cleanup.json','verified-restored-fixture-cleanup.json',
        'old-rehearsal-output-cache-cleanup.json','duplicate-input-cache-cleanup.json',
        'raw-rebuild-retry-preparation.json','unused-greenhouse-image-cleanup.json','docker-cache-headroom.json'):
        copy(root/'evidence'/name,name.replace('/','-'))
    for name in ('clean-tests.log','clean-tests-fixed.log','selector-regression-before.log',
        'shared-context-unit.log','native-archive-guards.log','source-prepare-state.log',
        'rebuilt-raw-rebuild.log','rebuilt-raw-rebuild-retry.log','restored-restored-verification.log',
        'restored-restored-verification-retry.log','backup.log','restore-physical.log','docker-cache-prune.log'):
        copy(root/'evidence'/name,name)
    backups = []
    for path in sorted((root/'backup').glob('*.sha256.json')):
        value = read(path)
        # Receipt suffix is .tar.gz.sha256.json.
        archive = path.with_name(path.name.removesuffix('.sha256.json'))
        assert archive.stat().st_size == value['bytes'] and sha(archive) == value['sha256']
        backups.append(dict(name=archive.name,bytes=value['bytes'],sha256=value['sha256']))
    bind = read(root/'backup/bind-state-inputs.json')
    assert sha(root/'backup/bind-state-inputs.tar.gz') == bind['archive_sha256']
    backups.append(dict(name='bind-state-inputs.tar.gz',bytes=bind['archive_bytes'],sha256=bind['archive_sha256']))
    native = read(source/'native-state.json')
    replay_audit = read(restored/'completed-replay-audit.json')
    files = [Path('src/greenhouse/app/shared_context.py'),Path('tests/clickhouse/test_threshold_ui.py'),
        Path('tests/unit/test_shared_context.py'),*sorted(Path('scripts/clickhouse_repro').glob('*.py')),
        Path('scripts/clickhouse_repro/README.md')]
    manifest = dict(status='passed',scope='Full AGC history plus a 30-event actual AGC replay selection',
        source_head=setup['source_head'],source_historical_publication=fresh['clickhouse_publication'],
        rebuilt_historical_publication=rebuilt_parity['clickhouse_publication'],
        native_archive_sha256=native['sha256'],completed_replay_audit_sha256=replay_audit['sha256'],
        source_metadata_file_sha256=sha(source/'source-metadata.json'),
        reference_files=[dict(path=p.name,sha256=sha(p)) for p in sorted((root/'references').glob('*.json'))],
        raw_or_metadata_payloads_committed=False,backups=backups,
        artifacts=copied,code_files=[dict(path=str(p),sha256=sha(p)) for p in files],
        native_package_unchanged_from_source_commit=True,ui_has_reviewed_shared_context_fix=True,
        privileged_host_reclaim_executed=False,failed_attempts_retained=True,
        scenario_versions_preserved=175,configuration_versions_preserved_or_mapped=302,
        physical_restore_all_tables_exact=True,raw_rebuild_fresh_checkpoint=True,
        original_raw_or_postgres_data_modified=False)
    (args.destination/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status='passed',artifacts=len(copied),code_files=len(files),destination=str(args.destination))))


if __name__ == '__main__':
    main()
