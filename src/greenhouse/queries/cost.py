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
    TariffProfile, TariffRate, exact_add, exact_product, exact_sub, exact_sum,
)
from greenhouse.queries.energy import EnergyService, energy_basis_gate


class CostAdapter(Protocol):
    def get_tariff(self, tariff_version: str) -> TariffProfile | None: ...
    def get_baseline_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile | None: ...
    def register_tariff(self, profile: TariffProfile) -> TariffProfile: ...
    def register_historical_tariff(self, data_scope_id: str, compartment_id: str | None = None) -> TariffProfile: ...
    def get_scenario(self, scenario_version: str) -> CostScenario | None: ...
    def get_scenario_by_request_id(self, request_id: str) -> CostScenario | None: ...
    def get_latest_scenario_version(self, scenario_id: str) -> CostScenario | None: ...
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

    def execute_scenario_command(self, command: ScenarioCommand) -> CostScenario:
        if not isinstance(command, ScenarioCommand):
            raise ValueError('Expected a validated ScenarioCommand')

        # Validate publication identity against authoritative publication
        authoritative_pub = self.energy.adapter.publication(command.publication.publication_id)
        if authoritative_pub != command.publication:
            raise ValueError(
                f'Scenario command publication {command.publication} does not match authoritative {authoritative_pub}'
            )

        baseline = self.adapter.get_tariff(command.baseline_version_id)
        if baseline is None:
            raise ValueError(f'Baseline tariff version not found: {command.baseline_version_id}')
        if baseline.data_scope_id != command.publication.scope_id:
            raise ValueError(
                f'Baseline scope {baseline.data_scope_id} does not match command publication scope {command.publication.scope_id}'
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

        if baseline_tariff_version is None:
            baseline_tariff_version = scenario_obj.baseline_tariff_version
        elif baseline_tariff_version != scenario_obj.baseline_tariff_version:
            raise ValueError(
                f'Explicit baseline {baseline_tariff_version} conflicts with scenario pinned baseline {scenario_obj.baseline_tariff_version}'
            )

        baseline = self.get_cost(context, tariff_version=baseline_tariff_version)
        scenario = self.get_cost(context, scenario_version=scenario_version)

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
