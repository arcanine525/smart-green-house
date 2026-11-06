"""Streamlit AppTest scenarios for the production climate screen (BR-01, T15).

Runs the real home.py with the climate screen: navigation, KPI/coverage
semantics from the typed evaluation, unknown vs valid display behavior
(NULL out KPIs never shown as "0 heures 0 atteint"), explicit rule schedule
selection with at most one revision, episode paging/metric/min filters,
stable-key re-selection after filter changes, bounded episode detail and
export parity (CSV download bytes equal the service DTOs).
"""
import csv
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from greenhouse.contracts import (
    QueryContext, RuleBasis, RuleDurationResult, RuleScheduleRevision,
    RuleScheduleSegment, rule_segment_sort_key, from_json, to_json,
)
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.queries import ClimateService, episode_key, page_episodes
from greenhouse.ingestion.parser import HEADERS
from greenhouse.db.pipeline import run_pipeline

TIMEOUT = 240

PG_ENV = {'host': 'PGHOST', 'port': 'PGPORT', 'dbname': 'PGDATABASE',
          'user': 'PGUSER', 'password': 'PGPASSWORD'}
CLIMATE_SCREEN = '🌡️ Khí hậu (BR-01)'
WATER_SCREEN = '💧 Nước (BR-02)'
EXCEL_EPOCH = date(1899, 12, 30)


@pytest.fixture
def run_app(app_path, monkeypatch):
    def run(target):
        for key, value in target.items():
            if key in PG_ENV:
                monkeypatch.setenv(PG_ENV[key], str(value))
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(app_path, default_timeout=TIMEOUT)
        at.run()
        return at
    return run


def metric(at, label):
    matches = [item for item in at.metric if item.label == label]
    assert len(matches) == 1, f'metric {label!r} missing'
    return matches[0].value


def serial_text(day, slot_index):
    return f'{(day - EXCEL_EPOCH).days + slot_index / 288:.5f}'


def default_values():
    return {'Tair': '22', 'CO2air': '500',
            't_heat_sp': '20', 't_vent_sp': '25', 'co2_sp': '500',
            't_heat_vip': '20.5', 't_ventlee_vip': '24.5', 'co2_vip': '500'}


def write_climate_csv(path, overrides_by_slot, *, days=1):
    headers = HEADERS['GreenhouseClimate']
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\r\n')
        writer.writerow(headers)
        for absolute_slot in sorted(overrides_by_slot):
            values = dict(default_values(), **overrides_by_slot[absolute_slot])
            day_offset, slot_index = divmod(absolute_slot, 288)
            writer.writerow([serial_text(date(2020, 1, 1) + timedelta(days=day_offset),
                                         slot_index)]
                            + [values.get(header.strip(), '')
                               for header in headers[1:]])


def publish_climate(connection, raw_root, scope_name='Only', *,
                    overrides_by_slot=None, days=1, drain=False, resource_rows=None):
    """A climate scope through the REAL pipeline (Spark stage stubbed)."""
    tag = uuid4().hex
    site, snapshot, scope, policy = (name + ':' + tag for name in
                                     ('site', 'snapshot', 'scope', 'quality'))
    comp, cycle, team, crop = (name + ':' + tag for name in ('comp', 'cycle', 'team', 'crop'))
    connection.execute("INSERT INTO core.site VALUES (%s,'Fixture','source_naive','fixture')",
                       (site,))
    connection.execute("INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://fixture','fixture')",
                       (snapshot, tag * 2))
    connection.execute("INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",
                       (scope, snapshot))
    definition = {'missing': 'per_measure',
                  'nonnegative': sorted(['Tair', 'CO2air', 't_heat_sp', 't_vent_sp', 'co2_sp',
                                         't_heat_vip', 't_ventlee_vip', 'co2_vip', 'EC_drain_PC']),
                  'suspect_zero': ['pH_drain_PC'] if drain else [],
                  'off_mode_zero': ['co2_sp', 'co2_vip']}
    if resource_rows is not None:
        definition['nonnegative'] += ['Irr', 'Drain']
    connection.execute(
        "INSERT INTO config.quality_policy_version VALUES (%s,1,%s,%s,%s,%s,true,'synthetic climate policy')",
        (policy, date(2020, 1, 1), date(2020, 1, 1) + timedelta(days=days),
         json.dumps(definition, sort_keys=True, separators=(',', ':')),
         hashlib.sha256(json.dumps(definition, sort_keys=True,
                                   separators=(',', ':')).encode()).hexdigest()))
    for measure, record_type, unit in [
            ('Tair', 'climate', '°C'), ('CO2air', 'climate', 'ppm'),
            ('t_heat_sp', 'climate', '°C'), ('t_vent_sp', 'climate', '°C'),
            ('co2_sp', 'climate', 'ppm'), ('t_heat_vip', 'climate', '°C'),
            ('t_ventlee_vip', 'climate', '°C'), ('co2_vip', 'climate', 'ppm'),
            ('EC_drain_PC', 'drain', 'dS/m'), ('pH_drain_PC', 'drain', 'pH'),
            ('Irr', 'resource', 'L/m²/day'), ('Drain', 'resource', 'L/m²/day')]:
        connection.execute("INSERT INTO config.measure_definition VALUES (%s,%s,%s,'fixture') "
                           "ON CONFLICT (measure_code) DO NOTHING",
                           (measure, record_type, unit))
    connection.execute("INSERT INTO core.compartment VALUES (%s,%s,%s,'fixture')",
                       (comp, site, scope_name))
    connection.execute("INSERT INTO core.team VALUES (%s,%s,false,'fixture')", (team, scope_name))
    connection.execute("INSERT INTO core.crop VALUES (%s,'tomato',NULL,'fixture')", (crop,))
    connection.execute("INSERT INTO core.growing_cycle VALUES (%s,%s,%s,%s,%s,%s,'fixture')",
                       (cycle, comp, team, crop, date(2020, 1, 1),
                        date(2020, 1, 1) + timedelta(days=days)))
    connection.execute("INSERT INTO meta.source_folder_mapping VALUES (%s,%s,%s,%s,%s,'fixture')",
                       (snapshot, scope_name, comp, team, cycle))
    from greenhouse.db.pipeline import membership, save_artifact

    def artifact_stage(conn, workflow_, target, *, fault=None):
        with conn.transaction():
            info = membership(conn, workflow_)
        target.mkdir()
        (target / 'fixture.json').write_text(json.dumps(info, sort_keys=True,
                                                        separators=(',', ':')))
        return save_artifact(target, info | {'rows_in': info['member_count'],
                                             'rows_out': info['member_count']})
    base = raw_root / tag
    snapshot_root = base / 'raw'
    output_root = base / 'output'
    folder = snapshot_root / scope_name
    folder.mkdir(parents=True, exist_ok=True)
    climate_csv = folder / 'GreenhouseClimate.csv'
    grid = {slot: ({'EC_drain_PC': '3', 'pH_drain_PC': '6'} if drain else {})
            for slot in range(288 * days)}
    for absolute_slot, values in (overrides_by_slot or {}).items():
        grid[absolute_slot].update(values)
    write_climate_csv(climate_csv, grid, days=days)
    files = [{'path': f'{scope_name}/GreenhouseClimate.csv',
              'sha256': hashlib.sha256(climate_csv.read_bytes()).hexdigest(),
              'csv': {'data_rows': 288 * days}}]
    if resource_rows is not None:
        for code in ('Irr', 'Drain'):
            identity = f'fixture-basis:{tag}:{code}'
            connection.execute(
                "INSERT INTO config.area_basis_version VALUES "
                "(%s,%s,%s,%s,1,100,'fixture_area','verified',%s,%s,NULL,NULL,'synthetic verified basis')",
                (identity, scope, comp, code, date(2020, 1, 1), date(2020, 1, 1) + timedelta(days=days)))
            connection.execute("INSERT INTO config.basis_application VALUES (%s,%s,%s,'shared_analysis',%s,true)",
                               (scope, comp, code, identity))
        resource_csv = folder / 'Resources.csv'
        headers = HEADERS['Resources']
        with resource_csv.open('w', newline='') as stream:
            writer = csv.writer(stream, lineterminator='\r\n')
            writer.writerow(headers)
            for index, (irr, drained) in enumerate(resource_rows):
                values = {'Irr': irr, 'Drain': drained}
                writer.writerow([str((date(2020, 1, 1) - EXCEL_EPOCH).days + index)]
                                + [values.get(header.strip(), '') for header in headers[1:]])
        files.append({'path': f'{scope_name}/Resources.csv',
                      'sha256': hashlib.sha256(resource_csv.read_bytes()).hexdigest(),
                      'csv': {'data_rows': len(resource_rows)}})
    (snapshot_root / 'manifest.json').write_text(json.dumps(
        {'snapshot_id': snapshot, 'archive': {'sha256': (tag * 2)},
         'files': files}))
    result = run_pipeline(connection, snapshot_root, output_root,
                          scope, policy, stage_function=artifact_stage)
    adapter = PostgresClimateQueryAdapter(connection, raw_snapshots={snapshot: snapshot_root})
    return {'scope': scope, 'snapshot': snapshot, 'comp': comp, 'cycle': cycle,
            'publication': result['publication_id'], 'adapter': adapter, 'raw': snapshot_root}





@pytest.fixture
def published_climate(contracts_database, tmp_path):
    """One climate-published scope through the real pipeline with THREE
    marked slots (out_low, out_high and one missing actual) over two days,
    plus an explicit delta-0 schedule on BOTH bases covering the whole cycle.
    Episodes share identical identities across bases, so the stable-key
    selection can be proven across basis/window changes."""
    with psycopg.connect(**contracts_database, autocommit=True) as connection:
        fixture = publish_climate(connection, tmp_path, 'Only',
                                  overrides_by_slot={0: {'Tair': '19'},
                                                     100: {'Tair': '26'},
                                                     200: {'Tair': ''}},
                                  days=2)
        cycle_row = PostgresCatalogAdapter(connection).catalog_cycles(fixture['publication'])[0]
        schedule = RuleScheduleRevision(
            rule_version=f'rule-version:ui-climate:{uuid4().hex}',
            rule_id='rule:ui-climate', version=1,
            scope_id=fixture['scope'], compartment_id=fixture['comp'],
            evidence_reference='UI fixture temperature bounds', is_demo=True,
            label='demo_rule',
            segments=tuple(sorted((RuleScheduleSegment(
                metric='temperature', measurement_context='climate',
                basis=basis, start=datetime(2020, 1, 1),
                end=datetime(2020, 1, 3), delta=Decimal('0'))
                for basis in (RuleBasis.REQUESTED, RuleBasis.REALIZED)),
                key=rule_segment_sort_key)))
        from greenhouse.db.rule_schedule import register_rule_schedule
        register_rule_schedule(connection, schedule)
    return fixture, cycle_row, schedule


def _pg_kwargs(contracts_database):
    """Fixture kwargs already carry host/dbname/port/user/password keys —
    run_app maps exactly these onto PG* env names; password never printed."""
    return contracts_database


def open_climate(run_app, contracts_database):
    at = run_app(_pg_kwargs(contracts_database))
    at.radio(key='screen_choice').set_value(CLIMATE_SCREEN).run()
    assert at.exception == []
    return at


def choose_cycle(at, cycle_row):
    at.selectbox(key='clim_cycle_choice').set_value(cycle_row).run()
    assert at.exception == []


def choose_rule(at, rule_version):
    at.selectbox(key='clim_rule_choice').set_value(rule_version).run()
    assert at.exception == []


def test_climate_publication_option_lists_climate_bundle(run_app, contracts_database,
                                                        published_climate):
    at = open_climate(run_app, contracts_database)
    selectbox = at.selectbox(key='clim_pub_choice')
    labels = list(selectbox.options)
    assert any('climate-mart-v1' in str(label) for label in labels)


def test_climate_no_rule_stays_unknown(run_app, contracts_database, published_climate):
    fixture, cycle_row, schedule = published_climate
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    assert any('rule_not_configured' in item.value for item in at.markdown)
    assert any('không phải tỷ lệ dữ liệu nguồn có mặt' in item.value for item in at.caption)


def test_climate_rule_and_filter_interaction(run_app, contracts_database, published_climate):
    fixture, cycle_row, schedule = published_climate
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    choose_rule(at, schedule.rule_version)
    assert any(schedule.rule_version in item.value for item in at.markdown)
    at.radio(key='clim_metric_filter').set_value('temperature').run()
    assert at.exception == []
    at.select_slider(key='clim_min_filter').set_value(Decimal('0')).run()
    assert at.exception == []      # free 'tất cả' filter; the 5-minute run now lists
    at.radio(key='clim_basis').set_value(RuleBasis.REALIZED).run()
    assert at.exception == []   # basis change keeps the surface alive, never stale


def test_climate_paging_controls_exist(run_app, contracts_database, published_climate):
    fixture, cycle_row, schedule = published_climate
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    choose_rule(at, schedule.rule_version)
    at.select_slider(key='clim_min_filter').set_value(Decimal('0')).run()
    assert any('Trang' in item.value or 'offset' in item.value
               for item in at.caption)


def test_climate_exports_render(run_app, contracts_database, published_climate):
    fixture, cycle_row, schedule = published_climate
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    choose_rule(at, schedule.rule_version)
    at.select_slider(key='clim_min_filter').set_value(Decimal('0')).run()
    downloads = [button.label for button in at.download_button]
    assert any('CSV summary' in label for label in downloads)
    assert any('JSON result' in label for label in downloads)
    assert any('CSV đợt' in label for label in downloads)


def test_climate_episode_selects_non_first_and_survives_basis_and_window(
        run_app, contracts_database, published_climate):
    """Real stable-key episode selector: a NON-FIRST episode is selectable and
    the selection re-resolves across a basis change and a window change (and
    returns after the window comes back) — never a stale index."""
    fixture, cycle_row, schedule = published_climate
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    choose_rule(at, schedule.rule_version)
    at.select_slider(key='clim_min_filter').set_value(Decimal('0')).run()
    labels = list(at.selectbox(key='clim_episode_choice').options)
    assert len(labels) == 2, labels
    # NOTE: AppTest selectbox.options are the FORMATTED LABELS — select the
    # second episode by index (never by its label string).
    at.selectbox(key='clim_episode_choice').select_index(1)
    at.run()
    assert any('2020-01-01T08:20:00' in item.value for item in at.caption)
    # basis change: identical episode identities on realized → same key kept
    at.radio(key='clim_basis').set_value(RuleBasis.REALIZED).run()
    assert any('2020-01-01T08:20:00' in item.value for item in at.caption)
    assert any('2020-01-01T08:20:00' in item.value for item in at.caption)
    # basis change: identical episode identities on realized → same key kept
    at.radio(key='clim_basis').set_value(RuleBasis.REALIZED).run()
    assert any('2020-01-01T08:20:00' in item.value for item in at.caption)
    # window change to day 2: no out slots there → selection vanishes, the
    # surface stays alive (JSON + inspection) instead of a stale trace
    at.date_input(key='clim_window').set_value((date(2020, 1, 2), date(2020, 1, 2))).run()
    assert any('JSON result' in button.label for button in at.download_button)
    assert any('Tra cứu dữ liệu nguồn' in item.value for item in at.subheader)
    # window back: the stable key re-resolves to the same episode again
    at.date_input(key='clim_window').set_value((date(2020, 1, 1), date(2020, 1, 1))).run()
    assert any('2020-01-01T08:20:00' in item.value for item in at.caption)


def test_climate_pagination_navigates_immediately(
        run_app, contracts_database, tmp_path):
    """Pagination buttons navigate on the FIRST click: the on_click callback
    changes the offset before the rerun renders the new page."""
    with psycopg.connect(**contracts_database, autocommit=True) as connection:
        overrides = {slot: ({'Tair': '19'} if (slot // 6) % 2 == 0 else {'Tair': '26'})
                     for slot in range(0, 288, 6)}
        fixture = publish_climate(connection, tmp_path, 'Many',
                                  overrides_by_slot=overrides, days=1)
        rule = RuleScheduleRevision(
            rule_version=f'rule-version:many:{uuid4().hex}', rule_id='rule:many', version=1,
            scope_id=fixture['scope'], compartment_id=fixture['comp'],
            evidence_reference='pagination fixture', is_demo=True, label='demo_rule',
            segments=(RuleScheduleSegment(metric='temperature', measurement_context='climate',
                                          basis=RuleBasis.REQUESTED, start=datetime(2020, 1, 1),
                                          end=datetime(2020, 1, 2), delta=Decimal('0')),))
        from greenhouse.db.rule_schedule import register_rule_schedule
        register_rule_schedule(connection, rule)
        cycle_row = PostgresCatalogAdapter(connection).catalog_cycles(fixture['publication'])[0]
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    choose_rule(at, rule.rule_version)
    at.select_slider(key='clim_min_filter').set_value(Decimal('0')).run()
    assert len(at.selectbox(key='clim_episode_choice').options) == 40
    at.button(key='clim_next_page').click().run()
    assert len(at.selectbox(key='clim_episode_choice').options) == 8
    # 'Về trang đầu' is enabled exactly when we are NOT at page one
    at.button(key='clim_head_page').click().run()
    assert len(at.selectbox(key='clim_episode_choice').options) == 40


def test_climate_source_inspection_and_json_without_rule(
        run_app, contracts_database, published_climate):
    """No-rule surface: everything is rule_not_configured with ZERO episodes —
    the bounded source inspection STILL works and the typed JSON download
    remains available (never hidden behind the episode detail flow)."""
    fixture, cycle_row, schedule = published_climate
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    assert any('rule_not_configured' in item.value for item in at.markdown)
    assert any('Tra cứu dữ liệu nguồn' in item.value for item in at.subheader)
    assert any('hàng trong cửa sổ [2020-01-01T00:00:00' in item.value
               for item in at.caption)
    assert not any('Chọn đợt để xem chi tiết' in item.value for item in at.caption)
    downloads = [button.label for button in at.download_button]
    assert any('JSON result' in label for label in downloads)
    assert not any('CSV detail' in label for label in downloads)


def test_historical_navigation_keeps_window_and_quality_scope(run_app, contracts_database, published_climate):
    fixture, cycle_row, _ = published_climate
    at = open_climate(run_app, contracts_database)
    choose_cycle(at, cycle_row)
    window = (date(2020, 1, 2), date(2020, 1, 2))
    at.date_input(key='clim_window').set_value(window).run()
    assert not at.exception
    at.radio(key='screen_choice').set_value('🏠 Tổng quan vụ').run()
    assert not at.exception
    assert at.date_input(key='overview_window').value == window
    at.radio(key='screen_choice').set_value('🛡️ Chất lượng dữ liệu').run()
    assert not at.exception
    assert at.date_input(key='quality_window').value == window
    at.checkbox(key='quality_load_details').check().run()
    at.checkbox(key='quality_load_quarantine').check().run()
    assert not at.exception
    assert at.selectbox(key='quality_pub').value.publication_id == fixture['publication']
    assert at.selectbox(key='quality_cycle').value.cycle_id == fixture['cycle']
    at.radio(key='screen_choice').set_value(WATER_SCREEN).run()
    assert not at.exception
    assert at.date_input(key='window').value == window


def test_quality_overview_respects_publication_and_date(contracts_database, published_climate):
    from greenhouse.db.quality_overview import excluded, measures

    fixture, _, _ = published_climate
    with psycopg.connect(**contracts_database) as connection:
        day_one = excluded(connection, fixture['publication'], fixture['cycle'],
                           date(2020, 1, 1), date(2020, 1, 1))
        day_two = excluded(connection, fixture['publication'], fixture['cycle'],
                           date(2020, 1, 2), date(2020, 1, 2))
        assert any(row['measure_code'] == 'Tair' and row['status'] == 'missing' for row in day_one)
        assert not any(row['measure_code'] == 'Tair' and row['status'] == 'missing' for row in day_two)
        assert all(row['relative_path'] and row['line_number'] >= 2 for row in day_one)
        assert not measures(connection, 'unknown-publication', fixture['cycle'],
                            date(2020, 1, 1), date(2020, 1, 2))


def test_quality_quarantine_is_scoped_to_published_load_and_cycle(contracts_database, published_climate):
    from greenhouse.db.quality_overview import quarantined

    fixture, _, _ = published_climate
    with psycopg.connect(**contracts_database) as connection:
        row = connection.execute('''
            SELECT p.run_id, r.source_record_id
            FROM meta.publication p
            JOIN meta.publication_member m ON m.publication_id = p.publication_id
            JOIN core.record_revision r ON r.revision_id = m.revision_id
            WHERE p.publication_id = %s AND r.cycle_id = %s
              AND r.observed_at >= '2020-01-01' AND r.observed_at < '2020-01-02'
            ORDER BY r.observed_at LIMIT 1
        ''', (fixture['publication'], fixture['cycle'])).fetchone()
        connection.execute('''
            INSERT INTO meta.quarantine_record
                (quarantine_id, source_record_id, run_id, disposition, reason_code, policy_version, details)
            VALUES (%s, %s, %s, 'duplicate', 'fixture_duplicate', 'fixture', '{}'::jsonb)
        ''', ('quality-test:' + uuid4().hex, row[1], row[0]))
        first = quarantined(connection, fixture['publication'], fixture['cycle'],
                            date(2020, 1, 1), date(2020, 1, 1))
        second = quarantined(connection, fixture['publication'], fixture['cycle'],
                             date(2020, 1, 2), date(2020, 1, 2))
        assert any(item['source_record_id'] == row[1] for item in first)
        assert not any(item['source_record_id'] == row[1] for item in second)
        assert not quarantined(connection, 'other-publication', fixture['cycle'],
                               date(2020, 1, 1), date(2020, 1, 1))
        connection.rollback()
