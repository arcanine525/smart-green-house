"""BR-01 climate bundle: pipeline → mart → slots → service → exports (T15).

Fixture publications run through the REAL pipeline (only the Spark stage is
replaced by a prepared artifact, exactly like the water/harvest harness).
The climate receipt freezes slot values plus per-measure quality for ALL
policy measures; the service combines each metric's OWN actual + basis
setpoint verdicts into one evaluation input (invalid > suspect > missing >
accepted) without cross-metric/basis poisoning; CO2 SP=0 stays numeric 0
(sp0_off_mode); requested and realized are separate evaluations; the exact
end sample adds no duration; rule replay is exact.

Every numeric expectation belongs to a DAY-scaled 5-minute grid and is hand
computed here (the literal 30-minute AC numbers of BR-01 §7 stay the T14
engine fixtures in tests/unit/test_rule_schedules.py).
"""
import csv
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from greenhouse.contracts import (
    QualityStatus, QueryContext, RuleBasis, RuleDeviationDirection, RuleDurationResult,
    RuleScheduleRevision, RuleScheduleSegment, rule_segment_sort_key, from_json, to_json,
)
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.demo_rules import seed_climate
from greenhouse.db.rule_schedule import read_rule_schedule, register_rule_schedule
from greenhouse.queries import ClimateService, episode_key, page_episodes
from greenhouse.app import exports
from greenhouse.ingestion.parser import HEADERS
from test_query_adapter import contracts_database, fixture_stage
from greenhouse.db.pipeline import run_pipeline

def pct(numerator, denominator):
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * Decimal(100) / Decimal(denominator)


EXCEL_EPOCH = date(1899, 12, 30)
CLIMATE_CONTEXT = 'climate' 
CLIMATE_MEASURES = ('Tair', 'CO2air', 't_heat_sp', 't_vent_sp', 'co2_sp',
                    't_heat_vip', 't_ventlee_vip', 'co2_vip')
POLICY_DEFINITION = {
    'missing': 'per_measure',
    'nonnegative': sorted(CLIMATE_MEASURES + ('EC_drain_PC',)),
    'suspect_zero': ['pH_drain_PC'], 'off_mode_zero': ['co2_sp', 'co2_vip']}


def serial_text(day, slot_index):
    """Source label serial on the documented 5-minute grid (288/day)."""
    return f'{(day - EXCEL_EPOCH).days + slot_index / 288:.5f}'


def default_values():
    """In-range defaults under both delta-0 and demo deltas: temperature 22
    vs 20/25 (or VIP 20.5/24.5), CO2 500 vs setpoint 500."""
    return {'Tair': '22', 'CO2air': '500',
            't_heat_sp': '20', 't_vent_sp': '25', 'co2_sp': '500',
            't_heat_vip': '20.5', 't_ventlee_vip': '24.5', 'co2_vip': '500'}


def day_grid(overrides_by_slot=None, days=2, endpoint=False):
    """FULL default grid (every slot present); `endpoint` adds the exact
    close-boundary row of the last cycle day."""
    grid = {}
    for day_offset in range(days):
        for slot_index in range(288):
            grid[day_offset * 288 + slot_index] = (overrides_by_slot or {}).get(
                day_offset * 288 + slot_index, {})
    if endpoint:
        grid[days * 288] = {}
    return grid


def write_climate_csv(path, grid):
    headers = HEADERS['GreenhouseClimate']
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\r\n')
        writer.writerow(headers)
        for absolute_slot in sorted(grid):
            values = dict(default_values(), **grid[absolute_slot])
            day_offset, slot_index = divmod(absolute_slot, 288)
            writer.writerow(
                [serial_text(date(2020, 1, 1) + timedelta(days=day_offset), slot_index)]
                + [values.get(header.strip(), '') for header in headers[1:]])


def publish_climate_fixture(connection, scratch, folders, *, start=date(2020, 1, 1),
                            end=date(2020, 1, 3), definition=POLICY_DEFINITION):
    """One scope; every compartment keeps its OWN cycle over the SAME window
    grid (so timestamps repeat across compartments).
    folders: {source_folder: {absolute_slot: {column: override}}}.
    Returns the fixture identity keys + raw handling info."""
    tag = uuid4().hex
    site, snapshot, scope, policy = (name + ':' + tag
                                     for name in ('site', 'snapshot', 'scope', 'quality'))
    connection.execute("INSERT INTO core.site VALUES (%s,'Fixture','source_naive','fixture')",
                       (site,))
    connection.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",
                       (snapshot, tag * 2))
    connection.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",
                       (scope, snapshot))
    definition_payload = json.dumps(definition, sort_keys=True, separators=(',', ':'))
    connection.execute(
        "INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic climate policy')",
        (policy, start, end, definition_payload,
         hashlib.sha256(definition_payload.encode()).hexdigest()))
    for measure, record_type, unit in [
            ('Tair', 'climate', '°C'), ('CO2air', 'climate', 'ppm'),
            ('t_heat_sp', 'climate', '°C'), ('t_vent_sp', 'climate', '°C'),
            ('co2_sp', 'climate', 'ppm'), ('t_heat_vip', 'climate', '°C'),
            ('t_ventlee_vip', 'climate', '°C'), ('co2_vip', 'climate', 'ppm'),
            ('EC_drain_PC', 'drain', 'dS/m'), ('pH_drain_PC', 'drain', '1')]:
        connection.execute(
            "INSERT INTO config.measure_definition VALUES (%s,%s,%s,'fixture') "
            "ON CONFLICT (measure_code) DO NOTHING",
            (measure, record_type, unit))
    cycles = {}
    files = []
    raw = scratch / tag / 'raw'
    raw.mkdir(parents=True)
    for folder_index, folder in enumerate(sorted(folders)):
        comp, cycle = f'comp:{tag}{folder_index}', f'cycle:{tag}{folder_index}'
        team, crop = f'team:{tag}{folder_index}', f'crop:{tag}{folder_index}'
        connection.execute("INSERT INTO core.compartment VALUES (%s,%s,%s,'fixture')",
                           (comp, site, folder))
        connection.execute("INSERT INTO core.team VALUES (%s,%s,false,'fixture')", (team, folder))
        connection.execute("INSERT INTO core.crop VALUES (%s,'tomato',NULL,'fixture')", (crop,))
        connection.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,%s,%s,%s,'fixture')",
                           (cycle, comp, team, crop, start, end))
        connection.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,%s,%s,%s,%s,'fixture')",
                           (snapshot, folder, comp, team, cycle))
        grid = day_grid(folders[folder], days=(end - start).days)
        folder_dir = raw / folder
        folder_dir.mkdir(parents=True, exist_ok=True)
        climate_csv = folder_dir / 'GreenhouseClimate.csv'
        write_climate_csv(climate_csv, grid)
        files.append({'path': f'{folder}/GreenhouseClimate.csv',
                      'sha256': hashlib.sha256(climate_csv.read_bytes()).hexdigest(),
                      'csv': {'data_rows': len(grid)}})
        cycles[folder] = {'comp': comp, 'cycle': cycle}
    (raw / 'manifest.json').write_text(json.dumps(
        {'snapshot_id': snapshot, 'archive': {'sha256': tag * 2}, 'files': files}))
    result = run_pipeline(connection, raw, scratch / tag / 'output', scope, policy,
                          stage_function=fixture_stage)
    return {
        'scope': scope, 'snapshot': snapshot, 'policy': policy, 'raw': raw,
        'publication': result['publication_id'], 'cycles': cycles,
        'days': (end - start).days,
    }


def replace_row(frozen):
    """Synthetic frozen map copy for pure build_slots mapping tests."""
    return {code: dict(value) for code, value in frozen.items()}


def _slot_rows(frozen):
    from greenhouse.db.climate_adapter import ClimateSlotRow
    return [ClimateSlotRow(
        slot_start=datetime(2020, 1, 1), revision_id='r', logical_key='k',
        quality_policy_version='p', correction_id=None, correction_version=None,
        tair=Decimal('22'), co2air=Decimal('400'), t_heat_sp=Decimal('20'),
        t_vent_sp=Decimal('25'), co2_sp=Decimal('800'), t_heat_vip=Decimal('20.5'),
        t_ventlee_vip=Decimal('24.5'), co2_vip=Decimal('500'), measure_quality=frozen)]


def make_service(connection, fixture=None):
    raw_snapshots = {fixture['snapshot']: fixture['raw']} if fixture else None
    return ClimateService(PostgresClimateQueryAdapter(connection, raw_snapshots=raw_snapshots))


def climate_context(fixture, folder, start=None, days=1):
    cycle = fixture['cycles'][folder]
    start = start or date(2020, 1, 1)
    return QueryContext(fixture['scope'], fixture['publication'], fixture['snapshot'],
                        cycle['cycle'], (cycle['comp'],), start, start + timedelta(days=days))


def evaluation_for(connection, fixture, folder, *, basis=RuleBasis.REQUESTED,
                   rule_version=None, start=None, days=1):
    return make_service(connection, fixture).get_evaluation(
        climate_context(fixture, folder, start, days=days),
        basis=basis, rule_version=rule_version)


def temperature_rule(fixture, *, delta='0', version=1, tag='cla', end=date(2020, 1, 2)):
    return RuleScheduleRevision(
        rule_version=f'rule-version:{tag}:{version}', rule_id=f'rule:{tag}', version=version,
        scope_id=fixture['scope'], compartment_id=fixture['cycles']['Only']['comp'],
        evidence_reference='fixture bounds', is_demo=True, label='demo_rule',
        segments=(RuleScheduleSegment(metric='temperature', measurement_context=CLIMATE_CONTEXT,
                                      basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                                      end=datetime.combine(end, datetime.min.time()),
                                      delta=Decimal(delta)),))


def both_basis_temperature_rule(fixture, *, delta='0', co2=False, tag=None):
    """One revision carrying temperature (optionally CO2) on BOTH climate
    bases so each basis evaluation loads the same rule identity exactly."""
    tag = tag or uuid4().hex
    metrics = ['temperature'] + (['co2'] if co2 else [])
    segments = [
        RuleScheduleSegment(metric=metric, measurement_context=CLIMATE_CONTEXT,
                            basis=basis, start=datetime(2020, 1, 1),
                            end=datetime(2020, 1, 2),
                            delta=Decimal(delta) if metric == 'temperature' else Decimal('0'))
        for metric in metrics
        for basis in (RuleBasis.REQUESTED, RuleBasis.REALIZED)]
    return RuleScheduleRevision(
        rule_version=f'rule-version:{tag}',
        rule_id=f'rule:{tag}', version=1,
        scope_id=fixture['scope'], compartment_id=fixture['cycles']['Only']['comp'],
        evidence_reference='two-basis fixture rule', is_demo=True, label='demo_rule',
        segments=tuple(sorted(segments, key=rule_segment_sort_key)))


def temperature_co2_rule(fixture, *, temperature_delta='0', co2_delta='0', tag='cla2',
                         end=date(2020, 1, 2), version=1):
    """delta is applied on BOTH setpoints (temperature/bounds, CO2 bounds)."""
    return RuleScheduleRevision(
        rule_version=f'rule-version:{tag}', rule_id=f'rule:{tag}', version=version,
        scope_id=fixture['scope'], compartment_id=fixture['cycles']['Only']['comp'],
        evidence_reference='fixture bounds', is_demo=True, label='demo_rule',
        segments=tuple(sorted((
            RuleScheduleSegment(metric='temperature', measurement_context=CLIMATE_CONTEXT,
                                basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                                end=datetime.combine(end, datetime.min.time()),
                                delta=Decimal(temperature_delta)),
            RuleScheduleSegment(metric='co2', measurement_context=CLIMATE_CONTEXT,
                                basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                                end=datetime.combine(end, datetime.min.time()),
                                delta=Decimal(co2_delta))), key=rule_segment_sort_key)))


@pytest.fixture()
def climate_database(contracts_database):
    """Function-scoped connection: one failing fixture can never poison
    later tests through an aborted transaction (module DB stays dropped)."""
    with psycopg.connect(**contracts_database, autocommit=True) as connection:
        yield connection


# ------------------------------------------------------------- mart facts

def test_two_compartments_share_timestamps_receipt_counts_exact(climate_database, tmp_path):
    """Same 5-minute labels across two cycles: materialized facts stay
    (publication, cycle, slot)-unique; the receipt reads the in-flight CTE
    (never the target table) and equals the exact frozen slots."""
    fixture = publish_climate_fixture(climate_database, tmp_path, {
        'SeatOne': {0: {'Tair': '19'}},
        'SeatTwo': {0: {'Tair': '21', 'CO2air': '800'}},
    }, start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        receipt = connection.execute(
            "SELECT bundle_version,slot_count,cycle_count FROM mart.climate_publication "
            "WHERE publication_id=%s", (fixture['publication'],)).fetchone()
        slots = connection.execute(
            "SELECT count(DISTINCT (cycle_id,slot_start)),count(*) "
            "FROM mart.fact_climate_slot WHERE publication_id=%s",
            (fixture['publication'],)).fetchone()
    assert receipt == ('climate-mart-v1', 2 * 288, 2)
    assert slots == (2 * 288, 2 * 288)


def test_measure_quality_freeze_all_eight(climate_database, tmp_path):
    """Per-measure triplets frozen for the policy's climate measures —
    actual AND setpoints; blanks carry missing exactly (quality never dies)."""
    fixture = publish_climate_fixture(climate_database, tmp_path,
                                      {'Only': {3: {'Tair': '', 'co2_sp': ''}}},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        row = connection.execute(
            "SELECT measure_quality FROM mart.fact_climate_slot WHERE publication_id=%s "
            "AND slot_start=%s", (fixture['publication'],
                                  datetime(2020, 1, 1) + timedelta(minutes=15))).fetchone()
    frozen = row[0]
    assert set(frozen) == set(CLIMATE_MEASURES)
    assert frozen['Tair']['status'] == 'missing'
    assert frozen['co2_sp']['status'] == 'missing'
    assert frozen['t_heat_sp']['status'] == 'accepted'
    assert frozen['t_heat_sp']['applicability'] == 'applicable'


# ------------------------------------------------------------ service ACs

def test_ac01_day_grid_out_valid_unknown_episodes(climate_database, tmp_path):
    grid = {0: {'Tair': '19'}, 1: {'Tair': '21'}, 2: {'Tair': '26'},
            3: {'Tair': '26'}, 4: {'Tair': ''}, 5: {'Tair': '24'}}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_rule(fixture, delta='0'))
        result = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:cla:1')
    temperature = result.result.metric_results[0]
    assert temperature.valid_minutes == Decimal('1435')   # 1440 − one missing slot
    assert temperature.out_minutes == Decimal('15')
    assert temperature.out_low_minutes == Decimal('5')
    assert temperature.out_high_minutes == Decimal('10')
    assert temperature.unknown_minutes == Decimal('5')
    assert temperature.out_hours == Decimal('0.25')
    assert temperature.out_percent == pct(15, 1435)
    assert temperature.coverage_pct == pct(1435, 1440)
    episode_minutes = [episode.original_minutes for episode in result.result.episodes]
    assert episode_minutes == [Decimal('5'), Decimal('10')]
    joint = result.result.joint_results[0]
    assert joint.jointly_valid_minutes == Decimal('0')    # co2 unconfigured → joint 0


def test_ac02_boundary_equality_inside(climate_database, tmp_path):
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': {7: {'Tair': '20'}}},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_rule(fixture, delta='0', tag='ac02'))
        result = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:ac02:1')
    temperature = result.result.metric_results[0]
    assert temperature.out_minutes == Decimal('0')
    assert temperature.valid_minutes == Decimal('1440')
    assert result.result.episodes == ()


def test_ac03_baseline_missing_setpoint_requested_unknown_realized_valid(
        climate_database, tmp_path):
    grid = {slot: {'t_heat_sp': '', 't_vent_sp': '', 'co2_sp': ''} for slot in range(288)}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        both = both_basis_temperature_rule(fixture)
        register_rule_schedule(connection, both)
        requested = evaluation_for(connection, fixture, 'Only', basis=RuleBasis.REQUESTED,
                                   rule_version=both.rule_version)
        realized = evaluation_for(connection, fixture, 'Only', basis=RuleBasis.REALIZED,
                                  rule_version=both.rule_version)
    temperature = requested.result.metric_results[0]
    assert temperature.valid_minutes == Decimal('0')
    assert temperature.unknown_minutes == Decimal('1440')
    assert [(share.reason_code, str(share.minutes))
            for share in temperature.unknown_reasons] == [('missing_input', '1440')]
    assert temperature.out_hours is None and temperature.out_percent is None
    realized_temperature = realized.result.metric_results[0]
    assert realized_temperature.valid_minutes == Decimal('1440')
    assert realized_temperature.out_minutes == Decimal('0')


def test_ac04_joint_or_not_metric_sum(climate_database, tmp_path):
    grid = {0: {'Tair': '26'}, 1: {'Tair': '26', 'CO2air': '900'}}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_co2_rule(
            fixture, temperature_delta='0', co2_delta='0', tag='ac04'))
        result = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:ac04')
    temperature, co2 = result.result.metric_results
    assert temperature.out_minutes == Decimal('10') and co2.out_minutes == Decimal('5')
    joint = result.result.joint_results[0]
    assert joint.jointly_valid_minutes == Decimal('1440')  # both metrics known all day
    assert joint.joint_out_minutes == Decimal('10')        # union: 10, never 15
    assert joint.joint_out_percent == pct(10, 1440)


def test_ac05_gap_breaks_episode(climate_database, tmp_path):
    grid = {0: {'Tair': '26'}, 1: {'Tair': ''}, 2: {'Tair': '26'}}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_rule(fixture, delta='0', tag='ac05'))
        result = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:ac05:1')
    temperature = result.result.metric_results[0]
    assert temperature.out_minutes == Decimal('10')
    assert [episode.original_minutes for episode in result.result.episodes] == [
        Decimal('5'), Decimal('5')]


def test_ac06_no_usable_values_null_kpis(climate_database, tmp_path):
    grid = {slot: {'Tair': '', 'CO2air': '', 't_heat_sp': '', 't_vent_sp': '',
                   'co2_sp': '', 't_heat_vip': '', 't_ventlee_vip': '', 'co2_vip': ''}
            for slot in range(288)}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        both = both_basis_temperature_rule(fixture, co2=True)
        register_rule_schedule(connection, both)
        for basis in (RuleBasis.REQUESTED, RuleBasis.REALIZED):
            evaluation = evaluation_for(connection, fixture, 'Only', basis=basis,
                                        rule_version=both.rule_version)
            for report in evaluation.result.metric_results:
                assert report.valid_minutes == Decimal('0')
                assert report.out_hours is None and report.out_percent is None
                assert report.coverage_pct == Decimal('0')
                assert [share.reason_code for share in report.unknown_reasons] == ['missing_input']
            joint = evaluation.result.joint_results[0]
            assert joint.jointly_valid_minutes == Decimal('0')
            assert joint.joint_out_percent is None and joint.joint_coverage_pct == Decimal('0')


def test_ac07_exact_end_sample_semantics(climate_database, tmp_path):
    """Rows outside [cycle start, cycle end) NEVER add duration: the read
    stays half-open, expected = span exactly, never a phantom extra 5-min
    slot beyond the cycle end. The AGC close-boundary sample (serial 43981)
    is exercised in the agc mode runner against the real window."""
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': {}},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        mart_bounds = connection.execute(
            "SELECT min(slot_start),count(*) FROM mart.fact_climate_slot "
            "WHERE publication_id=%s", (fixture['publication'],)).fetchone()
        register_rule_schedule(connection, temperature_rule(fixture, delta='0', tag='ac07'))
        evaluation = evaluation_for(connection, fixture, 'Only', basis=RuleBasis.REQUESTED,
                                    rule_version='rule-version:ac07:1')
        reviewed = make_service(connection, fixture).get_slot_review(
            climate_context(fixture, 'Only'), basis=RuleBasis.REQUESTED)
    assert mart_bounds[0] == datetime(2020, 1, 1)
    assert mart_bounds[1] == 288
    temperature = evaluation.result.metric_results[0]
    assert temperature.expected_minutes == Decimal('1440')  # never 1445
    assert temperature.unknown_minutes == Decimal('0')
    assert temperature.valid_minutes == Decimal('1440')
    assert max(review.start for review in reviewed) < datetime(2020, 1, 2)


def test_ac08_rule_version_replay_exact(climate_database, tmp_path):
    grid = {0: {'Tair': '19.5'}}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_rule(fixture, delta='0', version=1,
                                                            tag='ac08'))
        register_rule_schedule(connection, temperature_rule(fixture, delta='1', version=2,
                                                            tag='ac08'))
        with_v1 = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:ac08:1')
        with_v2 = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:ac08:2')
        replayed = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:ac08:1')
        publications = connection.execute(
            "SELECT count(*) FROM meta.publication WHERE data_scope_id=%s",
            (fixture['scope'],)).fetchone()[0]
    assert with_v1.result == replayed.result                     # exact replay by version
    assert with_v1.result != with_v2.result               # changed rule version recalculates
    assert with_v1.result.metric_results[0].out_minutes == Decimal('5')   # 19.5 under delta 0
    assert with_v2.result.metric_results[0].out_minutes == Decimal('0')   # inside [19,21]
    assert publications == 1                              # evaluation never publishes


def test_ac09_episode_before_window_keeps_original(climate_database, tmp_path):
    grid = {286: {'Tair': '26'}, 287: {'Tair': '26'},
            288: {'Tair': '26'}, 289: {'Tair': '26'}}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 3))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_co2_rule(
            fixture, temperature_delta='0', co2_delta='0', tag='ac09', end=date(2020, 1, 3)))
        service = make_service(connection, fixture)
        evaluation = service.get_evaluation(
            climate_context(fixture, 'Only', start=date(2020, 1, 2)),
            basis=RuleBasis.REQUESTED, rule_version='rule-version:ac09')
    episode = evaluation.result.episodes[0]
    assert episode.original_start == datetime(2020, 1, 1, 23, 50)
    assert episode.original_end == datetime(2020, 1, 2, 0, 10)
    assert episode.original_minutes == Decimal('20')
    assert episode.clipped_start == datetime(2020, 1, 2)
    assert episode.clipped_end == datetime(2020, 1, 2, 0, 10)
    assert episode.clipped_minutes == Decimal('10')


def test_ac10_off_mode_and_inverted_bounds_unknown(climate_database, tmp_path):
    grid = {slot: {'co2_sp': '0'} for slot in range(3)}
    grid[3] = {'t_heat_sp': '30', 't_vent_sp': '10'}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_co2_rule(
            fixture, temperature_delta='0', co2_delta='50', tag='ac10'))
        result = evaluation_for(connection, fixture, 'Only', rule_version='rule-version:ac10')
        temperature, co2 = result.result.metric_results
        assert [(share.reason_code, str(share.minutes))
                for share in temperature.unknown_reasons] == [('inverted_bounds', '5')]
        assert temperature.out_minutes == Decimal('0')
        assert [(share.reason_code, str(share.minutes))
                for share in co2.unknown_reasons] == [('sp0_off_mode', '15')]
        assert co2.out_minutes == Decimal('0')
        zero_row = connection.execute(
            "SELECT measure_quality FROM mart.fact_climate_slot WHERE publication_id=%s "
            "AND slot_start=%s", (fixture['publication'], datetime(2020, 1, 1))).fetchone()
        reviews = make_service(connection, fixture).get_slot_review(
            climate_context(fixture, 'Only'), basis=RuleBasis.REQUESTED,
            rule_version='rule-version:ac10')
        assert zero_row[0]['co2_sp']['status'] == 'accepted'
        assert zero_row[0]['co2_sp']['applicability'] == 'unknown'
        assert zero_row[0]['co2_sp']['reason'] == 'off_mode_unverified'
        co2_reviews = [row for row in reviews
                       if row.metric == 'co2'][:3]
        assert [row.class_code for row in co2_reviews] == ['sp0_off_mode'] * 3
        assert [row.lower for row in co2_reviews] == [None, None, None]
    # accepted + unknown-applicability NONZERO SP stays masked unknown (pure build_slots test)
    synthetic_frozen = {'Tair': {'status': 'accepted', 'applicability': 'applicable',
                                 'reason': None},
                        'CO2air': {'status': 'accepted', 'applicability': 'applicable',
                                   'reason': None},
                        'co2_sp': {'status': 'accepted', 'applicability': 'unknown',
                                   'reason': ''},
                        't_heat_sp': {'status': 'accepted', 'applicability': 'applicable',
                                      'reason': None},
                        't_vent_sp': {'status': 'accepted', 'applicability': 'applicable',
                                      'reason': None}}
    from greenhouse.db.climate_adapter import ClimateSlotRow
    from greenhouse.queries.climate import _verdict, build_slots
    synthetic_rows = [ClimateSlotRow(
        slot_start=datetime(2020, 1, 1), revision_id='r', logical_key='k',
        quality_policy_version='p', correction_id=None, correction_version=None,
        tair=Decimal('22'), co2air=Decimal('400'), t_heat_sp=Decimal('20'),
        t_vent_sp=Decimal('25'), co2_sp=Decimal('800'), t_heat_vip=Decimal('20.5'),
        t_ventlee_vip=Decimal('24.5'), co2_vip=Decimal('500'),
        measure_quality=synthetic_frozen)]
    slots = build_slots(synthetic_rows, basis=RuleBasis.REQUESTED)
    co2_slot = slots[1]
    assert co2_slot.setpoint is None          # non-zero unknown app: masked, not validated
    assert co2_slot.quality == QualityStatus.ACCEPTED  # statuses all accepted
    from greenhouse.queries.climate import evaluate_rule_schedule
    verdict = _verdict({'status': 'accepted', 'applicability': 'unknown'}, Decimal('800'))
    assert verdict is False
    assert _verdict({'status': 'accepted', 'applicability': 'unknown'}, Decimal('0')) is True


# ----------------------------------------------------- demo + detail + export

def test_demo_seed_climate_registers_fixed_identity(climate_database, tmp_path):
    fixture = publish_climate_fixture(climate_database, tmp_path, {'SeatOne': {}},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        planned = seed_climate(connection, apply=False, scope_id=fixture['scope'])
        applied = seed_climate(connection, apply=True, scope_id=fixture['scope'])
        again = seed_climate(connection, apply=True, scope_id=fixture['scope'])
        stored = read_rule_schedule(connection, planned['entries'][0]['rule_version'])
        result = evaluation_for(connection, fixture, 'SeatOne',
                                rule_version=planned['entries'][0]['rule_version'])
    assert all(entry['result'] == 'register' and entry['version'] == 1
               for entry in planned['entries'])
    assert all(entry['result'] == 'register' for entry in applied['entries'])
    assert all(entry['result'] == 'reuse' for entry in again['entries'])
    assert stored is not None and stored.is_demo and stored.label == 'demo_rule'
    keys = {(segment.metric, segment.basis) for segment in stored.segments}
    assert keys == {('temperature', RuleBasis.REQUESTED), ('co2', RuleBasis.REQUESTED),
                    ('temperature', RuleBasis.REALIZED), ('co2', RuleBasis.REALIZED)}
    for report in result.result.metric_results:
        assert report.rule_version == planned['entries'][0]['rule_version']


def test_bounded_slot_review_and_lineage_identity(climate_database, tmp_path):
    grid = {0: {'Tair': '19'}, 1: {'Tair': '26'}}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_rule(fixture, delta='0', tag='detail'))
        service = make_service(connection, fixture)
        context = climate_context(fixture, 'Only')
        rows = service.get_slot_review(
            context, basis=RuleBasis.REQUESTED, rule_version='rule-version:detail:1',
            window_start=datetime(2020, 1, 1), window_end=datetime(2020, 1, 1, 0, 20))
        facts = service.slot_facts(context, window_start=datetime(2020, 1, 1),
                                   window_end=datetime(2020, 1, 2))
    temperature = [row for row in rows if row.metric == 'temperature']
    assert [row.class_code for row in temperature[:2]] == ['out_low', 'out_high']
    assert [row.deviation for row in temperature[:2]] == [Decimal(1), Decimal(1)]
    assert all(row.basis == RuleBasis.REQUESTED for row in rows)
    assert len(rows) == 8 and len(facts) == 288   # two metrics, one slot row each time
    assert all(facts[0].logical_key for _ in facts)
    # Source facts (T15 export preflight): enriched reviews carry the ORIGINAL
    # frozen setpoints + per-measure quality note even where the evaluation
    # input was masked/uncertain — a blank field here would be the exact bug
    # main caught in the downloaded detail CSV.
    freeze = facts[0].measure_quality
    expected_lower = {'t_heat_sp': Decimal('20'), 't_heat_vip': Decimal('20.5')}
    for slot_review in temperature[:2]:
        assert slot_review.source_lower == expected_lower['t_heat_sp']
        assert slot_review.source_upper == Decimal('25')
        assert slot_review.source_setpoint is None
        assert slot_review.source_quality \
            and 'Tair accepted/' in slot_review.source_quality \
            and ('t_heat_sp' in slot_review.source_quality)
    co2_row = next(row for row in rows if row.metric == 'co2')
    assert co2_row.source_setpoint == Decimal('500')
    assert co2_row.source_quality and 'co2_sp ' in co2_row.source_quality


def test_exports_parity_with_service_dto(climate_database, tmp_path):
    grid = {0: {'Tair': '19'}, 1: {'Tair': '26'}}
    fixture = publish_climate_fixture(climate_database, tmp_path, {'Only': grid},
                                      start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        register_rule_schedule(connection, temperature_rule(fixture, delta='0', tag='export'))
        evaluation = evaluation_for(connection, fixture, 'Only',
                                    rule_version='rule-version:export:1')
        episode = evaluation.result.episodes[0]
    decoded = from_json(RuleDurationResult, to_json(evaluation.result))
    assert decoded == evaluation.result
    # T15 result JSON: the FULL typed ClimateEvaluation envelope (engine
    # result unchanged inside + selected-rule provenance verbatim; decode
    # with from_json(ClimateEvaluation, ...)).
    from greenhouse.contracts import ClimateEvaluation
    envelope = from_json(ClimateEvaluation, exports.climate_result_json(evaluation))
    assert envelope.result == evaluation.result
    assert envelope.publication == evaluation.publication
    assert envelope.requested_rule_version == evaluation.requested_rule_version
    assert envelope.rule_id == evaluation.rule_id
    assert envelope.rule_label == evaluation.rule_label == 'demo_rule'
    assert envelope.rule_is_demo is True
    assert envelope.rule_evidence_reference == evaluation.rule_evidence_reference
    temperature_row, _ = exports.climate_summary_rows(evaluation)
    assert temperature_row['out_minutes'] == Decimal('10')
    assert temperature_row['out_percent'] == pct(10, 1440)
    assert episode.original_minutes == Decimal('10')   # low then high contiguous same run
    assert episode.directions == (
        RuleDeviationDirection.HIGH, RuleDeviationDirection.LOW)
    page, total = page_episodes(evaluation.result, metric='temperature', min_minutes=None)
    assert total == 1
    assert episode_key(page[0]) == episode_key(episode)


# ------------------------------------------------ bounded evaluation cache (T15) --
# Main decision after the measured AGC smoke: a small bounded (2-entry)
# LRU on get_evaluation. Keys carry the non-secret endpoint namespace
# (host, port, dbname, user) + full publication/version identity + clamped
# cycle window + basis + RULE CONTENT signature; hits only after pin/context
# validation; namespace-less adapters disable the cache entirely.


class _NamespaceLessAdapter:
    """Cache-Protocol fake that delegates everything but exposes NO
    database_key — per the main decision the cache must then DISABLE,
    never share an anonymous key across databases."""

    def __init__(self, real):
        self.real = real

    def pin_publication(self, scope_id):
        return self.real.pin_publication(scope_id)

    def publication(self, publication_id):
        return self.real.publication(publication_id)

    def cycle_window(self, context):
        return self.real.cycle_window(context)

    def read_climate_slots(self, context, publication, *, window_start=None,
                           window_end=None):
        return self.real.read_climate_slots(context, publication,
                                            window_start=window_start,
                                            window_end=window_end)

    def schedule(self, rule_version):
        return self.real.schedule(rule_version)

    def read_lineage(self, publication, *, revision_id=None, record_key=None):
        return self.real.read_lineage(publication, revision_id=revision_id,
                                      record_key=record_key)


def _counting_adapter(connection, fixture):
    adapter = PostgresClimateQueryAdapter(connection, raw_snapshots={
        fixture['snapshot']: fixture['raw']})
    reads = []
    original = adapter.read_climate_slots
    boundaries = []

    def counted(context, publication, **kwargs):
        reads.append(kwargs.get('window_start'))
        return original(context, publication, **kwargs)

    adapter.read_climate_slots = counted
    return adapter, reads, boundaries


def test_bounded_evaluation_cache_hit_invalidation_and_eviction(
        climate_database, tmp_path):
    fixture = publish_climate_fixture(
        climate_database, tmp_path, {'Only': {0: {'Tair': '19'}}},
        start=date(2020, 1, 1), end=date(2020, 1, 4))
    with climate_database as connection:
        from greenhouse.queries.climate import (CACHE_MAX_ENTRIES, _CACHE,
                                                _CACHE_ORDER)
        rule = temperature_rule(fixture, tag='cacheA', end=date(2020, 1, 4))
        register_rule_schedule(connection, rule)
        adapter, reads, _ = _counting_adapter(connection, fixture)
        service = ClimateService(adapter)
        day = lambda index: climate_context(
            fixture, 'Only', date(2020, 1, 1) + timedelta(days=index), days=1)
        first = service.get_evaluation(day(0), basis=RuleBasis.REQUESTED,
                                       rule_version=rule.rule_version)
        assert len(reads) == 1                                # first: uncached MISS
        second = service.get_evaluation(day(0), basis=RuleBasis.REQUESTED,
                                        rule_version=rule.rule_version)
        assert len(reads) == 1 and second.result == first.result   # warm cache HIT
        service.get_evaluation(day(1), basis=RuleBasis.REQUESTED,
                               rule_version=rule.rule_version)
        assert len(reads) == 2                                # new window B: MISS [A,B]
        service.get_evaluation(day(0), basis=RuleBasis.REQUESTED,
                               rule_version=rule.rule_version)
        assert len(reads) == 2                                # hit A refreshes -> [B,A]
        service.get_evaluation(day(2), basis=RuleBasis.REQUESTED,
                               rule_version=rule.rule_version)
        assert len(reads) == 3                                # C evicts B (LRU, not A)
        service.get_evaluation(day(0), basis=RuleBasis.REQUESTED,
                               rule_version=rule.rule_version)
        assert len(reads) == 3                                # hit A still cached
        service.get_evaluation(day(1), basis=RuleBasis.REQUESTED,
                               rule_version=rule.rule_version)
        assert len(reads) == 4                                # B recomputes (was evicted)
        assert len(_CACHE) <= CACHE_MAX_ENTRIES
        # basis change -> always a different key even under the SAME revision
        # identity (the realized window may legally stay rule_not_configured
        # here — the cache test only proves MISS-by-key, not threshold math)
        both = both_basis_temperature_rule(fixture)
        register_rule_schedule(connection, both)
        service.get_evaluation(day(2), basis=RuleBasis.REALIZED,
                               rule_version=both.rule_version)
        assert len(reads) == 5
        # rule change -> different key (content signature), never a stale hit
        other = temperature_rule(fixture, delta='1', version=2, tag='cacheC',
                                 end=date(2020, 1, 4))
        register_rule_schedule(connection, other)
        service.get_evaluation(day(2), basis=RuleBasis.REQUESTED,
                               rule_version=other.rule_version)
        assert len(reads) == 6
        # explicit bypass: same key as before still recomputes
        service.get_evaluation(day(0), basis=RuleBasis.REQUESTED,
                               rule_version=rule.rule_version, use_cache=False)
        assert len(reads) == 7
        # foreign context: raises exactly as before, cache never serves/holds it
        foreign = QueryContext(fixture['scope'], fixture['publication'],
                               fixture['snapshot'], 'absent-cycle',
                               (fixture['cycles']['Only']['comp'],),
                               date(2020, 1, 1), date(2020, 1, 2))
        with pytest.raises(ValueError):
            service.get_evaluation(foreign, basis=RuleBasis.REQUESTED,
                                   rule_version=rule.rule_version)
        # cache state UNCHANGED by the failure: no serve, no store; day0 itself
        # was already evicted by the 2-entry LRU above, so this call re-reads
        assert service.get_evaluation(
            day(0), basis=RuleBasis.REQUESTED,
            rule_version=rule.rule_version).result is not None
        assert len(reads) == 8


def test_evaluation_cache_disables_without_namespace(climate_database, tmp_path):
    fixture = publish_climate_fixture(
        climate_database, tmp_path, {'Only': {0: {'Tair': '19'}}},
        start=date(2020, 1, 1), end=date(2020, 1, 2))
    with climate_database as connection:
        from greenhouse.queries.climate import evaluation_cache_key, rule_content_signature
        rule = temperature_rule(fixture, tag='cacheNS', end=date(2020, 1, 2))
        register_rule_schedule(connection, rule)
        adapter, reads, _ = _counting_adapter(connection, fixture)
        wrapped = ClimateService(_NamespaceLessAdapter(adapter))
        context = climate_context(fixture, 'Only')
        for _ in range(3):
            wrapped.get_evaluation(context, basis=RuleBasis.REQUESTED,
                                   rule_version=rule.rule_version)
        assert len(reads) == 3          # namespace-less: EVERY call recomputes
        assert rule_content_signature(None) is None
        first = rule_content_signature(rule)
        variant = replace(rule, segments=(
            RuleScheduleSegment(metric='temperature', measurement_context=CLIMATE_CONTEXT,
                                basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                                end=datetime(2020, 1, 2), delta=Decimal('1')),))
        # same content -> same signature; changed tolerance -> changed signature
        assert rule_content_signature(rule) == first
        assert rule_content_signature(variant) != first
        # the namespace is the NON-SECRET endpoint tuple (host, port, dbname,
        # user); no password/DSN ever enters keys/logs
        namespace = adapter.database_key
        assert namespace and len(namespace) == 4
        assert all(isinstance(part, str) for part in namespace)
        assert 'password' not in str(namespace).lower()
        # namespace partitioning is meaningful: identical endpoint tuples map
        # to ONE key (no per-adapter-instance isolation inside one database),
        # a different endpoint tuple maps to a DIFFERENT key (no cross-server
        # aliasing); the namespace is exactly the first element of every key.
        from greenhouse.queries.climate import evaluation_cache_key
        same = ClimateService(PostgresClimateQueryAdapter(connection, raw_snapshots={
            fixture['snapshot']: fixture['raw']}))
        assert same.adapter.database_key == adapter.database_key
        pinned = same.adapter.publication(fixture['publication'])
        cycle = same.adapter.cycle_window(context)
        signature = rule_content_signature(rule)
        key_here = evaluation_cache_key(adapter.database_key, pinned, context, cycle,
                                        RuleBasis.REQUESTED, signature)
        key_other = evaluation_cache_key(('other-host', namespace[1], namespace[2],
                                          namespace[3]), pinned, context, cycle,
                                         RuleBasis.REQUESTED, signature)
        assert key_here[0] == namespace          # namespace is the first key element
        assert key_here != key_other             # no cross-server aliasing
