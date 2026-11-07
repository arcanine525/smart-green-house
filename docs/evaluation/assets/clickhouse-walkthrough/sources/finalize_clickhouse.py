"""Verify real recording files and publish bounded, video-free evidence.

All videos/downloads/traces stay in the explicitly supplied local directory.
Frames are decoded from the MP4s, never substituted with browser screenshots.
The caller reviews those frames before accepting the candidate.
"""
import argparse
import csv
from decimal import Decimal,localcontext
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def verify_oracle_exports(videos, context):
    files=[];checks=[];oracle=context['raw_oracle']
    def read(folder, pattern):
        paths=list((videos/folder/'downloads').glob(pattern))
        assert len(paths)==1, (folder,pattern)
        files.append(dict(file=str(paths[0].relative_to(videos)),sha256=digest(paths[0])))
        return json.loads(paths[0].read_text())
    def equal(label, actual, expected, tolerance='0'):
        with localcontext() as decimal_context:
            decimal_context.prec=60
            assert actual is not None and abs(Decimal(actual)-Decimal(expected))<=Decimal(tolerance), label
        checks.append(dict(label=label,actual=actual,expected=expected,absolute_tolerance=tolerance))
    water=read('br-02','*.json')['data']
    equal('irrigation',water['irrigation_observed_l_m2'],oracle['irrigation_l_m2'])
    equal('drain',water['drain_observed_l_m2'],oracle['drain_l_m2'])
    equal('paired_ratio',water['drain_fraction_pct'],oracle['drain_paired_percent'],'1e-24')
    energy=read('br-04','*-result.json')['data']
    equal('energy_kwh',energy['energy']['official_total_kwh'],oracle['energy_kwh'],'1e-24')
    assert Decimal(energy['official_a_kg'])==0 and energy['intensity_kwh_kg_a'] is None
    harvest=read('br-05','*.json')['summary']['data']
    equal('harvest_a',harvest['prod_a_observed'],oracle['harvest_a_kg_m2'])
    equal('harvest_b',harvest['prod_b_observed'],oracle['harvest_b_kg_m2'])
    equal('harvest_share_b',harvest['class_b_share_pct'],oracle['harvest_share_b_percent'],'1e-24')
    cost=read('br-06','*-comparison.json')['data']
    equal('baseline_eur',cost['baseline']['official_total_cost_eur'],oracle['baseline_eur'])
    equal('scenario_eur',cost['scenario']['official_total_cost_eur'],oracle['electric_plus20_eur'])
    scenario=read('br-06','*-scenario.json')['data']
    assert len(scenario['changes'])==2
    assert {c['component'] for c in scenario['changes']}=={'elec_high','elec_low'}
    assert all(Decimal(c['change_percent'])==20 and c['price'] is None and c['price_unit']=='EUR/kWh'
               for c in scenario['changes'])
    assert {r['component']:Decimal(r['rate']) for r in scenario['rates']}=={
        'elec_high':Decimal('.096'),'elec_low':Decimal('.048'),'heat':Decimal('.0083')}
    return dict(status='passed',publication=context['publication'],checks=checks,
        zero_denominator_contract='passed',scenario_rate_and_change_contract='passed',
        raw_oracle=oracle,files=files,
        tolerance_source='Migration contract locked before implementation: absolute 1e-24 for repeating divisions; finite amounts exact')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preparation', type=Path, required=True)
    parser.add_argument('--context', type=Path)
    parser.add_argument('--videos', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prep, videos, output = args.preparation, args.videos, args.output
    if output.exists():
        raise ValueError('Preserve prior evidence; use a fresh output directory')
    context_file = args.context or prep / 'context.json'
    context = json.loads(context_file.read_text())
    assert context['backend'] == 'clickhouse' and not context['pg_credentials_present']
    subprocess.run(['node', 'tools/walkthrough/build-index.cjs', str(videos)], check=True)
    summary = json.loads((videos / 'summary.json').read_text())
    assert len(summary['videos']) == 7 and summary['backend'] == 'clickhouse'
    parity = json.loads((prep / 'fixed-full-parity.json').read_text())
    assert parity['status'] == 'passed' and parity['all_six_brs'] and parity['episodes_compared'] == 67829
    assert parity['clickhouse_publication'] == context['publication']
    player = json.loads((prep / 'player-verification.json').read_text())
    assert player['status'] == 'passed' and len(player['videos']) == 7
    assert Path(player['artifact_root']).resolve() == videos.resolve()
    live = json.loads(Path(context['live_replay']['complete_result_host_path']).read_text())
    assert live['status'] == 'passed' and live['complete'] and live['original_envelopes_exact']
    assert live['checksum_verified'] and live['accepted_events'] == 30
    partial = context['live_replay']
    assert partial['status'] == 'passed' and partial['accepted_events'] == 10 and not partial['complete']
    assert all(live[key] == partial[key] for key in ('replay_id', 'scope_id', 'source_sha256'))
    assert live['publication_id'] != partial['publication_id']
    for proof, provisional in ((partial, 'True'), (live, 'False')):
        export = videos / 'sensor-replay/downloads' / ('replay-' + proof['publication_id'][:12] + '.csv')
        with export.open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        assert rows and all(row['publication_id'] == proof['publication_id'] and
            row['provisional'] == provisional and row['quality_policy_version'] == 'AGC_QUALITY_V1' for row in rows)
        assert all(rule in export.read_text() for rule in
            ('climate-demo-b01:Automatoes:v1', 'drain-demo-b03:Automatoes:v1'))
    output.mkdir(parents=True)
    save(output/'native-export-oracle.json',verify_oracle_exports(videos,context))
    verified = []
    for record in summary['videos']:
        folder = 'sensor-replay' if record['br'] == '07' else 'br-' + record['br']
        directory = videos / folder
        video = directory / record['video']['file']
        assert record['context_sha256'] == digest(directory / 'context.json')
        record_context = json.loads((directory / 'context.json').read_text())
        assert all(record_context[key] == context[key] for key in
            ('publication', 'configuration_hash', 'archive_sha256', 'raw_oracle'))
        runtime = json.loads((prep / 'runtime.json').read_text())
        assert record_context['runtime_verification']['ui_image'] == runtime['ui_image']
        assert record_context['runtime_verification'] == context['runtime_verification']
        assert record_context['runtime_verification']['server_config_sha256'] == runtime['server_config_sha256']
        assert digest(Path('docker/clickhouse/local.xml')) == runtime['server_config_sha256']
        assert not record_context['runtime_verification']['app_postgres_credentials_present']
        # Decode every frame: dimensions alone do not prove a playable video.
        subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-xerror', '-i',
                        str(video), '-map', '0:v:0', '-f', 'null', '-'], check=True)
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                            '-show_format', '-of', 'json', str(video)], text=True))
        assert len(probe['streams']) == 1 and probe['streams'][0]['codec_type'] == 'video'
        assert (probe['streams'][0]['width'], probe['streams'][0]['height']) == (1920, 1080)
        result = json.loads((directory / 'result.json').read_text())
        tokens = ('Đối chiếu', 'Chọn đợt dài nhất', 'Xem đợt EC dài nhất',
                  'Đọc baseline, scenario', 'Làm mới và đối soát complete')
        json_exports = []
        for download in result['downloads']:
            if download['file'].endswith('.json'):
                exported = (directory / download['file']).read_text()
                json.loads(exported)
                assert all(value in exported for value in (context['publication'],
                    context['configuration_hash'], context['archive_sha256'], 'clickhouse-warehouse-v1'))
                expected_rule = {'01':'climate-demo-b01:IUACAAS:v1',
                    '02':'walkthrough-water:Automatoes:v1', '03':'drain-demo-b03:Reference:v1',
                    '05':'harvest-demo-b10:Digilog:v2'}.get(record['br'])
                if expected_rule:
                    assert expected_rule in exported
                json_exports.append(download['file'])
        selected_step = next((s for s in result['steps'] if any(t in s['title'] for t in tokens)), result['steps'][2])
        second = max(selected_step['start_seconds'], selected_step['end_seconds'] - 2)
        frame = output / (folder + '-frame.png')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', str(second),
                        '-i', str(video), '-frames:v', '1', str(frame)], check=True)
        shutil.copy2(directory / 'result.json', output / (folder + '.json'))
        originals = list((directory / 'recording').glob('*.webm'))
        assert len(originals) == 1
        verified.append(dict(folder=folder, json_exports_with_native_configuration=json_exports, video_sha256=digest(video), full_decode='passed',
            frame=frame.name, frame_seconds=second, frame_sha256=digest(frame),
            raw_webm_sha256=digest(originals[0]), trace_sha256=digest(directory / 'trace.zip'),
            screenshot_inventory=[dict(file=s['screenshot'], sha256=digest(directory / s['screenshot']))
                                  for s in result['steps']], probe=probe))
    shutil.copy2(context_file, output / 'context.json')
    save(output / 'live-partial.json', partial)
    contexts = output / 'contexts'
    contexts.mkdir()
    for record in summary['videos']:
        folder = 'sensor-replay' if record['br'] == '07' else 'br-' + record['br']
        shutil.copy2(videos / folder / 'context.json', contexts / (record['context_sha256'] + '.json'))
    for name in ('runtime.json', 'native-startup.json', 'player-verification.json'):
        shutil.copy2(prep / name, output / name)
    for name in ('warm-ui-initial-aborted.json', 'warm-ui.log', 'warm-ui-retry.log', 'live-prepare.log', 'prepare.log',
                 'sensor-checkbox-retry.json', 'sensor-slider-retry.json', 'live-complete-first-attempt.json',
                 'record.log', 'record-sensor-retry.log', 'live-prepare-v2.log', 'record-sensor-v2.log',
                 'disposed-input-cache-cleanup.json', 'docker-restart-interruption.json',
                 'record-final.log', 'cold-browser-runtimefix.log',
                 'live-prepare-v3.log', 'record-sensor-v3.log'):
        if (prep / name).exists():
            shutil.copy2(prep / name, output / name)
    for name in ('fixed-images.json', 'fixed-verification.json', 'metric-log-runtime-fix.json', 'metric-log-preservation.json', 'fixed-full-parity.json',
                 'fixed-verification-first-UI-failed.json', 'fixed-verification-raw-path-failed.json'):
        if (prep / name).exists():
            shutil.copy2(prep / name, output / name)
    diagnostics = output / 'cold-fix'
    diagnostics.mkdir()
    for name in ('regressions-final.log', 'memory-baseline-64m-wide.log',
                 'packaged-tests.log', 'packaged-ui-tests.log', 'fixed-full-parity.log',
                 'packaged-ui-tests-runtimefix.log', 'fixed-full-parity-runtimefix.log', 'fixed-full-parity-final.log',
                 'water-ui-repeat.log', 'water-ui-bounded.log', 'water-ui-small-blocks.log',
                 'water-ui-metric-paused.log', 'server-memory-samples.jsonl',
                 'ui-errors-memory-peaks.json', 'merge-setting-reference.json',
                 'root-cause.json', 'runtime-package-hashes.json'):
        if (prep / 'diagnostics' / name).exists():
            shutil.copy2(prep / 'diagnostics' / name, diagnostics / name)
    shutil.copy2(videos / 'summary.json', output / 'summary.json')
    save(output / 'live-complete.json', live)
    shutil.copy2(videos / 'sensor-replay/stream-completion.log', output / 'sensor-completion.log')
    cold = prep / 'cold-browser-startup'
    if cold.exists():
        cold_result = json.loads((cold / 'result.json').read_text())
        assert cold_result['status'] == 'passed'
        cold_output = output / 'cold-browser-startup'
        cold_output.mkdir()
        for name in ('result.json', 'browser.log', 'native-startup.json', 'native-startup.png'):
            shutil.copy2(cold / name, cold_output / name)
    for folder in ('cold-browser-startup-failed-01', 'cold-browser-startup-first-fixed'):
        previous = prep / folder
        if previous.exists():
            archived = output / folder
            archived.mkdir()
            for name in ('result.json', 'browser.log', 'native-startup.json'):
                if (previous / name).exists():
                    shutil.copy2(previous / name, archived / name)
    sources = output / 'sources'
    sources.mkdir()
    for filename in ('record.cjs', 'build-index.cjs', 'check-index.cjs', 'warm-ui.cjs', 'live_replay.py',
                     'prepare_clickhouse_context.py', 'finalize_clickhouse.py', 'package-lock.json'):
        shutil.copy2(Path(__file__).parent / filename, sources / filename)
    for filename in ('prepare_clickhouse_walkthrough.py', 'run_walkthrough_replay.py', 'verify_clickhouse_browser_startup.py'):
        shutil.copy2(Path('scripts') / filename, sources / filename)
    shutil.copy2(Path('docker/clickhouse/local.xml'), sources / 'clickhouse-local.xml')
    shutil.copy2(Path('scripts/clickhouse_repro/preserve_metric_log.py'), sources / 'preserve_metric_log.py')
    if (prep / 'helper-revisions').exists():
        shutil.copytree(prep / 'helper-revisions', output / 'helper-revisions')
    revisions = output / 'recorder-revisions'
    revisions.mkdir()
    for record in summary['videos']:
        folder = 'sensor-replay' if record['br'] == '07' else 'br-' + record['br']
        recorded_script = videos / folder / 'recorder.cjs'
        assert digest(recorded_script) == record['script_sha256']
        shutil.copy2(recorded_script, revisions / (record['script_sha256'] + '.cjs'))
    save(output / 'verification.json', dict(status='passed', backend='clickhouse',
        artifact_root=str(videos), all_mp4s_fully_decoded=True, main_visual_review='pending',
        sensor_publications=dict(historical_source=context['publication'], provisional=partial['publication_id'],
            complete=live['publication_id'], same_scope=True, csv_metadata_exact=True), videos=verified))
    save(output / 'manifest.json', dict(status='files_verified_visual_review_pending', backend='clickhouse',
        files=[dict(file=str(p.relative_to(output)), bytes=p.stat().st_size, sha256=digest(p))
               for p in sorted(output.rglob('*')) if p.is_file()]))
    print('Video files verified; review the decoded frames before accepting the candidate.')


if __name__ == '__main__':
    main()
