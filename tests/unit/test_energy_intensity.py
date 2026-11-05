"""BR04 acceptance through real sealed energy/harvest fixture publications."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import pytest

from greenhouse.contracts import EnergyIntensity, EnergyPortfolio, from_json, to_json
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.queries.intensity import EnergyIntensityService, portfolio
from test_energy_mart import energy_scope, contracts_database, fixture_stage, rerun


def service(e):
    return EnergyIntensityService(e.service, PostgresHarvestQueryAdapter(e.conn))


def test_distinct_areas_preharvest_a_only_rerun_and_exact_export(energy_scope):
    e = energy_scope()
    r = service(e).get_intensity(e.context)
    assert r.energy.official_total_kwh == 500
    assert r.official_a_kg == 100
    assert r.intensity_kwh_kg_a == 5  # not naive per-m² 2.5 or A+B 500/150
    assert (r.heat_kwh_kg_a, r.light_kwh_kg_a) == (3, 2)
    assert r.comparison_eligible and not r.assumptions
    assert r.harvest_a[0].record.prod_b.value == 1
    assert from_json(EnergyIntensity, to_json(r)) == r
    assert rerun(e)['publication_id'] == e.publication.publication_id
    assert service(e).get_intensity(e.context) == r


def test_effective_daily_areas_absolute_before_ratio(energy_scope):
    e = energy_scope(areas=('100', '200'))
    r = service(e).get_intensity(e.context)
    assert r.energy.official_total_kwh == 800
    assert r.intensity_kwh_kg_a == 8 and r.comparison_eligible
    assert not r.basis_gate.per_m2_compatible
    sub = service(e).get_intensity(replace(e.context, start=e.context.start+timedelta(days=1)))
    assert sub.energy.official_total_kwh == 600 and sub.intensity_kwh_kg_a == 6
    assert not sub.comparison_eligible


@pytest.mark.parametrize('values,coverage', [(('3.6','','0'),50), (('3.6','-1','0'),50)])
def test_missing_invalid_component_preserves_observed_but_blocks_ratio(energy_scope, values, coverage):
    e = energy_scope(rows=(values, ('7.2','0','1')))
    r = service(e).get_intensity(e.context)
    assert r.energy.complete_coverage_pct == coverage
    assert r.intensity_kwh_kg_a is None and not r.comparison_eligible
    assert 'incomplete_energy_quantity' in r.reason_codes
    assert r.official_a_kg == 100


@pytest.mark.parametrize('basis_status,enabled', [('unknown',True),('assumed_unverified',False)])
def test_unknown_or_disabled_assumption_is_null(energy_scope,basis_status,enabled):
    e = energy_scope(basis_status=basis_status,assumption_enabled=enabled)
    r = service(e).get_intensity(e.context)
    assert r.energy.components[0].observed_source_per_m2 == Decimal('10.8')
    assert r.intensity_kwh_kg_a is None and not r.comparison_eligible


@pytest.mark.parametrize('harvest,reason', [(((1,'0','1'),),'zero_class_a_production'),
    ((), 'no_harvest_in_window'), (((1,'','1'),),'harvest_snapshot_incomplete'),
    (((-321,'2','1'),), 'harvest_snapshot_incomplete')])
def test_zero_absent_missing_and_outside_harvest(energy_scope,harvest,reason):
    e = energy_scope(harvest_rows=harvest)
    r = service(e).get_intensity(e.context)
    assert r.intensity_kwh_kg_a is None and not r.comparison_eligible
    assert reason in r.reason_codes
    assert r.energy.official_total_kwh == 500


def test_subwindow_without_harvest_keeps_preharvest_energy(energy_scope):
    e = energy_scope()
    r = service(e).get_intensity(replace(e.context, end=e.context.end-timedelta(days=1)))
    assert r.energy.official_total_kwh == 200
    assert r.official_a_kg == 0 and r.intensity_kwh_kg_a is None
    assert 'no_harvest_in_window' in r.reason_codes
    assert not r.comparison_eligible


def test_accepted_zero_energy_is_not_missing(energy_scope):
    e = energy_scope(rows=(('0','0','0'),('0','0','0')))
    r = service(e).get_intensity(e.context)
    assert r.energy.complete_coverage_pct == 100 and r.intensity_kwh_kg_a == 0
    assert r.comparison_eligible


def test_assumed_primary_sensitivity_verified_production_bridge(energy_scope):
    e = energy_scope(basis_status='assumed_unverified',production_area='62.5',production_kind='production_area')
    r = service(e).get_intensity(e.context)
    assert r.intensity_kwh_kg_a == Decimal('2.5') and r.comparison_eligible
    assert r.assumptions == ('AGC_SHARED_BASIS_V1:v1',)
    assert r.basis_gate.label == 'giả định chưa xác minh'
    assert r.sensitivity_factor == Decimal('1.536') and r.sensitivity_kwh_kg_a == Decimal('3.84')
    # Same published selection context as actual: ProdA is also shared-assumed.
    source = r.harvest_a[0]
    basis = replace(source.basis, status=r.energy.components[0].basis_totals[0].basis.status,
        basis_kind='growing_area_assumed', assumption_id='AGC_SHARED_BASIS_V1', assumption_version=1,
        assumption_statement='explicit synthetic assumption',assumption_evidence='fixture',basis_label=r.basis_gate.label)
    measure = replace(source.record.prod_a,basis_status=basis.status,basis_kind=basis.basis_kind,
        assumption_id=basis.assumption_id,assumption_version=1,basis_label=basis.basis_label)
    record = replace(source.record,prod_a=measure)
    from greenhouse.queries.intensity import join_intensity
    shared = join_intensity(r.energy,r.cycle,(record,),r.harvest_state,(basis,))
    assert shared.sensitivity_kwh_kg_a == Decimal('3.84')
    assert shared.harvest_a[0].record.prod_a.basis_status.value == 'assumed_unverified'


def test_pairing_completeness_not_bypassed_by_a_only_denominator(energy_scope):
    e = energy_scope(harvest_rows=((1,'2',''),))
    r = service(e).get_intensity(e.context)
    assert r.observed_a_kg == 100 and not r.harvest_state.snapshot_complete
    assert r.official_a_kg is None and r.intensity_kwh_kg_a is None


def test_portfolio_ratio_sums_explicit_population_and_versions(energy_scope):
    e = energy_scope()
    first = service(e).get_intensity(e.context)
    # Pure drill-across oracle: second cycle E100/A50, independent summary
    # contract with identical publication/window/quality; no raw day join.
    context = replace(first.context,cycle_id='cycle:second')
    cycle = replace(first.cycle,cycle_id=context.cycle_id)
    h = first.harvest_a[0]
    record = replace(h.record,cycle_id=cycle.cycle_id,prod_a=replace(h.record.prod_a,value=Decimal('1')))
    contribution = replace(h,record=record,absolute_a_kg=Decimal('50'))
    components = tuple(replace(c,official_absolute_kwh=Decimal(v)) for c,v in zip(first.energy.components,('60','20','20')))
    energy = replace(first.energy,context=context,official_total_kwh=Decimal('100'),components=components)
    second = replace(first,context=context,cycle=cycle,energy=energy,harvest_a=(contribution,),
        observed_a_kg=Decimal('50'),official_a_kg=Decimal('50'),intensity_kwh_kg_a=Decimal('2'),
        heat_kwh_kg_a=Decimal('1.2'),light_kwh_kg_a=Decimal('.8'))
    result = portfolio((first,second))
    assert (result.energy_kwh,result.production_a_kg,result.intensity_kwh_kg_a)==(600,150,4)
    assert from_json(EnergyPortfolio,to_json(result))==result
    with pytest.raises(ValueError,match='duplicate'):
        portfolio((first,first))
    excluded = replace(second,comparison_eligible=False,intensity_kwh_kg_a=None,
        heat_kwh_kg_a=None,light_kwh_kg_a=None,reason_codes=('fixture_ineligible',))
    p = portfolio((first,excluded))
    assert p.intensity_kwh_kg_a==5 and p.excluded_cycle_ids==(second.cycle.cycle_id,)
    # Different comparison windows/assumption versions must never average.
    mixed = replace(second,assumptions=('different:v2',))
    p = portfolio((first,mixed))
    assert p.intensity_kwh_kg_a is None and len(p.excluded_cycle_ids)==2


def test_old_pin_and_basis_provenance_survive_toggle(energy_scope):
    e = energy_scope()
    before = service(e).get_intensity(e.context)
    e.conn.execute('UPDATE config.basis_application SET enabled=false WHERE data_scope_id=%s',(e.scope,))
    assert service(e).get_intensity(e.context)==before
    with pytest.raises(ValueError,match='identity'):
        service(e).get_intensity(replace(e.context,snapshot_id='wrong'))


def test_wire_rejects_fabricated_ratio_or_portfolio_average(energy_scope):
    e=energy_scope()
    r=service(e).get_intensity(e.context)
    with pytest.raises(ValueError,match='ratio'):
        replace(r,intensity_kwh_kg_a=Decimal('2.5'))
    p=portfolio((r,))
    with pytest.raises(ValueError,match='sums'):
        replace(p,intensity_kwh_kg_a=Decimal('4'))
    with pytest.raises(ValueError,match='complete'):
        replace(r,harvest_state=replace(r.harvest_state,snapshot_complete=False,
            source_resolved=False,reasons=('fixture_unresolved',)))
