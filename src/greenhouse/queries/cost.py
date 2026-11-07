"""BR-06 energy cost and tariff scenario evaluation service (T20).

Implements exact day/component pricing over sealed EnergyQuantity facts,
historical baseline profile registration, and append-only scenario commands.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from typing import Callable, Protocol

from greenhouse.contracts import (
    BasisStatus, ComponentCost, ComponentCostDelta, CostScenario, DayCost,
    EnergyComponent, EnergyCostSummary, EnergyQuantity,
    PublicationRef, QueryContext, ScenarioChange, ScenarioCommand, ScenarioComparison,
    TariffProfile, TariffRate, WeeklyCostRow, WeeklyComparisonRow,
    exact_add, exact_product, exact_sub, exact_sum,
)
from greenhouse.queries.energy import EnergyService, energy_basis_gate


class CostAdapter(Protocol):
    def validate_cycle(self, scope_id: str, snapshot_id: str, cycle_id: str, compartment_id: str) -> bool: ...
    def get_tariff(self, tariff_version: str) -> TariffProfile | None: ...
    def get_baseline_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile | None: ...
    def register_tariff(self, profile: TariffProfile) -> TariffProfile: ...
    def register_historical_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile: ...
    def get_scenario(self, scenario_version: str) -> CostScenario | None: ...
    def get_scenario_by_request_id(self, request_id: str) -> CostScenario | None: ...
    def get_latest_scenario_version(self, scenario_id: str) -> CostScenario | None: ...
    def list_scenarios(self, data_scope_id: str | None = None) -> tuple[CostScenario, ...]: ...
    def register_scenario(self, scenario: CostScenario, command_payload: dict | None = None) -> CostScenario: ...
    def execute_scenario_transaction(
        self, command: ScenarioCommand, rate_factory: Callable[[str], tuple[TariffRate, ...]],
    ) -> CostScenario: ...


def delta_pct(delta: Decimal | None, baseline: Decimal | None) -> Decimal | None:
    if delta is None or baseline is None or baseline == Decimal('0'):
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return (Decimal('100') * delta) / baseline


def share_pct(part: Decimal | None, total: Decimal | None) -> Decimal | None:
    if part is None or total is None or total == Decimal('0'):
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return (Decimal('100') * part) / total


def derive_scenario_rates(
    baseline: TariffProfile, scenario_version: str, scenario_name: str, changes: tuple[ScenarioChange, ...],
) -> tuple[TariffRate, ...]:
    changes_by_comp = {c.component: c for c in changes}
    scenario_rates = []
    for rate in baseline.rates:
        change = changes_by_comp.get(rate.component)
        if change is not None:
            if change.price is not None:
                new_rate = change.price
                price_unit = change.price_unit
            else:
                with localcontext() as ctx:
                    ctx.prec = 50
                    pct_fraction = change.change_percent / Decimal('100')
                multiplier = exact_add(Decimal('1'), pct_fraction)
                new_rate = exact_product(rate.rate, multiplier)
                price_unit = rate.price_unit
            if new_rate < 0:
                raise ValueError(f'Resulting rate for {rate.component} is negative')
            scenario_rates.append(TariffRate(
                rate_id=f'rate:{scenario_version}:{rate.component.value}:{rate.valid_from}',
                tariff_version=scenario_version,
                component=rate.component,
                currency=rate.currency,
                price_unit=price_unit,
                rate=new_rate,
                valid_from=rate.valid_from,
                valid_to_exclusive=rate.valid_to_exclusive,
                source_reference=f'{rate.source_reference}; scenario {scenario_name}',
            ))
        else:
            scenario_rates.append(TariffRate(
                rate_id=f'rate:{scenario_version}:{rate.component.value}:{rate.valid_from}',
                tariff_version=scenario_version,
                component=rate.component,
                currency=rate.currency,
                price_unit=rate.price_unit,
                rate=rate.rate,
                valid_from=rate.valid_from,
                valid_to_exclusive=rate.valid_to_exclusive,
                source_reference=rate.source_reference,
            ))
    return tuple(scenario_rates)


class EnergyCostService:
    def __init__(self, energy_service: EnergyService, cost_adapter: CostAdapter):
        self.energy = energy_service
        self.adapter = cost_adapter

    def register_historical_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile:
        return self.adapter.register_historical_tariff(data_scope_id, compartment_id)

    def register_tariff(self, profile: TariffProfile) -> TariffProfile:
        return self.adapter.register_tariff(profile)

    def get_tariff(self, tariff_version: str) -> TariffProfile | None:
        return self.adapter.get_tariff(tariff_version)

    def get_baseline_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile | None:
        return self.adapter.get_baseline_tariff(data_scope_id, compartment_id)

    def list_scenarios(self, data_scope_id: str | None = None) -> tuple[CostScenario, ...]:
        return self.adapter.list_scenarios(data_scope_id)

    def get_scenario(self, scenario_version: str) -> CostScenario | None:
        return self.adapter.get_scenario(scenario_version)

    def get_latest_scenario_version(self, scenario_id: str) -> CostScenario | None:
        return self.adapter.get_latest_scenario_version(scenario_id)

    def execute_scenario_command(self, command: ScenarioCommand) -> CostScenario:
        if not isinstance(command, ScenarioCommand):
            raise ValueError('Expected a validated ScenarioCommand')

        # Validate publication identity against authoritative publication
        authoritative_pub = self.energy.adapter.publication(command.publication.publication_id)
        if authoritative_pub != command.publication:
            raise ValueError(
                f'Scenario command publication {command.publication} does not match authoritative {authoritative_pub}'
            )

        # Validate baseline tariff exists and belongs to publication scope
        baseline = self.adapter.get_tariff(command.baseline_version_id)
        if baseline is None:
            raise ValueError(f'Baseline tariff version not found: {command.baseline_version_id}')
        if baseline.data_scope_id != command.publication.scope_id:
            raise ValueError(
                f'Baseline scope {baseline.data_scope_id} does not match command publication scope {command.publication.scope_id}'
            )

        if command.analysis_context is not None:
            ac = command.analysis_context
            if ac.rule_version is not None:
                raise ValueError('Scenario analysis context cannot have a threshold rule')
            if (ac.publication_id != command.publication.publication_id or
                ac.scope_id != command.publication.scope_id or
                ac.snapshot_id != command.publication.snapshot_id):
                raise ValueError(
                    f'Analysis context publication {ac.publication_id} does not match command publication {command.publication.publication_id}'
                )
            if ac.start >= ac.end:
                raise ValueError('Invalid analysis context calendar window')
            if not ac.compartment_ids:
                raise ValueError('Analysis context must specify at least one compartment')
            for comp_id in ac.compartment_ids:
                is_valid = False
                if hasattr(self.adapter, 'validate_cycle'):
                    is_valid = self.adapter.validate_cycle(ac.scope_id, ac.snapshot_id, ac.cycle_id, comp_id)
                elif hasattr(self.energy, 'adapter') and hasattr(self.energy.adapter, 'cycle_window'):
                    try:
                        comp_context = replace(ac, compartment_ids=(comp_id,))
                        self.energy.adapter.cycle_window(comp_context)
                        is_valid = True
                    except Exception:
                        is_valid = False
                if not is_valid:
                    raise ValueError(
                        f'Cycle {ac.cycle_id} and compartment {comp_id} are not mapped to scope {ac.scope_id} and snapshot {ac.snapshot_id}'
                    )
            if baseline.compartment_id is not None and baseline.compartment_id not in ac.compartment_ids:
                raise ValueError(
                    f'Baseline tariff compartment {baseline.compartment_id} does not match analysis context compartments {ac.compartment_ids}'
                )

        # Pre-check R3: append to existing scenario cannot replace publication, snapshot, scope, or baseline
        if command.scenario_id is not None:
            existing = self.adapter.get_latest_scenario_version(command.scenario_id)
            if existing is not None:
                if (existing.publication.publication_id != command.publication.publication_id or
                    existing.publication.snapshot_id != command.publication.snapshot_id or
                    existing.publication.scope_id != command.publication.scope_id or
                    existing.baseline_tariff_version != command.baseline_version_id):
                    raise ValueError(
                        f'Cannot change publication, snapshot, scope, or baseline of existing logical scenario {command.scenario_id}'
                    )
                if existing.analysis_context is not None:
                    if command.analysis_context is None:
                        command = replace(command, analysis_context=existing.analysis_context)
                    elif command.analysis_context != existing.analysis_context:
                        raise ValueError(
                            f'Cannot change analysis context of existing logical scenario {command.scenario_id}'
                        )
                elif command.analysis_context is not None:
                    raise ValueError(
                        f'Cannot add analysis context to legacy scenario {command.scenario_id} that has no context'
                    )

        def rate_factory(scenario_version: str) -> tuple[TariffRate, ...]:
            return derive_scenario_rates(baseline, scenario_version, command.name, command.changes)

        return self.adapter.execute_scenario_transaction(command, rate_factory)

    def append_scenario_version(self, scenario_id: str, command: ScenarioCommand) -> CostScenario:
        if command.scenario_id is not None and command.scenario_id != scenario_id:
            raise ValueError(f'Command scenario_id {command.scenario_id} does not match {scenario_id}')
        latest = self.adapter.get_latest_scenario_version(scenario_id)
        if latest is None:
            raise ValueError(f'Cannot append version to non-existent scenario {scenario_id}')
        if (latest.publication.publication_id != command.publication.publication_id or
            latest.publication.snapshot_id != command.publication.snapshot_id or
            latest.publication.scope_id != command.publication.scope_id or
            latest.baseline_tariff_version != command.baseline_version_id):
            raise ValueError(
                f'Cannot change publication, snapshot, scope, or baseline of existing logical scenario {scenario_id}'
            )
        if latest.analysis_context is not None:
            if command.analysis_context is None:
                command = replace(command, analysis_context=latest.analysis_context)
            elif command.analysis_context != latest.analysis_context:
                raise ValueError(
                    f'Cannot change analysis context of existing logical scenario {scenario_id}'
                )
        elif command.analysis_context is not None:
            raise ValueError(
                f'Cannot add analysis context to legacy scenario {scenario_id} that has no context'
            )
        cmd = replace(command, scenario_id=scenario_id)
        return self.execute_scenario_command(cmd)

    def get_cost(self, context: QueryContext, *, tariff_version: str | None = None,
                 scenario_version: str | None = None) -> EnergyCostSummary:
        if context.rule_version is not None:
            raise ValueError('Energy cost queries do not accept a threshold rule')

        if tariff_version is not None and scenario_version is not None:
            raise ValueError('Cannot specify both tariff_version and scenario_version')

        if scenario_version is not None:
            scenario = self.adapter.get_scenario(scenario_version)
            if scenario is None:
                raise ValueError(f'Unknown scenario version: {scenario_version}')
            # Validate complete frozen publication identity
            if (context.scope_id != scenario.publication.scope_id or
                context.publication_id != scenario.publication.publication_id or
                context.snapshot_id != scenario.publication.snapshot_id):
                raise ValueError(
                    f'QueryContext publication identity ({context.scope_id}, {context.publication_id}, {context.snapshot_id}) '
                    f'does not match scenario publication ({scenario.publication.scope_id}, {scenario.publication.publication_id}, {scenario.publication.snapshot_id})'
                )
            if scenario.analysis_context is not None:
                ac = scenario.analysis_context
                if (context.publication_id, context.scope_id, context.snapshot_id, context.cycle_id,
                    context.compartment_ids, context.start, context.end) != (
                    ac.publication_id, ac.scope_id, ac.snapshot_id, ac.cycle_id,
                    ac.compartment_ids, ac.start, ac.end
                ):
                    raise ValueError(
                        f'Query context does not match frozen analysis context of scenario {scenario_version}'
                    )
            rates = scenario.rates
            tariff_ver = scenario.baseline_tariff_version
            scenario_ver = scenario_version
        elif tariff_version is not None:
            tariff = self.adapter.get_tariff(tariff_version)
            if tariff is None:
                raise ValueError(f'Unknown tariff version: {tariff_version}')
            if context.scope_id != tariff.data_scope_id:
                raise ValueError(
                    f'Tariff scope {tariff.data_scope_id} does not match query context scope {context.scope_id}'
                )
            if tariff.compartment_id is not None and (not context.compartment_ids or context.compartment_ids[0] != tariff.compartment_id):
                raise ValueError(
                    f'Tariff compartment {tariff.compartment_id} does not match query context compartments {context.compartment_ids}'
                )
            rates = tariff.rates
            tariff_ver = tariff_version
            scenario_ver = None
        else:
            comp_id = context.compartment_ids[0] if context.compartment_ids else None
            tariff = self.adapter.get_baseline_tariff(context.scope_id, comp_id)
            if tariff is None:
                raise ValueError(f'No baseline tariff found for scope {context.scope_id}')
            rates = tariff.rates
            tariff_ver = tariff.tariff_version
            scenario_ver = None

        # Effective cycle window and clipping
        cycle = self.energy.adapter.cycle_window(context)
        eff_start = max(context.start, cycle.start)
        eff_end = min(context.end, cycle.end)
        if eff_start >= eff_end:
            raise ValueError('Energy cost query does not overlap cycle')
        expected_days = (eff_end - eff_start).days
        overall_reason_codes = set()
        if eff_start != context.start or eff_end != context.end:
            overall_reason_codes.add('window_clipped_to_cycle')

        effective_context = replace(context, start=eff_start, end=eff_end)
        quantities = self.energy.get_quantities(effective_context)
        rates_by_comp: dict[EnergyComponent, list[TariffRate]] = {}
        for r in rates:
            rates_by_comp.setdefault(r.component, []).append(r)

        day_costs_by_comp: dict[EnergyComponent, list[DayCost]] = {
            EnergyComponent.HEAT: [],
            EnergyComponent.ELEC_HIGH: [],
            EnergyComponent.ELEC_LOW: [],
        }

        for q in quantities:
            matching_rate = None
            for r in rates_by_comp.get(q.component, ()):
                if r.valid_from <= q.resource_date < r.valid_to_exclusive:
                    matching_rate = r
                    break

            rate_missing = matching_rate is None
            qty_invalid = not q.accepted

            if rate_missing and qty_invalid:
                q_reason = q.reason_code or 'missing_energy_quantity'
                day_costs_by_comp[q.component].append(DayCost(
                    resource_date=q.resource_date,
                    component=q.component,
                    quantity=q,
                    rate=None,
                    cost_per_m2=None,
                    cost_eur=None,
                    reason_code=f'missing_rate;{q_reason}',
                ))
            elif rate_missing:
                day_costs_by_comp[q.component].append(DayCost(
                    resource_date=q.resource_date,
                    component=q.component,
                    quantity=q,
                    rate=None,
                    cost_per_m2=None,
                    cost_eur=None,
                    reason_code='missing_rate',
                ))
            elif qty_invalid:
                day_costs_by_comp[q.component].append(DayCost(
                    resource_date=q.resource_date,
                    component=q.component,
                    quantity=q,
                    rate=matching_rate,
                    cost_per_m2=None,
                    cost_eur=None,
                    reason_code=q.reason_code or 'missing_energy_quantity',
                ))
            else:
                if q.component == EnergyComponent.HEAT:
                    if matching_rate.price_unit == 'EUR/MJ':
                        cost_per_m2 = exact_product(q.source_value, matching_rate.rate)
                    elif matching_rate.price_unit == 'EUR/kWh':
                        with localcontext() as ctx:
                            ctx.prec = 50
                            rate_per_mj = matching_rate.rate / Decimal('3.6')
                        cost_per_m2 = exact_product(q.source_value, rate_per_mj)
                    else:
                        raise ValueError(f'Unsupported price unit: {matching_rate.price_unit}')
                else:
                    if matching_rate.price_unit == 'EUR/kWh':
                        cost_per_m2 = exact_product(q.source_value, matching_rate.rate)
                    else:
                        raise ValueError(f'Unsupported price unit: {matching_rate.price_unit}')

                cost_eur = exact_product(cost_per_m2, q.basis.area_m2) if (q.basis is not None and q.basis.area_m2) else None
                day_costs_by_comp[q.component].append(DayCost(
                    resource_date=q.resource_date,
                    component=q.component,
                    quantity=q,
                    rate=matching_rate,
                    cost_per_m2=cost_per_m2,
                    cost_eur=cost_eur,
                ))

        comp_costs = []
        for comp in (EnergyComponent.HEAT, EnergyComponent.ELEC_HIGH, EnergyComponent.ELEC_LOW):
            d_costs = tuple(day_costs_by_comp[comp])
            priced_days_list = [d for d in d_costs if d.cost_per_m2 is not None]
            priced_days = len(priced_days_list)
            accepted_days = sum(1 for d in d_costs if d.quantity.accepted)

            pricing_coverage_pct = (Decimal(str(priced_days)) * Decimal('100')) / Decimal(str(expected_days)) if expected_days > 0 else Decimal('0')
            quantity_coverage_pct = (Decimal(str(accepted_days)) * Decimal('100')) / Decimal(str(expected_days)) if expected_days > 0 else Decimal('0')

            comp_quantities = [d.quantity for d in d_costs]
            comp_basis_gate = energy_basis_gate(q.basis for q in comp_quantities if q.accepted)
            all_basis_none = bool(priced_days_list and all(d.quantity.basis is None for d in priced_days_list))

            reasons = set()
            for d in d_costs:
                if d.reason_code is not None:
                    for r_part in d.reason_code.split(';'):
                        reasons.add(r_part)
                        overall_reason_codes.add(r_part)

            can_pool_m2 = comp_basis_gate.per_m2_compatible or all_basis_none
            if priced_days == 0:
                obs_cost_m2 = None
            elif can_pool_m2:
                obs_cost_m2 = exact_sum(d.cost_per_m2 for d in priced_days_list)
            else:
                obs_cost_m2 = None

            eur_priced_days = [d for d in priced_days_list if d.cost_eur is not None]
            if priced_days > 0 and len(eur_priced_days) == priced_days and comp_basis_gate.absolute_compatible:
                obs_cost_eur = exact_sum(d.cost_eur for d in eur_priced_days)
            else:
                obs_cost_eur = None

            if priced_days == expected_days:
                official_cost_m2 = obs_cost_m2 if comp_basis_gate.per_m2_compatible else None
                official_cost_eur = obs_cost_eur if (obs_cost_eur is not None and comp_basis_gate.absolute_compatible) else None
            else:
                official_cost_m2 = None
                official_cost_eur = None

            unit = 'EUR/MJ' if comp == EnergyComponent.HEAT else 'EUR/kWh'
            comp_costs.append(ComponentCost(
                component=comp,
                price_unit=unit,
                expected_days=expected_days,
                priced_days=priced_days,
                pricing_coverage_pct=pricing_coverage_pct,
                observed_cost_per_m2=obs_cost_m2,
                observed_cost_eur=obs_cost_eur,
                official_cost_per_m2=official_cost_m2,
                official_cost_eur=official_cost_eur,
                day_costs=d_costs,
                reason_codes=tuple(sorted(reasons)),
                quantity_accepted_days=accepted_days,
                quantity_coverage_pct=quantity_coverage_pct,
            ))

        # Evaluate basis gate for total pooling
        basis_gate = energy_basis_gate(q.basis for q in quantities if q.accepted)
        if not basis_gate.per_m2_compatible:
            overall_reason_codes.add('incompatible_or_unknown_basis')
        overall_reason_codes.update(basis_gate.reason_codes)

        # Complete priced days intersection across calendar dates for all 3 components
        priced_dates_per_comp = [
            {d.resource_date for d in day_costs_by_comp[c] if d.cost_per_m2 is not None}
            for c in (EnergyComponent.HEAT, EnergyComponent.ELEC_HIGH, EnergyComponent.ELEC_LOW)
        ]
        complete_priced_dates = set.intersection(*priced_dates_per_comp)
        total_priced_days = len(complete_priced_dates)
        total_pricing_coverage_pct = (Decimal(str(total_priced_days)) * Decimal('100')) / Decimal(str(expected_days)) if expected_days > 0 else Decimal('0')

        accepted_dates_per_comp = [
            {d.resource_date for d in day_costs_by_comp[c] if d.quantity.accepted}
            for c in (EnergyComponent.HEAT, EnergyComponent.ELEC_HIGH, EnergyComponent.ELEC_LOW)
        ]
        complete_qty_dates = set.intersection(*accepted_dates_per_comp)
        total_qty_days = len(complete_qty_dates)
        total_qty_coverage_pct = (Decimal(str(total_qty_days)) * Decimal('100')) / Decimal(str(expected_days)) if expected_days > 0 else Decimal('0')

        if all(c.observed_cost_per_m2 is not None for c in comp_costs) and basis_gate.per_m2_compatible:
            observed_total_cost_per_m2 = exact_sum(c.observed_cost_per_m2 for c in comp_costs)
        else:
            observed_total_cost_per_m2 = None

        if all(c.observed_cost_eur is not None for c in comp_costs) and basis_gate.absolute_compatible:
            observed_total_cost_eur = exact_sum(c.observed_cost_eur for c in comp_costs)
        else:
            observed_total_cost_eur = None

        all_days_priced = (total_priced_days == expected_days)
        if all_days_priced and basis_gate.per_m2_compatible:
            official_total_cost_per_m2 = exact_sum(c.official_cost_per_m2 for c in comp_costs if c.official_cost_per_m2 is not None)
        else:
            official_total_cost_per_m2 = None

        if all_days_priced and basis_gate.absolute_compatible and all(c.official_cost_eur is not None for c in comp_costs):
            official_total_cost_eur = exact_sum(c.official_cost_eur for c in comp_costs)
        else:
            official_total_cost_eur = None

        sensitivity_factor = None
        sensitivity_total_cost_eur = None
        is_explicit_agc_shared_basis = (
            basis_gate.status == BasisStatus.ASSUMED
            and all(
                q.basis is not None and q.basis.status == BasisStatus.ASSUMED
                and q.basis.assumption_id == 'AGC_SHARED_BASIS_V1'
                and q.basis.assumption_version == 1
                and q.basis.area_m2 == Decimal('62.5')
                for q in quantities if q.accepted
            )
        )
        if is_explicit_agc_shared_basis and official_total_cost_eur is not None:
            sensitivity_factor = Decimal('1.536')
            sensitivity_total_cost_eur = exact_product(official_total_cost_eur, sensitivity_factor)

        return EnergyCostSummary(
            context=context,
            tariff_version=tariff_ver,
            scenario_version=scenario_ver,
            currency='EUR',
            components=(comp_costs[0], comp_costs[1], comp_costs[2]),
            expected_days=expected_days,
            priced_days=total_priced_days,
            pricing_coverage_pct=total_pricing_coverage_pct,
            observed_total_cost_per_m2=observed_total_cost_per_m2,
            observed_total_cost_eur=observed_total_cost_eur,
            official_total_cost_per_m2=official_total_cost_per_m2,
            official_total_cost_eur=official_total_cost_eur,
            basis_status=basis_gate.status,
            basis_label=basis_gate.label,
            sensitivity_factor=sensitivity_factor,
            sensitivity_total_cost_eur=sensitivity_total_cost_eur,
            reason_codes=tuple(sorted(overall_reason_codes)),
            quantity_complete_days=total_qty_days,
            quantity_coverage_pct=total_qty_coverage_pct,
        )

    def compare_scenarios(self, context: QueryContext, *, scenario_version: str,
                          baseline_tariff_version: str | None = None) -> ScenarioComparison:
        scenario_obj = self.adapter.get_scenario(scenario_version)
        if scenario_obj is None:
            raise ValueError(f'Scenario not found: {scenario_version}')

        if scenario_obj.analysis_context is not None:
            ac = scenario_obj.analysis_context
            if (context.publication_id, context.scope_id, context.snapshot_id, context.cycle_id,
                context.compartment_ids, context.start, context.end) != (
                ac.publication_id, ac.scope_id, ac.snapshot_id, ac.cycle_id,
                ac.compartment_ids, ac.start, ac.end
            ):
                raise ValueError(
                    f'Comparison context does not match frozen analysis context of scenario {scenario_version}'
                )

        if baseline_tariff_version is None:
            baseline_tariff_version = scenario_obj.baseline_tariff_version
        elif baseline_tariff_version != scenario_obj.baseline_tariff_version:
            raise ValueError(
                f'Explicit baseline {baseline_tariff_version} conflicts with scenario pinned baseline {scenario_obj.baseline_tariff_version}'
            )

        baseline = self.get_cost(context, tariff_version=baseline_tariff_version)
        scenario = self.get_cost(context, scenario_version=scenario_version)
        return compare_summaries(baseline, scenario)

    def get_weekly_cost(self, context_or_summary: QueryContext | EnergyCostSummary, *,
                        scenario_version: str | None = None,
                        tariff_version: str | None = None) -> tuple[WeeklyCostRow, ...]:
        if isinstance(context_or_summary, EnergyCostSummary):
            return aggregate_weekly_cost(context_or_summary)
        summary = self.get_cost(context_or_summary, scenario_version=scenario_version, tariff_version=tariff_version)
        return aggregate_weekly_cost(summary)

    def compare_scenarios_weekly(self, comp_or_context: ScenarioComparison | QueryContext, *,
                                 scenario_version: str | None = None,
                                 baseline_tariff_version: str | None = None) -> tuple[WeeklyComparisonRow, ...]:
        if isinstance(comp_or_context, ScenarioComparison):
            return compare_weekly(comp_or_context)
        if scenario_version is None:
            raise ValueError('scenario_version required when querying by context')
        comp = self.compare_scenarios(comp_or_context, scenario_version=scenario_version, baseline_tariff_version=baseline_tariff_version)
        return compare_weekly(comp)


def compare_summaries(baseline: EnergyCostSummary, scenario: EnergyCostSummary) -> ScenarioComparison:
    deltas = []
    for b_comp, s_comp in zip(baseline.components, scenario.components):
        delta_m2 = (exact_sub(s_comp.official_cost_per_m2, b_comp.official_cost_per_m2)
                    if (s_comp.official_cost_per_m2 is not None and b_comp.official_cost_per_m2 is not None) else None)
        delta_eur = (exact_sub(s_comp.official_cost_eur, b_comp.official_cost_eur)
                     if (s_comp.official_cost_eur is not None and b_comp.official_cost_eur is not None) else None)

        d_pct = delta_pct(delta_m2, b_comp.official_cost_per_m2)
        if d_pct is None and delta_eur is not None and b_comp.official_cost_eur:
            d_pct = delta_pct(delta_eur, b_comp.official_cost_eur)

        b_share = share_pct(b_comp.official_cost_per_m2, baseline.official_total_cost_per_m2)
        if b_share is None and b_comp.official_cost_eur is not None and baseline.official_total_cost_eur:
            b_share = share_pct(b_comp.official_cost_eur, baseline.official_total_cost_eur)

        s_share = share_pct(s_comp.official_cost_per_m2, scenario.official_total_cost_per_m2)
        if s_share is None and s_comp.official_cost_eur is not None and scenario.official_total_cost_eur:
            s_share = share_pct(s_comp.official_cost_eur, scenario.official_total_cost_eur)

        deltas.append(ComponentCostDelta(
            component=b_comp.component,
            baseline_cost_per_m2=b_comp.official_cost_per_m2,
            scenario_cost_per_m2=s_comp.official_cost_per_m2,
            delta_cost_per_m2=delta_m2,
            delta_cost_pct=d_pct,
            baseline_cost_eur=b_comp.official_cost_eur,
            scenario_cost_eur=s_comp.official_cost_eur,
            delta_cost_eur=delta_eur,
            baseline_share_pct=b_share,
            scenario_share_pct=s_share,
        ))

    delta_total_m2 = (exact_sub(scenario.official_total_cost_per_m2, baseline.official_total_cost_per_m2)
                      if (scenario.official_total_cost_per_m2 is not None and baseline.official_total_cost_per_m2 is not None) else None)
    delta_total_eur = (exact_sub(scenario.official_total_cost_eur, baseline.official_total_cost_eur)
                       if (scenario.official_total_cost_eur is not None and baseline.official_total_cost_eur is not None) else None)

    delta_total_p = delta_pct(delta_total_m2, baseline.official_total_cost_per_m2)
    if delta_total_p is None and delta_total_eur is not None and baseline.official_total_cost_eur:
        delta_total_p = delta_pct(delta_total_eur, baseline.official_total_cost_eur)

    return ScenarioComparison(
        baseline=baseline,
        scenario=scenario,
        component_deltas=(deltas[0], deltas[1], deltas[2]),
        delta_total_cost_per_m2=delta_total_m2,
        delta_total_pct=delta_total_p,
        delta_total_cost_eur=delta_total_eur,
    )


def aggregate_weekly_cost(summary: EnergyCostSummary) -> tuple[WeeklyCostRow, ...]:
    by_week: dict[str, dict] = {}

    for comp_summary in summary.components:
        comp = comp_summary.component
        for dc in comp_summary.day_costs:
            dt = dc.resource_date
            year, week_num, _ = dt.isocalendar()
            iso_week = f'{year}-W{week_num:02d}'

            entry = by_week.setdefault(iso_week, {
                'start_date': dt,
                'end_date': dt,
                'dates': set(),
                'days': {},
            })
            if dt < entry['start_date']:
                entry['start_date'] = dt
            if dt > entry['end_date']:
                entry['end_date'] = dt
            entry['dates'].add(dt)
            entry['days'].setdefault(dt, {})[comp] = dc

    weekly_rows = []
    for iso_week in sorted(by_week.keys()):
        data = by_week[iso_week]
        dates = sorted(data['dates'])
        days_count = len(dates)

        priced_count = 0
        qty_count = 0
        heat_dc_list = []
        high_dc_list = []
        low_dc_list = []
        reasons = set()

        for dt in dates:
            day_map = data['days'].get(dt, {})
            heat_dc = day_map.get(EnergyComponent.HEAT)
            high_dc = day_map.get(EnergyComponent.ELEC_HIGH)
            low_dc = day_map.get(EnergyComponent.ELEC_LOW)

            day_priced = all(dc and dc.cost_per_m2 is not None for dc in (heat_dc, high_dc, low_dc))
            if day_priced:
                priced_count += 1
            day_qty = all(dc and dc.quantity and dc.quantity.accepted for dc in (heat_dc, high_dc, low_dc))
            if day_qty:
                qty_count += 1

            for dc in (heat_dc, high_dc, low_dc):
                if dc and dc.reason_code:
                    for r_part in dc.reason_code.split(';'):
                        reasons.add(r_part)

            if heat_dc:
                heat_dc_list.append(heat_dc)
            if high_dc:
                high_dc_list.append(high_dc)
            if low_dc:
                low_dc_list.append(low_dc)

        # Collect all accepted quantities in this week across all components
        all_week_dcs = heat_dc_list + high_dc_list + low_dc_list
        week_quantities = [dc.quantity for dc in all_week_dcs if dc.quantity and dc.quantity.accepted]

        # Evaluate week-level basis gate
        week_basis_gate = energy_basis_gate(q.basis for q in week_quantities) if week_quantities else None
        if week_basis_gate is not None:
            if not week_basis_gate.per_m2_compatible:
                reasons.add('incompatible_or_unknown_basis')
            reasons.update(week_basis_gate.reason_codes)
        elif all_week_dcs and any(dc.quantity and dc.quantity.basis is None for dc in all_week_dcs):
            reasons.add('incompatible_or_unknown_basis')

        # Effective area: must be single common area across all accepted quantities in the week
        distinct_areas = {
            q.basis.area_m2 for q in week_quantities
            if q.basis and q.basis.area_m2 is not None
        }
        if len(distinct_areas) == 1 and week_basis_gate and week_basis_gate.per_m2_compatible:
            effective_area = next(iter(distinct_areas))
        else:
            effective_area = None

        # Component-level pooling
        def pool_component(dc_list):
            if len(dc_list) != days_count or any(d.cost_per_m2 is None for d in dc_list):
                comp_m2 = None
            else:
                comp_quantities = [d.quantity for d in dc_list if d.quantity and d.quantity.accepted]
                all_none = bool(dc_list and all(d.quantity.basis is None for d in dc_list if d.quantity))
                c_gate = energy_basis_gate(q.basis for q in comp_quantities) if comp_quantities else None
                if all_none or (c_gate and c_gate.per_m2_compatible):
                    comp_m2 = exact_sum(d.cost_per_m2 for d in dc_list)
                else:
                    comp_m2 = None

            if len(dc_list) == days_count and all(d.cost_eur is not None for d in dc_list):
                comp_eur = exact_sum(d.cost_eur for d in dc_list)
            else:
                comp_eur = None

            return comp_m2, comp_eur

        heat_m2, heat_eur = pool_component(heat_dc_list)
        high_m2, high_eur = pool_component(high_dc_list)
        low_m2, low_eur = pool_component(low_dc_list)

        # Total per-m2 requires week_basis_gate to be per_m2_compatible
        if (
            heat_m2 is not None and high_m2 is not None and low_m2 is not None
            and week_basis_gate is not None and week_basis_gate.per_m2_compatible
        ):
            total_m2 = exact_sum([heat_m2, high_m2, low_m2])
        else:
            total_m2 = None

        # Total EUR requires absolute_compatible
        if (
            heat_eur is not None and high_eur is not None and low_eur is not None
            and (week_basis_gate is None or week_basis_gate.absolute_compatible)
        ):
            total_eur = exact_sum([heat_eur, high_eur, low_eur])
        else:
            total_eur = None

        weekly_rows.append(WeeklyCostRow(
            iso_week=iso_week,
            start_date=data['start_date'],
            end_date=data['end_date'],
            days_count=days_count,
            priced_days=priced_count,
            quantity_complete_days=qty_count,
            heat_cost_per_m2=heat_m2,
            elec_high_cost_per_m2=high_m2,
            elec_low_cost_per_m2=low_m2,
            total_cost_per_m2=total_m2,
            heat_cost_eur=heat_eur,
            elec_high_cost_eur=high_eur,
            elec_low_cost_eur=low_eur,
            total_cost_eur=total_eur,
            effective_area_m2=effective_area,
            reason_codes=tuple(sorted(reasons)),
        ))
    return tuple(weekly_rows)


def compare_weekly(comparison: ScenarioComparison) -> tuple[WeeklyComparisonRow, ...]:
    b_weeks = {w.iso_week: w for w in aggregate_weekly_cost(comparison.baseline)}
    s_weeks = {w.iso_week: w for w in aggregate_weekly_cost(comparison.scenario)}

    all_weeks = sorted(set(b_weeks.keys()) | set(s_weeks.keys()))
    rows = []
    for iso_week in all_weeks:
        bw = b_weeks.get(iso_week)
        sw = s_weeks.get(iso_week)
        s_date = bw.start_date if bw else sw.start_date
        e_date = bw.end_date if bw else sw.end_date
        days = bw.days_count if bw else sw.days_count

        b_tot_m2 = bw.total_cost_per_m2 if bw else None
        s_tot_m2 = sw.total_cost_per_m2 if sw else None
        b_tot_eur = bw.total_cost_eur if bw else None
        s_tot_eur = sw.total_cost_eur if sw else None

        delta_m2 = exact_sub(s_tot_m2, b_tot_m2) if (s_tot_m2 is not None and b_tot_m2 is not None) else None
        delta_eur = exact_sub(s_tot_eur, b_tot_eur) if (s_tot_eur is not None and b_tot_eur is not None) else None
        d_pct = delta_pct(delta_m2, b_tot_m2)
        if d_pct is None and delta_eur is not None and b_tot_eur:
            d_pct = delta_pct(delta_eur, b_tot_eur)

        heat_delta = exact_sub(sw.heat_cost_eur, bw.heat_cost_eur) if (bw and sw and bw.heat_cost_eur is not None and sw.heat_cost_eur is not None) else None
        high_delta = exact_sub(sw.elec_high_cost_eur, bw.elec_high_cost_eur) if (bw and sw and bw.elec_high_cost_eur is not None and sw.elec_high_cost_eur is not None) else None
        low_delta = exact_sub(sw.elec_low_cost_eur, bw.elec_low_cost_eur) if (bw and sw and bw.elec_low_cost_eur is not None and sw.elec_low_cost_eur is not None) else None

        rows.append(WeeklyComparisonRow(
            iso_week=iso_week,
            start_date=s_date,
            end_date=e_date,
            days_count=days,
            baseline_total_per_m2=b_tot_m2,
            scenario_total_per_m2=s_tot_m2,
            delta_total_per_m2=delta_m2,
            delta_total_pct=d_pct,
            baseline_total_eur=b_tot_eur,
            scenario_total_eur=s_tot_eur,
            delta_total_eur=delta_eur,
            heat_delta_eur=heat_delta,
            elec_high_delta_eur=high_delta,
            elec_low_delta_eur=low_delta,
        ))
    return tuple(rows)
