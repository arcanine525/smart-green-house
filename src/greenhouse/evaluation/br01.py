"""BR-01 evidence runner: AGC reconciliation + performance.

Fixture acceptance is NOT re-run here: the exact §7 known answers live in
tests/unit/test_rule_schedules.py (test_temperature_dispatch_fixture_and_peak:
30-minute window -> valid 25 / out 15 / unknown 5 / out 60% / coverage
83.333…%, events 5 + 10 minutes) and the real-pipeline day-grid integration
in tests/unit/test_climate_mart.py. This runner adds the two things those
tests cannot: read-only reconstruction against a REAL published AGC
publication, and timings under recorded container limits.

Modes (python -m greenhouse.evaluation.br01 --mode <name>):
  agc          — reconciliation for the current climate publication (READ-ONLY):
                 raw CSV -> frozen mart equality per slot (all 8 measures),
                 frozen quality triplets (off-mode rows), boundary-sample
                 placement, then a policy oracle REIMPLEMENTED here from doc
                 §4 (no import of the service's slot builder) reconciled
                 against the typed service per cycle × basis, plus lineage
                 spot checks. Requires the demo schedule to be seeded first
                 (CLI `python -m greenhouse.db.demo_rules --kind climate
                 --apply`); this runner never registers or writes anything.
  performance  — BR-01 §2 convention timings: first request kept separate,
                 then 5 warmups + 30 measured runs per workload, p95 against
                 the <=2s target, cgroup limits recorded. Run separately in
                 the job (2GiB/1.5CPU) and ui-test (768MiB/0.5CPU) containers;
                 each report labels its own limits — no cold-cache claim.

Existing databases are never dropped or modified. Output is one JSON document
on stdout or --output (ids and facts; no raw payload dumps).
"""
import argparse
import csv
import json
import math
import os
import platform
import resource
import statistics
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from time import perf_counter

import psycopg

from greenhouse.app import wiring
from greenhouse.contracts import RuleBasis
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.rule_schedule import read_rule_schedule
from greenhouse.queries import ClimateService, page_episodes
from greenhouse.queries.catalog import CatalogService
from greenhouse.settings import Settings

EXCEL_EPOCH = date(1899, 12, 30)
P95_TARGET_SECONDS = Decimal('2')
CADENCE_MINUTES = 5
STATUS_PRECEDENCE = {'invalid': 0, 'suspect': 1, 'missing': 2, 'accepted': 3}
VALID_CLASSES = frozenset(('valid', 'out_low', 'out_high'))
OUT_CLASSES = frozenset(('out_low', 'out_high'))
CLIMATE_COLUMNS = ('tair', 'co2air', 't_heat_sp', 't_vent_sp', 'co2_sp',
                   't_heat_vip', 't_ventlee_vip', 'co2_vip')
# Raw CSV headers (case as written by the source) paired with the mart's
# frozen column keys (lowercase); everything except the two actuals shares
# the same name in both.
RAW_COLUMNS = ('Tair', 'CO2air', 't_heat_sp', 't_vent_sp', 'co2_sp',
               't_heat_vip', 't_ventlee_vip', 'co2_vip')
COLUMN_PAIRS = tuple(zip(RAW_COLUMNS, CLIMATE_COLUMNS))
REQUESTED_SETPOINTS = {'temperature': ('t_heat_sp', 't_vent_sp'), 'co2': ('co2_sp',)}
REALIZED_SETPOINTS = {'temperature': ('t_heat_vip', 't_ventlee_vip'), 'co2': ('co2_vip',)}
ACTUAL_CODE = {'temperature': 'tair', 'co2': 'co2air'}
# The frozen quality triplets key measures by their RAW measure codes
# (Tair/CO2air capitalized like the policy), not by the mart column aliases.
ACTUAL_QUALITY = {'temperature': 'Tair', 'co2': 'CO2air'}
ZERO = Decimal('0')


def exact(value):
    return None if value is None else str(value)


def pct(numerator, denominator):
    """Independent percent at the engine's own 50-digit precision."""
    if denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * Decimal(100) / Decimal(denominator)


def close(observed, expected, tolerance=Decimal('1e-6')):
    """"§7 rule: ratios within 1e-6; exact values (None-aware) match exactly."""
    if observed is None or expected is None:
        return observed is expected
    return abs(Decimal(observed) - Decimal(expected)) <= tolerance


def serial_slot(serial):
    """Loader-aligned slot index: round(serial × 288) at the parser's
    rounding (HALF_EVEN), after a residual guard; the doc's epoch applies.
    Returns the DATETIME slot instant (midnight epoch + cadence minutes),
    never a bare date, so every comparison here is datetime-to-datetime."""
    scaled = Decimal(serial) * 288
    index = int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
    residual = abs(scaled - Decimal(index))
    instant = datetime.combine(EXCEL_EPOCH, time.min) \
        + timedelta(minutes=CADENCE_MINUTES * index)
    return instant, residual


def frozen_number(text):
    """Raw CSV cell -> Decimal or None (empty stays empty, never 0). NaN /
    inf tokens normalize to None under the frozen per-measure policy, exactly
    like the loader treats them as missing source values."""
    if text is None or str(text).strip() == '':
        return None
    value = Decimal(str(text).strip())
    if not value.is_finite():
        return None
    return value


# ------------------------------------------------------------------- AGC --
def read_raw_climate(path, window_start, window_end):
    """Independent raw reader (csv + Decimal + documented Excel epoch)."""
    slots, boundary = {}, []
    first = last = None
    window_start = datetime.combine(window_start, time.min)
    window_end = datetime.combine(window_end, time.min)
    with open(path, newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        stripped = [(name, name.strip()) for name in (reader.fieldnames or ())]
        for row in reader:
            values = {strip: row.get(name) for name, strip in stripped}
            raw_serial = (values.get('%time') or values.get('%Time') or '').strip()
            if not raw_serial:
                continue
            slot_start, residual = serial_slot(raw_serial)
            if residual > Decimal('0.01'):
                raise ValueError(f'{path}: serial {raw_serial} maps off the 5-minute grid')
            sample = {mart_code: frozen_number(values.get(raw_code))
                      for raw_code, mart_code in COLUMN_PAIRS}
            if slot_start >= window_end:
                if slot_start == window_end:
                    boundary.append(sample)
                continue
            if slot_start < window_start:
                continue
            slots[slot_start] = sample
            first = first or slot_start
            last = slot_start
    return slots, boundary, first, last


def triplet_of(quality, code):
    entry = (quality or {}).get(code)
    if entry is None:
        return {'status': 'missing', 'applicability': 'unknown',
                'reason': 'measure_quality_absent'}
    return {'status': entry.get('status'), 'applicability': entry.get('applicability'),
            'reason': entry.get('reason')}


def combined_status(triplets):
    return min((triplet.get('status') or 'missing' for triplet in triplets),
               key=lambda status: STATUS_PRECEDENCE.get(status, 2))


def usable(triplet, value):
    """Doc/main evaluation-input rule: accepted values count only when the
    measure is applicable — plus the ONE numeric exception (a SELECTED CO2
    setpoint with accepted status, unknown applicability and the EXACT value
    0 keeps its number so off-mode stays classifiable)."""
    if triplet.get('status') != 'accepted':
        return False
    if triplet.get('applicability') == 'applicable':
        return True
    return value is not None and value == ZERO


def out_class(actual, lower, upper):
    if actual < lower:
        return 'out_low', lower - actual
    if actual > upper:
        return 'out_high', actual - upper
    return 'valid', ZERO


def oracle_classify(mart_rows, steps, segment_map, basis):
    """Doc §4 §4.1 reimplemented, independent of the service path: per grid
    step one class (+ deviation/bounds) per metric for the declared basis.

    Precedence mirrors the documented semantics: rule_not_configured covers
    absent segments; absent rows / combined invalid / suspect / missing mask
    the inputs to their unknown code; the accepted path applies the own-set
    bounds (boundary equality in-range; exact-0 CO2 setpoint is sp0
    off-mode, never a biological verdict)."""
    setpoints = REQUESTED_SETPOINTS if basis == RuleBasis.REQUESTED else REALIZED_SETPOINTS
    classified = {metric: {} for metric in ACTUAL_CODE}
    for step_start in steps:
        row = mart_rows.get(step_start)
        for metric, actual_code in ACTUAL_CODE.items():
            if metric not in segment_map:
                classified[metric][step_start] = ('rule_not_configured', ZERO, None, None)
                continue
            _, _, tolerance = segment_map[metric]
            if row is None:
                classified[metric][step_start] = ('missing_input', ZERO, None, None)
                continue
            quality = row['measure_quality']
            triplets = ([triplet_of(quality, ACTUAL_QUALITY[metric])]
                        + [triplet_of(quality, code) for code in setpoints[metric]])
            status = combined_status(triplets)
            actual = row.get(actual_code)
            if status == 'invalid':
                classified[metric][step_start] = ('quality_invalid', ZERO, None, None)
                continue
            if status == 'suspect':
                classified[metric][step_start] = ('quality_suspect', ZERO, None, None)
                continue
            if status == 'missing':
                classified[metric][step_start] = ('missing_input', ZERO, None, None)
                continue
            if metric == 'co2':
                sp_value, sp_triplet = row.get(setpoints['co2'][0]), triplets[1]
                if not usable(sp_triplet, sp_value):
                    classified[metric][step_start] = ('missing_input', ZERO, None, None)
                    continue
                if sp_value == ZERO:
                    classified[metric][step_start] = ('sp0_off_mode', ZERO, None, None)
                    continue
                actual_triplet = triplets[0]
                if actual_triplet.get('applicability') != 'applicable' or actual is None:
                    classified[metric][step_start] = ('missing_input', ZERO, None, None)
                    continue
                lower, upper = max(ZERO, sp_value - tolerance), sp_value + tolerance
                class_code, deviation = out_class(actual, lower, upper)
                classified[metric][step_start] = (class_code, deviation, lower, upper)
                continue
            low_value, high_value = (row.get(code) for code in setpoints['temperature'])
            if not (usable(triplets[1], low_value) and usable(triplets[2], high_value)):
                classified[metric][step_start] = ('missing_input', ZERO, None, None)
                continue
            lower, upper = low_value - tolerance, high_value + tolerance
            if lower > upper:
                classified[metric][step_start] = ('inverted_bounds', ZERO, lower, upper)
                continue
            if actual is None:
                classified[metric][step_start] = ('missing_input', ZERO, lower, upper)
                continue
            class_code, deviation = out_class(actual, lower, upper)
            classified[metric][step_start] = (class_code, deviation, lower, upper)
    return classified


def oracle_aggregate(classified, steps):
    """Doc §4.2 aggregation: valid = evaluable population (in-range + out),
    everything else unknown/suspect; ratios per §4.2 with NULL rules."""
    per_metric = {}
    for metric, slots in classified.items():
        valid = out = out_low = out_high = unknown = suspect = 0
        reasons = {}
        for step_start in steps:
            entry = slots.get(step_start)
            class_code = entry[0] if entry else 'missing_input'
            if class_code in VALID_CLASSES:
                valid += CADENCE_MINUTES
            else:
                unknown += CADENCE_MINUTES
                reasons[class_code] = reasons.get(class_code, 0) + CADENCE_MINUTES
            if class_code == 'out_low':
                out += CADENCE_MINUTES
                out_low += CADENCE_MINUTES
            elif class_code == 'out_high':
                out += CADENCE_MINUTES
                out_high += CADENCE_MINUTES
            elif class_code == 'quality_suspect':
                suspect += CADENCE_MINUTES
        per_metric[metric] = {
            'expected_minutes': Decimal(str(CADENCE_MINUTES * len(steps))),
            'valid_minutes': Decimal(str(valid)), 'out_minutes': Decimal(str(out)),
            'out_low_minutes': Decimal(str(out_low)),
            'out_high_minutes': Decimal(str(out_high)),
            'suspect_minutes': Decimal(str(suspect)),
            'unknown_minutes': Decimal(str(unknown)),
            'unknown_reasons': {code: Decimal(str(minutes))
                                for code, minutes in sorted(reasons.items())},
            'out_hours': None if valid == 0 else Decimal(str(out)) / Decimal(60),
            'out_percent': pct(out, valid),
            'coverage_pct': pct(valid, CADENCE_MINUTES * len(steps)),
        }
    return per_metric


def oracle_episodes(classified, cycle_end, rule_version):
    """§4.3: contiguous OUT runs per metric on the FULL cycle; a gap or any
    non-out class breaks a run. Returns the identity tuple per episode. The
    rule_version of every episode is the explicitly selected revision being
    reconciled (the param rules the identity; None is never legal here)."""
    episodes = []
    for metric in sorted(classified):
        slots = classified[metric]
        runs, run = [], []
        for step_start in sorted(slots):
            if slots[step_start][0] in OUT_CLASSES:
                if run and step_start != run[-1] + timedelta(minutes=CADENCE_MINUTES):
                    runs.append(run)
                    run = []
                run.append(step_start)
            elif run:
                runs.append(run)
                run = []
        if run:
            runs.append(run)
        for run in runs:
            peak = max(slots[start][1] for start in run)
            directions = sorted({('low' if slots[start][0] == 'out_low' else 'high')
                                 for start in run})
            episodes.append({
                'metric': metric,
                'rule_version': rule_version,
                'original_start': run[0].isoformat(),
                'original_end': min(run[-1] + timedelta(minutes=CADENCE_MINUTES),
                                    cycle_end).isoformat(),
                'original_minutes': Decimal(str(CADENCE_MINUTES * len(run))),
                'directions': directions, 'peak_deviation': peak,
                'peak_time': next(start for start in run
                                  if slots[start][1] == peak).isoformat(),
                'slot_count': len(run),
            })
    episodes.sort(key=lambda item: (('temperature', 'co2').index(item['metric']),
                                    item['original_start'], item['original_end']))
    return episodes


def oracle_joint(classified, steps):
    """Doc §4.2 OR row over temperature+co2 on the jointly evaluable set."""
    jointly_valid = jointly_out = 0
    for step_start in steps:
        codes = [classified[metric][step_start][0] if step_start in classified[metric]
                 else 'missing_input' for metric in classified]
        if any(code not in VALID_CLASSES for code in codes):
            continue
        jointly_valid += CADENCE_MINUTES
        if any(code in OUT_CLASSES for code in codes):
            jointly_out += CADENCE_MINUTES
    expected = CADENCE_MINUTES * len(steps)
    return {'jointly_valid_minutes': Decimal(str(jointly_valid)),
            'joint_out_minutes': Decimal(str(jointly_out)),
            'joint_out_percent': pct(jointly_out, jointly_valid),
            'joint_coverage_pct': pct(jointly_valid, expected)}


def read_demo_revision(connection, compartment_id):
    rule_version = f'climate-demo-b01:{compartment_id}:v1'
    revision = read_rule_schedule(connection, rule_version)
    if revision is not None and not (revision.is_demo and revision.label == 'demo_rule'):
        raise ValueError(f'rule {rule_version} exists but is not the documented demo seed')
    return revision


def segments_for_basis(revision, basis):
    """{metric: (start, end, delta)} of the demo revision's declared-basis
    climate segments (frozen content; the oracle never re-derives deltas)."""
    mapping = {}
    for segment in revision.segments:
        if segment.measurement_context != 'climate' or segment.basis != basis:
            continue
        mapping[segment.metric] = (segment.start, segment.end, segment.delta)
    return mapping


def engine_metric_facts(engine_result):
    reports = {}
    for report in engine_result.metric_results:
        reports[report.metric] = {
            'expected_minutes': report.expected_minutes,
            'valid_minutes': report.valid_minutes, 'out_minutes': report.out_minutes,
            'out_low_minutes': report.out_low_minutes,
            'out_high_minutes': report.out_high_minutes,
            'suspect_minutes': report.suspect_minutes,
            'unknown_minutes': report.unknown_minutes,
            'unknown_reasons': {share.reason_code: share.minutes
                                for share in report.unknown_reasons},
            'out_hours': report.out_hours, 'out_percent': report.out_percent,
            'coverage_pct': report.coverage_pct,
        }
    return reports


def engine_episode_facts(engine_result):
    return [{
        'metric': episode.metric, 'rule_version': episode.rule_version,
        'original_start': episode.original_start.isoformat(),
        'original_end': episode.original_end.isoformat(),
        'original_minutes': episode.original_minutes,
        'directions': sorted({direction.value for direction in episode.directions}),
        'peak_deviation': episode.peak_deviation,
        'peak_time': episode.peak_time.isoformat(),
        'slot_count': len(episode.slots),
    } for episode in engine_result.episodes]


def compare_metric_facts(observed, expected, differences):
    keys = ('expected_minutes', 'valid_minutes', 'out_minutes', 'out_low_minutes',
            'out_high_minutes', 'suspect_minutes', 'unknown_minutes', 'unknown_reasons',
            'out_hours', 'out_percent', 'coverage_pct')
    for key in keys:
        value, other = observed.get(key), expected.get(key)
        if key == 'unknown_reasons':
            if value != other:
                differences.append(f'{key}: {value} != {other}')
        elif key in ('out_percent', 'coverage_pct', 'out_hours'):
            if not close(value, other):
                differences.append(f'{key}: {exact(value)} != {exact(other)}')
        else:
            if value != other:
                differences.append(f'{key}: {exact(value)} != {exact(other)}')


def reconcile_basis(service, publication, cycle, basis, revision,
                    mart_by_start, steps, window_end):
    """One declared-basis reconciliation: oracle vs the typed service, exact
    episode sets and one lineage spot on the longest episode."""
    segment_map = segments_for_basis(revision, basis)
    classified = oracle_classify(mart_by_start, steps, segment_map, basis)
    expected_summary = oracle_aggregate(classified, steps)
    expected_episodes = oracle_episodes(classified, window_end, revision.rule_version)
    expected_joint = oracle_joint(classified, steps)
    context = wiring.build_context(publication, cycle, cycle.start, cycle.end - timedelta(days=1))
    evaluation = service.get_evaluation(context, basis=basis, rule_version=revision.rule_version)
    observed_summary = engine_metric_facts(evaluation.result)
    observed_episodes = engine_episode_facts(evaluation.result)
    observed_joint = evaluation.result.joint_results[0]
    differences = []
    for metric in expected_summary:
        compare_metric_facts(observed_summary.get(metric, {}),
                             expected_summary[metric], differences)
        if metric not in observed_summary:
            differences.append(f'{metric}: engine result missing')
    expected_episodes_sorted = expected_episodes  # already in engine order
    if expected_episodes_sorted != observed_episodes:
        differences.append(f'episodes: oracle {len(expected_episodes_sorted)} vs engine '
                           f'{len(observed_episodes)}')
        for oracle_item, engine_item in zip(expected_episodes_sorted, observed_episodes):
            if oracle_item != engine_item:
                differences.append('episode: ' + json.dumps({
                    key: exact(value) for key, value in oracle_item.items()})
                    + ' != engine ' + json.dumps({
                        key: exact(value) for key, value in engine_item.items()}))
                break
    joint_row = observed_joint
    for key in expected_joint:
        value, other = getattr(joint_row, key), expected_joint[key]
        if key in ('joint_out_percent', 'joint_coverage_pct'):
            if not close(value, other):
                differences.append(f'joint {key}: {exact(value)} != {exact(other)}')
        elif value != other:
            differences.append(f'joint {key}: {exact(value)} != {exact(other)}')
    # Identity checks that must hold for ANY engine result
    if evaluation.result.publication != publication:
        differences.append('engine result lost the pinned publication identity')
    if evaluation.basis != basis:
        differences.append('engine result lost the declared basis')
    # Lineage spot: the LONGEST engine episode's peak slot -> raw provenance
    spot = None
    if observed_episodes:
        longest = max(observed_episodes, key=lambda item: (item['original_minutes'],
                                                           item['original_start']))
        peak_at = datetime.fromisoformat(longest['peak_time'])
        mart_row = mart_by_start.get(peak_at)
        if mart_row is None:
            differences.append('lineage spot: peak slot missing in the frozen mart')
        else:
            lineage = service.get_lineage(publication, revision_id=mart_row['revision_id'])
            mapped_slot, residual = serial_slot(lineage.source_time_serial)
            verified = (residual <= Decimal('0.01') and mapped_slot == peak_at
                        and lineage.raw_hash_verified is True
                        and lineage.relative_path.endswith('GreenhouseClimate.csv'))
            spot = {'metric': longest['metric'], 'peak_time': peak_at.isoformat(),
                    'revision_id': lineage.revision_id, 'relative_path': lineage.relative_path,
                    'line_number': lineage.line_number,
                    'source_time_serial_serialized': exact(lineage.source_time_serial),
                    'serial_maps_to_peak_slot': mapped_slot == peak_at,
                    'raw_hash_verified': lineage.raw_hash_verified}
            if not verified:
                differences.append('lineage spot: serial/hash/path consistency failed')
    return {
        'match': not differences,
        'differences': differences[:12],
        'oracle_summary': expected_summary,
        'engine_summary': observed_summary,
        'episode_counts': {'oracle': len(expected_episodes_sorted),
                           'engine': len(observed_episodes)},
        'lineage_spot': spot,
    }


def run_agc_reconciliation(kwargs, raw_root):
    """Read-only reconciliation against the current climate publication."""
    results = {'mode': 'agc', 'read_only': True,
               'oracle': 'doc §4 reimplemented inside this runner (no service '
                         'import of the slot/classification code)',
               'cycles': [], 'all_match': True}
    with psycopg.connect(**kwargs) as connection:
        catalog = CatalogService(PostgresCatalogAdapter(connection))
        usable = catalog.climate_usable()
        if len(usable) != 1:
            raise ValueError('Expected exactly one climate-usable publication on this '
                             f'database: found {len(usable)}')
        row = usable[0]
        adapter = PostgresClimateQueryAdapter(connection)
        publication = adapter.publication(row.publication_id)
        results['publication'] = {
            'publication_id': publication.publication_id,
            'scope_id': publication.scope_id, 'snapshot_id': publication.snapshot_id,
            'committed_at': publication.committed_at.isoformat(),
            'finality_status': publication.finality_status,
            'code_version': publication.versions.code_version,
            'quality_policy_version': publication.versions.quality_policy_version,
            'parser_version': publication.versions.parser_version,
            'climate_bundle': row.climate_bundle_version,
            'cycle_count': row.cycle_count,
        }
        with connection.cursor() as cursor:
            current = cursor.execute(
                'SELECT publication_id FROM meta.current_publication WHERE data_scope_id=%s',
                (publication.scope_id,)).fetchone()
        results['publication']['is_current'] = bool(current) \
            and current[0] == publication.publication_id
        mapping = wiring.raw_snapshot_mapping(Path(raw_root))
        snapshot_dir = mapping.get(publication.snapshot_id)
        if snapshot_dir is None:
            raise ValueError('Raw snapshot for the pinned publication is not mounted '
                             f'{sorted(mapping)[:4]}')
        adapter = PostgresClimateQueryAdapter(
            connection, raw_snapshots={publication.snapshot_id: snapshot_dir})
        service = ClimateService(adapter)
        for cycle in catalog.cycles(publication):
            folder = connection.execute(
                'SELECT source_folder FROM meta.source_folder_mapping '
                'WHERE snapshot_id=%s AND compartment_id=%s',
                (publication.snapshot_id, cycle.compartment_id)).fetchone()[0]
            window_start = datetime.combine(cycle.start, time.min)
            window_end = datetime.combine(cycle.end, time.min)
            raw_slots, boundary, raw_first, raw_last = read_raw_climate(
                snapshot_dir / folder / 'GreenhouseClimate.csv', window_start, window_end)
            mart_rows = connection.execute(
                "SELECT slot_start, tair, co2air, t_heat_sp, t_vent_sp, co2_sp, "
                "t_heat_vip, t_ventlee_vip, co2_vip, measure_quality, revision_id "
                "FROM mart.fact_climate_slot WHERE publication_id=%s AND cycle_id=%s "
                "ORDER BY slot_start", (publication.publication_id, cycle.cycle_id)
            ).fetchall()
            mart_by_start = {
                mart_row[0]: {
                    'revision_id': mart_row[10], 'measure_quality': mart_row[9],
                    **dict(zip(CLIMATE_COLUMNS, mart_row[1:9])),
                }
                for mart_row in mart_rows
            }
            steps = [window_start + timedelta(minutes=CADENCE_MINUTES * step)
                     for step in range(int((window_end - window_start)
                                            / timedelta(minutes=CADENCE_MINUTES)))]
            reconciled = {
                'cycle_id': cycle.cycle_id, 'compartment': cycle.compartment_id,
                'window': f'[{cycle.start.isoformat()},{cycle.end.isoformat()})',
                'raw_slots': len(raw_slots), 'mart_slots': len(mart_rows),
                'raw_first': raw_first.isoformat() if raw_first else None,
                'raw_last': raw_last.isoformat() if raw_last else None,
                'boundary_sample_stored_count': len(boundary),
                'mart_rows_beyond_cycle': sum(1 for key in mart_by_start
                                              if key >= window_end),
                'mart_rows_at_end_only': sum(1 for key in mart_by_start
                                             if key == window_end),
            }
            value_mismatch, quality_shape_mismatch, offmode_mismatch = 0, 0, 0
            samples = []
            reconcile_keys = sorted(key for key in set(raw_slots) | set(mart_by_start)
                                    if key < window_end)
            for slot_start in reconcile_keys:
                raw_row, mart_row = raw_slots.get(slot_start), mart_by_start.get(slot_start)
                if raw_row is None or mart_row is None:
                    value_mismatch += 1
                    samples.append({'slot': slot_start.isoformat(),
                                    'issue': 'no_raw_row' if raw_row is None
                                    else 'no_mart_row'})
                    continue
                for raw_code, mart_code in COLUMN_PAIRS:
                    if raw_row[mart_code] != mart_row[mart_code]:
                        value_mismatch += 1
                        if len(samples) < 6:
                            samples.append({'slot': slot_start.isoformat(),
                                            'code': mart_code,
                                            'raw': exact(raw_row[mart_code]),
                                            'mart': exact(mart_row[mart_code])})
                triplets = mart_row['measure_quality']
                if set(triplets) != set(RAW_COLUMNS) or any(
                        triplet_of(triplets, code).get('status') not in STATUS_PRECEDENCE
                        for code in RAW_COLUMNS):
                    quality_shape_mismatch += 1
                for column in ('co2_sp', 'co2_vip'):
                    if mart_row.get(column) == ZERO:
                        triplet = triplet_of(triplets, column)
                        if [triplet.get('status'), triplet.get('applicability'),
                            triplet.get('reason')] != ['accepted', 'unknown',
                                                       'off_mode_unverified']:
                            offmode_mismatch += 1
            # The EXACT-END sample is intentionally STORED once per cycle (main:
            # 47,809 mart rows vs 47,808 duration slots): it is kept as source
            # but contributes ZERO duration. Reconcile it separately: exactly
            # one raw end row; the mart may hold exactly that one row and its
            # values must match verbatim; NOTHING may sit after the end.
            raw_end_count = len(boundary)
            mart_end = mart_by_start.get(window_end)
            mart_beyond = sum(1 for key in mart_by_start if key > window_end)
            boundary_value_mismatch = 0
            if raw_end_count == 1:
                if mart_end is None:
                    boundary_value_mismatch += 1
                else:
                    for raw_code, mart_code in COLUMN_PAIRS:
                        if boundary[0][mart_code] != mart_end[mart_code]:
                            boundary_value_mismatch += 1
            elif raw_end_count > 1:
                boundary_value_mismatch += raw_end_count - 1
            reconciled['value_mismatch'] = value_mismatch
            reconciled['quality_shape_mismatch'] = quality_shape_mismatch
            reconciled['off_mode_triplet_mismatch'] = offmode_mismatch
            reconciled['boundary_value_mismatch'] = boundary_value_mismatch
            reconciled['mart_rows_beyond_cycle'] = mart_beyond
            reconciled['mismatch_samples'] = samples
            demo = read_demo_revision(connection, cycle.compartment_id)
            reconciled['demo_rule'] = demo.rule_version if demo else None
            reconciled['kpis'] = {}
            if demo is not None:
                for basis in (RuleBasis.REQUESTED, RuleBasis.REALIZED):
                    reconciled['kpis'][basis.value] = reconcile_basis(
                        service, publication, cycle, basis, demo, mart_by_start,
                        steps, window_end)
            else:
                reconciled['demo_missing'] = True
            reconciled['mart_end_row'] = mart_end is not None
            reconciled['match'] = (
                reconciled['raw_slots'] == reconciled['mart_slots']
                - (reconciled['mart_end_row'] and 1 or 0)
                and mart_beyond == 0
                and boundary_value_mismatch == 0
                and (raw_end_count == 1) == (mart_end is not None)
                and value_mismatch == 0 and quality_shape_mismatch == 0
                and offmode_mismatch == 0
                and bool(reconciled['kpis'])
                and all(entry['match'] for entry in reconciled['kpis'].values()))
            results['cycles'].append(reconciled)
            results['all_match'] = results['all_match'] and reconciled['match']
    results['lineage_spots'] = [
        {'cycle': reconciled['cycle_id'], 'basis': basis_value,
         **entry['lineage_spot']}
        for reconciled in results['cycles'] if reconciled.get('kpis')
        for basis_value, entry in reconciled['kpis'].items()
        if entry.get('lineage_spot')]
    return results


# ------------------------------------------------------------ performance --
def percentile(sorted_values, fraction):
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return sorted_values[min(index, len(sorted_values) - 1)]


def container_limits():
    limits = {}
    for key, path in (('memory_max', '/sys/fs/cgroup/memory.max'),
                      ('memory_peak', '/sys/fs/cgroup/memory.peak'),
                      ('cpu_max', '/sys/fs/cgroup/cpu.max')):
        try:
            limits[key] = Path(path).read_text().strip()
        except OSError:
            limits[key] = 'unavailable'
    return limits


def timed(call):
    """perf alias note: the module global `time` is the datetime.time CLASS
    (shadowed by the datetime import), so the stopwatch comes from the
    stdlib-imported perf_counter below."""
    start = perf_counter()
    call()
    return perf_counter() - start


def measure_component(call, runs, warmups):
    """Timing convention: the first call stays separate, then 5 warmups +
    `runs` measured runs. The report persists the RAW durations in capture
    order (not only quantiles) plus every warmup duration."""
    first_request = timed(call)
    warmup_raw = [round(timed(call), 6) for _ in range(warmups)]
    raw_samples = [round(timed(call), 6) for _ in range(runs)]
    sorted_samples = sorted(raw_samples)
    p95 = Decimal(repr(percentile(sorted_samples, 0.95))).quantize(Decimal('0.000001'))
    return {
        'first_request_seconds': round(first_request, 6),
        'warmup_durations_seconds': warmup_raw,
        'raw_samples_seconds': raw_samples,
        'p50_seconds': round(statistics.median(sorted_samples), 6),
        'p95_seconds': float(p95), 'min_seconds': sorted_samples[0],
        'max_seconds': sorted_samples[-1],
        'target_seconds': exact(P95_TARGET_SECONDS),
        'target_met': p95 <= P95_TARGET_SECONDS,
        'first_request_note': 'first timed call of this component on the shared '
                              'connection; earlier calls of OTHER workloads may '
                              'have warmed caches — not a verified cold-cache run',
    }


def run_performance(kwargs, runs=30, warmups=5):
    results = {'mode': 'performance', 'target_seconds': exact(P95_TARGET_SECONDS),
               'runs': runs, 'warmups': warmups, 'workloads': {},
               'cache': {'bounded_result_cache': 'ClimateService max 2 entries; keys '
                                                  '(host,port,dbname,user)+publication/'
                                                  'versions+cycle window+basis+rule '
                                                  'content signature'},
               'environment': {
                   'python': platform.python_version(), 'platform': platform.platform(),
                   'limits': container_limits()}}
    with psycopg.connect(**kwargs) as connection:
        catalog = CatalogService(PostgresCatalogAdapter(connection))
        usable = catalog.climate_usable()
        if len(usable) != 1:
            raise ValueError('Expected exactly one climate-usable publication on this '
                             f'database: found {len(usable)}')
        row = usable[0]
        service = ClimateService(PostgresClimateQueryAdapter(connection))
        publication = service.adapter.publication(row.publication_id)
        cycles = sorted(catalog.cycles(publication),
                        key=lambda cycle: (cycle.end - cycle.start, cycle.cycle_id))
        cycle = cycles[-1]  # THE AUTOMATORS representative: tied-longest cycle
        # window on this publication. This is NOT a proven worst-case latency
        # claim: the other five compartments are not benchmarked here.
        demo = read_demo_revision(connection, cycle.compartment_id)
        if demo is None:
            raise ValueError('perf evidence requires the climate demo seed first '
                             '(python -m greenhouse.db.demo_rules --kind climate --apply)')
        results['workload'] = {
            'publication_id': publication.publication_id,
            'compartment': cycle.compartment_id, 'cycle': cycle.cycle_id,
            'window': f'[{cycle.start.isoformat()},{cycle.end.isoformat()})',
            'rule_version': demo.rule_version,
            'connection_state': 'one persistent connection; per call the service '
                                're-resolves the pinned publication by ID',
        }
        context = wiring.build_context(publication, cycle, cycle.start, cycle.end
                                       - timedelta(days=1))
        draft = {
            'summary_full_cycle': {
                'label': 'typed evaluation of the full cycle (summary + full-cycle '
                         'episode construction); first call is an UNCACHED MISS, '
                         'measured runs are warm cache HITS on the bounded '
                         '2-entry result cache',
                'components': {'evaluation': lambda: service.get_evaluation(
                    context, basis=RuleBasis.REQUESTED, rule_version=demo.rule_version)},
            },
            'summary_plus_first_page': {
                'label': 'typed evaluation + first episode page (list of 40) — '
                         'SERVICE-level workload only: it is a SUBSET of screen '
                         'work (the real screen open also loads detail chart/trace, '
                         'source inspection and export/catalog reads, which are '
                         'NOT measured here); every call INCLUDING the first is '
                         'warm cache HITS because the summary workload above has '
                         'already evaluated this identical key', 'components': {
                    'evaluation_plus_page': lambda: page_episodes(
                        service.get_evaluation(context, basis=RuleBasis.REQUESTED,
                                               rule_version=demo.rule_version).result,
                        limit=40, offset=0)},
            },
            'rule_change_recalc': {
                'label': 'REAL recomputation forced by a rule selection change, '
                         'reported as TWO SEPARATE forced-uncached evaluations '
                         '(NO combined latency is implied for one change): the '
                         'no-rule selection and the demo revision selection are '
                         'each re-evaluated from frozen slots with the cache '
                         'BYPASSED (use_cache=False) so no cache hit contaminates '
                         'either measurement',
                'components': {
                    'no_rule_uncached': lambda: service.get_evaluation(
                        context, basis=RuleBasis.REQUESTED, use_cache=False),
                    'rule_selected_uncached': lambda: service.get_evaluation(
                        context, basis=RuleBasis.REQUESTED,
                        rule_version=demo.rule_version, use_cache=False),
                },
            },
        }
        for name, spec in draft.items():
            component_reports = {}
            for component_name, call in spec['components'].items():
                report_i = measure_component(call, runs, warmups)
                if name == 'rule_change_recalc':
                    # two forced-uncached re-evaluations: no ≤2s target claim here
                    report_i['target_seconds'] = None
                    report_i['target_met'] = None
                component_reports[component_name] = report_i
            # single-component workloads ALSO surface their numbers at the top
            # level; rule_change keeps ONLY explicit per-component numbers.
            entry = {'label': spec['label'], 'components': component_reports,
                     'ru_maxrss_kb_after_workload': resource.getrusage(
                         resource.RUSAGE_SELF).ru_maxrss,
                     'cgroup_memory_peak_bytes_after_workload': container_limits()[
                         'memory_peak']}
            if len(component_reports) == 1:
                entry.update(next(iter(component_reports.values())))
            results['workloads'][name] = entry
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m greenhouse.evaluation.br01')
    parser.add_argument('--mode', choices=['agc', 'performance', 'all'], required=True)
    parser.add_argument('--raw-root', default=os.environ.get('GREENHOUSE_RAW_DIR', '/data/raw'))
    parser.add_argument('--runs', type=int, default=30)
    parser.add_argument('--output', type=Path, help='write the JSON report here instead of stdout')
    options = parser.parse_args(argv)
    kwargs = Settings.from_env().connection_kwargs()
    report = {'tool': 'greenhouse.evaluation.br01',
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'database': kwargs['dbname']}
    if options.mode in ('agc', 'all'):
        report['agc'] = run_agc_reconciliation(kwargs, options.raw_root)
    if options.mode in ('performance', 'all'):
        report['performance'] = run_performance(kwargs, runs=options.runs)
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if options.output:
        options.output.write_text(payload + '\n', encoding='utf-8')
        print(f'report written: {options.output}')
    else:
        print(payload)
    if report.get('agc') and not report['agc']['all_match']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
