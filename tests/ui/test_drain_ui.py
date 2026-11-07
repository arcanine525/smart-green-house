"""BR-03 real pipeline, pinned water context and Streamlit interaction checks."""
import csv
import io
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import psycopg
import pytest

from greenhouse.app import wiring, drain_actions, drain_exports
from greenhouse.contracts import DrainEvaluation, RuleBasis, from_json
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.db.drain_profiles import save_drain_profile
from greenhouse.db.rule_schedule import read_rule_schedule
from greenhouse.queries import DrainService, page_episodes
from test_climate_ui import publish_climate, run_app, metric

SCREEN = '🧪 EC/pH drain (BR-03)'


@pytest.fixture
def published_drain(contracts_database, tmp_path, monkeypatch):
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        fixture = publish_climate(conn, tmp_path, 'DrainFixture', drain=True, days=2,
            overrides_by_slot={0: {'EC_drain_PC': '6'}, 1: {'EC_drain_PC': '6'},
                               2: {'pH_drain_PC': '0'}, 3: {'EC_drain_PC': '-1'},
                               100: {'EC_drain_PC': '6', 'pH_drain_PC': '7'}},
            resource_rows=(('10', '2'), ('20', '6')))
        service = DrainService(PostgresDrainQueryAdapter(conn))
        publication = service.adapter.publication(fixture['publication'])
        cycle = wiring.catalog_service(conn).cycles(publication)[0]
        revision = save_drain_profile(conn, publication, cycle, name='fixture', version=1,
            ec_lower='2', ec_upper='5', ph_lower='5.5', ph_upper='6.5',
            provenance='Synthetic UI fixture; unverified target bands')
        fixture.update(cycle_row=cycle, revision=revision, publication_ref=publication)
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(fixture['raw']))
    return fixture


def open_drain(run_app, kwargs, fixture, *, rule=True):
    at = run_app(kwargs)
    at.radio(key='screen_choice').set_value(SCREEN).run()
    assert not at.exception
    if rule:
        at.selectbox(key='drain_rule_choice').set_value(fixture['revision'].rule_version).run()
    assert not at.exception
    assert not at.error, [item.value for item in at.error]
    return at


def test_drain_no_rule_keeps_raw_quality_and_json(run_app, contracts_database, published_drain):
    at = open_drain(run_app, contracts_database, published_drain, rule=False)
    assert metric(at, 'EC ngoài dải (phút)') == 'NULL'
    assert metric(at, 'PH ngoài dải %') == 'NULL'
    assert any('rule_not_configured' in item.value for item in at.info)
    assert any('không phải tỷ lệ dữ liệu nguồn có mặt' in item.value for item in at.caption)
    tables = [frame.value for frame in at.dataframe]
    source = next(frame for frame in tables if 'ec_status' in frame.columns)
    assert str(source.iloc[2]['ph']) == '0.0' or Decimal(str(source.iloc[2]['ph'])) == 0
    assert source.iloc[2]['ph_status'] == 'suspect'
    assert source.iloc[2]['ec_status'] == 'accepted'
    assert Decimal(str(source.iloc[3]['ec'])) == -1
    assert source.iloc[3]['ec_status'] == 'invalid'
    assert any(button.label == 'JSON result drain (typed)' for button in at.download_button)
    assert len(at.get('plotly_chart')) == 2


def test_drain_filters_preserve_totals_and_invalidate_selection(run_app, contracts_database, published_drain):
    at = open_drain(run_app, contracts_database, published_drain)
    before = metric(at, 'EC ngoài dải (phút)')
    chosen = at.selectbox(key='drain_episode_choice')
    assert len(chosen.options) >= 2
    chosen.select_index(1).run()
    selected = at.session_state['drain_selected_key']
    at.run()
    assert at.session_state['drain_selected_key'] == selected
    at.radio(key='drain_metric_filter').set_value('ph').run()
    assert at.session_state['drain_selected_key'][0] == 'ph'
    assert metric(at, 'EC ngoài dải (phút)') == before
    at.select_slider(key='drain_min_filter').set_value(Decimal('1440')).run()
    assert metric(at, 'EC ngoài dải (phút)') == before
    assert not at.exception


def test_drain_profile_save_reopen_and_invalid_bounds(run_app, contracts_database, published_drain):
    at = open_drain(run_app, contracts_database, published_drain)
    old_rule = published_drain['revision'].rule_version
    before = metric(at, 'EC ngoài dải (phút)')
    at.text_input(key='drain_profile_name').set_value('replay')
    at.text_input(key='drain_profile_evidence').set_value('Engineer experiment, unverified')
    at.text_input(key='drain_ec_upper').set_value('10')
    at.button(key='drain_save_profile').click().run()
    assert not at.exception
    new_rule = at.selectbox(key='drain_rule_choice').value
    assert new_rule != old_rule and 'replay' in new_rule
    assert metric(at, 'EC ngoài dải (phút)') != before
    at.selectbox(key='drain_rule_choice').set_value(old_rule).run()
    assert metric(at, 'EC ngoài dải (phút)') == before
    at.selectbox(key='drain_rule_choice').set_value(new_rule).run()
    assert not at.exception
    at.text_input(key='drain_ec_lower').set_value('NaN')
    at.number_input(key='drain_profile_version').set_value(2)
    at.button(key='drain_save_profile').click().run()
    assert any('fini' in item.value or 'hữu hạn' in item.value for item in at.error)
    assert at.selectbox(key='drain_rule_choice').value == new_rule


def test_drain_water_unique_days_midnight_partial_and_missing(contracts_database, published_drain):
    with psycopg.connect(**contracts_database) as conn:
        fixture = published_drain
        service = wiring.drain_service(conn)
        publication = service.adapter.publication(fixture['publication'])
        context = wiring.build_context(publication, fixture['cycle_row'], date(2020, 1, 1),
                                        date(2020, 1, 2), fixture['revision'].rule_version)
        evaluation = service.get_evaluation(context, rule_version=fixture['revision'].rule_version)
        water_service = wiring.irrigation_service(conn)
        first = drain_actions.daily_water_context(water_service, evaluation,
            datetime(2020, 1, 1, 23, 55), datetime(2020, 1, 2))
        assert len(first.action.days) == 1
        assert first.action.summary.irrigation_observed_l_m2 == Decimal('10')
        assert first.partial_dates == (date(2020, 1, 1),)
        two = drain_actions.daily_water_context(water_service, evaluation,
            datetime(2020, 1, 1, 23, 55), datetime(2020, 1, 2, 0, 5))
        direct = water_service.get_irrigation_summary(replace(context, rule_version=None))
        assert len(two.action.days) == 2
        assert two.action.summary == direct
        assert direct.irrigation_observed_l_m2 == Decimal('30')
        assert direct.drain_observed_l_m2 == Decimal('8')
        assert two.partial_dates == (date(2020, 1, 1), date(2020, 1, 2))
        assert drain_actions.daily_water_context(water_service, evaluation).action.summary == direct
        assert len({day.resource_date for day in two.action.days}) == 2


def test_drain_exports_provenance_bound_page_and_roundtrip(contracts_database, published_drain):
    with psycopg.connect(**contracts_database) as conn:
        service = wiring.drain_service(conn)
        fixture = published_drain
        publication = service.adapter.publication(fixture['publication'])
        context = wiring.build_context(publication, fixture['cycle_row'], date(2020, 1, 1), date(2020, 1, 2))
        evaluation = service.get_evaluation(context, rule_version=fixture['revision'].rule_version)
        assert from_json(DrainEvaluation, drain_exports.result_json(evaluation)) == evaluation
        page, total = page_episodes(evaluation.result, metric_order=('ec', 'ph'), limit=40)
        encoded = drain_exports.episodes_csv(evaluation, page, total=total, metric=None,
                                             min_minutes=0).decode('utf-8-sig')
        assert 'user_unverified' in encoded and 'ec=dS/m, ph=pH' in encoded
        rows = list(csv.DictReader(line for line in encoded.splitlines() if not line.startswith('#')))
        assert rows[0]['unit'] == 'dS/m'
        trace, offset, count = drain_actions.trace_page(service, context,
            fixture['revision'].rule_version, datetime(2020, 1, 1), datetime(2020, 1, 3))
        assert trace.window_slots == 144 and len(trace.reviews) <= 288
        assert trace.truncated and trace.end == datetime(2020, 1, 1, 12)
        assert b'# truncated=true' in drain_exports.detail_csv(evaluation, trace)
        next_page, _, _ = drain_actions.trace_page(service, context,
            fixture['revision'].rule_version, datetime(2020, 1, 1), datetime(2020, 1, 3), 144)
        assert next_page.start == trace.end
        water = drain_actions.daily_water_context(wiring.irrigation_service(conn), evaluation)
        assert b'full-day amounts, not prorated' in drain_exports.water_csv(evaluation, water)


def test_drain_lineage_actual_raw(run_app, contracts_database, published_drain):
    at = open_drain(run_app, contracts_database, published_drain)
    at.button(key='drain_verify_lineage').click().run()
    assert not at.exception
    assert any('Raw hash xác minh khớp' in item.value for item in at.success)


def test_no_episode_and_no_valid_water_keep_br02_nulls(run_app, contracts_database, tmp_path):
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        fixture = publish_climate(conn, tmp_path, 'NoEvent', drain=True,
                                  resource_rows=(('', ''),))
        service = wiring.drain_service(conn)
        publication = service.adapter.publication(fixture['publication'])
        cycle = wiring.catalog_service(conn).cycles(publication)[0]
        revision = save_drain_profile(conn, publication, cycle, name='inside', version=1,
            ec_lower='2', ec_upper='5', ph_lower='5.5', ph_upper='6.5', provenance='Synthetic test')
        fixture.update(revision=revision)
        context = wiring.build_context(publication, cycle, cycle.start, cycle.start)
        evaluation = service.get_evaluation(context, rule_version=revision.rule_version)
        assert not evaluation.result.episodes
        water_service = wiring.irrigation_service(conn)
        composed = drain_actions.daily_water_context(water_service, evaluation)
        assert composed.action.summary == water_service.get_irrigation_summary(context)
        assert composed.action.summary.irrigation_observed_l_m2 is None
        assert composed.action.summary.drain_observed_l_m2 is None
        assert composed.action.summary.paired_coverage_pct == 0
        assert composed.partial_dates == ()
    at = open_drain(run_app, contracts_database, fixture)
    assert metric(at, 'Tưới nguồn (L/m², ngày tính một lần)') == 'NULL'
    assert any('Không có đợt' in item.value for item in at.info)


def test_long_episode_ui_trace_pages_and_profile_reset(run_app, contracts_database, tmp_path):
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        fixture = publish_climate(conn, tmp_path, 'LongEvent', drain=True, days=2,
            overrides_by_slot={slot: {'EC_drain_PC': '6'} for slot in range(576)})
        publication = wiring.drain_service(conn).adapter.publication(fixture['publication'])
        cycle = wiring.catalog_service(conn).cycles(publication)[0]
        fixture['revision'] = save_drain_profile(conn, publication, cycle, name='long', version=1,
            ec_lower='2', ec_upper='5', ph_lower='5.5', ph_upper='6.5', provenance='Synthetic long event')
    at = open_drain(run_app, contracts_database, fixture)
    before = metric(at, 'EC ngoài dải (phút)')
    assert any('144 mốc / 576' in item.value for item in at.caption)
    at.button(key='drain_trace_next').click().run()
    assert at.session_state['drain_trace_offset'] == 144
    assert any('offset 144' in item.value for item in at.caption)
    assert metric(at, 'EC ngoài dải (phút)') == before
    at.selectbox(key='drain_rule_choice').set_value(None).run()
    assert not at.exception
    assert not any(button.key == 'drain_trace_next' for button in at.button)
    at.selectbox(key='drain_rule_choice').set_value(fixture['revision'].rule_version).run()
    assert not at.exception
    assert at.session_state.get('drain_trace_offset', 0) == 0
