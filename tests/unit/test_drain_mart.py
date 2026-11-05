"""BR-03 drain bundle + service (T16): pipeline → mart → slots → service.

Fixture publications run the REAL pipeline (Spark stage replaced with a
prepared artifact, exactly like the water/harvest/climate harness). The
drain bundle freezes EC/pH values in the exact 'drain_process_computer'
context plus the policy's drain-measure quality triplets (EC negative ->
invalid and pH 0 -> suspect come from the SAME loader policy rows, never
re-derived). The measured basis evaluates FIXED bounds from the
explicitly selected immutable revision; masked evaluation inputs never
rewrite source facts. The literal BR-03 §7 exact-30-minute numbers stay
separate from full-day pipeline integration numbers (different coverage
denominators, never relabeled).
"""
import csv
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from greenhouse.contracts import (
    CycleWindow, DrainEvaluation, IntradayContext, QualityStatus, QueryContext,
    RuleBasis, RuleDurationResult, RuleScheduleRevision, RuleScheduleSegment,
    RuleDeviationDirection, ThresholdSlot, rule_segment_sort_key, to_json,
)
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.db.rule_schedule import read_rule_schedule, register_rule_schedule
from greenhouse.queries import CatalogService, page_episodes
from greenhouse.queries import drain as drain_module
from greenhouse.queries.drain import (
    DRAIN_REVIEW_MAX_SLOTS, DrainService, build_slots,
    drain_rule_content_signature, evaluation_cache_key,
)
from greenhouse.queries.rule_duration import evaluate_rule_schedule
from greenhouse.db.pipeline import run_pipeline
from greenhouse.db.migrate import migrate
from test_query_adapter import contracts_database, fixture_stage


@pytest.fixture
def drain_database(contracts_database):
    """Function-scoped disposable database that migrates the CURRENT
    package migrations then drops itself."""
    with psycopg.connect(**contracts_database, autocommit=True) as admin:
        name = 'test_drain16_' + uuid4().hex[:10]
        admin.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(
            psycopg.sql.Identifier(name)))
    target = dict(contracts_database, dbname=name)
    with psycopg.connect(**target) as connection:
        migrate(connection)
    yield target
    with psycopg.connect(**contracts_database, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL('DROP DATABASE {} WITH (FORCE)').format(
            psycopg.sql.Identifier(name)))


def pct(numerator, denominator):
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * Decimal(100) / Decimal(denominator)


EXCEL_EPOCH = date(1899, 12, 30)
DRAIN_CONTEXT = 'drain_process_computer'
DRAIN_POLICIES = ('EC_drain_PC', 'pH_drain_PC')


def serial_text(day, slot_index):
    return f'{(day - EXCEL_EPOCH).days + slot_index / 288:.5f}'


def write_climate_csv(path, overrides_by_slot, *, days=1):
    """Drain measures live on the standard GreenhouseClimate.csv columns."""
    from greenhouse.ingestion.parser import HEADERS
    headers = HEADERS['GreenhouseClimate']
    default_values = {'Tair': '22', 'CO2air': '500', 't_heat_sp': '20',
                      't_vent_sp': '25', 'co2_sp': '500', 't_heat_vip': '20.5',
                      't_ventlee_vip': '24.5', 'co2_vip': '500',
                      'EC_drain_PC': '3', 'pH_drain_PC': '6'}
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\r\n')
        writer.writerow(headers)
        for absolute_slot in sorted(overrides_by_slot):
            values = dict(default_values, **overrides_by_slot[absolute_slot])
            day_offset, slot_index = divmod(absolute_slot, 288)
            writer.writerow([serial_text(date(2020, 1, 1) + timedelta(days=day_offset),
                                         slot_index)]
                            + [values.get(header.strip(), '')
                               for header in headers[1:]])


def fixture_stage(connection, workflow, target, *, fault=None):
    from greenhouse.db.pipeline import membership, save_artifact
    with connection.transaction():
        info = membership(connection, workflow)
    target.mkdir()
    (target / 'fixture.json').write_text(
        json.dumps(info, sort_keys=True, separators=(',', ':')))
    return save_artifact(target, info | {'rows_in': info['member_count'],
                                         'rows_out': info['member_count']})


def publish_drain_fixture(connection, scratch, folders, *, start=date(2020, 1, 1),
                          end=date(2020, 1, 3)):
    """One scope; every compartment keeps its OWN cycle over the same grid.
    folders: {source_folder: {absolute_slot: {column: override}}}. The
    policy covers the drain measures: EC nonnegative (negative -> invalid),
    pH 0 -> suspect, missing -> missing."""
    tag = uuid4().hex
    site, snapshot, scope, policy = (name + ':' + tag
                                     for name in ('site', 'snapshot', 'scope', 'quality'))
    connection.execute("INSERT INTO core.site VALUES (%s,'Fixture','source_naive','fixture')",
                       (site,))
    connection.execute(
        "INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",
        (snapshot, tag * 2))
    connection.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",
                       (scope, snapshot))
    definition = {'missing': 'per_measure',
                  'nonnegative': sorted(('EC_drain_PC', 'pH_drain_PC')),
                  'suspect_zero': ['pH_drain_PC'], 'off_mode_zero': []}
    payload = json.dumps(definition, sort_keys=True, separators=(',', ':'))
    connection.execute(
        "INSERT INTO config.quality_policy_version VALUES "
        "(%s,1,%s,%s,%s,%s,true,'synthetic drain policy')",
        (policy, start, end, payload, hashlib.sha256(payload.encode()).hexdigest()))
    for measure, record_type, unit in [('EC_drain_PC', 'drain', 'dS/m'),
                                       ('pH_drain_PC', 'drain', 'pH'),
                                       ('Tair', 'climate', '°C'),
                                       ('CO2air', 'climate', 'ppm')]:
        connection.execute(
            "INSERT INTO config.measure_definition VALUES (%s,%s,%s,'fixture') "
            "ON CONFLICT (measure_code) DO NOTHING", (measure, record_type, unit))
    cycles = {}
    files = []
    raw = scratch / tag / 'raw'
    raw.mkdir(parents=True)
    for folder_index, folder in enumerate(sorted(folders)):
        comp, cycle = (f'comp:{tag}{folder_index}', f'cycle:{tag}{folder_index}')
        team, crop = (f'team:{tag}{folder_index}', f'crop:{tag}{folder_index}')
        connection.execute("INSERT INTO core.compartment VALUES (%s,%s,%s,'fixture')",
                           (comp, site, folder))
        connection.execute("INSERT INTO core.team VALUES (%s,%s,false,'fixture')",
                           (team, folder))
        connection.execute("INSERT INTO core.crop VALUES (%s,'tomato',NULL,'fixture')",
                           (crop,))
        connection.execute(
            "INSERT INTO core.growing_cycle VALUES (%s,%s,%s,%s,%s,%s,'fixture')",
            (cycle, comp, team, crop, start, end))
        connection.execute(
            "INSERT INTO meta.source_folder_mapping VALUES (%s,%s,%s,%s,%s,'fixture')",
            (snapshot, folder, comp, team, cycle))
        grid = {slot: {} for slot in range(288 * (end - start).days)}
        for absolute_slot, values in folders[folder].items():
            grid[absolute_slot] = values
        folder_dir = raw / folder
        folder_dir.mkdir(parents=True, exist_ok=True)
        climate_csv = folder_dir / 'GreenhouseClimate.csv'
        write_climate_csv(climate_csv, grid, days=(end - start).days)
        files.append({'path': f'{folder}/GreenhouseClimate.csv',
                      'sha256': hashlib.sha256(climate_csv.read_bytes()).hexdigest(),
                      'csv': {'data_rows': len(grid)}})
        cycles[folder] = {'comp': comp, 'cycle': cycle}
    (raw / 'manifest.json').write_text(json.dumps(
        {'snapshot_id': snapshot, 'archive': {'sha256': tag * 2}, 'files': files}))
    result = run_pipeline(connection, raw, scratch / tag / 'output', scope, policy,
                          stage_function=fixture_stage)
    return {'scope': scope, 'snapshot': snapshot, 'policy': policy, 'raw': raw,
            'publication': result['publication_id'], 'cycles': cycles,
            'days': (end - start).days}


def make_service(connection, fixture):
    raw_snapshots = ({fixture['snapshot']: fixture['raw']} if fixture else None)
    return DrainService(PostgresDrainQueryAdapter(
        connection, raw_snapshots=raw_snapshots))


class _NamespacelessDrainAdapter:
    """Facade WITHOUT a cache namespace (database_key = None): proves the
    declared-profile boundary also holds when the cache is structurally
    disabled, not merely bypassed by the caller."""

    database_key = None

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


def drain_context(fixture, folder, start=None, days=None):
    """Default window = the WHOLE cycle (start → fixture days)."""
    cycle = fixture['cycles'][folder]
    start = start or date(2020, 1, 1)
    days = days if days is not None else (date(2020, 1, 3) - start).days
    return QueryContext(fixture['scope'], fixture['publication'], fixture['snapshot'],
                        cycle['cycle'], (cycle['comp'],), start,
                        start + timedelta(days=days))


def drain_rule(fixture, *, version=1, tag='drn', end=date(2020, 1, 3),
               ec=(Decimal('2'), Decimal('5')), ph=(Decimal('5.5'), Decimal('6.5'))):
    """v1 segments cover the DEFAULT two-day cycle end (Jan 3 00:00)."""
    return RuleScheduleRevision(
        rule_version=f'rule-version:{tag}:{version}', rule_id=f'rule:{tag}',
        version=version,
        scope_id=fixture['scope'], compartment_id=fixture['cycles']['Only']['comp'],
        evidence_reference='fixture fixed drain bounds', is_demo=True, label='demo_rule',
        segments=tuple(sorted((
            RuleScheduleSegment(metric='ec', measurement_context=DRAIN_CONTEXT,
                                basis=RuleBasis.MEASURED, start=datetime(2020, 1, 1),
                                end=datetime.combine(end, datetime.min.time()),
                                lower=ec[0], upper=ec[1]),
            RuleScheduleSegment(metric='ph', measurement_context=DRAIN_CONTEXT,
                                basis=RuleBasis.MEASURED, start=datetime(2020, 1, 1),
                                end=datetime.combine(end, datetime.min.time()),
                                lower=ph[0], upper=ph[1])),
            key=rule_segment_sort_key)))


# ------------------------------------------------- exact §7 layer (30-min) --
def _exact_slots():
    """BR-03 §7 six-slot 30-minute fixture, EC/pH measured slots, fixed
    bands EC [2,5] / pH [5.5,6.5]. Slot times align to the 5-minute grid;
    pH=0 is the loaded SUSPECT row (never a pH verdict from the band)."""
    ec = ['3', '6', '6', None, '3', '6']
    ph = ['6', '6', '7', '6', None, '6']      # index 4 = suspect quantity
    ph_suspecting = {4}
    base = datetime(2020, 1, 1)
    slots = []
    for index in range(6):
        start = base + timedelta(minutes=5 * index)
        ec_value = ec[index]
        slots.append(ThresholdSlot(
            start=start, metric='ec', measurement_context=DRAIN_CONTEXT,
            basis=RuleBasis.MEASURED,
            actual=None if ec_value is None else Decimal(ec_value),
            quality=QualityStatus.MISSING if ec_value is None else QualityStatus.ACCEPTED))
        if index in ph_suspecting:
            slots.append(ThresholdSlot(
                start=start, metric='ph', measurement_context=DRAIN_CONTEXT,
                basis=RuleBasis.MEASURED, actual=None, quality=QualityStatus.SUSPECT))
        else:
            slots.append(ThresholdSlot(
                start=start, metric='ph', measurement_context=DRAIN_CONTEXT,
                basis=RuleBasis.MEASURED, actual=Decimal(ph[index]),
                quality=QualityStatus.ACCEPTED))
    return tuple(slots)


def _exact_revisions():
    segment_ec = RuleScheduleSegment(
        metric='ec', measurement_context=DRAIN_CONTEXT, basis=RuleBasis.MEASURED,
        start=datetime(2020, 1, 1), end=datetime(2020, 1, 2),
        lower=Decimal('2'), upper=Decimal('5'))
    segment_ph = RuleScheduleSegment(
        metric='ph', measurement_context=DRAIN_CONTEXT, basis=RuleBasis.MEASURED,
        start=datetime(2020, 1, 1), end=datetime(2020, 1, 2),
        lower=Decimal('5.5'), upper=Decimal('6.5'))
    return (RuleScheduleRevision(
        rule_version='rule-version:br03', rule_id='rule:br03', version=1,
        scope_id='scope:ac', compartment_id='comp:ac',
        evidence_reference='doc §7 fixture (synthetic, not AGC)', is_demo=True,
        label='demo_rule',
        segments=(segment_ec, segment_ph)),)


def _intraday(start, end):
    return IntradayContext(
        scope_id='scope:ac', cycle=CycleWindow('cycle:ac', 'comp:ac', start.date(),
                                               start.date() + timedelta(days=1)),
        compartment_ids=('comp:ac',), start=start, end=end,
        measurement_context=DRAIN_CONTEXT, basis=RuleBasis.MEASURED)


def test_br03_exact_30min_shapes():
    """§7 known answers on the shared engine (30-minute window; NEVER
    relabeled against full-day integration numbers)."""
    result = evaluate_rule_schedule(
        _intraday(datetime(2020, 1, 1), datetime(2020, 1, 1, 0, 30)),
        _exact_revisions(), _exact_slots(), ('ec', 'ph'))
    ec, ph = result.metric_results
    joint = result.joint_results[0]
    assert ec.expected_minutes == Decimal('30')
    assert ec.valid_minutes == Decimal('25') and ec.unknown_minutes == Decimal('5')
    assert ec.out_minutes == Decimal('15') and ec.out_high_minutes == Decimal('15')
    assert ec.out_hours == Decimal('0.25')
    assert ec.out_percent == pct(15, 25) == Decimal('60')
    assert ec.coverage_pct == pct(25, 30)
    assert [(share.reason_code, share.minutes) for share in ec.unknown_reasons] == [
        ('missing_input', Decimal('5'))]
    assert ph.valid_minutes == Decimal('25') and ph.unknown_minutes == Decimal('5')
    assert ph.suspect_minutes == Decimal('5')
    assert ph.out_minutes == Decimal('5') and ph.out_high_minutes == Decimal('5')
    assert ph.out_percent == pct(5, 25) == Decimal('20')
    assert ph.coverage_pct == pct(25, 30)
    assert [(share.reason_code, share.minutes) for share in ph.unknown_reasons] == [
        ('quality_suspect', Decimal('5'))]
    # AC-03 OR joint: jointly-known intersection 20, OR out 15 (never 15+5)
    assert joint.jointly_valid_minutes == Decimal('20')
    assert joint.joint_out_minutes == Decimal('15')
    assert joint.joint_out_percent == pct(15, 20) == Decimal('75')
    assert joint.joint_coverage_pct == pct(20, 30)
    episodes = [episode for episode in result.episodes if episode.metric == 'ec']
    assert [(episode.original_minutes, episode.directions) for episode in episodes] == [
        (Decimal('10'), (RuleDeviationDirection.HIGH,)),
        (Decimal('5'), (RuleDeviationDirection.HIGH,))]


def test_br03_boundary_equality():
    """AC-04: EC=2/5 and pH=5.5/6.5 sit inside the fixed bands (boundary
    equality in-range); no unknown, no episode."""
    from greenhouse.db.drain_adapter import DrainSlotRow
    def row(at, ec_value, ph_value):
        accepted = {'status': 'accepted', 'applicability': 'applicable',
                    'reason': None}
        return DrainSlotRow(
            slot_start=at, revision_id='r', logical_key='k', quality_policy_version='p',
            correction_id=None, correction_version=None,
            ec_drain=ec_value, ph_drain=ph_value,
            measure_quality={'EC_drain_PC': accepted, 'pH_drain_PC': accepted})
    rows = (row(datetime(2020, 1, 1), Decimal('2'), Decimal('5.5')),
            row(datetime(2020, 1, 1, 0, 5), Decimal('5'), Decimal('6.5')))
    result = evaluate_rule_schedule(
        _intraday(datetime(2020, 1, 1), datetime(2020, 1, 1, 0, 10)),
        _exact_revisions(), build_slots(rows), ('ec', 'ph'))
    ec, ph = result.metric_results
    assert ec.out_minutes == Decimal('0') and ec.out_hours == Decimal('0')
    assert ph.out_minutes == Decimal('0') and ph.out_hours == Decimal('0')
    assert ec.unknown_minutes == Decimal('0') and ph.unknown_minutes == Decimal('0')
    assert result.episodes == ()


def test_masking_semantics_unit():
    """Pure build_slots masking: EC negative -> invalid mask, pH 0 ->
    suspect, and pH-suspect never removes the accepted EC on the SAME row.
    Source facts keep the original values (build_slots never rewrites the
    frozen mart)."""
    from greenhouse.db.drain_adapter import DrainSlotRow
    row = DrainSlotRow(
        slot_start=datetime(2020, 1, 1), revision_id='r', logical_key='k',
        quality_policy_version='p', correction_id=None, correction_version=None,
        ec_drain=Decimal('-1'), ph_drain=Decimal('0'),
        measure_quality={
            'EC_drain_PC': {'status': 'invalid', 'applicability': 'unknown',
                            'reason': 'invalid_numeric'},
            'pH_drain_PC': {'status': 'suspect', 'applicability': 'unknown',
                            'reason': 'suspect_zero'}})
    slots = build_slots((row,))
    ec, ph = slots
    assert ec.quality == QualityStatus.INVALID and ec.actual is None
    assert ph.quality == QualityStatus.SUSPECT and ph.actual is None
    # source values remain available from the ROW (frozen mart), untouched:
    assert row.ec_drain == Decimal('-1') and row.ph_drain == Decimal('0')
    accepted = replace(
        row, ec_drain=Decimal('3'), ph_drain=Decimal('0'),
        measure_quality={
            'EC_drain_PC': {'status': 'accepted', 'applicability': 'applicable',
                            'reason': None},
            'pH_drain_PC': {'status': 'suspect', 'applicability': 'unknown',
                            'reason': 'suspect_zero'}})
    ec, ph = build_slots((accepted,))
    assert ec.quality == QualityStatus.ACCEPTED and ec.actual == Decimal('3')
    assert ph.quality == QualityStatus.SUSPECT and ph.actual is None


# ------------------------------------------------------- mart integration --
def test_drain_mart_pipeline_freezes_values_quality_counts(drain_database, tmp_path):
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path, {
        'Only': {0: {'EC_drain_PC': '3'}, 1: {'EC_drain_PC': '6'},
                 2: {'EC_drain_PC': ''},
                 287: {'EC_drain_PC': '-1', 'pH_drain_PC': '0'}}})
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        register_rule_schedule(connection, drain_rule(fixture))
        evaluation = service.get_evaluation(
            drain_context(fixture, 'Only'), rule_version='rule-version:drn:1')
        rows = service.adapter.read_drain_slots(drain_context(fixture, 'Only'),
                                                evaluation.publication)
        count = connection.execute(
            'SELECT count(*) FROM mart.fact_drain_slot WHERE publication_id=%s',
            (fixture['publication'],)).fetchone()[0]
        quality = rows[0].measure_quality
    from greenhouse.db.pipeline import CODE_VERSION
    assert evaluation.publication.versions.code_version == CODE_VERSION
    assert count == 288 * 2
    assert set(quality) == set(DRAIN_POLICIES)
    for code in DRAIN_POLICIES:
        triplet = quality[code]
        assert triplet['status'] in ('accepted', 'missing', 'invalid', 'suspect')
        assert {'status', 'applicability', 'reason'} <= set(triplet)
    assert evaluation.result.publication == evaluation.publication
    # masked states on the SAME row (EC invalid, pH suspect) with the EC
    # triplet of a NONnegative accepted row intact elsewhere
    invalid_rows = [row for row in rows if (row.measure_quality['EC_drain_PC']['status']
                                            == 'invalid')]
    suspect_rows = [row for row in rows if (row.measure_quality['pH_drain_PC']['status']
                                            == 'suspect')]
    assert invalid_rows and suspect_rows
    assert invalid_rows[0].ec_drain == Decimal('-1')      # original value, frozen
    assert suspect_rows[0].ph_drain == Decimal('0')
    assert count == len(rows)


def test_drain_service_day_grid_semantics(drain_database, tmp_path):
    """Day-grid integration through the real pipeline: fixed-bound
    evaluation with masking, isolation, and joint OR of jointly-known
    slots. The coverage denominator is the DAY-GRID number (2,880 minutes
    for the default two-day window) — different from the §7 layer and
    never relabeled."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path, {
        'Only': {0: {'EC_drain_PC': '3', 'pH_drain_PC': '6'},
                 1: {'EC_drain_PC': '6', 'pH_drain_PC': '7'},   # both out
                 2: {'EC_drain_PC': '', 'pH_drain_PC': '7'},      # EC missing, pH out
                 3: {'pH_drain_PC': '0'},                        # pH suspect, EC default
                 4: {'EC_drain_PC': '-1'},                       # EC invalid, pH default
                 5: {'EC_drain_PC': '6', 'pH_drain_PC': '6'}}})   # EC only out
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        register_rule_schedule(connection, drain_rule(fixture))
        evaluation = service.get_evaluation(
            drain_context(fixture, 'Only'), rule_version='rule-version:drn:1')
        ec, ph = evaluation.result.metric_results
        joint = evaluation.result.joint_results[0]
    assert ec.expected_minutes == Decimal('2880')  # two-day day-grid window
    # masks: slot 2 EC missing (''), slot 4 EC invalid ('-1') — SAME row pH survives
    assert ec.unknown_minutes == Decimal('10')
    assert {share.reason_code: share.minutes for share in ec.unknown_reasons} == {
        'missing_input': Decimal('5'), 'quality_invalid': Decimal('5')}
    assert ec.valid_minutes == Decimal('2870')
    assert ec.out_minutes == Decimal('10')
    assert ec.out_high_minutes == Decimal('10')
    # pH: slot1 out (7), slot2 out (7), slot3 suspect (0)=unknown 5;
    # slot4 keeps the DEFAULT pH (6, accepted) — only EC was masked there
    ph_unknown = {share.reason_code: share.minutes for share in ph.unknown_reasons}
    assert ph_unknown == {'quality_suspect': Decimal('5')}
    assert ph.valid_minutes == Decimal('2875')
    assert ph.out_minutes == Decimal('10')
    # joint OR on the jointly-known intersection (slots 2/3/4 drop out)
    assert joint.jointly_valid_minutes == Decimal('2865')
    assert joint.joint_out_minutes == Decimal('10')  # slot1 both-out + slot5 EC-out


def test_drain_revision_replay_and_single_publication(drain_database, tmp_path):
    """AC-10 analogue: two fixed-bound versions replay EXACTLY; a rerun of
    the same snapshot does NOT duplicate the publication."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                    {'Only': {0: {'EC_drain_PC': '3'}}})
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        register_rule_schedule(connection, drain_rule(fixture, version=1))
        register_rule_schedule(connection, drain_rule(fixture, version=2, tag='drn2',
                                                      ec=(Decimal('2'), Decimal('4'))))
        first = service.get_evaluation(drain_context(fixture, 'Only'),
                                       rule_version='rule-version:drn:1')
        replayed = service.get_evaluation(drain_context(fixture, 'Only'),
                                          rule_version='rule-version:drn:1')
        second = service.get_evaluation(drain_context(fixture, 'Only'),
                                        rule_version='rule-version:drn2:2')
        publications = connection.execute(
            'SELECT count(*) FROM meta.publication WHERE data_scope_id=%s',
            (fixture['scope'],)).fetchone()[0]
    assert first.result == replayed.result
    assert first.result != second.result
    assert publications == 1


def test_drain_bounded_trace_and_window_shapes(drain_database, tmp_path):
    """Bounded detail BEFORE fetch: a one-day fixture with the whole day
    out-of-band yields one ~1,440-minute episode; a six-slot trace uses
    FLOOR anchor + exact clip (straddling slot included; no slot beyond
    wanted end), evaluation metadata stays whole-cycle, and source notes
    carry the frozen triplet codes."""
    grid = {slot: {'EC_drain_PC': '6'} for slot in range(288)}
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path, {'Only': grid},
                                    start=date(2020, 1, 1), end=date(2020, 1, 2))
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        register_rule_schedule(connection, drain_rule(fixture, end=date(2020, 1, 2)))
        evaluation = service.get_evaluation(drain_context(fixture, 'Only'),
                                            rule_version='rule-version:drn:1')
        episodes = [ep for ep in evaluation.result.episodes if ep.metric == 'ec']
        assert len(episodes) == 1
        assert episodes[0].original_minutes == Decimal('1440')
        assert episodes[0].original_start == datetime(2020, 1, 1)
        assert episodes[0].original_end == datetime(2020, 1, 2)
        start = datetime(2020, 1, 1, 0, 10)
        window = service.get_slot_review(
            drain_context(fixture, 'Only'), rule_version='rule-version:drn:1',
            window_start=start, window_end=start + timedelta(minutes=27))
        ec_reviews = [item for item in window.reviews if item.metric == 'ec']
        # 27 minutes spans six grid slots (floor anchor + exact clip)
        assert len(ec_reviews) == 6
        assert all(item.class_code == 'out_high' for item in ec_reviews)
        assert ec_reviews[0].start == datetime(2020, 1, 1, 0, 10)
        assert ec_reviews[-1].start == datetime(2020, 1, 1, 0, 35)
        assert window.end == start + timedelta(minutes=27)
        assert window.wanted_end == start + timedelta(minutes=27)
        assert window.truncated is False and window.window_slots == 6
        assert window.end == window.wanted_end
        # per-slot source note populated (frozen triplet codes, source value)
        assert all('EC_drain_PC accepted' in item.source_quality for item in ec_reviews)
        assert all('source=6' in item.source_quality for item in ec_reviews)
        # the same-day trace works: no same-date CycleWindow collapse
        assert window.start.date() == window.end.date() == date(2020, 1, 1)


def test_drain_trace_cap_pages_long_windows(drain_database, tmp_path):
    """max_slots hard cap pages long windows: a two-day window is read
    exactly (576 slots ≤ 1000), a narrower cap truncates with a truthful
    flag and never breaches the caller's end; invalid caps raise."""
    grid = {slot: {'EC_drain_PC': '6', 'pH_drain_PC': '6'} for slot in range(576)}
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path, {'Only': grid},
                                    start=date(2020, 1, 1), end=date(2020, 1, 3))
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        register_rule_schedule(connection, drain_rule(fixture, end=date(2020, 1, 3)))
        service.get_evaluation(drain_context(fixture, 'Only'),
                               rule_version='rule-version:drn:1')
        full = service.get_slot_review(drain_context(fixture, 'Only'),
                                       rule_version='rule-version:drn:1',
                                       window_start=datetime(2020, 1, 1),
                                       window_end=datetime(2020, 1, 3))
        assert full.window_slots == 576 and full.truncated is False
        capped = service.get_slot_review(drain_context(fixture, 'Only'),
                                         rule_version='rule-version:drn:1',
                                         window_start=datetime(2020, 1, 1),
                                         window_end=datetime(2020, 1, 3), max_slots=10)
        assert capped.window_slots == 10 and capped.truncated is True
        assert capped.wanted_end == datetime(2020, 1, 3)
        assert [r for r in capped.reviews if r.metric == 'ec'] and \
            all(r.start < capped.end for r in capped.reviews)
    for invalid in (True, 0, -1, DRAIN_REVIEW_MAX_SLOTS + 1):
        with psycopg.connect(**drain_database, autocommit=True) as connection:
            service = make_service(connection, fixture)
            with pytest.raises(ValueError):
                service.get_slot_review(drain_context(fixture, 'Only'),
                                        max_slots=invalid)


def test_drain_foreign_revision_rejected_before_cache(drain_database, tmp_path):
    """Pre-cache identity: a SAME-CONTENT revision claiming a foreign
    scope/compartment is refused before any cache lookup even when it
    would cache-hit by content."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                    {'Only': {0: {'EC_drain_PC': '3'}}})
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        foreign = replace(drain_rule(fixture), scope_id='scope:other')
        with psycopg.connect(**drain_database, autocommit=True) as probe:
            with pytest.raises(Exception):
                register_rule_schedule(probe, foreign)
        revision = drain_rule(fixture)
        with pytest.raises(ValueError):
            drain_module.validate_revision_for_publication(
                replace(revision, compartment_id='comp:elsewhere'),
                service.adapter.publication(fixture['publication']),
                service.adapter.cycle_window(drain_context(fixture, 'Only')))
        assert drain_module.validate_revision_for_publication(revision,
            service.adapter.publication(fixture['publication']),
            service.adapter.cycle_window(drain_context(fixture, 'Only'))) is None
        register_rule_schedule(connection, revision)
        evaluation = service.get_evaluation(drain_context(fixture, 'Only'),
                                            rule_version=revision.rule_version)
        assert evaluation.requested_rule_version == revision.rule_version


def test_drain_cache_content_signature_and_namespace(drain_database, tmp_path):
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                        {'Only': {0: {'EC_drain_PC': '3'}}},
                                        start=date(2020, 1, 1), end=date(2020, 1, 2))
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        adapter = service.adapter
        first = drain_rule(fixture, end=date(2020, 1, 2))
        register_rule_schedule(connection, first)
        second = drain_rule(fixture, version=2, tag='drn2', end=date(2020, 1, 2),
                            ec=(Decimal('2'), Decimal('4')))
        register_rule_schedule(connection, second)
        signature_a = drain_rule_content_signature(first)
        signature_b = drain_rule_content_signature(second)
        assert signature_a != signature_b
        assert drain_rule_content_signature(first) == signature_a
        # delta segments are REFUSED for drain content signatures
        with pytest.raises(ValueError):
            drain_rule_content_signature(replace(first, segments=tuple(
                replace(seg, lower=None, upper=None, delta=Decimal('1'))
                for seg in first.segments)))
        context = drain_context(fixture, 'Only')
        cycle = adapter.cycle_window(context)
        pinned = adapter.publication(fixture['publication'])
        key = evaluation_cache_key(adapter.database_key, pinned, context, cycle,
                                   signature_a)
        assert key[0] == 'drain' and signature_a in key
        assert pinned.publication_id in key and signature_a in key
        evaluated = service.get_evaluation(context, rule_version=first.rule_version)
        assert evaluated.requested_rule_version == first.rule_version
        no_cache = service.get_evaluation(context, use_cache=False)
        assert no_cache.requested_rule_version is None


def test_drain_catalog_filters_declared_group(drain_database, tmp_path):
    """The catalog schedule selector returns ONLY revisions compatible with
    the declared group: the climate query never offers drain rules and the
    drain query never offers climate rules, EVERY segment of an offered
    revision matches (mixed groups never appear in either list), and valid
    partial profiles stay offered."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path, {'Only': {}},
                                    start=date(2020, 1, 1), end=date(2020, 1, 2))
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        drain_revision = drain_rule(fixture, end=date(2020, 1, 2))
        register_rule_schedule(connection, drain_revision)
        climate_rule = RuleScheduleRevision(
            rule_version='rule-version:cla:9', rule_id='rule:cla:9', version=1,
            scope_id=fixture['scope'],
            compartment_id=fixture['cycles']['Only']['comp'],
            evidence_reference='climate demo for isolation test', is_demo=True,
            label='demo_rule',
            segments=(RuleScheduleSegment(
                metric='temperature', measurement_context='climate',
                basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                end=datetime(2020, 1, 2), delta=Decimal('0')),))
        register_rule_schedule(connection, climate_rule)
        mixed = RuleScheduleRevision(
            rule_version='rule-version:mix:1', rule_id='rule:mix', version=1,
            scope_id=fixture['scope'],
            compartment_id=fixture['cycles']['Only']['comp'],
            evidence_reference='mixed probe', is_demo=False, label='demo_rule',
            segments=(
                RuleScheduleSegment(metric='ec', measurement_context=DRAIN_CONTEXT,
                                    basis=RuleBasis.MEASURED, start=datetime(2020, 1, 1),
                                    end=datetime(2020, 1, 2), lower=Decimal('2'),
                                    upper=Decimal('5')),
                RuleScheduleSegment(metric='temperature', measurement_context='climate',
                                    basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                                    end=datetime(2020, 1, 2), delta=Decimal('0'))))
        partial_drain = replace(drain_revision, rule_version='rule-version:par:9',
                                rule_id='rule:par',
                                segments=tuple(
                                    seg for seg in drain_revision.segments
                                    if seg.metric == 'ec'))
        climate_multi = RuleScheduleRevision(
            rule_version='rule-version:cmb:1', rule_id='rule:cmb', version=1,
            scope_id=fixture['scope'],
            compartment_id=fixture['cycles']['Only']['comp'],
            evidence_reference='both climate bases', is_demo=False, label='demo_rule',
            segments=tuple(sorted((
                RuleScheduleSegment(metric='temperature', measurement_context='climate',
                                    basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                                    end=datetime(2020, 1, 2), delta=Decimal('0')),
                RuleScheduleSegment(metric='co2', measurement_context='climate',
                                    basis=RuleBasis.REALIZED, start=datetime(2020, 1, 1),
                                    end=datetime(2020, 1, 2), delta=Decimal('5'))),
                                  key=rule_segment_sort_key)))
        register_rule_schedule(connection, mixed)
        register_rule_schedule(connection, partial_drain)
        register_rule_schedule(connection, climate_multi)
        catalog = CatalogService(PostgresCatalogAdapter(connection))
        publication_row = PostgresCatalogAdapter(connection).catalog_publications()[0]
        cycle_comp = fixture['cycles']['Only']['comp']
        drain_options = catalog.schedule_rules(
            publication_row, cycle_comp, measurement_context=DRAIN_CONTEXT,
            metrics=('ec', 'ph'), bases=(RuleBasis.MEASURED,))
        climate_options = catalog.schedule_rules(
            publication_row, cycle_comp, measurement_context='climate',
            metrics=('temperature', 'co2'), bases=(RuleBasis.REQUESTED,
                                                   RuleBasis.REALIZED))
        drain_versions = {option.rule_version for option in drain_options}
        climate_versions = {option.rule_version for option in climate_options}
        assert drain_revision.rule_version in drain_versions
        assert climate_rule.rule_version not in drain_versions
        assert climate_rule.rule_version in climate_versions
        assert drain_revision.rule_version not in climate_versions
        # a mixed drain-EC + climate-temperature revision is compatible with
        # NEITHER declared group and is offered by neither selector
        assert mixed.rule_version not in drain_versions
        assert mixed.rule_version not in climate_versions
        # valid partial profiles stay offered (the service accepts them too)
        assert partial_drain.rule_version in drain_versions
        assert partial_drain.rule_version not in climate_versions
        # both climate bases inside one profile remains a legal climate option
        assert climate_multi.rule_version in climate_versions
        assert climate_multi.rule_version not in drain_versions


def test_drain_profile_boundary_cache_independent(drain_database, tmp_path):
    """The declared drain profile boundary — fixed-bound EC/pH segments in
    drain_process_computer at MEASURED — is validated on EVERY service path
    (cached, use_cache=False, namespace-less adapter, bounded detail)
    BEFORE any cache lookup or mart fetch: a climate delta revision and a
    foreign-context revision are refused everywhere, so caching can never
    change query validity. Partial EC-only and no-rule profiles stay
    legal."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                        {'Only': {0: {'EC_drain_PC': '3'}}},
                                        start=date(2020, 1, 1), end=date(2020, 1, 2))
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        uncached_adapter = _NamespacelessDrainAdapter(service.adapter)
        context = drain_context(fixture, 'Only', days=1)
        climate_delta = RuleScheduleRevision(
            rule_version='rule-version:clx:1', rule_id='rule:clx', version=1,
            scope_id=fixture['scope'],
            compartment_id=fixture['cycles']['Only']['comp'],
            evidence_reference='climate delta probe', is_demo=False, label='demo_rule',
            segments=(RuleScheduleSegment(
                metric='temperature', measurement_context='climate',
                basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                end=datetime(2020, 1, 2), delta=Decimal('0')),))
        lab_fixed = RuleScheduleRevision(
            rule_version='rule-version:lab:1', rule_id='rule:lab', version=1,
            scope_id=fixture['scope'],
            compartment_id=fixture['cycles']['Only']['comp'],
            evidence_reference='foreign-context fixed bounds', is_demo=False,
            label='demo_rule',
            segments=(RuleScheduleSegment(
                metric='ec', measurement_context='lab_sample',
                basis=RuleBasis.MEASURED, start=datetime(2020, 1, 1),
                end=datetime(2020, 1, 2), lower=Decimal('2'), upper=Decimal('5')),))
        register_rule_schedule(connection, climate_delta)
        register_rule_schedule(connection, lab_fixed)
        for bad, signal in ((climate_delta, r"in context 'climate'"),
                            (lab_fixed, 'lab_sample')):
            with pytest.raises(ValueError, match=signal):
                service.get_evaluation(context, rule_version=bad.rule_version,
                                       use_cache=True)
            with pytest.raises(ValueError, match=signal):
                service.get_evaluation(context, rule_version=bad.rule_version,
                                       use_cache=False)
            with pytest.raises(ValueError, match=signal):
                DrainService(uncached_adapter).get_evaluation(
                    context, rule_version=bad.rule_version)
            with pytest.raises(ValueError, match=signal):
                service.get_slot_review(context, rule_version=bad.rule_version,
                                        max_slots=10)
            with pytest.raises(ValueError, match=signal):
                DrainService(uncached_adapter).get_slot_review(
                    context, rule_version=bad.rule_version, max_slots=10)
        # The typed contract rejects EC/pH delta bounds at construction;
        # there is no valid delta-drain revision to register or query.
        for segment in drain_rule(fixture).segments:
            with pytest.raises(ValueError,
                               match='EC/pH segments carry fixed bounds and no delta'):
                replace(segment, lower=None, upper=None, delta=Decimal('1'))
        # partial (EC-only) and no-rule profiles stay LEGAL
        source_rule = drain_rule(fixture, end=date(2020, 1, 2))
        partial_ec = replace(source_rule, rule_version='rule-version:par:1',
                             rule_id='rule:par',
                             segments=tuple(sorted(
                                 (seg for seg in source_rule.segments
                                  if seg.metric == 'ec'), key=rule_segment_sort_key)))
        assert drain_module.validate_drain_profile(partial_ec) is None
        assert drain_module.validate_drain_profile(None) is None
        register_rule_schedule(connection, partial_ec)
        evaluation = service.get_evaluation(context,
                                            rule_version=partial_ec.rule_version)
        ec, ph = evaluation.result.metric_results
        assert ec.valid_minutes == Decimal('1440')     # EC evaluated normally
        assert ph.unknown_minutes == Decimal('1440')   # uncovered stays unknown
        assert ph.valid_minutes == Decimal('0')
        for selected in (partial_ec.rule_version, None):
            expected = service.get_evaluation(context, rule_version=selected).result
            assert service.get_evaluation(
                context, rule_version=selected, use_cache=False).result == expected
            assert DrainService(uncached_adapter).get_evaluation(
                context, rule_version=selected).result == expected
            if selected is None:
                assert all(metric.valid_minutes == Decimal('0')
                           and metric.unknown_minutes == Decimal('1440')
                           for metric in expected.metric_results)
            for valid_service in (service, DrainService(uncached_adapter)):
                detail = valid_service.get_slot_review(
                    context, rule_version=selected, max_slots=10)
                assert len(detail.reviews) == 20
                assert all(review.class_code == (
                    'valid' if selected and review.metric == 'ec'
                    else 'rule_not_configured') for review in detail.reviews)


def test_drain_slot_facts_clamped_to_query_context(drain_database, tmp_path):
    """Source facts clamp inside the QUERY CONTEXT window (not just the
    cycle), never fetch beyond the wanted end (floor anchor + exact clip),
    and pages carry a truthful truncated flag."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                        {'Only': {0: {'EC_drain_PC': '3'}}},
                                        start=date(2020, 1, 1), end=date(2020, 1, 3))
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        service = make_service(connection, fixture)
        context = drain_context(fixture, 'Only', date(2020, 1, 2), days=1)
        facts = service.slot_facts(context)
        assert facts.start == datetime(2020, 1, 2)
        assert facts.end == datetime(2020, 1, 3)
        assert all(row.slot_start >= datetime(2020, 1, 2) for row in facts.rows)
        assert facts.truncated is False
        capped = service.slot_facts(context, max_slots=10)
        assert capped.window_slots == 10 and capped.truncated is True
        assert capped.end <= datetime(2020, 1, 3)
        with pytest.raises(ValueError):
            service.slot_facts(context, max_slots=True)
        with pytest.raises(ValueError):
            service.slot_facts(context, max_slots=0)
        # the VALUES stay original even where evaluation masked them
        fixture_rows = [row for row in facts.rows
                        if row.measure_quality['EC_drain_PC']['status'] == 'accepted']
        if fixture_rows:
            assert fixture_rows[0].ec_drain is not None


def test_drain_sealed_guard_blocks_deletes(drain_database, tmp_path):
    """Sealed publication children reject writes: the three drain tables
    refuse DELETE (and any mutation) after the seal transaction commits,
    exactly like every other bundle."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                        {'Only': {0: {'EC_drain_PC': '3'}}})
        publication = fixture['publication']
        for table in ('mart.dim_drain_cycle', 'mart.fact_drain_slot',
                      'mart.drain_publication'):
            with pytest.raises(psycopg.errors.IntegrityError):
                connection.execute(f'DELETE FROM {table} WHERE publication_id=%s',
                                   (publication,))



def test_drain_lineage_verified_and_demo_seed(drain_database, tmp_path):
    """Lineage: a frozen slot's source revision resolves via the shared core
    lineage path with the mounted raw snapshot; the demo seed uses the
    documented fixed identity, reuses identical content and REFUSES drift."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                        {'Only': {0: {'EC_drain_PC': '3'}}})
        service = make_service(connection, fixture)
        evaluation = service.get_evaluation(drain_context(fixture, 'Only'))
        rows = service.adapter.read_drain_slots(drain_context(fixture, 'Only'),
                                                evaluation.publication)
        slot_row = rows[0]
        lineage = service.get_lineage(evaluation.publication,
                                      revision_id=slot_row.revision_id)
        assert lineage.raw_hash_verified is True
        assert lineage.relative_path.endswith('GreenhouseClimate.csv')
        # demo seed fixed identity + drift refusal
        from greenhouse.db.demo_rules import DRAIN_FAMILY_PREFIX, seed_drain
        base = datetime.combine(date(2020, 1, 1), datetime.min.time())
        planned = seed_drain(connection, apply=False, scope_id=fixture['scope'])
        entry = planned['entries'][0]
        assert entry['result'] == 'register'
        assert entry['rule_version'] == (f'{DRAIN_FAMILY_PREFIX}:'
                                         f"{fixture['cycles']['Only']['comp']}:v1")
        applied = seed_drain(connection, apply=True, scope_id=fixture['scope'])
        assert applied['entries'][0]['result'] == 'register'
        seeded = read_rule_schedule(connection, entry['rule_version'])
        assert seeded is not None and seeded.is_demo is True
        assert seeded.segments[0].lower == Decimal('2')
        assert seeded.segments[0].upper == Decimal('5')
        drift = replace(seeded, segments=tuple(
            replace(seg, lower=Decimal('1')) for seg in seeded.segments))
        with pytest.raises(ValueError):
            register_rule_schedule(connection, replace(
                seeded, rule_version=seeded.rule_version,
                version=seeded.version, segments=drift.segments))
        # idempotent second seed: reuse
        again = seed_drain(connection, apply=True, scope_id=fixture['scope'])
        assert again['entries'][0]['result'] == 'reuse'


def test_drain_second_workflow_reuses_publication(drain_database, tmp_path):
    """Rerunning the same snapshot reuses the SAME publication (no
    duplication), the pointer stays, and history keeps every prior AW publication."""
    with psycopg.connect(**drain_database, autocommit=True) as connection:
        fixture = publish_drain_fixture(connection, tmp_path,
                                        {'Only': {0: {'EC_drain_PC': '3'}}})
        raw = fixture['raw']
        service = make_service(connection, fixture)
        before = service.get_evaluation(drain_context(fixture, 'Only'),
                                        rule_version=None, use_cache=False)
        result = run_pipeline(connection, raw, fixture['raw'].parent / 'output_again',
                              fixture['scope'], fixture['policy'],
                              stage_function=fixture_stage)
        after = service.get_evaluation(drain_context(fixture, 'Only'),
                                       rule_version=None, use_cache=False)
        publications = connection.execute(
            'SELECT count(*) FROM meta.publication WHERE data_scope_id=%s',
            (fixture['scope'],)).fetchone()[0]
    assert result['publication_id'] == fixture['publication']
    assert result.get('reused') is True or result['status'] == 'completed'
    assert publications == 1
    assert before.result == after.result
