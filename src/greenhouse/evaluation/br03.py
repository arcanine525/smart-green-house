"""Reproducible BR-03 raw reconciliation and capped application benchmarks.

The raw oracle below does not call the duration engine or read frozen quality
to decide classes. It independently applies the documented demo bands and
AGC baseline EC-negative/pH-zero policy to the source CSV at five-minute grain.
No main-owned scratch artifacts are dependencies of this runner.
"""
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import gc
import hashlib
import json
from pathlib import Path
import platform

import psycopg

from greenhouse.app import wiring, drain_actions
from greenhouse.db.demo_rules import DRAIN_FAMILY_PREFIX
from greenhouse.db.loader import AGC_SCOPE, AGC_SHA
from greenhouse.db.rule_schedule import read_rule_schedule
from greenhouse.evaluation.br01 import container_limits, measure_component
from greenhouse.queries import page_episodes
from greenhouse.settings import Settings

STEP = timedelta(minutes=5)
EPOCH = datetime(1899, 12, 30)
BOUNDS = {'ec': (Decimal('2'), Decimal('5')), 'ph': (Decimal('5.5'), Decimal('6.5'))}
CODES = {'ec': 'EC_drain_PC', 'ph': 'pH_drain_PC'}
KNOWN = ('inside', 'low', 'high')
OUT = ('low', 'high')


def percent(numerator, denominator):
    if not denominator:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * 100 / Decimal(denominator)


def raw_measure(token, metric):
    text = (token or '').strip()
    # Frozen parser contract: these are source NULL lexemes, not malformed
    # numeric measurements. Keep the original per-measure reason as well.
    null_reasons = {'': 'empty', 'nan': 'nan_token', 'na': 'na_token', 'n/a': 'n_a_token'}
    if text.lower() in null_reasons:
        return None, 'missing', null_reasons[text.lower()]
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, 'invalid', 'invalid_numeric'
    if not number.is_finite():
        return None, 'invalid', 'invalid_numeric'
    if metric == 'ec' and number < 0:
        return number, 'invalid', 'negative_quantity'
    if metric == 'ph' and number == 0:
        return number, 'suspect', 'demo_zero_ph_suspect'
    return number, 'accepted', None


def read_raw(path, start, end):
    import csv
    rows = {}
    with path.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.reader(stream)
        headers = [value.strip() for value in next(reader)]
        for line_number, cells in enumerate(reader, 2):
            fields = dict(zip(headers, cells))
            serial = Decimal(fields['%time'].strip())
            stamp = EPOCH + int((serial * 288).to_integral_value()) * STEP
            if start <= stamp <= end:
                if stamp in rows:
                    raise ValueError(f'Duplicate raw source slot at {path.name}:{line_number}')
                values, quality, reasons = {}, {}, {}
                for metric, code in CODES.items():
                    values[metric], quality[metric], reasons[metric] = raw_measure(fields.get(code), metric)
                rows[stamp] = {'values': values, 'quality': quality, 'line': line_number,
                               'reasons': reasons, 'serial': str(serial)}
    return rows


def raw_oracle(rows, start, end):
    """Independent fixed-band class/duration/run scan over a complete grid."""
    classified = {metric: [] for metric in BOUNDS}
    stamp = start
    while stamp < end:
        raw = rows.get(stamp)
        for metric, (lower, upper) in BOUNDS.items():
            value = raw['values'][metric] if raw else None
            status = raw['quality'][metric] if raw else 'missing'
            if status != 'accepted':
                code, deviation = status, Decimal(0)
            elif value < lower:
                code, deviation = 'low', lower - value
            elif value > upper:
                code, deviation = 'high', value - upper
            else:
                code, deviation = 'inside', Decimal(0)
            classified[metric].append((stamp, code, deviation))
        stamp += STEP
    result = {'metrics': {}, 'joint': {}}
    for metric, records in classified.items():
        counts = Counter(record[1] for record in records)
        valid = sum(counts[code] for code in KNOWN) * 5
        out = sum(counts[code] for code in OUT) * 5
        expected = len(records) * 5
        episodes, current = [], None
        for stamp, code, deviation in records + [(end, 'inside', Decimal(0))]:
            if code not in OUT:
                if current:
                    current['end'] = stamp.isoformat()
                    current['directions'] = sorted(current['directions'])
                    episodes.append(current)
                    current = None
                continue
            if current is None:
                current = {'start': stamp.isoformat(), 'minutes': 0, 'peak': deviation,
                           'peak_at': stamp.isoformat(), 'directions': set()}
            current['minutes'] += 5
            current['directions'].add(code)
            if deviation > current['peak']:
                current['peak'], current['peak_at'] = deviation, stamp.isoformat()
        result['metrics'][metric] = {
            'expected_minutes': expected, 'valid_minutes': valid, 'out_minutes': out,
            'out_low_minutes': counts['low'] * 5, 'out_high_minutes': counts['high'] * 5,
            'unknown_minutes': expected - valid, 'suspect_minutes': counts['suspect'] * 5,
            'out_percent': percent(out, valid), 'coverage_pct': percent(valid, expected),
            'minutes_by_class': {key: value * 5 for key, value in sorted(counts.items())},
            'episodes': episodes,
        }
    joint_valid = joint_out = 0
    for left, right in zip(classified['ec'], classified['ph']):
        if left[1] in KNOWN and right[1] in KNOWN:
            joint_valid += 5
            if left[1] in OUT or right[1] in OUT:
                joint_out += 5
    expected = result['metrics']['ec']['expected_minutes']
    result['joint'] = {'jointly_valid_minutes': joint_valid, 'joint_out_minutes': joint_out,
                       'joint_out_percent': percent(joint_out, joint_valid),
                       'joint_coverage_pct': percent(joint_valid, expected)}
    return result


def pin(connection, scope):
    catalog = wiring.catalog_service(connection)
    rows = [row for row in catalog.drain_usable() if row.scope_id == scope]
    if len(rows) != 1:
        raise ValueError(f'Expected one drain-usable publication for scope {scope}')
    service = wiring.drain_service(connection)
    publication = service.adapter.publication(rows[0].publication_id)
    return catalog, service, publication


def demo(connection, cycle):
    revision = read_rule_schedule(connection, f'{DRAIN_FAMILY_PREFIX}:{cycle.compartment_id}:v1')
    if revision is None or not revision.is_demo or any(
            (segment.lower, segment.upper) != BOUNDS[segment.metric] for segment in revision.segments):
        raise ValueError('Explicit documented drain demo seed required before evaluation')
    return revision


def same_number(observed, expected):
    if observed is None or expected is None:
        return observed is expected
    return abs(Decimal(str(observed)) - Decimal(str(expected))) <= Decimal('1e-6')


def reconcile(kwargs, raw_snapshot, scope=AGC_SCOPE):
    manifest = json.loads((raw_snapshot / 'manifest.json').read_text())
    file_hashes = {entry['path']: entry['sha256'] for entry in manifest['files']}
    results = {'mode': 'agc', 'oracle': 'independent raw CSV + documented demo policy/bounds',
               'source_naive': True, 'cycles': [], 'all_match': False}
    with psycopg.connect(**kwargs) as connection:
        catalog, service, publication = pin(connection, scope)
        if manifest['snapshot_id'] != publication.snapshot_id:
            raise ValueError('Raw snapshot must match the pinned publication')
        service = wiring.drain_service(connection, {publication.snapshot_id: raw_snapshot})
        results['publication'] = {'id': publication.publication_id, 'snapshot': publication.snapshot_id,
                                  'scope': publication.scope_id, 'versions': asdict(publication.versions)}
        for cycle in catalog.cycles(publication):
            relative = f'{cycle.compartment_name}/GreenhouseClimate.csv'
            path = raw_snapshot / relative
            with path.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            assert checksum == file_hashes[relative], relative
            start, end = datetime.combine(cycle.start, time.min), datetime.combine(cycle.end, time.min)
            raw = read_raw(path, start, end)
            expected = raw_oracle(raw, start, end)
            revision = demo(connection, cycle)
            context = wiring.build_context(publication, cycle, cycle.start, cycle.end - timedelta(days=1))
            evaluation = service.get_evaluation(context, rule_version=revision.rule_version, use_cache=False)
            frozen = service.adapter.read_drain_slots(context, publication, window_end=end + STEP)
            mismatches = []
            for row in frozen:
                source = raw.get(row.slot_start)
                if source is None:
                    mismatches.append(f'raw slot absent: {row.slot_start}')
                    continue
                for metric, column in (('ec', 'ec_drain'), ('ph', 'ph_drain')):
                    stored = getattr(row, column)
                    stored = Decimal(str(stored)) if stored is not None else None
                    if stored != source['values'][metric]:
                        mismatches.append(f'value {metric}: {row.slot_start}')
                    triplet = row.measure_quality.get(CODES[metric], {})
                    if triplet.get('status') != source['quality'][metric]:
                        mismatches.append(f'quality {metric}: {row.slot_start}')
                    applicability = 'unknown' if source['values'][metric] is None else 'applicable'
                    if triplet.get('applicability') != applicability:
                        mismatches.append(f'applicability {metric}: {row.slot_start}')
                    if triplet.get('reason') != source['reasons'][metric]:
                        mismatches.append(f'reason {metric}: {row.slot_start}')
            assert len(frozen) == len(raw) and sum(row.slot_start == end for row in frozen) == 1
            metrics, lineage = {}, []
            for report in evaluation.result.metric_results:
                oracle = expected['metrics'][report.metric]
                for field in ('expected_minutes', 'valid_minutes', 'out_minutes', 'unknown_minutes',
                              'out_low_minutes', 'out_high_minutes', 'suspect_minutes', 'out_percent', 'coverage_pct'):
                    matches = (same_number(getattr(report, field), oracle[field])
                               if field in ('out_percent', 'coverage_pct')
                               else getattr(report, field) == oracle[field])
                    if not matches:
                        mismatches.append(f'{report.metric}.{field}')
                reasons = {'missing': 'missing_input', 'invalid': 'quality_invalid',
                           'suspect': 'quality_suspect'}
                expected_unknown = {reason: oracle['minutes_by_class'][code]
                                    for code, reason in reasons.items()
                                    if oracle['minutes_by_class'].get(code)}
                if {share.reason_code: share.minutes for share in report.unknown_reasons} != expected_unknown:
                    mismatches.append(f'{report.metric}.unknown_reason_partition')
                episodes = sorted((episode for episode in evaluation.result.episodes if episode.metric == report.metric),
                                  key=lambda episode: episode.original_start)
                observed = [{'start': ep.original_start.isoformat(), 'end': ep.original_end.isoformat(),
                             'minutes': int(ep.original_minutes), 'peak': ep.peak_deviation,
                             'peak_at': ep.peak_time.isoformat(),
                             'directions': sorted(direction.value for direction in ep.directions)} for ep in episodes]
                if observed != oracle['episodes']:
                    mismatches.append(f'{report.metric}.all_episode_spans_durations_peaks_directions')
                if any((ep.clipped_start, ep.clipped_end, ep.clipped_minutes) !=
                       (ep.original_start, ep.original_end, ep.original_minutes) for ep in episodes):
                    mismatches.append(f'{report.metric}.full_cycle_clipping')
                longest = sorted(oracle['episodes'], key=lambda ep: (-ep['minutes'], ep['start']))[:3]
                metrics[report.metric] = {key: value for key, value in oracle.items() if key != 'episodes'} | {
                    'episode_count': len(episodes), 'longest': longest}
                if longest:
                    stamp = datetime.fromisoformat(longest[0]['peak_at'])
                    source_row = next(row for row in frozen if row.slot_start == stamp)
                    trace = service.get_lineage(publication, revision_id=source_row.revision_id)
                    assert trace.raw_hash_verified and trace.line_number == raw[stamp]['line']
                    serial = Decimal(raw[stamp]['serial'])
                    source_stamp = EPOCH + timedelta(microseconds=int(serial * 86400000000))
                    mapped_slot = EPOCH + int((serial * 288).to_integral_value()) * STEP
                    assert trace.source_time_serial == serial and trace.source_timestamp == source_stamp
                    assert mapped_slot == stamp and trace.relative_path == relative
                    lineage.append({'metric': report.metric, 'slot': stamp, 'line': trace.line_number,
                                    'source_timestamp': source_stamp, 'source_time_serial': serial,
                                    'serial_maps_to_peak_slot': mapped_slot == stamp,
                                    'raw_hash_verified': trace.raw_hash_verified, 'file': relative})
            joint = evaluation.result.joint_results[0]
            for field, value in expected['joint'].items():
                if not same_number(getattr(joint, field), value):
                    mismatches.append('joint.' + field)
            results['cycles'].append({'compartment': cycle.compartment_name, 'cycle': cycle.cycle_id,
                'window': [start, end], 'rule_version': revision.rule_version,
                'file_sha256': checksum, 'raw_and_frozen_slots': len(frozen), 'endpoint_rows': 1,
                'metrics': metrics, 'joint': expected['joint'], 'lineage': lineage,
                'mismatches': mismatches[:25], 'mismatch_count': len(mismatches), 'match': not mismatches})
            del frozen, raw, evaluation
            gc.collect()
        results['source_counts'] = {table: connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
            for table in ('core.record_revision', 'core.climate_observation', 'core.drain_observation',
                          'core.measure_quality', 'meta.correction')}
        results['all_publications'] = [row[0] for row in connection.execute(
            'SELECT publication_id FROM meta.publication ORDER BY publication_id').fetchall()]
    results['all_match'] = len(results['cycles']) == 6 and all(cycle['match'] for cycle in results['cycles'])
    return results


def performance(kwargs, scope=AGC_SCOPE, runs=30, warmups=5):
    """Fresh-process miss, warm hits, bounded screen action, forced changes."""
    from greenhouse.app import figures
    result = {'mode': 'performance', 'runs': runs, 'warmups': warmups,
              'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                              'limits': container_limits()}, 'workloads': {}}
    with psycopg.connect(**kwargs) as conn:
        catalog, service, publication = pin(conn, scope)
        cycle = next(cycle for cycle in catalog.cycles(publication) if cycle.compartment_name == 'Reference')
        revision = demo(conn, cycle)
        context = wiring.build_context(publication, cycle, cycle.start, cycle.end - timedelta(days=1))
        water_service = wiring.irrigation_service(conn)
        result['scope'] = {'publication': publication.publication_id, 'compartment': cycle.compartment_name,
                           'cycle': cycle.cycle_id, 'rule': revision.rule_version,
                           'window': [cycle.start, cycle.end], 'trace_cap_slots': drain_actions.TRACE_PAGE_SLOTS}
        result['workloads']['summary_and_first_page'] = measure_component(
            lambda: page_episodes(service.get_evaluation(context, rule_version=revision.rule_version).result,
                                  metric_order=('ec', 'ph'), limit=40), runs, warmups)
        result['workloads']['summary_and_first_page']['cache_note'] = (
            'First call is a fresh-process application-cache miss, not cold PostgreSQL/OS. '
            'Warmups/measured calls hit the bounded two-entry result cache; target p95<=2s applies here.')

        def screen_action():
            catalog.publications()
            catalog.cycles(publication)
            evaluation = service.get_evaluation(context, rule_version=revision.rule_version)
            episodes, _ = page_episodes(evaluation.result, metric='ec', metric_order=('ec', 'ph'), limit=40)
            episode = episodes[0]
            trace, _, _ = drain_actions.trace_page(service, context, revision.rule_version,
                                                    episode.original_start, episode.original_end)
            facts = service.slot_facts(context, window_start=trace.start, window_end=trace.end,
                                        max_slots=drain_actions.TRACE_PAGE_SLOTS)
            source = service.slot_facts(context, window_start=datetime.combine(cycle.start, time.min),
                                         max_slots=drain_actions.SOURCE_PAGE_SLOTS)
            source_trace = service.get_slot_review(context, rule_version=revision.rule_version,
                window_start=source.start, window_end=source.end, max_slots=drain_actions.SOURCE_PAGE_SLOTS)
            drain_actions.daily_water_context(water_service, evaluation, episode.original_start, episode.original_end)
            for metric in ('ec', 'ph'):
                figures.drain_figure(trace.reviews, metric, facts.rows).to_json()
                figures.drain_figure(source_trace.reviews, metric, source.rows).to_json()
        measured = measure_component(screen_action, runs, warmups)
        measured.pop('target_met', None)
        measured.pop('target_seconds', None)
        measured['scope_note'] = ('Pinned service screen action: catalog, warm evaluation + 40-event list, '
            'bounded 144-slot trace/facts, 12-slot source/reviews, unique daily water context and '
            'four Plotly serializations. Excludes browser/Streamlit rendering, user-click lineage '
            'and deferred download generation. No <=2s whole-screen claim.')
        result['workloads']['bounded_screen_action'] = measured
        for name, rule in (('forced_no_rule', None), ('forced_demo_rule', revision.rule_version)):
            measured = measure_component(lambda: service.get_evaluation(context, rule_version=rule, use_cache=False),
                                         runs, warmups)
            measured.pop('target_met', None)
            measured.pop('target_seconds', None)
            measured['scope_note'] = 'One forced uncached evaluation per sample; separate rule selection component, not a combined change latency.'
            result['workloads'][name] = measured
        result['environment']['limits_after'] = container_limits()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('agc', 'performance'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scope-id', default=AGC_SCOPE)
    parser.add_argument('--raw-snapshot', type=Path)
    parser.add_argument('--runs', type=int, default=30)
    parser.add_argument('--warmups', type=int, default=5)
    args = parser.parse_args(argv)
    if args.runs < 30 or args.warmups < 5:
        parser.error('At least 30 measured runs and 5 warmups are required')
    settings = Settings.from_env()
    if args.mode == 'agc':
        raw = args.raw_snapshot or settings.raw_dir / 'agc-2nd-2019' / AGC_SHA
        result = reconcile(settings.connection_kwargs(), raw, args.scope_id)
    else:
        result = performance(settings.connection_kwargs(), args.scope_id, args.runs, args.warmups)
    result['generated_at'] = datetime.now(timezone.utc).isoformat()
    result['database'] = settings.pg_database
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str) + '\n')
    print(json.dumps({'mode': args.mode, 'all_match': result.get('all_match'), 'output': str(args.output)}))
    return 0 if result.get('all_match', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
