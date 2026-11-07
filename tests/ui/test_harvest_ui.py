"""Streamlit AppTest scenarios for the production harvest screen.

Runs the real home.py: screen navigation water/harvest, KPI semantics from
published DTOs, incomplete states, rule demo, record detail/lineage,
authorized unresolved-source audit and export buttons. Water behaviors stay
in test_app_ui.py; this file covers only the harvest additions.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from greenhouse.db.harvest_rules import register_harvest_rule
from greenhouse.contracts import HarvestRuleProfile

TIMEOUT = 180

PG_ENV = {'host': 'PGHOST', 'port': 'PGPORT', 'dbname': 'PGDATABASE',
          'user': 'PGUSER', 'password': 'PGPASSWORD'}
HARVEST_SCREEN = '🍅 Thu hoạch (BR-05)'
WATER_SCREEN = '💧 Nước (BR-02)'


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


def markdowns(at):
    return [item.value for item in at.markdown]


def captions(at):
    return [item.value for item in at.caption]


def open_harvest(run, contracts_database):
    at = run(contracts_database)
    at.radio(key='screen_choice').set_value(HARVEST_SCREEN).run()
    assert at.exception == []
    return at


def demo_profile(connection, scope, comp, threshold='10'):
    tag = uuid4().hex
    profile = HarvestRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, scope, comp,
                                 Decimal(threshold),
                                 'fixture demo threshold; not agronomic guidance', True, 'demo_rule')
    register_harvest_rule(connection, profile)
    return profile


DEMO_LEGACY_EVIDENCE = ('DEMO fixture: AGC case material shows a 10 percent class-B share '
                        'boundary example; NOT an agronomic or quality standard. '
                        '(unsupported wording kept for history)')


def demo_family_profiles(connection, scope, comp):
    """Register ONE explicit demo family with a legacy v1 row plus the v2
    replacement whose provenance cites the BR-05 synthetic example."""
    family = 'harvest-demo-b10:' + comp
    legacy = HarvestRuleProfile(family, family, 1, scope, comp, Decimal('10'),
                                DEMO_LEGACY_EVIDENCE, True, 'demo_rule')
    replacement = HarvestRuleProfile(family + ':v2', family, 2, scope, comp, Decimal('10'),
                                     'Synthetic demo per docs/br/br_05_harvest-quality-tracking.md '
                                     '§4.2/AC-08: 10% class-B share boundary example; explicitly NOT '
                                     'an AGC standard threshold. Supersedes the legacy v1 wording.',
                                     True, 'demo_rule')
    register_harvest_rule(connection, legacy)
    register_harvest_rule(connection, replacement)
    return legacy, replacement


def test_demo_family_supersession_labels_no_autoselect(published_harvest_scope,
                                                       contracts_database, run_app):
    """The demo family shows legacy/supersession labels; the legacy profile
    remains selectable (historical lookup) with a warning; the v2 replacement
    is identified as such; nothing is selected by default."""
    from greenhouse.app.harvest import harvest_rule_grade_label, latest_demo_versions
    e = published_harvest_scope([(1, '1', '0.2')], start=date(2020, 2, 1), end=date(2020, 2, 4))
    legacy, replacement = demo_family_profiles(e.connection, e.scope, e.comp)
    at = open_harvest(run_app, contracts_database)
    # No auto-selection, no default profile: not_configured by default.
    assert at.selectbox(key='harv_rule_choice').value is None
    assert any('not_configured' in value for value in markdowns(at))
    # Labels: legacy marked superseded; replacement marked as the successor.
    rules = [row for row in e.catalog.harvest_rules(
        [row for row in e.catalog.publications() if row.scope_id == e.scope][0],
        e.comp)]
    latest = latest_demo_versions(rules)
    assert latest == {legacy.rule_id: 2}
    assert 'legacy v1 (superseded: accurate provenance lives in v2)' in harvest_rule_grade_label(legacy, latest)
    assert 'replacement of legacy v1' in harvest_rule_grade_label(replacement, latest)
    assert 'synthetic demo provenance per BR-05 §4.2/AC-08' in harvest_rule_grade_label(replacement, latest)
    # Selecting the LEGACY profile keeps working, with the supersede warning
    # in AppTest.warning (st.warning is its own element class).
    at.selectbox(key='harv_rule_choice').set_value(legacy).run()
    assert at.exception == []
    warnings_text = [item.value for item in at.warning]
    assert any('Đã chọn demo legacy v1' in value for value in warnings_text)
    assert any('tra cứu lịch sử/đối chiếu' in value for value in warnings_text)
    assert any(legacy.evidence_reference in value
               for value in warnings_text + captions(at))
    # Selecting the REPLACEMENT identifies itself (st.info) and its provenance.
    at.selectbox(key='harv_rule_choice').set_value(replacement).run()
    assert at.exception == []
    infos_text = [item.value for item in at.info]
    assert any('Demo v2 là bản thay thế của legacy v1' in value for value in infos_text)
    assert any('ví dụ tổng hợp BR-05 §4.2/AC-08' in value for value in infos_text)
    assert any(replacement.evidence_reference in value
               for value in infos_text + captions(at))


def test_harvest_screen_renders_ratio_of_masses(published_harvest_scope, contracts_database,
                                                run_app):
    e = published_harvest_scope([(1, '1', '0.2'), (2, '2', '0')], start=date(2020, 2, 1),
                                end=date(2020, 2, 4))
    at = open_harvest(run_app, contracts_database)
    assert metric(at, 'A observed') == '3,00 kg/m²'
    assert metric(at, 'B observed') == '0,20 kg/m²'
    assert metric(at, 'Tổng paired A+B') == '3,20 kg/m²'
    assert metric(at, 'Share B (paired)') == '6,25 %'
    assert metric(at, 'Paired A (kg/m²)') == '3,00' and metric(at, 'Paired B (kg/m²)') == '0,20'
    assert metric(at, 'Share A (paired)') == '93,75 %'
    assert any('đếm chất lượng do service trả từ DTO' in value for value in captions(at))
    assert any('not_configured' in value for value in markdowns(at))
    assert any('origin 2020-02-01' in value for value in markdowns(at))
    assert any('records cửa sổ: 2' in value for value in markdowns(at))
    assert any('known_zero_harvest_day' in str(item.value) for item in at.dataframe)
    assert len(at.download_button) == 5  # records/days/weekly/summary JSON/excluded


def test_screen_navigation_keeps_each_session_independent(published_harvest_scope,
                                                          contracts_database, run_app):
    e = published_harvest_scope([(1, '1', '0.2'), (2, '2', '0')], start=date(2020, 2, 1),
                                end=date(2020, 2, 4))
    profile = demo_profile(e.connection, e.scope, e.comp)  # registered BEFORE the UI opens
    at = run_app(contracts_database)
    assert metric(at, 'Irrigation observed') == '— L/m²'  # Production-only scope: no water facts
    at.radio(key='screen_choice').set_value(HARVEST_SCREEN).run()
    assert at.exception == []
    assert metric(at, 'A observed') == '3,00 kg/m²'
    at.selectbox(key='harv_rule_choice').set_value(profile).run()
    assert any(profile.rule_version in value for value in markdowns(at))
    at.radio(key='screen_choice').set_value(WATER_SCREEN).run()
    assert at.exception == []
    assert len(at.download_button) == 3  # water exports unchanged by the harvest screen
    assert any('not_configured' in value for value in markdowns(at))


def test_harvest_incomplete_window_is_neutral(published_harvest_scope, contracts_database,
                                              run_app):
    e = published_harvest_scope([(1, '1', '0.2')], start=date(2020, 2, 1), end=date(2020, 2, 4))
    at = open_harvest(run_app, contracts_database)
    at.date_input(key='harv_window').set_value((date(2020, 2, 2),)).run()
    assert at.exception == []
    assert any('chưa đủ hai đầu' in item.value for item in at.info)
    assert at.metric == []


def test_narrow_harvest_window_keeps_cycle_cumulative(published_harvest_scope, contracts_database,
                                                      run_app):
    e = published_harvest_scope([(1, '1', '0.2'), (2, '2', '0')], start=date(2020, 2, 1),
                                end=date(2020, 2, 5))
    at = open_harvest(run_app, contracts_database)
    at.date_input(key='harv_window').set_value((date(2020, 2, 4), date(2020, 2, 4))).run()
    assert at.exception == []
    values = markdowns(at)
    assert any('lũy kế từ đầu vụ: origin 2020-02-01 · A 3,00' in value for value in values)
    assert any('cumulative_from_cycle_start' in value for value in captions(at))


def test_incomplete_compartment_shows_warning_but_no_ranking(published_harvest_scope,
                                                             contracts_database, run_app):
    e = published_harvest_scope([(1, '1', '0.2'), (2, '2', '')], start=date(2020, 2, 1),
                                end=date(2020, 2, 4))
    at = open_harvest(run_app, contracts_database)
    assert metric(at, 'A observed') == '3,00 kg/m²'
    assert metric(at, 'B observed') == '0,20 kg/m²'
    assert metric(at, 'Share B (paired)') == '16,67 %'  # paired subset (A=1, B=0.2)
    assert any('chưa hoàn chỉnh' in value for value in markdowns(at))
    assert any('records_without_paired_measures' in value for value in captions(at))
    assert any('KHÔNG THAM GIA xếp hạng' in value for value in markdowns(at))


def test_rule_demo_marks_only_the_over_threshold_record(published_harvest_scope,
                                                        contracts_database, run_app):
    # Monday 2020-02-03 start keeps both events in ONE ISO week, so the record
    # counter (1 outside) and the week verdict (own 6.25% → within) separate.
    e = published_harvest_scope([(0, '1', '0.2'), (1, '2', '0')], start=date(2020, 2, 3),
                                end=date(2020, 2, 6))
    profile = demo_profile(e.connection, e.scope, e.comp)
    at = open_harvest(run_app, contracts_database)
    at.selectbox(key='harv_rule_choice').set_value(profile).run()
    assert at.exception == []
    values = markdowns(at)
    assert any(profile.rule_version in value and 'demo_rule' in value for value in values)
    assert any('bằng ngưỡng là trong dải' in value for value in values)
    # Record grain (default): the record counters stay record-grain facts while
    # the window verdict is the window's own share 6.25% → 'trong dải'.
    assert any('kết luận cửa sổ: <b>trong dải</b>' in value for value in values)
    assert any('1 lần thu ngoài ngưỡng' in value for value in values)
    assert any('evidence: fixture demo threshold' in value for value in values)
    assert not any('chưa chọn profile' in value for value in values)
    # Plum day grain: each day carries its own verdict (16.67% day vs 0% day);
    # the radio itself reports the ACTUAL selected grain.
    at.radio(key='harv_rule_grain').set_value('day').run()
    assert at.exception == []
    assert at.radio(key='harv_rule_grain').value == 'day'
    values = markdowns(at)
    assert any('kết luận grain ngày: 1 ngày ngoài ngưỡng' in value for value in values)
    assert any('1 ngày trong dải' in value and '1 ngày không xác định' in value
               for value in values)  # 2020-02-03 0% within; no-event 2020-02-01
    # Week grain: that single ISO week's OWN share (6.25%) decides — within —
    # while the over-threshold record stays exactly a record counter.
    at.radio(key='harv_rule_grain').set_value('week').run()
    assert at.exception == []
    assert at.radio(key='harv_rule_grain').value == 'week'
    values = markdowns(at)
    assert any('kết luận grain tuần: 0 tuần ngoài ngưỡng' in value for value in values)
    assert any('1 tuần trong dải' in value for value in values)
    assert any('0 tuần không xác định' in value for value in values)
    # Export buttons remain exact DTO parity per grain (5 harvest exports).
    assert len(at.download_button) == 5


def test_rule_change_rebinds_record_detail(published_harvest_scope, contracts_database, run_app):
    """R1: the record detail/lineage must resolve the CURRENT action's DTO by
    stable identity — changing/removing the selected profile updates the
    record's own rule caption (transitions None → 10% → 50% → None). The
    third record (A 2 / B 1 → 33.33…%) is outside at 10 and inside at 50."""
    e = published_harvest_scope([(0, '1', '0.2'), (1, '2', '0'), (2, '2', '1')],
                                start=date(2020, 2, 3), end=date(2020, 2, 6))
    strict = demo_profile(e.connection, e.scope, e.comp, '10')
    loose = demo_profile(e.connection, e.scope, e.comp, '50')
    at = open_harvest(run_app, contracts_database)
    # Select the NON-FIRST record explicitly via its formatted label.
    third_label = at.selectbox(key='harv_record_choice').options[2]
    at.selectbox(key='harv_record_choice').set_value(third_label).run()
    assert at.exception == []
    assert chosen_record_date(at) == date(2020, 2, 5)
    captions_now = [value for value in captions(at)]
    assert any('Rule: not_configured' in value for value in captions_now)
    at.selectbox(key='harv_rule_choice').set_value(strict).run()
    assert at.exception == []
    values = markdowns(at) + captions(at)
    assert any(f'Rule: outside_range · {strict.rule_version}' in value for value in values)
    # The third record stays selected through the profile change.
    assert chosen_record_date(at) == date(2020, 2, 5)
    # The DOWNLOADED record CSV carries the current rule on its rows.
    buttons = [b for b in at.download_button if 'CSV lần thu' in b.label]
    assert buttons
    payload = buttons[0].value
    if payload:  # AppTest exposes the download payload; the unit suite pins it otherwise
        assert strict.rule_version in payload.decode('utf-8-sig')
    at.selectbox(key='harv_rule_choice').set_value(loose).run()
    assert at.exception == []
    values = markdowns(at) + captions(at)
    assert any(f'Rule: within_range · {loose.rule_version}' in value for value in values)
    chosen_record_date(at) == date(2020, 2, 5)
    # ...and back to no rule: the detail becomes honestly not_configured again.
    at.selectbox(key='harv_rule_choice').set_value(None).run()
    assert at.exception == []
    assert any('Rule: not_configured' in value for value in captions(at))


def chosen_record_date(at):
    """Widget value keeps the CURRENT DTO after rebind; None when empty."""
    value = at.selectbox(key='harv_record_choice').value
    return value.harvest_date if value is not None else None


def test_out_of_cycle_record_shows_excluded_provenance_and_audit(published_harvest_scope,
                                                                 contracts_database, run_app,
                                                                 tmp_path, monkeypatch):
    e = published_harvest_scope([(1, '1', '0.2'), (700, '0.32', '0.1')], start=date(2020, 2, 1),
                                end=date(2020, 2, 4))
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(e.raw))
    at = open_harvest(run_app, contracts_database)
    assert metric(at, 'A observed') == '1,00 kg/m²'  # 0.32 excluded, never summed
    assert any('chưa hoàn chỉnh' in value for value in markdowns(at))
    assert any('unresolved_quarantine_records' in value for value in captions(at))
    data = [item.value for item in at.dataframe]  # pandas DataFrames, typed cells
    unresolved_rows = [row for frame in data if hasattr(frame, 'to_dict')
                       for row in frame.to_dict(orient='records')]
    assert unresolved_rows, 'unresolved provenance row must render'
    reason_cells = {row.get('Reason') for row in unresolved_rows} | {
        cell for row in unresolved_rows for cell in row.values() if isinstance(cell, str)}
    assert 'out_of_cycle_unresolved' in reason_cells  # typed frozen issue reason
    item = e.service.adapter.harvest_state(e.context).unresolved_items[0]
    assert item.reason_code == 'out_of_cycle_unresolved'
    buttons = [button for button in at.button if 'Đọc & xác minh từ raw' in button.label]
    assert buttons
    buttons[0].click().run()  # the frozen item is authorized for this publication
    assert at.exception == []
    assert any('Raw record xác minh khớp' in item.value for item in at.success)
    assert any('0.32' in str(item.value) for item in at.dataframe)  # exact raw token shown


def test_record_detail_lineage_verifies_raw(published_harvest_scope, contracts_database,
                                            run_app, tmp_path, monkeypatch):
    e = published_harvest_scope([(1, '1', '0.2'), (2, '2', '0')], start=date(2020, 2, 1),
                                end=date(2020, 2, 4))
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(e.raw))
    at = open_harvest(run_app, contracts_database)
    record = at.selectbox(key='harv_record_choice').options[0]
    at.selectbox(key='harv_record_choice').set_value(record).run()
    assert at.exception == []
    assert any('Raw hash xác minh khớp' in item.value for item in at.success)
    assert any("Correction: —" in value for value in markdowns(at))


def test_record_lineage_mount_with_manifest_only_stays_unverified(published_harvest_scope,
                                                                  contracts_database, run_app,
                                                                  tmp_path, monkeypatch):
    import json
    e = published_harvest_scope([(1, '1', '0.2')], start=date(2020, 2, 1), end=date(2020, 2, 4))
    raw_root = tmp_path / 'rawmount'
    snapshot = raw_root / e.snapshot
    snapshot.mkdir(parents=True)
    (snapshot / 'manifest.json').write_text(json.dumps(
        {'snapshot_id': e.snapshot, 'dataset': 'fixture'}))  # manifest declares, CSV absent
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(raw_root))
    at = open_harvest(run_app, contracts_database)
    record = at.selectbox(key='harv_record_choice').options[0]
    at.selectbox(key='harv_record_choice').set_value(record).run()
    assert at.exception == []
    assert any('File raw không có trên mount' in item.value for item in at.warning)


def test_harvest_screen_actionable_without_harvest_bundle(legacy_database, run_app):
    at = run_app(legacy_database)
    assert at.exception == []
    at.radio(key='screen_choice').set_value(HARVEST_SCREEN).run()
    assert at.exception == []
    assert at.error and 'harvest mart' in at.error[0].value
    # The water screen remains the entry for water-only sessions.
    at.radio(key='screen_choice').set_value(WATER_SCREEN).run()
    assert at.error and 'water mart' in at.error[0].value


def test_second_record_zoom_in_ui(published_harvest_scope, contracts_database, run_app):
    """R3 AC-09 parity: the UI narrows to exactly the INCLUDED second record
    (not an empty later day) and keeps the cycle-origin cumulative visible."""
    e = published_harvest_scope([(0, '1', '0.2'), (1, '2', '0')], start=date(2020, 2, 1),
                                end=date(2020, 2, 9))
    at = open_harvest(run_app, contracts_database)
    assert metric(at, 'A observed') == '3,00 kg/m²'
    at.date_input(key='harv_window').set_value((date(2020, 2, 2), date(2020, 2, 2))).run()
    assert at.exception == []
    assert metric(at, 'A observed') == '2,00 kg/m²'
    assert metric(at, 'B observed') == '0,00 kg/m²'
    values = markdowns(at)
    assert any('records cửa sổ: 1' in value for value in values)
    # Cumulative stays cycle-origin: A 3.00 / total 3.20, origin unchanged.
    assert any('lũy kế từ đầu vụ: origin 2020-02-01 · A 3,00 · total 3,20 kg/m²' in value
               for value in values)
    # The single INCLUDED record is the second one; the selectbox option is the
    # formatted label with the exact record shapes.
    included_label = at.selectbox(key='harv_record_choice').options[0]
    assert '2020-02-02' in included_label and 'A 2,00 / B 0,00' in included_label
    at.selectbox(key='harv_record_choice').set_value(included_label).run()
    assert at.exception == []
    assert any('Source record/revision:' in value for value in markdowns(at))


def test_synthetic_failed_file_completeness_shows_unknown(published_harvest_scope,
                                                          contracts_database, run_app,
                                                          monkeypatch):
    """AC-07 seam (honestly labelled): a typed wrapper adapter injects
    HarvestCompleteness(files_failed=1, snapshot_complete=False) so the UI
    shows the incomplete banner and never claims known-zero for no-event
    areas. No sealed or durable state is modified."""
    from greenhouse.app import wiring as wiring_module
    from greenhouse.contracts import HarvestCompleteness
    from greenhouse.queries.harvest import HarvestService
    from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter

    e = published_harvest_scope([(0, '1', '0.2'), (2, '2', '0')], start=date(2020, 2, 1),
                                end=date(2020, 2, 9))
    real = HarvestService(PostgresHarvestQueryAdapter(
        e.connection, raw_snapshots={e.snapshot: e.raw}))
    failed = HarvestCompleteness(
        snapshot_complete=False, source_resolved=False, expected_files=1,
        files_present=0, files_loaded=0, files_failed=1,
        records_read=None, records_accepted=None, records_quarantine=None,
        records_duplicate=None, records_conflict=None,
        unresolved_records=0, conflicted_records=0,
        accepted_records=0, paired_records=0, prod_a_usable_records=0,
        prod_b_usable_records=0, event_a_dates=0, event_b_dates=0,
        reasons=('ui_synthetic_failed_file_fixture',), unresolved_items=())

    class WrapAdapter:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def harvest_state(self, context):
            return failed

    wrapped = HarvestService(WrapAdapter(real.adapter))
    monkeypatch.setattr(wiring_module, 'harvest_service', lambda *args, **kwargs: wrapped)
    at = open_harvest(run_app, contracts_database)
    assert at.exception == []
    assert any('_snapshot chưa hoàn chỉnh' in value for value in markdowns(at))
    assert any('KHÔNG THAM GIA xếp hạng' in value for value in markdowns(at))
    # Nothing anywhere claims a verified known zero under a failed file.
    every = [str(item.value) for item in at.dataframe] + markdowns(at) + captions(at)
    assert not any('known_zero' in text for text in every)
    # Concrete typed service evidence through the SAME injected seam: the
    # no-event day BETWEEN events is NULL/UNKNOWN, never a fabricated zero.
    service_window = replace(e.context, start=date(2020, 2, 5), end=date(2020, 2, 6))
    mid_days = wrapped.get_harvest_days(service_window)
    assert mid_days[0].record_present is False and mid_days[0].prod_a is None
    assert 'unverified_no_harvest_record' in mid_days[0].reason_codes
    # The UI mirrors it honestly: a no-event zoom renders the dash, not 0.
    at.date_input(key='harv_window').set_value((date(2020, 2, 5), date(2020, 2, 6))).run()
    assert at.exception == []
    assert metric(at, 'A observed') == '— kg/m²'
    frames = [item.value for item in at.dataframe]
    weekly_rows = [row for frame in frames if hasattr(frame, 'to_dict')
                   for row in frame.to_dict(orient='records')]
    assert all('no_usable_records' in row.get('Lý do', '')
               for row in weekly_rows if row.get('Trạng thái') == 'Chưa xác định')


def test_cycle_change_resets_harvest_rule_and_window(published_harvest_scope, contracts_database,
                                                     run_app):
    e = published_harvest_scope([(1, '1', '0.2')], start=date(2020, 2, 1), end=date(2020, 2, 4))
    profile = demo_profile(e.connection, e.scope, e.comp)
    second = published_harvest_scope([(1, '5', '1')], start=date(2020, 3, 1), end=date(2020, 3, 4))
    other = [row for row in second.catalog.publications() if row.scope_id == second.scope][0]
    at = open_harvest(run_app, contracts_database)
    first_row = [row for row in e.catalog.publications() if row.scope_id == e.scope][0]
    at.selectbox(key='harv_pub_choice').set_value(first_row).run()  # newest scope is natural default
    at.selectbox(key='harv_rule_choice').set_value(profile).run()
    assert any(profile.rule_version in value for value in markdowns(at))
    at.radio(key='harv_rule_grain').set_value('week').run()
    assert any('kết luận grain tuần' in value for value in markdowns(at))
    at.selectbox(key='harv_pub_choice').set_value(other).run()
    assert at.exception == []
    assert at.selectbox(key='harv_rule_choice').value is None
    assert any('not_configured' in value for value in markdowns(at))
    # Without a selected rule the grain widget is absent (always explicit).
    assert not [widget for widget in at.radio if widget.key == 'harv_rule_grain']
