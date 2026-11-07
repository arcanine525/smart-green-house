"""Streamlit AppTest scenarios for the production water screen.

Runs the real home.py against a disposable fixture database (guarded test_
prefix): startup, filters, empty/pre-mart states, ratio semantics, chart gaps,
drilldown and rule selection. No host browser and no host venv involved.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest

from greenhouse.db.irrigation_rules import register_irrigation_rule
from greenhouse.contracts import IrrigationRuleProfile

TIMEOUT = 180

# psycopg connection kwargs → the PG* variables Settings.from_env() reads.
PG_ENV = {'host': 'PGHOST', 'port': 'PGPORT', 'dbname': 'PGDATABASE',
          'user': 'PGUSER', 'password': 'PGPASSWORD'}


@pytest.fixture
def run_app(app_path, monkeypatch):
    def run(target):
        for key, value in target.items():
            if key in PG_ENV:
                monkeypatch.setenv(PG_ENV[key], str(value))
        at = AppTest.from_file(app_path, default_timeout=TIMEOUT)
        at.run()
        return at
    return run


def metric(at, label):
    matches = [item for item in at.metric if item.label == label]
    assert len(matches) == 1, f'metric {label!r} missing'
    return matches[0].value


def markdowns(at):
    return [item.value for item in at.markdown]


def test_renders_cycle_summary_ratio_of_sums(published_scope, contracts_database, run_app):
    published_scope(end=date(2020, 1, 3))  # default full-cycle window covers both days
    at = run_app(contracts_database)
    assert at.exception == []
    assert metric(at, 'Irrigation observed') == '40,00 L/m²'
    assert metric(at, 'Drain observed') == '14,00 L/m²'
    assert metric(at, 'Tỷ lệ drain (paired)') == '35,00 %'  # ratio of sums, not 30% average
    assert metric(at, 'Độ phủ paired') == '100,00 %'
    # Chart element itself is covered by figures tests + smoke run; AppTest has no plotly accessor.
    assert any('ratio of sums' in item.value for item in at.caption)
    assert any('not_configured' in value for value in markdowns(at))


def test_single_day_window_updates_summary(published_scope, contracts_database, run_app):
    published_scope((('10', '2'), ('30', '12')), end=date(2020, 1, 3))
    at = run_app(contracts_database)
    at.date_input(key='window').set_value((date(2020, 1, 2), date(2020, 1, 2))).run()
    assert at.exception == []
    assert metric(at, 'Irrigation observed') == '30,00 L/m²'
    assert metric(at, 'Drain observed') == '12,00 L/m²'
    assert metric(at, 'Tỷ lệ drain (paired)') == '40,00 %'
    assert metric(at, 'Độ phủ paired') == '100,00 %'


def test_incomplete_window_is_neutral_then_recovers(published_scope, contracts_database, run_app):
    published_scope((('10', '2'), ('30', '12')), end=date(2020, 1, 3))
    at = run_app(contracts_database)
    assert at.exception == []
    at.date_input(key='window').set_value((date(2020, 1, 2),)).run()  # one date picked
    assert at.exception == []
    assert any('chưa đủ hai đầu' in item.value for item in at.info)
    assert at.metric == []  # no query, no KPI and no export for a transient window
    at.date_input(key='window').set_value((date(2020, 1, 2), date(2020, 1, 3))).run()
    assert at.exception == []
    assert at.info == []  # neutral state clears once the range is complete again
    assert metric(at, 'Tỷ lệ drain (paired)') == '35,00 %'


def test_partial_pairing_reports_fifty_percent_and_reason(published_scope, contracts_database, run_app):
    published_scope((('10', '2'), ('30', None)), end=date(2020, 1, 3))
    at = run_app(contracts_database)
    assert at.exception == []
    assert metric(at, 'Tỷ lệ drain (paired)') == '20,00 %'
    assert metric(at, 'Độ phủ paired') == '50,00 %'
    captions = [item.value for item in at.caption]
    assert any('partial_coverage' in caption for caption in captions)


def test_all_zero_keeps_ratio_null_with_zero_totals(published_scope, contracts_database, run_app):
    published_scope((('0', '0'), ('0', '0')), end=date(2020, 1, 3))
    at = run_app(contracts_database)
    assert at.exception == []
    assert metric(at, 'Irrigation observed') == '0,00 L/m²'
    assert metric(at, 'Tỷ lệ drain (paired)') == '— %'
    assert metric(at, 'Độ phủ paired') == '100,00 %'
    assert any('no_irrigation_quantity' in item.value for item in at.caption)


def test_over_one_hundred_ratio_is_retained(published_scope, contracts_database, run_app):
    published_scope((('5', '6'), ('5', '6')), end=date(2020, 1, 3))
    at = run_app(contracts_database)
    assert at.exception == []
    assert metric(at, 'Tỷ lệ drain (paired)') == '120,00 %'
    assert any('drain&gt;irr: 2' in value for value in markdowns(at))


def test_missing_day_is_a_chart_gap_without_fake_source(published_scope, contracts_database, run_app):
    e = published_scope((('10', '2'),), end=date(2020, 1, 3))
    at = run_app(contracts_database)
    assert at.exception == []
    missing = e.service.get_irrigation_days(e.context)[1]
    assert missing.revision_id is None
    at.selectbox(key='day_choice').set_value(missing).run()
    assert at.exception == []
    assert any('không có bản ghi nguồn' in item.value for item in at.info)


def test_assumed_basis_keeps_visible_label(published_scope, contracts_database, run_app):
    published_scope((('5', '6'), ('5', '6')), basis='assumed_unverified', end=date(2020, 1, 3))
    at = run_app(contracts_database)
    assert at.exception == []
    assert metric(at, 'Tỷ lệ drain (paired)') == '120,00 %'
    values = markdowns(at)
    assert any('giả định chưa xác minh' in value for value in values)
    assert any('AGC_SHARED_BASIS_V1' in value for value in values)


def test_rule_selection_shows_version_and_outside_count(published_scope, contracts_database, run_app):
    e = published_scope((('10', '2'), ('10', '5')), end=date(2020, 1, 3))
    tag = uuid4().hex
    profile = IrrigationRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, e.scope, e.comp,
                                    Decimal('20'), Decimal('40'), Decimal('0'), Decimal('100'),
                                    'synthetic review thresholds; not agronomic guidance', True, 'demo_rule')
    register_irrigation_rule(e.connection, profile)
    at = run_app(contracts_database)
    at.selectbox(key='rule_choice').set_value(profile).run()
    assert at.exception == []
    values = markdowns(at)
    assert any(profile.rule_version in value for value in values)
    assert any('ngoài dải 1 ngày' in value for value in values)
    assert any('outside_irrigation_rule' in item.value for item in at.caption)


def test_changing_publication_resets_rule_and_filters(published_scope, contracts_database, run_app):
    first = published_scope((('10', '2'), ('10', '5')), end=date(2020, 1, 3))
    tag = uuid4().hex
    profile = IrrigationRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, first.scope, first.comp,
                                    Decimal('20'), Decimal('40'), Decimal('0'), Decimal('100'),
                                    'synthetic review thresholds; not agronomic guidance', True, 'demo_rule')
    register_irrigation_rule(first.connection, profile)
    second = published_scope()
    rows = second.catalog.publications()
    first_row = [row for row in rows if row.scope_id == first.scope][0]
    second_row = [row for row in rows if row.scope_id == second.scope][0]
    at = run_app(contracts_database)
    at.selectbox(key='pub_choice').set_value(first_row).run()
    at.selectbox(key='rule_choice').set_value(profile).run()
    assert at.selectbox(key='rule_choice').value is not None
    at.selectbox(key='pub_choice').set_value(second_row).run()
    assert at.exception == []
    assert at.selectbox(key='rule_choice').value is None
    assert any('not_configured' in value for value in markdowns(at))


def test_cycle_change_resets_window_rule_and_day(published_scope, contracts_database, run_app):
    e = published_scope((('10', '2'), ('10', '5')), end=date(2020, 1, 3), cycles=2)
    tag = uuid4().hex
    profile = IrrigationRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, e.scope, e.comp,
                                    Decimal('20'), Decimal('40'), Decimal('0'), Decimal('100'),
                                    'synthetic review thresholds; not agronomic guidance', True, 'demo_rule')
    register_irrigation_rule(e.connection, profile)
    cycle_b = [c for c in e.catalog.cycles(e.publication) if c.compartment_id == e.comp_b][0]
    at = run_app(contracts_database)
    at.selectbox(key='rule_choice').set_value(profile).run()
    at.date_input(key='window').set_value((date(2020, 1, 1), date(2020, 1, 1))).run()
    assert at.selectbox(key='rule_choice').value is not None
    at.selectbox(key='cycle_choice').set_value(cycle_b).run()
    assert at.exception == []
    # Window snaps back to cycle B's own full range; scoped rule and day reset.
    assert at.date_input(key='window').value == (cycle_b.start, cycle_b.end - timedelta(days=1))
    assert at.selectbox(key='rule_choice').value is None
    assert any('not_configured' in value for value in markdowns(at))
    # Cycle B has no published facts: totals stay NULL with zero coverage, never 0.
    assert metric(at, 'Irrigation observed') == '— L/m²'
    assert metric(at, 'Độ phủ paired') == '0,00 %'
    assert any('missing_resource_day' in item.value for item in at.caption)


def test_missing_raw_stays_unverified_and_exports_remain(published_scope, contracts_database,
                                                         run_app, tmp_path, monkeypatch):
    e = published_scope()
    raw_root = tmp_path / 'rawmount'
    (raw_root / e.snapshot).mkdir(parents=True)  # snapshot-named dir without manifest
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(raw_root))
    at = run_app(contracts_database)
    assert at.exception == []
    assert at.metric != []
    assert len(at.download_button) == 3
    assert any('không xác minh được' in item.value for item in at.warning)


def test_manifest_without_raw_file_stays_unverified(published_scope, contracts_database,
                                                    run_app, tmp_path, monkeypatch):
    import json
    e = published_scope()
    raw_root = tmp_path / 'rawmount'
    snapshot = raw_root / e.snapshot
    snapshot.mkdir(parents=True)
    (snapshot / 'manifest.json').write_text(json.dumps({'snapshot_id': e.snapshot, 'dataset': 'fixture'}))
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(raw_root))
    at = run_app(contracts_database)
    assert at.exception == []
    assert len(at.download_button) == 3
    assert any('File raw không có trên mount' in item.value for item in at.warning)


def test_empty_database_shows_actionable_state(empty_database, run_app):
    at = run_app(empty_database)
    assert at.exception == []
    assert at.info and 'Chưa có publication nào' in at.info[0].value
    assert not at.error


def test_pre_mart_database_shows_error_state_without_fallback(legacy_database, run_app):
    at = run_app(legacy_database)
    assert at.exception == []
    assert at.error and 'water mart' in at.error[0].value
