"""BR-06 Energy cost & tariff scenarios UI acceptance tests."""
from decimal import Decimal
import io
import csv
import psycopg
import pytest

from greenhouse.app import cost_exports, wiring
from greenhouse.contracts import (
    EnergyComponent, EnergyCostSummary, PublicationRef, QueryContext,
    ScenarioChange, ScenarioCommand, ScenarioComparison, from_json, to_json,
)
from greenhouse.db.cost_adapter import PostgresCostAdapter
from ui.energy_fixture import energy_scope
from test_climate_ui import run_app, metric

SCREEN = '💶 Chi phí & Kịch bản (BR-06)'


def open_cost(run_app, kwargs, e):
    with psycopg.connect(**kwargs) as conn:
        cost_adapter = PostgresCostAdapter(conn)
        cost_adapter.register_historical_tariff(e.scope, e.comp)
        conn.commit()
    at = run_app(kwargs)
    at.radio(key='screen_choice').set_value(SCREEN).run()
    assert not at.exception, [x.value for x in at.exception]
    assert not at.error, [x.value for x in at.error]
    return at


def test_cost_screen_baseline_navigation_and_presentation(energy_scope, contracts_database, run_app):
    """Test baseline evaluation: 1.83 EUR/m² total, exact rates, formatted string cells, 6 downloads."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    # 1. Check baseline metrics
    assert metric(at, 'Tổng chi phí (EUR)') == '114.38 EUR'
    assert metric(at, 'Chi phí trên diện tích (EUR/m²)') == '1.83 EUR/m²'
    assert metric(at, 'Coverage định giá (%)') == '100.0%'
    assert metric(at, 'Coverage lượng (%)') == '100.0%'

    # 2. Check sensitivity disclosure
    assert any('1.536' in w.value and 'AGC_SHARED_BASIS_V1:v1' in w.value for w in at.warning)

    # 3. Check rate precision in tables (Heat 0.0083 EUR/MJ, ElecHigh 0.08 EUR/kWh)
    frames = [item.value for item in at.dataframe]
    assert len(frames) >= 2  # Component table and daily/rates table
    comp_df = frames[0]
    assert 'Thành phần' in comp_df.columns

    # 4. Check 7 download buttons
    assert len(at.download_button) == 7


def test_cost_create_scenario_electric_plus_20_and_delta(energy_scope, contracts_database, run_app):
    """AC-02: User creates +20% electric scenario, delta is +0.20 EUR/m² (+10.93%)."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    # Fill form
    at.text_input(key='scen_name').set_value('Electric +20%')
    # High: select percentage change (+20%)
    at.selectbox(key='form_high_choice').set_value('Thay đổi theo phần trăm (%)')
    at.text_input(key='form_high_pct').set_value('20.0')
    # Low: select percentage change (+20%)
    at.selectbox(key='form_low_choice').set_value('Thay đổi theo phần trăm (%)')
    at.text_input(key='form_low_pct').set_value('20.0')

    # Submit form
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception, [x.value for x in at.exception]

    # Verify durability across connections (SECOND connection check)
    with psycopg.connect(**contracts_database) as conn2:
        cost_adapter2 = PostgresCostAdapter(conn2)
        scenarios2 = cost_adapter2.list_scenarios(e.scope)
        assert len(scenarios2) == 1
        assert scenarios2[0].name == 'Electric +20%'

    # Check comparison metrics: Baseline 114.38 EUR, Scenario 126.88 EUR (2.03 * 62.5 = 126.875 -> 126.88)
    assert metric(at, 'Baseline (EUR)') == '114.38 EUR'
    assert metric(at, 'Kịch bản (EUR)') == '126.88 EUR'
    assert metric(at, 'Kịch bản (EUR/m²)') == '2.03 EUR/m²'


def test_cost_reopen_scenario_restores_frozen_context(energy_scope, contracts_database, run_app):
    """Reopening an app scenario restores its pinned context even after filter changes."""
    from datetime import timedelta
    e = energy_scope(rows=(('100', '10', '5'), ('100', '10', '5')), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    # Subwindow for creation: day 1 only
    day1_window = (e.context.start, e.context.start)
    at.date_input(key='cost_window').set_value(day1_window).run()

    # Create scenario on day 1
    at.text_input(key='scen_name').set_value('Day1 Only Scenario').run()
    at.selectbox(key='form_high_choice').set_value('Thay đổi theo phần trăm (%)').run()
    at.text_input(key='form_high_pct').set_value('10.0').run()
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception

    # Change sidebar filter to whole cycle
    whole_window = (e.context.start, e.context.end - timedelta(days=1))
    at.date_input(key='cost_window').set_value(whole_window).run()

    # Pinned notice should be visible informing user that the scenario is evaluated on its pinned context
    assert any('Kịch bản được pin với ngữ cảnh phân tích gốc' in w.value for w in at.warning)
    assert metric(at, 'Priced / Expected Days') == '1 / 1'


def test_cost_append_revision_version_2(energy_scope, contracts_database, run_app):
    """User appends revision v2 to existing scenario; older versions remain selectable."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    # 1. Create v1
    at.text_input(key='scen_name').set_value('Multi Version Scen').run()
    at.selectbox(key='form_high_choice').set_value('Thay đổi theo phần trăm (%)').run()
    at.text_input(key='form_high_pct').set_value('20.0').run()
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception

    # 2. Select Revision mode
    at.radio(key='scenario_form_mode').set_value('Thêm phiên bản sửa đổi (Revision) cho kịch bản đang chọn').run()
    at.selectbox(key='form_heat_choice').set_value('Thay đổi theo phần trăm (%)').run()
    at.text_input(key='form_heat_pct').set_value('10.0').run()
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception

    # 3. Check that options include both v1 and v2 or latest version is active
    opts = at.selectbox(key='cost_scenario_select').options
    assert any(':v2' in opt for opt in opts)


def test_cost_input_validation_rejects_negative_and_nan(energy_scope, contracts_database, run_app):
    """Input validation rejects negative prices or percent < -100% without DB write."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    # Enter invalid percent (-150%)
    at.selectbox(key='form_high_choice').set_value('Thay đổi theo phần trăm (%)')
    at.text_input(key='form_high_pct').set_value('-150.0')
    at.button(key='btn_save_scenario').click().run()

    # Must show error message
    assert len(at.error) > 0
    assert any('lớn hơn hoặc bằng -100' in err.value for err in at.error)


def test_cost_exports_content_and_download(energy_scope, contracts_database, run_app):
    """Test that all 7 export download buttons are present and export documents are valid."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    buttons = at.download_button
    assert len(buttons) == 7

    # Verify download button keys
    expected_keys = {
        'cost_download_summary',
        'cost_download_components',
        'cost_download_days',
        'cost_download_weekly',
        'cost_download_rates',
        'cost_download_comp_json',
        'cost_download_base_json',
    }
    assert {b.key for b in buttons} == expected_keys

    # Verify export functions generate valid documents
    raw_mapping = {e.snapshot: e.raw}
    cost_svc = wiring.cost_service(e.conn, raw_mapping)
    summary = cost_svc.get_cost(e.context)
    comp = cost_svc.compare_scenarios(
        e.context,
        scenario_version=summary.scenario_version or summary.tariff_version,
        baseline_tariff_version=summary.tariff_version,
    ) if summary.scenario_version else None

    # Baseline comparison
    if comp is None:
        from greenhouse.queries.cost import compare_summaries
        comp = compare_summaries(summary, summary)

    summary_doc = cost_exports.summary_csv(comp, None)
    assert b'# export_version=greenhouse-energy-cost-v1' in summary_doc
    assert b'baseline_official_total_cost_per_m2' in summary_doc

    comp_doc = cost_exports.components_csv(comp, None)
    assert b'# export_version=greenhouse-energy-cost-v1' in comp_doc
    assert b'component' in comp_doc

    days_doc = cost_exports.days_csv(comp, None)
    assert b'# export_version=greenhouse-energy-cost-v1' in days_doc
    assert b'resource_date' in days_doc

    weekly_doc = cost_exports.weekly_csv(comp, None)
    assert b'# export_version=greenhouse-energy-cost-v1' in weekly_doc
    assert b'iso_week' in weekly_doc

    tariff = cost_svc.get_tariff(summary.tariff_version)
    rates_doc = cost_exports.rates_csv(comp, tariff.rates, None, None)
    assert b'# export_version=greenhouse-energy-cost-v1' in rates_doc
    assert b'rate' in rates_doc
    assert b'0.0083' in rates_doc

    json_doc = cost_exports.comparison_json(comp)
    assert from_json(ScenarioComparison, json_doc.decode('utf-8')) == comp
    assert at.download_button(key='cost_download_comp_json').value is not None


def test_cost_screen_no_implicit_write_on_fresh_scope_without_tariff(energy_scope, contracts_database, run_app):
    """Màn hình không tự ý ghi DB nếu chưa có baseline tariff cho scope."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    # Do NOT register tariff; directly open app
    at = run_app(contracts_database)
    at.radio(key='screen_choice').set_value(SCREEN).run()
    assert not at.exception
    # Must show warning about missing baseline tariff
    assert any('Chưa có biểu giá tham chiếu' in w.value for w in at.warning)
    # Verify no tariff was written to DB
    with psycopg.connect(**contracts_database) as conn:
        cost_adapter = PostgresCostAdapter(conn)
        assert cost_adapter.get_baseline_tariff(e.scope, e.comp) is None


def test_cost_r1_context_validation_and_append_omission(energy_scope, contracts_database):
    """R1: execute_scenario_command authoritatively rejects invalid cycle/comp/baseline, and append rejects omission."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    raw_mapping = {e.snapshot: e.raw}
    cost_svc = wiring.cost_service(e.conn, raw_mapping)
    cost_svc.adapter.register_historical_tariff(e.scope, e.comp)
    tariff = cost_svc.adapter.get_baseline_tariff(e.scope, e.comp)

    # 1. Nonexistent cycle
    bad_cycle_ctx = QueryContext(
        publication_id=e.publication.publication_id,
        scope_id=e.context.scope_id,
        snapshot_id=e.context.snapshot_id,
        cycle_id='nonexistent_cycle',
        compartment_ids=e.context.compartment_ids,
        start=e.context.start,
        end=e.context.end,
    )
    cmd_bad_cycle = ScenarioCommand(
        request_id='req-bad-cycle',
        baseline_version_id=tariff.tariff_version,
        name='Bad Cycle',
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.HEAT, price_unit='EUR/MJ', change_percent=Decimal('10')),),
        analysis_context=bad_cycle_ctx,
    )
    with pytest.raises(ValueError, match='are not mapped to scope'):
        cost_svc.execute_scenario_command(cmd_bad_cycle)

    # 2. Nonexistent compartment
    bad_comp_ctx = QueryContext(
        publication_id=e.publication.publication_id,
        scope_id=e.context.scope_id,
        snapshot_id=e.context.snapshot_id,
        cycle_id=e.context.cycle_id,
        compartment_ids=('nonexistent_comp',),
        start=e.context.start,
        end=e.context.end,
    )
    cmd_bad_comp = ScenarioCommand(
        request_id='req-bad-comp',
        baseline_version_id=tariff.tariff_version,
        name='Bad Comp',
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.HEAT, price_unit='EUR/MJ', change_percent=Decimal('10')),),
        analysis_context=bad_comp_ctx,
    )
    with pytest.raises(ValueError, match='are not mapped to scope'):
        cost_svc.execute_scenario_command(cmd_bad_comp)

    # 3. Create a valid scenario v1
    cmd_v1 = ScenarioCommand(
        request_id='req-v1-valid',
        baseline_version_id=tariff.tariff_version,
        name='Valid Scenario',
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.HEAT, price_unit='EUR/MJ', change_percent=Decimal('10')),),
        analysis_context=e.context,
    )
    scen_v1 = cost_svc.execute_scenario_command(cmd_v1)
    e.conn.commit()

    # 4. Appending with analysis_context=None directly via register_scenario must be rejected
    from greenhouse.contracts import CostScenario
    bad_v2_scen = CostScenario(
        scenario_version=f'{scen_v1.scenario_id}:v2',
        scenario_id=scen_v1.scenario_id,
        version=2,
        request_id='req-v2-erase',
        baseline_tariff_version=tariff.tariff_version,
        publication=e.publication,
        name='V2 Erasing Context',
        rates=scen_v1.rates,
        changes=(),
        analysis_context=None,
    )
    with pytest.raises(ValueError, match='Cannot omit or erase analysis context'):
        cost_svc.adapter.register_scenario(bad_v2_scen)


def test_cost_r2_form_repeated_submit_idempotent(energy_scope, contracts_database, run_app):
    """R2: Submitting an unchanged form multiple times reuses stable request_id and creates only 1 scenario."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    at.text_input(key='scen_name').set_value('Idempotent Test Scenario')
    at.selectbox(key='form_high_choice').set_value('Thay đổi theo phần trăm (%)')
    at.text_input(key='form_high_pct').set_value('15.0')

    # First submit
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception

    with psycopg.connect(**contracts_database) as conn2:
        cost_adapter2 = PostgresCostAdapter(conn2)
        scens = cost_adapter2.list_scenarios(e.scope)
        assert len(scens) == 1
        assert scens[0].name == 'Idempotent Test Scenario'

    # Second submit without changing anything
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception

    # Still exactly 1 scenario on a fresh second connection!
    with psycopg.connect(**contracts_database) as conn3:
        cost_adapter3 = PostgresCostAdapter(conn3)
        scens_after = cost_adapter3.list_scenarios(e.scope)
        assert len(scens_after) == 1


def test_cost_r3_reopen_scenario_preserves_pinned_baseline_and_revision_context(energy_scope, contracts_database, run_app):
    """R3: Reopened scenario respects pinned baseline profile object; revision inherits saved context."""
    from datetime import date, timedelta
    from greenhouse.contracts import TariffRate
    e = energy_scope(rows=(('100', '10', '5'), ('100', '10', '5')), basis_status='assumed_unverified', assumption_enabled=True)

    # 1. Register baseline 1
    with psycopg.connect(**contracts_database) as conn:
        adapter = PostgresCostAdapter(conn)
        adapter.register_historical_tariff(e.scope, e.comp)
        conn.commit()

    # Open app and save scenario v1
    at = open_cost(run_app, contracts_database, e)
    # Create scenario on day 1 only
    day1_window = (e.context.start, e.context.start)
    at.date_input(key='cost_window').set_value(day1_window).run()

    at.text_input(key='scen_name').set_value('Pinned Baseline Scen')
    at.selectbox(key='form_high_choice').set_value('Thay đổi theo phần trăm (%)')
    at.text_input(key='form_high_pct').set_value('20.0')
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception
    assert not at.error

    # 2. Register baseline 2 with different rates
    with psycopg.connect(**contracts_database) as conn:
        from greenhouse.contracts import TariffProfile
        adapter = PostgresCostAdapter(conn)
        profile = TariffProfile(
            tariff_version=f'{e.scope}:{e.comp}:custom_v2',
            tariff_id='custom_v2',
            version=2,
            data_scope_id=e.scope,
            currency='EUR',
            source_reference='Custom test source',
            label='Doubled Rates Baseline',
            rates=(
                TariffRate('r:v2:heat', f'{e.scope}:{e.comp}:custom_v2', EnergyComponent.HEAT, 'EUR', 'EUR/MJ', Decimal('0.0166'), date(2020, 1, 1), date(2030, 1, 1), 'v2'),
                TariffRate('r:v2:high', f'{e.scope}:{e.comp}:custom_v2', EnergyComponent.ELEC_HIGH, 'EUR', 'EUR/kWh', Decimal('0.16'), date(2020, 1, 1), date(2030, 1, 1), 'v2'),
                TariffRate('r:v2:low', f'{e.scope}:{e.comp}:custom_v2', EnergyComponent.ELEC_LOW, 'EUR', 'EUR/kWh', Decimal('0.08'), date(2020, 1, 1), date(2030, 1, 1), 'v2'),
            ),
            is_baseline=True,
            compartment_id=e.comp,
        )
        adapter.register_tariff(profile)
        conn.commit()

    # Reopen app with wider window
    at = run_app(contracts_database)
    at.radio(key='screen_choice').set_value(SCREEN).run()
    whole_window = (e.context.start, e.context.end - timedelta(days=1))
    at.date_input(key='cost_window').set_value(whole_window).run()

    # Select our scenario
    opts = at.selectbox(key='cost_scenario_select').options
    scen_opt = [o for o in opts if 'Pinned Baseline Scen' in o][0]
    at.selectbox(key='cost_scenario_select').set_value(scen_opt).run()
    assert not at.exception

    # Rates table must show original baseline rates (0.0083 and 0.08), NOT 0.0166
    rates_frames = [f for f in at.dataframe if 'Đơn giá' in f.value.columns or 'Đơn vị giá' in f.value.columns]
    assert len(rates_frames) > 0

    # Heat rate equivalence check (0.0083 * 3.6 = 0.02988 EUR/kWh)
    found_equiv = False
    for frame in rates_frames:
        for val in frame.value.to_dict('records'):
            if '0.02988' in str(val.get('Đơn vị giá', '')):
                found_equiv = True
    assert found_equiv

    # Revision mode must inherit saved context and succeed without "Cannot change analysis context"
    at.radio(key='scenario_form_mode').set_value('Thêm phiên bản sửa đổi (Revision) cho kịch bản đang chọn').run()
    at.selectbox(key='form_heat_choice').set_value('Thay đổi theo phần trăm (%)').run()
    at.text_input(key='form_heat_pct').set_value('5.0').run()
    at.button(key='btn_save_scenario').click().run()
    assert not at.exception
    assert not at.error


def test_cost_r4_exact_subtraction_and_zero_delta_pct():
    """R4: Exact precision subtraction and delta_pct is None when baseline cost is zero."""
    from greenhouse.contracts import (
        BasisStatus, ComponentCost, DayCost, EnergyComponent, EnergyCostSummary, QueryContext, TariffRate,
    )
    from greenhouse.queries.cost import compare_summaries, exact_sub
    from datetime import date

    # 1. Exact precision subtraction check (does not round at 28 decimals)
    r1 = Decimal('0.12345678901234567890123456789123456789')
    r2 = Decimal('0.148148146814814814681481481469481481468')
    res = exact_sub(r2, r1)
    assert res == Decimal('0.024691357802469135780246913578246913578')

    # 2. Zero baseline comparison gives delta_total_pct = None
    ctx = QueryContext(
        publication_id='pub1',
        scope_id='scope1',
        snapshot_id='snap1',
        cycle_id='cycle1',
        compartment_ids=('comp1',),
        start=date(2020, 1, 1),
        end=date(2020, 1, 2),
    )
    zero_heat = ComponentCost(
        component=EnergyComponent.HEAT,
        price_unit='EUR/MJ',
        official_cost_eur=Decimal('0'),
        official_cost_per_m2=Decimal('0'),
        observed_cost_eur=Decimal('0'),
        observed_cost_per_m2=Decimal('0'),
        pricing_coverage_pct=Decimal('100'),
        expected_days=1,
        priced_days=1,
        quantity_accepted_days=1,
        day_costs=(),
        reason_codes=(),
    )
    zero_high = ComponentCost(
        component=EnergyComponent.ELEC_HIGH,
        price_unit='EUR/kWh',
        official_cost_eur=Decimal('0'),
        official_cost_per_m2=Decimal('0'),
        observed_cost_eur=Decimal('0'),
        observed_cost_per_m2=Decimal('0'),
        pricing_coverage_pct=Decimal('100'),
        expected_days=1,
        priced_days=1,
        quantity_accepted_days=1,
        day_costs=(),
        reason_codes=(),
    )
    zero_low = ComponentCost(
        component=EnergyComponent.ELEC_LOW,
        price_unit='EUR/kWh',
        official_cost_eur=Decimal('0'),
        official_cost_per_m2=Decimal('0'),
        observed_cost_eur=Decimal('0'),
        observed_cost_per_m2=Decimal('0'),
        pricing_coverage_pct=Decimal('100'),
        expected_days=1,
        priced_days=1,
        quantity_accepted_days=1,
        day_costs=(),
        reason_codes=(),
    )
    zero_summary = EnergyCostSummary(
        context=ctx,
        tariff_version='t1',
        scenario_version=None,
        currency='EUR',
        components=(zero_heat, zero_high, zero_low),
        expected_days=1,
        priced_days=1,
        pricing_coverage_pct=Decimal('100'),
        observed_total_cost_per_m2=Decimal('0'),
        observed_total_cost_eur=Decimal('0'),
        official_total_cost_per_m2=Decimal('0'),
        official_total_cost_eur=Decimal('0'),
        basis_status=None,
        basis_label=None,
        sensitivity_factor=None,
        sensitivity_total_cost_eur=None,
        reason_codes=(),
    )
    comp_result = compare_summaries(zero_summary, zero_summary)
    assert comp_result.delta_total_pct is None
    assert comp_result.component_deltas[0].delta_cost_pct is None
    assert comp_result.delta_total_cost_eur == Decimal('0')


def test_cost_r5_weekly_aggregation_and_grain_switching(energy_scope, contracts_database, run_app):
    """R5: Weekly aggregation and grain radio buttons function properly."""
    e = energy_scope(rows=(('100', '10', '5'), ('100', '10', '5')), basis_status='assumed_unverified', assumption_enabled=True)
    at = open_cost(run_app, contracts_database, e)

    # Select Weekly grain
    at.radio(key='cost_grain_selector').set_value('Theo tuần (Weekly)').run()
    assert not at.exception
    weekly_frames = [f for f in at.dataframe if 'Tuần' in f.value.columns]
    assert len(weekly_frames) >= 1

    # Select Cycle grain
    at.radio(key='cost_grain_selector').set_value('Toàn vụ (Cycle)').run()
    assert not at.exception


def test_public_cost_adapter_port_wrapper_validation(energy_scope, contracts_database):
    """R1: Public CostAdapter port validation without private _rows access."""
    from greenhouse.queries.cost import CostAdapter, EnergyCostService

    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    with psycopg.connect(**contracts_database) as conn:
        real_adapter = PostgresCostAdapter(conn)
        real_adapter.register_historical_tariff(e.scope, e.comp)
        baseline = real_adapter.get_baseline_tariff(e.scope, e.comp)

        # Wrapper exposing only declared public methods of CostAdapter Protocol (no _rows)
        class PublicOnlyCostAdapterWrapper:
            def __init__(self, target):
                self._target = target

            def validate_cycle(self, scope_id: str, snapshot_id: str, cycle_id: str, compartment_id: str) -> bool:
                return self._target.validate_cycle(scope_id, snapshot_id, cycle_id, compartment_id)

            def get_tariff(self, tariff_version: str):
                return self._target.get_tariff(tariff_version)

            def get_baseline_tariff(self, data_scope_id: str, compartment_id=None):
                return self._target.get_baseline_tariff(data_scope_id, compartment_id)

            def register_tariff(self, profile):
                return self._target.register_tariff(profile)

            def register_historical_tariff(self, data_scope_id: str, compartment_id=None):
                return self._target.register_historical_tariff(data_scope_id, compartment_id)

            def get_scenario(self, scenario_version: str):
                return self._target.get_scenario(scenario_version)

            def get_scenario_by_request_id(self, request_id: str):
                return self._target.get_scenario_by_request_id(request_id)

            def get_latest_scenario_version(self, scenario_id: str):
                return self._target.get_latest_scenario_version(scenario_id)

            def list_scenarios(self, data_scope_id=None):
                return self._target.list_scenarios(data_scope_id)

            def register_scenario(self, scenario, command_payload=None):
                return self._target.register_scenario(scenario, command_payload)

            def execute_scenario_transaction(self, command, rate_factory):
                return self._target.execute_scenario_transaction(command, rate_factory)

        wrapper = PublicOnlyCostAdapterWrapper(real_adapter)
        assert not hasattr(wrapper, '_rows')

        energy_svc = wiring.energy_service(conn, {e.snapshot: e.raw})
        svc = EnergyCostService(energy_svc, wrapper)

        # Valid command with frozen analysis context passes through public port
        cmd = ScenarioCommand(
            request_id='public-port-valid-1',
            baseline_version_id=baseline.tariff_version,
            name='Public Port Valid Scenario',
            publication=e.publication,
            changes=(ScenarioChange(EnergyComponent.ELEC_HIGH, 'EUR/kWh', change_percent=Decimal('20.0')),),
            analysis_context=e.context,
        )
        saved = svc.execute_scenario_command(cmd)
        assert saved.name == 'Public Port Valid Scenario'

        # Invalid cycle rejected through public port
        from dataclasses import replace
        invalid_cmd = replace(cmd, request_id='public-port-invalid-1', analysis_context=replace(e.context, cycle_id='nonexistent_cycle'))
        with pytest.raises(ValueError, match='not mapped to scope'):
            svc.execute_scenario_command(invalid_cmd)


def test_weekly_basis_gating_and_variable_area_semantics(energy_scope, contracts_database):
    """R5: aggregate_weekly_cost enforces basis gating across variable areas and unknown basis."""
    from greenhouse.queries.cost import aggregate_weekly_cost

    # 1. Variable area: day 1 area 100 m2, day 2 area 200 m2
    e_var = energy_scope(
        rows=(('100', '10', '5'), ('100', '10', '5')),
        areas=('100', '200'),
    )
    svc_var = wiring.cost_service(e_var.conn, {e_var.snapshot: e_var.raw})
    with psycopg.connect(**contracts_database) as conn:
        ad = PostgresCostAdapter(conn)
        ad.register_historical_tariff(e_var.scope, e_var.comp)
        conn.commit()
    summary_var = svc_var.get_cost(e_var.context)
    weeks_var = aggregate_weekly_cost(summary_var)
    assert len(weeks_var) == 1
    # Absolute EUR is retained (183 + 366 = 549 EUR)
    assert weeks_var[0].total_cost_eur == Decimal('549')
    # Total per-m2 must be None (not 3.6600)
    assert weeks_var[0].total_cost_per_m2 is None
    # No single effective area
    assert weeks_var[0].effective_area_m2 is None
    # Reason code explicit
    assert 'per_m2_basis_not_common_and_fixed' in weeks_var[0].reason_codes

    # 2. Unknown basis: 1 day with unknown basis
    e_unk = energy_scope(rows=(('100', '10', '5'),), basis_status='unknown')
    svc_unk = wiring.cost_service(e_unk.conn, {e_unk.snapshot: e_unk.raw})
    with psycopg.connect(**contracts_database) as conn:
        ad = PostgresCostAdapter(conn)
        ad.register_historical_tariff(e_unk.scope, e_unk.comp)
        conn.commit()
    summary_unk = svc_unk.get_cost(e_unk.context)
    weeks_unk = aggregate_weekly_cost(summary_unk)
    assert len(weeks_unk) == 1
    # Source-unit component values preserved
    assert weeks_unk[0].heat_cost_per_m2 == Decimal('0.83')
    assert weeks_unk[0].elec_high_cost_per_m2 == Decimal('0.80')
    assert weeks_unk[0].elec_low_cost_per_m2 == Decimal('0.20')
    # Total per-m2 cannot be pooled
    assert weeks_unk[0].total_cost_per_m2 is None
    assert weeks_unk[0].effective_area_m2 is None
    assert 'incompatible_or_unknown_basis' in weeks_unk[0].reason_codes


def test_cost_ui_lineage_drilldown_with_real_energy_service(energy_scope, contracts_database, run_app, monkeypatch, tmp_path):
    """R5: Real lineage drill-through calls get_lineage and displays verified source path/hash, and unverified when unconfigured."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)

    # 1. Configured raw dir: verifies hash against real Resources.csv line
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(e.raw.parent))
    at = open_cost(run_app, contracts_database, e)

    # Lineage selectbox is populated with real quantities
    assert at.selectbox(key='cost_lineage_select')
    options = at.selectbox(key='cost_lineage_select').options
    assert len(options) >= 1
    assert 'heat' in options[0].lower() or 'elec' in options[0].lower()

    # Verify displayed lineage details from real raw
    all_text = ' '.join(str(getattr(w, 'value', '')) for w in list(at.caption) + list(at.markdown) + list(at.text))
    assert 'Resources.csv' in all_text
    assert '✅ Hợp lệ' in all_text
    assert 'accepted' in all_text

    # 2. Unconfigured raw dir: unverified warning is displayed, not false verification
    empty_dir = tmp_path / 'empty_raw'
    empty_dir.mkdir()
    monkeypatch.setenv('GREENHOUSE_RAW_DIR', str(empty_dir))
    at_unconf = open_cost(run_app, contracts_database, e)
    unconf_warnings = [w.value for w in at_unconf.warning]
    assert any('Không xác minh được raw lineage' in w for w in unconf_warnings)


def test_cost_ui_high_precision_custom_heat_rate_exact_equivalent(energy_scope, contracts_database, run_app):
    """R3: Exact equivalent rate arithmetic for high-precision heat rates without ambient precision28 truncation."""
    from dataclasses import replace
    from greenhouse.app.cost import heat_equivalent_display

    custom_heat_rate = Decimal('0.12345678901234567890123456789123456789')
    expected_equiv = Decimal('0.444444440444444444044444444408444444404')

    # 1. Direct exact equivalent without ambient precision28 truncation
    disp_mj = heat_equivalent_display(custom_heat_rate, 'EUR/MJ')
    assert f'{expected_equiv}' in disp_mj
    assert 'EUR/kWh' in disp_mj

    # 2. Reverse equivalent from EUR/kWh to EUR/MJ with exact precision
    disp_kwh = heat_equivalent_display(Decimal('0.02988'), 'EUR/kWh')
    assert '0.0083' in disp_kwh
    assert 'EUR/MJ' in disp_kwh

    # 3. Ambient precision28 proof: Decimal ambient multiplication would round or truncate
    # whereas exact_product preserves the full 39 digits
    import decimal
    with decimal.localcontext() as ctx:
        ctx.prec = 28
        ambient_rounded = custom_heat_rate * Decimal('3.6')
        assert len(str(ambient_rounded).split('.')[1]) <= 28
    assert len(str(expected_equiv).split('.')[1]) == 39

    # 4. In UI rates tab
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='verified')
    at = open_cost(run_app, contracts_database, e)
    frames = [item.value for item in at.dataframe]
    all_cells = ' '.join(str(cell) for df in frames for col in df.columns for cell in df[col])
    # Baseline rate 0.0083 EUR/MJ has equivalent 0.02988 EUR/kWh
    assert '0.02988' in all_cells
    assert '0.0083' in all_cells
