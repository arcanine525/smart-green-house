"""BR-06 energy cost and tariff scenarios acceptance tests (T20).

Covers AC-01 through AC-11:
- AC-01: Baseline Reference Evaluation and Area Scaling
- AC-02: Scenario Rate Changes, Deltas and Comparison
- AC-03: Heat Rate Unit Conversion Equivalence (0.02988 EUR/kWh == 0.0083 EUR/MJ)
- AC-04: Temporal Rate Weighting
- AC-05: Temporal Exclusion & Overlap Rejection
- AC-06: Rate Gaps and Missing Quantities
- AC-07: Zero Quantities and Zero Baseline Handling
- AC-08: Unknown Basis Pooling Gate
- AC-09: Input Validation
- AC-10: Scenario Command Idempotency and Conflict Detection
- AC-11: Immutability Sealing (Rate set sealing + Update/Delete rejection)
- Shared EnergyQuantity Identity
"""
from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, localcontext
import psycopg
import pytest

from greenhouse.app.wiring import cost_service
from greenhouse.queries.cost import aggregate_weekly_cost
from greenhouse.contracts import (
    BasisStatus, ComponentCost, ComponentCostDelta, CostScenario, DayCost,
    EffectiveAreaBasis, EnergyComponent, EnergyCostSummary, EnergyQuantity,
    PublicationRef, QueryContext, ScenarioChange, ScenarioCommand, ScenarioComparison,
    TariffProfile, TariffRate, from_json, to_json,
)
from greenhouse.db.cost_adapter import HISTORICAL_TARIFF_ID, PostgresCostAdapter
from test_energy_mart import contracts_database, energy_scope

D0 = date(2020, 1, 1)


def test_import_preserves_known_scenario_audit_and_rejects_changed_retry(energy_scope):
    from greenhouse.queries.cost import derive_scenario_rates
    e=energy_scope();adapter=PostgresCostAdapter(e.conn)
    baseline=register_baseline_reference(adapter,e.scope)
    sid='import-audit:'+e.scope;version=sid+':v1';name='Imported audit fixture'
    changes=(ScenarioChange(EnergyComponent.ELEC_HIGH,'EUR/kWh',change_percent=Decimal('10')),)
    source=CostScenario(version,sid,1,'request:'+sid,baseline.tariff_version,e.publication,name,
        derive_scenario_rates(baseline,version,name,changes),changes,author='original-author',
        created_at=datetime(2024,1,2,3,4,5,123456,tzinfo=timezone.utc),analysis_context=e.context)
    assert adapter.register_scenario(source)==source
    assert adapter.register_scenario(source)==source
    with pytest.raises(ValueError,match='audit identity'):
        adapter.register_scenario(replace(source,author='changed-author'))
    with pytest.raises(ValueError,match='audit identity'):
        adapter.register_scenario(replace(source,created_at=source.created_at+timedelta(microseconds=1)))


def register_baseline_reference(adapter: PostgresCostAdapter, scope_id: str) -> TariffProfile:
    """Helper registering standard AGC reference rates for a test scope."""
    tariff_version = f'tariff:AGC_2020_REFERENCE_V1:{scope_id}'
    rates = (
        TariffRate(
            rate_id=f'rate:{tariff_version}:heat',
            tariff_version=tariff_version,
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/MJ',
            rate=Decimal('0.0083'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=365),
            source_reference='Economics.pdf page 2',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:elec_high',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_HIGH,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.08'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=365),
            source_reference='Economics.pdf page 1',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:elec_low',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_LOW,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.04'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=365),
            source_reference='Economics.pdf page 2',
        ),
    )
    profile = TariffProfile(
        tariff_version=tariff_version,
        tariff_id=HISTORICAL_TARIFF_ID,
        version=1,
        data_scope_id=scope_id,
        currency='EUR',
        source_reference='Economics.pdf pages 1-2',
        label='AGC 2020 Reference Tariff',
        rates=rates,
        is_baseline=True,
    )
    return adapter.register_tariff(profile)


def test_ac01_baseline_reference_evaluation_and_area_scaling(energy_scope):
    """AC-01: Baseline Reference Evaluation with 100 MJ/m², 10 kWh/m², 5 kWh/m².

    Heat: 100 * 0.0083 = 0.83 EUR/m²
    High: 10 * 0.08 = 0.80 EUR/m²
    Low: 5 * 0.04 = 0.20 EUR/m²
    Total: 1.83 EUR/m²
    Absolute EUR: 1.83 * 62.5 = 114.375 EUR
    Sensitivity (96 m² / 62.5 m² = 1.536): 114.375 * 1.536 = 175.68 EUR
    """
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    register_baseline_reference(svc.adapter, e.scope)

    summary = svc.get_cost(e.context)

    # Component-level assertions
    heat = summary.components[0]
    high = summary.components[1]
    low = summary.components[2]

    assert heat.component == EnergyComponent.HEAT
    assert heat.observed_cost_per_m2 == Decimal('0.83')
    assert heat.official_cost_per_m2 == Decimal('0.83')
    assert heat.observed_cost_eur == Decimal('51.875')
    assert heat.official_cost_eur == Decimal('51.875')

    assert high.component == EnergyComponent.ELEC_HIGH
    assert high.observed_cost_per_m2 == Decimal('0.80')
    assert high.official_cost_per_m2 == Decimal('0.80')
    assert high.observed_cost_eur == Decimal('50.000')
    assert high.official_cost_eur == Decimal('50.000')

    assert low.component == EnergyComponent.ELEC_LOW
    assert low.observed_cost_per_m2 == Decimal('0.20')
    assert low.official_cost_per_m2 == Decimal('0.20')
    assert low.observed_cost_eur == Decimal('12.500')
    assert low.official_cost_eur == Decimal('12.500')

    # Total assertions
    assert summary.official_total_cost_per_m2 == Decimal('1.83')
    assert summary.observed_total_cost_per_m2 == Decimal('1.83')
    assert summary.official_total_cost_eur == Decimal('114.375')
    assert summary.observed_total_cost_eur == Decimal('114.375')

    # Sensitivity check
    assert summary.sensitivity_factor == Decimal('1.536')
    assert summary.sensitivity_total_cost_eur == Decimal('175.68')

    # Serialization roundtrip
    assert from_json(EnergyCostSummary, to_json(summary)) == summary


def test_ac02_scenario_rate_changes_delta_and_comparison(energy_scope):
    """AC-02: Scenario Rate Changes & Delta (+20% on electric high and low).

    High: 0.08 * 1.2 = 0.096 -> 10 * 0.096 = 0.96 EUR/m²
    Low: 0.04 * 1.2 = 0.048 -> 5 * 0.048 = 0.24 EUR/m²
    Heat: unchanged = 0.83 EUR/m²
    Scenario Total: 0.83 + 0.96 + 0.24 = 2.03 EUR/m²
    Delta: 2.03 - 1.83 = +0.20 EUR/m²
    """
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='assumed_unverified', assumption_enabled=True)
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    baseline = register_baseline_reference(svc.adapter, e.scope)

    cmd = ScenarioCommand(
        request_id=f'req-{e.tag}',
        name='Electric +20%',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=(
            ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('20')),
            ScenarioChange(component=EnergyComponent.ELEC_LOW, price_unit='EUR/kWh', change_percent=Decimal('20')),
        ),
    )
    scenario = svc.execute_scenario_command(cmd)
    assert scenario.version == 1

    scenario_cost = svc.get_cost(e.context, scenario_version=scenario.scenario_version)
    assert scenario_cost.official_total_cost_per_m2 == Decimal('2.03')
    assert scenario_cost.components[0].official_cost_per_m2 == Decimal('0.83')
    assert scenario_cost.components[1].official_cost_per_m2 == Decimal('0.96')
    assert scenario_cost.components[2].official_cost_per_m2 == Decimal('0.24')

    # Compare scenarios
    comparison = svc.compare_scenarios(e.context, baseline_tariff_version=baseline.tariff_version,
                                       scenario_version=scenario.scenario_version)
    assert comparison.delta_total_cost_per_m2 == Decimal('0.20')
    assert comparison.component_deltas[0].delta_cost_per_m2 == Decimal('0')
    assert comparison.component_deltas[1].delta_cost_per_m2 == Decimal('0.16')
    assert comparison.component_deltas[2].delta_cost_per_m2 == Decimal('0.04')

    with localcontext() as ctx:
        ctx.prec = 50
        expected_pct = (Decimal('100') * comparison.delta_total_cost_per_m2) / comparison.baseline.official_total_cost_per_m2
    assert comparison.delta_total_pct == expected_pct

    # Serialization roundtrip
    assert from_json(ScenarioComparison, to_json(comparison)) == comparison


def test_ac03_equivalent_heat_conversion(energy_scope):
    """AC-03: Equivalent heat rate 0.02988 EUR/kWh yields exact 0.83 EUR/m² without rounding."""
    e = energy_scope(rows=(('100', '0', '0'),), areas=('100',))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})

    tariff_version = f'tariff:equiv_heat:{e.tag}'
    rates = (
        TariffRate(
            rate_id=f'rate:{tariff_version}:heat',
            tariff_version=tariff_version,
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.02988'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=10),
            source_reference='Equivalent test',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:high',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_HIGH,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.08'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=10),
            source_reference='Standard',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:low',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_LOW,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.04'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=10),
            source_reference='Standard',
        ),
    )
    profile = TariffProfile(
        tariff_version=tariff_version,
        tariff_id='EQUIV_HEAT_TEST',
        version=1,
        data_scope_id=e.scope,
        currency='EUR',
        source_reference='Equivalent Heat Test',
        label='Equiv Heat',
        rates=rates,
        is_baseline=True,
    )
    svc.adapter.register_tariff(profile)

    summary = svc.get_cost(e.context)
    heat = summary.components[0]
    assert heat.official_cost_per_m2 == Decimal('0.83')


def test_ac04_temporal_rate_weighting(energy_scope):
    """AC-04: Temporal rates [10, 30] with rates [0.08, 0.10] yields 3.80, not 3.60."""
    e = energy_scope(rows=(('0', '10', '0'), ('0', '30', '0')), areas=('100', '100'))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})

    tariff_version = f'tariff:temporal:{e.tag}'
    rates = (
        TariffRate(
            rate_id=f'rate:{tariff_version}:heat',
            tariff_version=tariff_version,
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/MJ',
            rate=Decimal('0.0083'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=10),
            source_reference='Heat',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:high_d1',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_HIGH,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.08'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=1),
            source_reference='High day 1',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:high_d2',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_HIGH,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.10'),
            valid_from=D0 + timedelta(days=1),
            valid_to_exclusive=D0 + timedelta(days=2),
            source_reference='High day 2',
        ),
        TariffRate(
            rate_id=f'rate:{tariff_version}:low',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_LOW,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.04'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=10),
            source_reference='Low',
        ),
    )
    profile = TariffProfile(
        tariff_version=tariff_version,
        tariff_id='TEMPORAL_TEST',
        version=1,
        data_scope_id=e.scope,
        currency='EUR',
        source_reference='Temporal Test',
        label='Temporal Test',
        rates=rates,
        is_baseline=True,
    )
    svc.adapter.register_tariff(profile)

    summary = svc.get_cost(e.context)
    high = summary.components[1]
    # Day 1: 10 * 0.08 = 0.80; Day 2: 30 * 0.10 = 3.00 => Total = 3.80 (not 40 * 0.09 = 3.60)
    assert high.official_cost_per_m2 == Decimal('3.80')
    assert high.official_cost_per_m2 != Decimal('3.60')


def test_ac05_temporal_exclusion_overlap_rejected_and_adjacent_accepted(contracts_database):
    """AC-05: Overlap rejected by DB exclusion constraint; adjacent intervals accepted."""
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        adapter = PostgresCostAdapter(conn)
        tariff_v1 = 'tariff:exclusion_test:v1'
        # Adjacent intervals are legal
        rates_adjacent = (
            TariffRate(
                rate_id='r1',
                tariff_version=tariff_v1,
                component=EnergyComponent.ELEC_HIGH,
                currency='EUR',
                price_unit='EUR/kWh',
                rate=Decimal('0.08'),
                valid_from=D0,
                valid_to_exclusive=D0 + timedelta(days=5),
                source_reference='part 1',
            ),
            TariffRate(
                rate_id='r2',
                tariff_version=tariff_v1,
                component=EnergyComponent.ELEC_HIGH,
                currency='EUR',
                price_unit='EUR/kWh',
                rate=Decimal('0.09'),
                valid_from=D0 + timedelta(days=5),
                valid_to_exclusive=D0 + timedelta(days=10),
                source_reference='part 2',
            ),
        )
        p1 = TariffProfile(
            tariff_version=tariff_v1,
            tariff_id='ADJACENT_TEST',
            version=1,
            data_scope_id='scope:dummy',
            currency='EUR',
            source_reference='ref',
            label='Adjacent',
            rates=rates_adjacent,
        )
        # Register scope first in meta.data_scope
        conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", ('snap:adj', 'a'*64, 'url', 'test'))
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:dummy', 'snap:adj', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")
        adapter.register_tariff(p1)
        assert adapter.get_tariff(tariff_v1) is not None

        # Overlapping intervals rejected by contract validation
        tariff_v2 = 'tariff:overlap_test:v1'
        rates_overlap = (
            TariffRate(
                rate_id='ro1',
                tariff_version=tariff_v2,
                component=EnergyComponent.ELEC_HIGH,
                currency='EUR',
                price_unit='EUR/kWh',
                rate=Decimal('0.08'),
                valid_from=D0,
                valid_to_exclusive=D0 + timedelta(days=5),
                source_reference='part 1',
            ),
            TariffRate(
                rate_id='ro2',
                tariff_version=tariff_v2,
                component=EnergyComponent.ELEC_HIGH,
                currency='EUR',
                price_unit='EUR/kWh',
                rate=Decimal('0.09'),
                valid_from=D0 + timedelta(days=3),
                valid_to_exclusive=D0 + timedelta(days=8),
                source_reference='part 2 overlap',
            ),
        )
        with pytest.raises(ValueError, match='Overlapping rate intervals'):
            TariffProfile(
                tariff_version=tariff_v2,
                tariff_id='OVERLAP_TEST',
                version=1,
                data_scope_id='scope:dummy',
                currency='EUR',
                source_reference='ref',
                label='Overlap',
                rates=rates_overlap,
            )

        # Direct SQL bypass of Python validation also fails DB exclusion constraint
        with pytest.raises(psycopg.Error, match='exclusion constraint'):
            with conn.transaction():
                conn.execute("""
                    INSERT INTO config.tariff_profile (
                        tariff_version, tariff_id, version, data_scope_id, currency, source_reference, label
                    ) VALUES (
                        'tariff:overlap_db:v1', 'OVERLAP_DB', 1, 'scope:dummy', 'EUR', 'ref', 'Overlap DB'
                    )
                """)
                conn.execute("""
                    INSERT INTO config.tariff_rate (
                        rate_id, tariff_version, component, currency, price_unit, rate,
                        valid_from, valid_to_exclusive, source_reference
                    ) VALUES (
                        'r_db_1', 'tariff:overlap_db:v1', 'elec_high', 'EUR', 'EUR/kWh',
                        0.08, '2020-01-01', '2020-01-06', 'ref'
                    )
                """)
                conn.execute("""
                    INSERT INTO config.tariff_rate (
                        rate_id, tariff_version, component, currency, price_unit, rate,
                        valid_from, valid_to_exclusive, source_reference
                    ) VALUES (
                        'r_db_2', 'tariff:overlap_db:v1', 'elec_high', 'EUR', 'EUR/kWh',
                        0.10, '2020-01-03', '2020-01-08', 'ref'
                    )
                """)


def test_ac06_rate_gap_missing_quantity_reason_code_and_official_total_null(energy_scope):
    """AC-06: Missing rate coverage and missing quantity set official totals to NULL with reasons."""
    # 2 days of quantities, but rates only cover Day 1
    e = energy_scope(rows=(('100', '10', '5'), ('100', '10', '5')), areas=('100', '100'))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})

    tariff_version = f'tariff:gap:{e.tag}'
    rates = (
        TariffRate(
            rate_id=f'r:heat:{e.tag}',
            tariff_version=tariff_version,
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/MJ',
            rate=Decimal('0.0083'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=1),  # Only day 1!
            source_reference='ref',
        ),
        TariffRate(
            rate_id=f'r:high:{e.tag}',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_HIGH,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.08'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=1),
            source_reference='ref',
        ),
        TariffRate(
            rate_id=f'r:low:{e.tag}',
            tariff_version=tariff_version,
            component=EnergyComponent.ELEC_LOW,
            currency='EUR',
            price_unit='EUR/kWh',
            rate=Decimal('0.04'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=1),
            source_reference='ref',
        ),
    )
    profile = TariffProfile(
        tariff_version=tariff_version,
        tariff_id='GAP_TEST',
        version=1,
        data_scope_id=e.scope,
        currency='EUR',
        source_reference='ref',
        label='Gap',
        rates=rates,
        is_baseline=True,
    )
    svc.adapter.register_tariff(profile)

    summary = svc.get_cost(e.context)
    assert summary.official_total_cost_per_m2 is None
    assert summary.official_total_cost_eur is None
    assert summary.pricing_coverage_pct == Decimal('50')
    assert 'missing_rate' in summary.reason_codes
    assert summary.components[0].observed_cost_per_m2 == Decimal('0.83')
    assert summary.components[0].official_cost_per_m2 is None


def test_ac07_zero_quantities(energy_scope):
    """AC-07: All zero quantities evaluate to cost 0, delta 0, and delta_pct is None."""
    e = energy_scope(rows=(('0', '0', '0'),), areas=('100',))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    baseline = register_baseline_reference(svc.adapter, e.scope)

    summary = svc.get_cost(e.context)
    assert summary.official_total_cost_per_m2 == Decimal('0')
    assert summary.official_total_cost_eur == Decimal('0')
    assert summary.components[0].official_cost_per_m2 == Decimal('0')
    assert summary.components[1].official_cost_per_m2 == Decimal('0')
    assert summary.components[2].official_cost_per_m2 == Decimal('0')

    # Scenario comparison on zero baseline: delta_pct must be None
    cmd = ScenarioCommand(
        request_id=f'req-zero-{e.tag}',
        name='Zero scenario',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('20')),),
    )
    scenario = svc.execute_scenario_command(cmd)
    comp = svc.compare_scenarios(e.context, baseline_tariff_version=baseline.tariff_version,
                                 scenario_version=scenario.scenario_version)
    assert comp.delta_total_cost_per_m2 == Decimal('0')
    assert comp.delta_total_pct is None


def test_ac08_unknown_basis_pooling_gate(energy_scope):
    """AC-08: Unknown basis retains component costs, official total NULL with incompatible_or_unknown_basis."""
    e = energy_scope(rows=(('100', '10', '5'),), basis_status='unknown')
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    register_baseline_reference(svc.adapter, e.scope)

    summary = svc.get_cost(e.context)
    assert summary.components[0].observed_cost_per_m2 == Decimal('0.83')
    assert summary.components[1].observed_cost_per_m2 == Decimal('0.80')
    assert summary.components[2].observed_cost_per_m2 == Decimal('0.20')
    assert summary.official_total_cost_per_m2 is None
    assert 'incompatible_or_unknown_basis' in summary.reason_codes

    # Weekly aggregation must also enforce basis gating:
    # Component source-unit values preserved, total_cost_per_m2 is None, effective_area_m2 is None, reason explicit
    weeks = aggregate_weekly_cost(summary)
    assert len(weeks) == 1
    assert weeks[0].heat_cost_per_m2 == Decimal('0.83')
    assert weeks[0].elec_high_cost_per_m2 == Decimal('0.80')
    assert weeks[0].elec_low_cost_per_m2 == Decimal('0.20')
    assert weeks[0].total_cost_per_m2 is None
    assert weeks[0].effective_area_m2 is None
    assert 'incompatible_or_unknown_basis' in weeks[0].reason_codes


def test_ac09_input_validation():
    """AC-09: Input validation rejects invalid rates, units, and intervals."""
    # Negative rate
    with pytest.raises(ValueError, match='Expected finite Decimal >= 0'):
        TariffRate(
            rate_id='r:neg',
            tariff_version='tariff:test',
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/MJ',
            rate=Decimal('-0.01'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=1),
            source_reference='test',
        )

    # Invalid currency
    with pytest.raises(ValueError, match='Tariff rate currency must be EUR'):
        TariffRate(
            rate_id='r:cur',
            tariff_version='tariff:test',
            component=EnergyComponent.HEAT,
            currency='USD',
            price_unit='EUR/MJ',
            rate=Decimal('0.01'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=1),
            source_reference='test',
        )

    # Invalid price unit
    with pytest.raises(ValueError, match='Unexpected price unit for component'):
        TariffRate(
            rate_id='r:unit',
            tariff_version='tariff:test',
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/GJ',
            rate=Decimal('0.01'),
            valid_from=D0,
            valid_to_exclusive=D0 + timedelta(days=1),
            source_reference='test',
        )

    # Inverted date range
    with pytest.raises(ValueError, match='Window requires whole dates with start < end'):
        TariffRate(
            rate_id='r:inv',
            tariff_version='tariff:test',
            component=EnergyComponent.HEAT,
            currency='EUR',
            price_unit='EUR/MJ',
            rate=Decimal('0.01'),
            valid_from=D0 + timedelta(days=5),
            valid_to_exclusive=D0,
            source_reference='test',
        )


def test_ac10_scenario_command_idempotency_and_conflict(energy_scope):
    """AC-10: Same request_id returns existing scenario; conflicting command raises ValueError."""
    e = energy_scope(rows=(('100', '10', '5'),), areas=('100',))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    baseline = register_baseline_reference(svc.adapter, e.scope)

    cmd1 = ScenarioCommand(
        request_id=f'req-idemp-{e.tag}',
        name='Scenario A',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('10')),),
    )
    s1 = svc.execute_scenario_command(cmd1)

    # Idempotent replay: exact same command returns same scenario
    s1_replay = svc.execute_scenario_command(cmd1)
    assert s1_replay.scenario_version == s1.scenario_version
    assert s1_replay.rates == s1.rates

    # Conflicting command with same request_id but different changes
    cmd_conflict = ScenarioCommand(
        request_id=f'req-idemp-{e.tag}',
        name='Scenario A modified',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('25')),),
    )
    with pytest.raises(ValueError, match='Conflicting scenario command'):
        svc.execute_scenario_command(cmd_conflict)


def test_ac11_immutability_sealing(contracts_database):
    """AC-11: Immutability sealing rejects later child rate additions, updates, and deletes."""
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        adapter = PostgresCostAdapter(conn)
        # Register scope
        conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", ('snap:seal', 'a'*64, 'url', 'test'))
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:seal', 'snap:seal', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")

        tariff_version = 'tariff:seal_test:v1'
        rates = (
            TariffRate(
                rate_id='rate:seal:1',
                tariff_version=tariff_version,
                component=EnergyComponent.HEAT,
                currency='EUR',
                price_unit='EUR/MJ',
                rate=Decimal('0.0083'),
                valid_from=D0,
                valid_to_exclusive=D0 + timedelta(days=10),
                source_reference='test',
            ),
        )
        profile = TariffProfile(
            tariff_version=tariff_version,
            tariff_id='SEAL_TEST',
            version=1,
            data_scope_id='scope:seal',
            currency='EUR',
            source_reference='test',
            label='Seal Test',
            rates=rates,
            is_baseline=True,
        )
        adapter.register_tariff(profile)

        # In a separate transaction, appending a child rate to this profile must FAIL
        with pytest.raises(psycopg.Error, match='is sealed: registered rates cannot change membership'):
            conn.execute("""
                INSERT INTO config.tariff_rate (
                    rate_id, tariff_version, component, currency, price_unit, rate,
                    valid_from, valid_to_exclusive, source_reference
                ) VALUES (
                    'rate:seal:late', 'tariff:seal_test:v1', 'elec_high', 'EUR', 'EUR/kWh',
                    0.08, '2020-01-11', '2020-01-20', 'late rate'
                )
            """)

        # UPDATE on tariff_profile must FAIL
        with pytest.raises(psycopg.Error, match='version is immutable'):
            conn.execute("UPDATE config.tariff_profile SET label = 'modified' WHERE tariff_version = 'tariff:seal_test:v1'")

        # DELETE on tariff_rate must FAIL
        with pytest.raises(psycopg.Error, match='version is immutable'):
            conn.execute("DELETE FROM config.tariff_rate WHERE rate_id = 'rate:seal:1'")


def test_shared_quantity_identity(energy_scope):
    """Shared EnergyQuantity instances from mart are consumed directly without alteration."""
    e = energy_scope(rows=(('100', '10', '5'),), areas=('100',))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    register_baseline_reference(svc.adapter, e.scope)

    quantities = svc.energy.get_quantities(e.context)
    summary = svc.get_cost(e.context)

    for i, comp in enumerate(summary.components):
        day_cost = comp.day_costs[0]
        # Exact instance match
        assert day_cost.quantity == quantities[i]
        assert day_cost.quantity.resource_date == quantities[i].resource_date
        assert day_cost.quantity.source_value == quantities[i].source_value
        assert day_cost.quantity.accepted is True


def test_historical_baseline_registration_and_actual_scope(contracts_database):
    """Historical baseline AGC_2020_REFERENCE_V1 registers standard window [2019-12-16, 2020-05-30)."""
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        adapter = PostgresCostAdapter(conn)
        conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", ('snap:hist', 'a'*64, 'url', 'test'))
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:hist', 'snap:hist', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")

        profile = adapter.register_historical_tariff('scope:hist')
        assert profile.tariff_id == 'AGC_2020_REFERENCE_V1'
        assert profile.is_baseline is True
        assert len(profile.rates) == 3

        rates_by_comp = {r.component: r for r in profile.rates}
        assert rates_by_comp[EnergyComponent.HEAT].rate == Decimal('0.0083')
        assert rates_by_comp[EnergyComponent.HEAT].price_unit == 'EUR/MJ'
        assert rates_by_comp[EnergyComponent.HEAT].valid_from == date(2019, 12, 16)
        assert rates_by_comp[EnergyComponent.HEAT].valid_to_exclusive == date(2020, 5, 30)

        assert rates_by_comp[EnergyComponent.ELEC_HIGH].rate == Decimal('0.08')
        assert rates_by_comp[EnergyComponent.ELEC_HIGH].price_unit == 'EUR/kWh'

        assert rates_by_comp[EnergyComponent.ELEC_LOW].rate == Decimal('0.04')
        assert rates_by_comp[EnergyComponent.ELEC_LOW].price_unit == 'EUR/kWh'

        # Idempotent re-registration returns same profile
        re_profile = adapter.register_historical_tariff('scope:hist')
        assert re_profile == profile


def test_f1_publication_identity_and_scenario_comparison_pinning(energy_scope):
    """F1: Publication and scope pinning at query and compare boundaries."""
    e1 = energy_scope(rows=(('100', '10', '5'),), areas=('100',))
    svc1 = cost_service(e1.conn, raw_snapshots={e1.snapshot: e1.raw})
    baseline1 = register_baseline_reference(svc1.adapter, e1.scope)

    cmd = ScenarioCommand(
        request_id=f'req-f1-{e1.tag}',
        name='Scenario F1',
        baseline_version_id=baseline1.tariff_version,
        publication=e1.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('20')),),
    )
    scenario = svc1.execute_scenario_command(cmd)

    # 1. Reject both tariff_version and scenario_version
    with pytest.raises(ValueError, match='Cannot specify both'):
        svc1.get_cost(e1.context, tariff_version=baseline1.tariff_version, scenario_version=scenario.scenario_version)

    # 2. Reject foreign context publication against scenario publication
    foreign_context = replace(e1.context, publication_id='pub:foreign')
    with pytest.raises(ValueError, match='publication identity'):
        svc1.get_cost(foreign_context, scenario_version=scenario.scenario_version)

    # 3. Reject foreign context scope against explicit tariff
    foreign_scope_context = replace(e1.context, scope_id='scope:foreign')
    with pytest.raises(ValueError, match='Tariff scope'):
        svc1.get_cost(foreign_scope_context, tariff_version=baseline1.tariff_version)

    # 4. compare_scenarios defaults to scenario.baseline_tariff_version
    comparison = svc1.compare_scenarios(e1.context, scenario_version=scenario.scenario_version)
    assert comparison.baseline.tariff_version == baseline1.tariff_version
    assert comparison.scenario.scenario_version == scenario.scenario_version

    # 5. compare_scenarios rejects conflicting explicit baseline
    with pytest.raises(ValueError, match='conflicts with scenario pinned baseline'):
        svc1.compare_scenarios(e1.context, scenario_version=scenario.scenario_version, baseline_tariff_version='tariff:other:v1')


def test_f2_baseline_version_selection_and_compartment_precedence(contracts_database):
    """F2: Baseline lookup selects single header, highest version, and compartment precedence."""
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        adapter = PostgresCostAdapter(conn)
        conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", ('snap:f2', 'a'*64, 'url', 'test'))
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:f2', 'snap:f2', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.site VALUES ('site:f2', 'Site F2', 'source-naive', 'test') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.compartment VALUES ('comp:f2:1', 'site:f2', 'C1', 'test') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.compartment VALUES ('comp:f2:2', 'site:f2', 'C2', 'test') ON CONFLICT DO NOTHING")

        # 1. Register baseline v1 and v2 for scope:f2 (compartment NULL)
        p1 = TariffProfile(
            tariff_version='tariff:f2:v1',
            tariff_id='F2_TARIFF',
            version=1,
            data_scope_id='scope:f2',
            currency='EUR',
            source_reference='v1 ref',
            label='v1',
            rates=(
                TariffRate('r:f2:v1:h', 'tariff:f2:v1', EnergyComponent.HEAT, 'EUR', 'EUR/MJ', Decimal('0.0080'), D0, D0 + timedelta(days=365), 'ref'),
            ),
            is_baseline=True,
        )
        adapter.register_tariff(p1)

        p2 = TariffProfile(
            tariff_version='tariff:f2:v2',
            tariff_id='F2_TARIFF',
            version=2,
            data_scope_id='scope:f2',
            currency='EUR',
            source_reference='v2 ref',
            label='v2',
            rates=(
                TariffRate('r:f2:v2:h', 'tariff:f2:v2', EnergyComponent.HEAT, 'EUR', 'EUR/MJ', Decimal('0.0090'), D0, D0 + timedelta(days=365), 'ref'),
            ),
            is_baseline=True,
        )
        adapter.register_tariff(p2)

        # Baseline lookup must pick v2 (highest version) and not merge rates
        baseline = adapter.get_baseline_tariff('scope:f2')
        assert baseline.tariff_version == 'tariff:f2:v2'
        assert baseline.version == 2
        assert len(baseline.rates) == 1
        assert baseline.rates[0].rate == Decimal('0.0090')

        # 2. Register compartment-specific baseline for comp:f2:1
        p_c1 = TariffProfile(
            tariff_version='tariff:f2:c1:v1',
            tariff_id='F2_TARIFF_C1',
            version=1,
            data_scope_id='scope:f2',
            compartment_id='comp:f2:1',
            currency='EUR',
            source_reference='c1 ref',
            label='c1',
            rates=(
                TariffRate('r:f2:c1:h', 'tariff:f2:c1:v1', EnergyComponent.HEAT, 'EUR', 'EUR/MJ', Decimal('0.0120'), D0, D0 + timedelta(days=365), 'ref'),
            ),
            is_baseline=True,
        )
        adapter.register_tariff(p_c1)

        # Query with comp:f2:1 picks compartment override
        baseline_c1 = adapter.get_baseline_tariff('scope:f2', 'comp:f2:1')
        assert baseline_c1.tariff_version == 'tariff:f2:c1:v1'
        assert baseline_c1.rates[0].rate == Decimal('0.0120')

        # Query with comp:f2:2 falls back to scope level v2
        baseline_c2 = adapter.get_baseline_tariff('scope:f2', 'comp:f2:2')
        assert baseline_c2.tariff_version == 'tariff:f2:v2'

        # 3. Register historical tariff in 2 different scopes without conflict
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:f2:b', 'snap:f2', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")
        h_a = adapter.register_historical_tariff('scope:f2')
        h_b = adapter.register_historical_tariff('scope:f2:b')
        assert h_a.tariff_version != h_b.tariff_version
        assert h_a.data_scope_id == 'scope:f2'
        assert h_b.data_scope_id == 'scope:f2:b'

        # 4. Unrelated existing baseline is not returned as historical
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:f2:unrelated', 'snap:f2', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")
        p_unrelated = TariffProfile(
            tariff_version='tariff:unrelated:v1',
            tariff_id='CUSTOM_TARIFF',
            version=1,
            data_scope_id='scope:f2:unrelated',
            currency='EUR',
            source_reference='unrelated',
            label='Unrelated',
            rates=(
                TariffRate('r:unrelated:h', 'tariff:unrelated:v1', EnergyComponent.HEAT, 'EUR', 'EUR/MJ', Decimal('0.0500'), D0, D0 + timedelta(days=365), 'ref'),
            ),
            is_baseline=True,
        )
        adapter.register_tariff(p_unrelated)
        h_unrelated = adapter.register_historical_tariff('scope:f2:unrelated')
        assert h_unrelated.tariff_id == HISTORICAL_TARIFF_ID
        assert h_unrelated.tariff_version != p_unrelated.tariff_version


def test_f3_changing_area_549_eur_and_cycle_window_clipping(energy_scope):
    """F3: Two days with areas 100 then 200 m² yield exact 549 EUR; cycle clipping."""
    # Day 1: 100 MJ, 10 high, 5 low, area 100 m²
    # Day 2: 100 MJ, 10 high, 5 low, area 200 m²
    e = energy_scope(
        rows=(
            ('100', '10', '5'),
            ('100', '10', '5'),
        ),
        areas=('100', '200'),
    )
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    register_baseline_reference(svc.adapter, e.scope)

    summary = svc.get_cost(e.context)
    # Absolute EUR must be exact 549 EUR
    assert summary.official_total_cost_eur == Decimal('549')
    assert summary.observed_total_cost_eur == Decimal('549')

    # Per-m² total must be None due to changing areas
    assert summary.official_total_cost_per_m2 is None
    assert summary.observed_total_cost_per_m2 is None

    # Component breakdown:
    # Heat: 100*0.0083*100 + 100*0.0083*200 = 83 + 166 = 249 EUR
    # High: 10*0.08*100 + 10*0.08*200 = 80 + 160 = 240 EUR
    # Low: 5*0.04*100 + 5*0.04*200 = 20 + 40 = 60 EUR
    heat_comp = summary.components[0]
    high_comp = summary.components[1]
    low_comp = summary.components[2]

    assert heat_comp.official_cost_eur == Decimal('249')
    assert high_comp.official_cost_eur == Decimal('240')
    assert low_comp.official_cost_eur == Decimal('60')

    assert heat_comp.official_cost_per_m2 is None
    assert high_comp.official_cost_per_m2 is None
    assert low_comp.official_cost_per_m2 is None

    # Weekly aggregation must also enforce basis gating across variable areas:
    # Absolute 549 EUR retained, total_cost_per_m2 is None, effective_area_m2 is None, reason explicit
    weeks = aggregate_weekly_cost(summary)
    assert len(weeks) == 1
    assert weeks[0].total_cost_eur == Decimal('549')
    assert weeks[0].total_cost_per_m2 is None
    assert weeks[0].effective_area_m2 is None
    assert 'per_m2_basis_not_common_and_fixed' in weeks[0].reason_codes

    # Cycle window clipping: query with 1 extra day on each side of a 1-day cycle
    e1 = energy_scope(rows=(('100', '10', '5'),), areas=('100',))
    svc1 = cost_service(e1.conn, raw_snapshots={e1.snapshot: e1.raw})
    register_baseline_reference(svc1.adapter, e1.scope)

    wide_context = replace(e1.context, start=e1.context.start - timedelta(days=1), end=e1.context.end + timedelta(days=1))
    summary_wide = svc1.get_cost(wide_context)
    # expected_days should be clipped to the 1-day cycle
    assert summary_wide.expected_days == 1
    assert summary_wide.priced_days == 1
    assert summary_wide.pricing_coverage_pct == Decimal('100')
    assert summary_wide.official_total_cost_per_m2 == Decimal('1.83')
    assert 'window_clipped_to_cycle' in summary_wide.reason_codes


def test_f4_complete_priced_day_intersection_and_separate_quantity_coverage(energy_scope):
    """F4: Day 1 missing high, Day 2 missing low -> 0 complete priced days (0%)."""
    # Rows: 2 days
    # Day 1: heat 100, elec_high None (missing), elec_low 5
    # Day 2: heat 100, elec_high 10, elec_low None (missing)
    e = energy_scope(
        rows=(
            ('100', None, '5'),
            ('100', '10', None),
        ),
        areas=('100', '100'),
    )
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    register_baseline_reference(svc.adapter, e.scope)

    summary = svc.get_cost(e.context)
    # Intersection of complete priced days across all 3 components is 0
    assert summary.priced_days == 0
    assert summary.pricing_coverage_pct == Decimal('0')

    # Quantity complete days is also 0
    assert summary.quantity_complete_days == 0
    assert summary.quantity_coverage_pct == Decimal('0')

    # Individual components have partial priced days
    assert summary.components[0].priced_days == 2  # heat
    assert summary.components[1].priced_days == 1  # elec_high
    assert summary.components[2].priced_days == 1  # elec_low

    # Component quantity coverage:
    assert summary.components[0].quantity_accepted_days == 2
    assert summary.components[1].quantity_accepted_days == 1
    assert summary.components[2].quantity_accepted_days == 1


def test_f4_no_priced_observations_is_null_not_zero(energy_scope):
    """F4: If no day has usable priced quantity, observed cost is NULL rather than fabricated 0."""
    e = energy_scope(
        rows=((None, None, None),),
        areas=('100',),
    )
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    register_baseline_reference(svc.adapter, e.scope)

    summary = svc.get_cost(e.context)
    for comp in summary.components:
        assert comp.observed_cost_per_m2 is None
        assert comp.observed_cost_eur is None
        assert comp.official_cost_per_m2 is None
        assert comp.official_cost_eur is None

    assert summary.observed_total_cost_per_m2 is None
    assert summary.observed_total_cost_eur is None
    assert summary.official_total_cost_per_m2 is None
    assert summary.official_total_cost_eur is None


def test_f5_persisted_payload_roundtrip_append_version_and_conflict(energy_scope):
    """F5: Changes and command payload persistence, version appending, conflict detection."""
    e = energy_scope(rows=(('100', '10', '5'),), areas=('100',))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    baseline = register_baseline_reference(svc.adapter, e.scope)

    changes = (
        ScenarioChange(component=EnergyComponent.HEAT, price_unit='EUR/MJ', change_percent=Decimal('10')),
        ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', price=Decimal('0.10')),
    )
    cmd = ScenarioCommand(
        request_id=f'req-f5-{e.tag}',
        name='Scenario F5',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=changes,
    )
    s1 = svc.execute_scenario_command(cmd)

    # 1. Reload from DB directly via get_scenario
    reloaded = svc.adapter.get_scenario(s1.scenario_version)
    assert reloaded.changes == changes
    assert reloaded.created_at is not None
    assert s1.created_at == reloaded.created_at

    # 2. Append version 2 to existing scenario
    cmd_v2 = ScenarioCommand(
        request_id=f'req-f5-v2-{e.tag}',
        name='Scenario F5 v2',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_LOW, price_unit='EUR/kWh', change_percent=Decimal('-15')),),
    )
    s2 = svc.append_scenario_version(s1.scenario_id, cmd_v2)
    assert s2.scenario_id == s1.scenario_id
    assert s2.version == 2
    assert s2.scenario_version == f'{s1.scenario_id}:v2'

    # Version 1 is unchanged
    s1_still = svc.adapter.get_scenario(s1.scenario_version)
    assert s1_still.version == 1
    assert s1_still.changes == changes

    # 3. Conflict on same request_id but different changes deriving same rate
    cmd_conflicting_payload = ScenarioCommand(
        request_id=f'req-f5-{e.tag}',
        name='Scenario F5',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.HEAT, price_unit='EUR/MJ', price=Decimal('0.00913')),),
    )
    with pytest.raises(ValueError, match='Conflicting scenario command'):
        svc.execute_scenario_command(cmd_conflicting_payload)


def test_f6_exact_decimal_arithmetic_without_ambient_rounding(energy_scope):
    """F6: Finite Decimal rates with 29 digits retain full precision without 28-digit ambient rounding."""
    e = energy_scope(rows=(('1', '1', '1'),), areas=('1',))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})

    # Register tariff with 29-digit rate
    long_rate = Decimal('0.12345678901234567890123456789')
    tariff_version = f'tariff:f6:{e.scope}'
    rates = (
        TariffRate(f'r:f6:{e.scope}:h', tariff_version, EnergyComponent.HEAT, 'EUR', 'EUR/MJ', long_rate, D0, D0 + timedelta(days=365), 'ref'),
        TariffRate(f'r:f6:{e.scope}:eh', tariff_version, EnergyComponent.ELEC_HIGH, 'EUR', 'EUR/kWh', Decimal('0.08'), D0, D0 + timedelta(days=365), 'ref'),
        TariffRate(f'r:f6:{e.scope}:el', tariff_version, EnergyComponent.ELEC_LOW, 'EUR', 'EUR/kWh', Decimal('0.04'), D0, D0 + timedelta(days=365), 'ref'),
    )
    profile = TariffProfile(
        tariff_version=tariff_version,
        tariff_id=f'F6_TARIFF_{e.scope}',
        version=1,
        data_scope_id=e.scope,
        currency='EUR',
        source_reference='ref',
        label='F6',
        rates=rates,
        is_baseline=True,
    )
    svc.adapter.register_tariff(profile)

    summary = svc.get_cost(e.context, tariff_version=tariff_version)
    heat_comp = summary.components[0]
    # In 28-digit ambient context, long_rate would be rounded to 0.1234567890123456789012345679
    # With exact arithmetic, it must retain the exact 29-digit value
    assert heat_comp.official_cost_per_m2 == long_rate
    assert heat_comp.official_cost_eur == long_rate


def test_r1_historical_scope_and_compartment_coexistence_and_precedence(contracts_database):
    """R1: Historical scope and compartment baselines coexist in the same scope without collision."""
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        adapter = PostgresCostAdapter(conn)
        conn.execute("INSERT INTO meta.dataset_snapshot VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", ('snap:r1', 'a'*64, 'url', 'test'))
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:r1', 'snap:r1', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.site VALUES ('site:r1', 'Site R1', 'source-naive', 'test') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.compartment VALUES ('comp:r1:1', 'site:r1', 'C1', 'test') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.compartment VALUES ('comp:r1:2', 'site:r1', 'C2', 'test') ON CONFLICT DO NOTHING")

        # 1. Register scope-level historical tariff
        p_scope = adapter.register_historical_tariff('scope:r1')
        assert p_scope.data_scope_id == 'scope:r1'
        assert p_scope.compartment_id is None

        # 2. Register compartment-level historical tariff for comp:r1:1 in the SAME scope
        p_comp1 = adapter.register_historical_tariff('scope:r1', 'comp:r1:1')
        assert p_comp1.data_scope_id == 'scope:r1'
        assert p_comp1.compartment_id == 'comp:r1:1'
        assert p_comp1.tariff_version != p_scope.tariff_version

        # 3. Specific compartment wins
        b_c1 = adapter.get_baseline_tariff('scope:r1', 'comp:r1:1')
        assert b_c1.tariff_version == p_comp1.tariff_version

        # 4. Other compartment falls back to scope baseline
        b_c2 = adapter.get_baseline_tariff('scope:r1', 'comp:r1:2')
        assert b_c2.tariff_version == p_scope.tariff_version

        # 5. Query without compartment returns scope baseline
        b_null = adapter.get_baseline_tariff('scope:r1', None)
        assert b_null.tariff_version == p_scope.tariff_version

        # 6. Idempotent re-registration returns own exact profile
        assert adapter.register_historical_tariff('scope:r1') == p_scope
        assert adapter.register_historical_tariff('scope:r1', 'comp:r1:1') == p_comp1

        # 7. Multiple distinct scopes and compartments coexist
        conn.execute("INSERT INTO meta.data_scope VALUES ('scope:r1:b', 'snap:r1', 'historical', NULL, 'test') ON CONFLICT DO NOTHING")
        p_scope_b = adapter.register_historical_tariff('scope:r1:b')
        p_comp1_b = adapter.register_historical_tariff('scope:r1:b', 'comp:r1:1')
        assert p_scope_b.tariff_version != p_scope.tariff_version
        assert p_comp1_b.tariff_version != p_comp1.tariff_version


def test_r2_concurrent_append_separate_connections_and_same_request_replay(energy_scope, contracts_database):
    """R2: Concurrent append commands allocate distinct monotone versions; same-request retries succeed."""
    from concurrent.futures import ThreadPoolExecutor

    e = energy_scope(rows=(('100', '10', '5'),), areas=('100',))
    svc = cost_service(e.conn, raw_snapshots={e.snapshot: e.raw})
    baseline = register_baseline_reference(svc.adapter, e.scope)
    scen_id = f'scen-r2-{e.tag}'

    # Create v1 initial scenario
    cmd_v1 = ScenarioCommand(
        request_id=f'req-r2-v1-{e.tag}',
        name='Scenario R2 v1',
        baseline_version_id=baseline.tariff_version,
        publication=e.publication,
        changes=(ScenarioChange(component=EnergyComponent.HEAT, price_unit='EUR/MJ', change_percent=Decimal('10')),),
        scenario_id=scen_id,
    )
    s1 = svc.execute_scenario_command(cmd_v1)
    assert s1.version == 1

    # 1. Run 2 concurrent append commands with separate DB connections and separate request IDs
    def run_append(req_id: str, pct: Decimal):
        with psycopg.connect(**contracts_database, autocommit=True) as thread_conn:
            thread_svc = cost_service(thread_conn, raw_snapshots={e.snapshot: e.raw})
            cmd = ScenarioCommand(
                request_id=req_id,
                name=f'Scenario {req_id}',
                baseline_version_id=baseline.tariff_version,
                publication=e.publication,
                changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=pct),),
                scenario_id=scen_id,
            )
            return thread_svc.execute_scenario_command(cmd)

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_a = executor.submit(run_append, f'req-r2-appA-{e.tag}', Decimal('20'))
        f_b = executor.submit(run_append, f'req-r2-appB-{e.tag}', Decimal('30'))
        res_a = f_a.result(timeout=10)
        res_b = f_b.result(timeout=10)

    # Both must commit distinct monotone versions 2 and 3
    versions = {res_a.version, res_b.version}
    assert versions == {2, 3}

    # 2. Replay either request returns its own exact stored version
    with psycopg.connect(**contracts_database, autocommit=True) as conn:
        svc_conn = cost_service(conn, raw_snapshots={e.snapshot: e.raw})
        replay_a = svc_conn.execute_scenario_command(ScenarioCommand(
            request_id=f'req-r2-appA-{e.tag}',
            name=f'Scenario req-r2-appA-{e.tag}',
            baseline_version_id=baseline.tariff_version,
            publication=e.publication,
            changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('20')),),
            scenario_id=scen_id,
        ))
        assert replay_a.version == res_a.version
        assert replay_a.scenario_version == res_a.scenario_version

        replay_b = svc_conn.execute_scenario_command(ScenarioCommand(
            request_id=f'req-r2-appB-{e.tag}',
            name=f'Scenario req-r2-appB-{e.tag}',
            baseline_version_id=baseline.tariff_version,
            publication=e.publication,
            changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('30')),),
            scenario_id=scen_id,
        ))
        assert replay_b.version == res_b.version
        assert replay_b.scenario_version == res_b.scenario_version

    # 3. Concurrent same-request append (two threads with same request_id)
    same_req_id = f'req-r2-same-{e.tag}'

    def run_same_request():
        with psycopg.connect(**contracts_database, autocommit=True) as thread_conn:
            thread_svc = cost_service(thread_conn, raw_snapshots={e.snapshot: e.raw})
            cmd = ScenarioCommand(
                request_id=same_req_id,
                name='Scenario Same',
                baseline_version_id=baseline.tariff_version,
                publication=e.publication,
                changes=(ScenarioChange(component=EnergyComponent.ELEC_LOW, price_unit='EUR/kWh', change_percent=Decimal('5')),),
                scenario_id=scen_id,
            )
            return thread_svc.execute_scenario_command(cmd)

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_s1 = executor.submit(run_same_request)
        f_s2 = executor.submit(run_same_request)
        res_s1 = f_s1.result(timeout=10)
        res_s2 = f_s2.result(timeout=10)

    assert res_s1.version == 4
    assert res_s2.version == 4
    assert res_s1.scenario_version == res_s2.scenario_version


def test_r3_append_path_rejects_replacing_pinned_analysis(energy_scope):
    """R3: Append path rejects changing publication, snapshot, scope, or baseline of existing scenario."""
    e_a = energy_scope(rows=(('100', '10', '5'),), areas=('100',))
    e_b = energy_scope(rows=(('100', '10', '5'),), areas=('100',))

    svc_a = cost_service(e_a.conn, raw_snapshots={e_a.snapshot: e_a.raw})
    base_a = register_baseline_reference(svc_a.adapter, e_a.scope)

    svc_b = cost_service(e_b.conn, raw_snapshots={e_b.snapshot: e_b.raw})
    base_b = register_baseline_reference(svc_b.adapter, e_b.scope)

    scen_id = f'scen-r3-pinned-{e_a.tag}'
    cmd_v1 = ScenarioCommand(
        request_id=f'req-r3-v1-{e_a.tag}',
        name='Scenario A v1',
        baseline_version_id=base_a.tariff_version,
        publication=e_a.publication,
        changes=(ScenarioChange(component=EnergyComponent.HEAT, price_unit='EUR/MJ', change_percent=Decimal('10')),),
        scenario_id=scen_id,
    )
    s1 = svc_a.execute_scenario_command(cmd_v1)
    assert s1.version == 1

    # 1. Attempt append with foreign publication B and foreign baseline B via execute_scenario_command
    cmd_foreign_both = ScenarioCommand(
        request_id=f'req-r3-foreign-both-{e_a.tag}',
        name='Scenario Foreign Both',
        baseline_version_id=base_b.tariff_version,
        publication=e_b.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('20')),),
        scenario_id=scen_id,
    )
    with pytest.raises(ValueError, match='Cannot change publication'):
        svc_a.execute_scenario_command(cmd_foreign_both)

    # 2. Attempt append with foreign baseline B but same publication A via execute_scenario_command
    cmd_foreign_base = ScenarioCommand(
        request_id=f'req-r3-foreign-base-{e_a.tag}',
        name='Scenario Foreign Base',
        baseline_version_id=base_b.tariff_version,
        publication=e_a.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_HIGH, price_unit='EUR/kWh', change_percent=Decimal('20')),),
        scenario_id=scen_id,
    )
    with pytest.raises(ValueError, match='Baseline scope'):
        svc_a.execute_scenario_command(cmd_foreign_base)

    # 3. Attempt append with foreign publication via append_scenario_version
    with pytest.raises(ValueError, match='Cannot change publication'):
        svc_a.append_scenario_version(scen_id, cmd_foreign_both)

    # 4. Attempt direct register_scenario with foreign publication on existing scenario_id
    scenario_direct_foreign = CostScenario(
        scenario_version=f'{scen_id}:v2',
        scenario_id=scen_id,
        version=2,
        request_id=f'req-r3-direct-foreign-{e_a.tag}',
        baseline_tariff_version=base_b.tariff_version,
        publication=e_b.publication,
        name='Direct Foreign',
        rates=s1.rates,
        changes=s1.changes,
    )
    with pytest.raises(ValueError, match='Cannot change publication'):
        svc_a.adapter.register_scenario(scenario_direct_foreign)

    # 5. Attempt direct register_scenario with foreign baseline on existing scenario_id
    scenario_direct_foreign_base = CostScenario(
        scenario_version=f'{scen_id}:v2b',
        scenario_id=scen_id,
        version=2,
        request_id=f'req-r3-direct-foreign-base-{e_a.tag}',
        baseline_tariff_version=base_b.tariff_version,
        publication=e_a.publication,
        name='Direct Foreign Base',
        rates=s1.rates,
        changes=s1.changes,
    )
    with pytest.raises(ValueError, match='Cannot change.*baseline'):
        svc_a.adapter.register_scenario(scenario_direct_foreign_base)

    # 6. Existing v1 still replays cleanly
    v1_replay = svc_a.adapter.get_scenario(s1.scenario_version)
    assert v1_replay.version == 1
    assert v1_replay.publication == e_a.publication

    # 7. Valid v2 (same publication, scope, baseline, but new changes) is accepted
    cmd_v2 = ScenarioCommand(
        request_id=f'req-r3-v2-{e_a.tag}',
        name='Scenario A v2',
        baseline_version_id=base_a.tariff_version,
        publication=e_a.publication,
        changes=(ScenarioChange(component=EnergyComponent.ELEC_LOW, price_unit='EUR/kWh', change_percent=Decimal('15')),),
        scenario_id=scen_id,
    )
    s2 = svc_a.append_scenario_version(scen_id, cmd_v2)
    assert s2.version == 2
    assert s2.scenario_version == f'{scen_id}:v2'
