"""Exact BR-06 exports of energy cost and scenario DTOs shown on screen."""
from decimal import Decimal

from greenhouse.app import exports
from greenhouse.contracts import (
    CostScenario, EnergyComponent, EnergyCostSummary, ScenarioComparison, TariffRate,
    exact_sub, to_dict, to_json,
)


def cost_metadata(summary: EnergyCostSummary, scenario: CostScenario | None = None,
                  grain: str | None = None) -> list[str]:
    c = summary.context
    meta = [
        '# export_version=greenhouse-energy-cost-v1',
        f'# publication_id={c.publication_id}',
        f'# scope_id={c.scope_id}',
        f'# snapshot_id={c.snapshot_id}',
        f'# cycle_id={c.cycle_id}',
        f'# compartment_ids={";".join(c.compartment_ids)}',
        f'# window=[{c.start.isoformat()},{c.end.isoformat()})',
        f'# metric_version={summary.metric_version}',
        f'# tariff_version={summary.tariff_version}',
        f'# scenario_version={summary.scenario_version or ""}',
        f'# currency={summary.currency}',
        f'# basis_status={summary.basis_status.value if summary.basis_status else ""}',
        f'# basis_label={summary.basis_label or ""}',
        f'# sensitivity_factor={summary.sensitivity_factor or ""}',
        f'# quantity_coverage_pct={summary.quantity_coverage_pct if summary.quantity_coverage_pct is not None else ""}',
        f'# pricing_coverage_pct={summary.pricing_coverage_pct if summary.pricing_coverage_pct is not None else ""}',
        f'# expected_days={summary.expected_days}',
        f'# priced_days={summary.priced_days}',
        f'# quantity_complete_days={summary.quantity_complete_days}',
    ]
    if grain is not None:
        meta.append(f'# evaluation_grain={grain}')
    meta.extend([
        '# notice=Price-only scenario analysis; unchanged physical consumption; historical EUR only',
        '# NULL is empty; accepted zero stays 0; no extrapolation; display 2dp differs from exact exports',
    ])
    if scenario is not None:
        meta.append(f'# scenario_name={scenario.name}')
        meta.append(f'# scenario_id={scenario.scenario_id}')
        meta.append(f'# scenario_version_num={scenario.version}')
        meta.append(f'# baseline_tariff_version={scenario.baseline_tariff_version}')
    return meta


def comparison_summary_rows(comparison: ScenarioComparison) -> list[dict]:
    b = comparison.baseline
    s = comparison.scenario
    c = b.context
    row = {
        'cycle_id': c.cycle_id,
        'compartment_ids': ';'.join(c.compartment_ids),
        'start': c.start.isoformat(),
        'end_exclusive': c.end.isoformat(),
        'currency': b.currency,
        'baseline_tariff_version': b.tariff_version,
        'scenario_version': s.scenario_version or '',
        'expected_days': b.expected_days,
        'baseline_priced_days': b.priced_days,
        'scenario_priced_days': s.priced_days,
        'baseline_pricing_coverage_pct': b.pricing_coverage_pct,
        'scenario_pricing_coverage_pct': s.pricing_coverage_pct,
        'baseline_quantity_complete_days': b.quantity_complete_days,
        'scenario_quantity_complete_days': s.quantity_complete_days,
        'baseline_official_total_cost_per_m2': b.official_total_cost_per_m2,
        'scenario_official_total_cost_per_m2': s.official_total_cost_per_m2,
        'delta_total_cost_per_m2': comparison.delta_total_cost_per_m2,
        'delta_total_pct': comparison.delta_total_pct,
        'baseline_official_total_cost_eur': b.official_total_cost_eur,
        'scenario_official_total_cost_eur': s.official_total_cost_eur,
        'delta_total_cost_eur': comparison.delta_total_cost_eur,
        'baseline_observed_total_cost_per_m2': b.observed_total_cost_per_m2,
        'scenario_observed_total_cost_per_m2': s.observed_total_cost_per_m2,
        'baseline_observed_total_cost_eur': b.observed_total_cost_eur,
        'scenario_observed_total_cost_eur': s.observed_total_cost_eur,
        'basis_status': b.basis_status.value if b.basis_status else '',
        'basis_label': b.basis_label or '',
        'sensitivity_factor': b.sensitivity_factor,
        'baseline_sensitivity_total_cost_eur': b.sensitivity_total_cost_eur,
        'scenario_sensitivity_total_cost_eur': s.sensitivity_total_cost_eur,
        'baseline_reasons': ';'.join(b.reason_codes),
        'scenario_reasons': ';'.join(s.reason_codes),
    }
    return [row]


def comparison_component_rows(comparison: ScenarioComparison) -> list[dict]:
    rows = []
    b_map = {comp.component: comp for comp in comparison.baseline.components}
    s_map = {comp.component: comp for comp in comparison.scenario.components}
    delta_map = {delta.component: delta for delta in comparison.component_deltas}

    for component in (EnergyComponent.HEAT, EnergyComponent.ELEC_HIGH, EnergyComponent.ELEC_LOW):
        bc = b_map.get(component)
        sc = s_map.get(component)
        delta = delta_map.get(component)
        if bc is None or sc is None:
            continue
        row = {
            'component': component.value,
            'price_unit': bc.price_unit,
            'expected_days': bc.expected_days,
            'baseline_priced_days': bc.priced_days,
            'scenario_priced_days': sc.priced_days,
            'baseline_pricing_coverage_pct': bc.pricing_coverage_pct,
            'scenario_pricing_coverage_pct': sc.pricing_coverage_pct,
            'quantity_accepted_days': bc.quantity_accepted_days,
            'quantity_coverage_pct': bc.quantity_coverage_pct,
            'baseline_official_cost_per_m2': bc.official_cost_per_m2,
            'scenario_official_cost_per_m2': sc.official_cost_per_m2,
            'delta_cost_per_m2': delta.delta_cost_per_m2 if delta else None,
            'delta_cost_pct': delta.delta_cost_pct if delta else None,
            'baseline_official_cost_eur': bc.official_cost_eur,
            'scenario_official_cost_eur': sc.official_cost_eur,
            'delta_cost_eur': delta.delta_cost_eur if delta else None,
            'baseline_observed_cost_per_m2': bc.observed_cost_per_m2,
            'scenario_observed_cost_per_m2': sc.observed_cost_per_m2,
            'baseline_observed_cost_eur': bc.observed_cost_eur,
            'scenario_observed_cost_eur': sc.observed_cost_eur,
            'baseline_share_pct': delta.baseline_share_pct if delta else None,
            'scenario_share_pct': delta.scenario_share_pct if delta else None,
            'baseline_reasons': ';'.join(bc.reason_codes),
            'scenario_reasons': ';'.join(sc.reason_codes),
        }
        rows.append(row)
    return rows


def comparison_daily_rows(comparison: ScenarioComparison) -> list[dict]:
    rows = []
    b_days = {}
    for c in comparison.baseline.components:
        for dc in c.day_costs:
            b_days[(dc.resource_date, dc.component)] = dc

    s_days = {}
    for c in comparison.scenario.components:
        for dc in c.day_costs:
            s_days[(dc.resource_date, dc.component)] = dc

    all_keys = sorted(set(b_days.keys()) | set(s_days.keys()))
    for dt, comp in all_keys:
        bdc = b_days.get((dt, comp))
        sdc = s_days.get((dt, comp))
        qty = bdc.quantity if bdc else (sdc.quantity if sdc else None)
        delta_eur = None
        if bdc and sdc and bdc.cost_eur is not None and sdc.cost_eur is not None:
            delta_eur = exact_sub(sdc.cost_eur, bdc.cost_eur)
        delta_m2 = None
        if bdc and sdc and bdc.cost_per_m2 is not None and sdc.cost_per_m2 is not None:
            delta_m2 = exact_sub(sdc.cost_per_m2, bdc.cost_per_m2)

        row = {
            'resource_date': dt.isoformat(),
            'component': comp.value,
            'source_quantity': qty.source_value if qty else None,
            'source_unit': qty.source_unit if qty else '',
            'accepted': qty.accepted if qty else False,
            'quality': qty.quality.value if qty else '',
            'applicability': qty.applicability.value if qty else '',
            'effective_area_m2': qty.basis.area_m2 if (qty and qty.basis) else None,
            'baseline_rate': bdc.rate.rate if (bdc and bdc.rate) else None,
            'baseline_price_unit': bdc.rate.price_unit if (bdc and bdc.rate) else '',
            'baseline_cost_per_m2': bdc.cost_per_m2 if bdc else None,
            'baseline_cost_eur': bdc.cost_eur if bdc else None,
            'baseline_reason': bdc.reason_code if bdc else '',
            'scenario_rate': sdc.rate.rate if (sdc and sdc.rate) else None,
            'scenario_price_unit': sdc.rate.price_unit if (sdc and sdc.rate) else '',
            'scenario_cost_per_m2': sdc.cost_per_m2 if sdc else None,
            'scenario_cost_eur': sdc.cost_eur if sdc else None,
            'scenario_reason': sdc.reason_code if sdc else '',
            'delta_cost_per_m2': delta_m2,
            'delta_cost_eur': delta_eur,
            'revision_id': qty.revision_id if qty else '',
            'logical_record_key': qty.logical_record_key if qty else '',
            'quantity_reason': qty.reason_code if qty else '',
        }
        rows.append(row)
    return rows


def comparison_weekly_rows(comparison: ScenarioComparison) -> list[dict]:
    from greenhouse.queries.cost import compare_weekly
    rows = []
    for w in compare_weekly(comparison):
        rows.append({
            'iso_week': w.iso_week,
            'start_date': w.start_date.isoformat(),
            'end_date': w.end_date.isoformat(),
            'days_count': w.days_count,
            'baseline_total_per_m2': w.baseline_total_per_m2,
            'scenario_total_per_m2': w.scenario_total_per_m2,
            'delta_total_per_m2': w.delta_total_per_m2,
            'delta_total_pct': w.delta_total_pct,
            'baseline_total_eur': w.baseline_total_eur,
            'scenario_total_eur': w.scenario_total_eur,
            'delta_total_eur': w.delta_total_eur,
            'heat_delta_eur': w.heat_delta_eur,
            'elec_high_delta_eur': w.elec_high_delta_eur,
            'elec_low_delta_eur': w.elec_low_delta_eur,
        })
    return rows


def tariff_rates_rows(tariff_rates: tuple[TariffRate, ...], scenario_rates: tuple[TariffRate, ...] | None = None) -> list[dict]:
    rows = []
    for r in tariff_rates:
        rows.append({
            'profile_or_scenario': 'baseline',
            'version_id': r.tariff_version,
            'rate_id': r.rate_id,
            'component': r.component.value,
            'currency': r.currency,
            'price_unit': r.price_unit,
            'rate': r.rate,
            'valid_from': r.valid_from.isoformat(),
            'valid_to_exclusive': r.valid_to_exclusive.isoformat(),
            'source_reference': r.source_reference,
        })
    if scenario_rates:
        for r in scenario_rates:
            rows.append({
                'profile_or_scenario': 'scenario',
                'version_id': r.tariff_version,
                'rate_id': r.rate_id,
                'component': r.component.value,
                'currency': r.currency,
                'price_unit': r.price_unit,
                'rate': r.rate,
                'valid_from': r.valid_from.isoformat(),
                'valid_to_exclusive': r.valid_to_exclusive.isoformat(),
                'source_reference': r.source_reference,
            })
    return rows


def summary_csv(comparison: ScenarioComparison, scenario: CostScenario | None = None) -> bytes:
    rows = comparison_summary_rows(comparison)
    meta = cost_metadata(comparison.scenario, scenario, grain='summary')
    return exports._document(tuple(rows[0]), rows, meta)


def components_csv(comparison: ScenarioComparison, scenario: CostScenario | None = None) -> bytes:
    rows = comparison_component_rows(comparison)
    meta = cost_metadata(comparison.scenario, scenario, grain='component')
    return exports._document(tuple(rows[0]), rows, meta)


def days_csv(comparison: ScenarioComparison, scenario: CostScenario | None = None) -> bytes:
    rows = comparison_daily_rows(comparison)
    meta = cost_metadata(comparison.scenario, scenario, grain='day')
    return exports._document(tuple(rows[0]), rows, meta)


def weekly_csv(comparison: ScenarioComparison, scenario: CostScenario | None = None) -> bytes:
    rows = comparison_weekly_rows(comparison)
    meta = cost_metadata(comparison.scenario, scenario, grain='week')
    if not rows:
        return exports._document(('iso_week', 'start_date', 'end_date'), [], meta)
    return exports._document(tuple(rows[0]), rows, meta)


def rates_csv(comparison: ScenarioComparison, baseline_rates: tuple[TariffRate, ...],
              scenario_rates: tuple[TariffRate, ...] | None = None,
              scenario: CostScenario | None = None) -> bytes:
    rows = tariff_rates_rows(baseline_rates, scenario_rates)
    meta = cost_metadata(comparison.scenario, scenario, grain='rates')
    return exports._document(tuple(rows[0]), rows, meta)


def comparison_json(comparison: ScenarioComparison) -> bytes:
    return to_json(comparison).encode('utf-8')


def scenario_json(scenario: CostScenario) -> bytes:
    return to_json(scenario).encode('utf-8')
