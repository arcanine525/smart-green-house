"""BR04 drill-across: independent sealed quantities and harvest A aggregates."""
from dataclasses import replace
from decimal import Decimal

from greenhouse.contracts import (
    BasisStatus, EnergyIntensity, EnergyPortfolio, HarvestAContribution, exact_product, quantity_ratio,
)
from greenhouse.queries.energy import energy_basis_gate
from greenhouse.queries.harvest import exact_sum, verify_grain


ratio = quantity_ratio


def join_intensity(energy, cycle, records, state, production_bases):
    """Both source sets already aggregated/selected independently; no day join.

    Harvest completeness deliberately follows the sealed BR05 contract (which
    includes unresolved records and pairing). Only accepted ProdA enters mass.
    A descriptive subwindow ratio never enters whole-cycle ranking/portfolio.
    """
    context, publication = energy.context, energy.publication
    verify_grain(context, publication, cycle, records)
    bases = {b.basis_version_id: b for b in production_bases}
    if len(bases) != len(production_bases):
        raise ValueError('Duplicate frozen production basis')
    contributions = tuple(HarvestAContribution(r, bases.get(r.prod_a.basis_version_id),
        exact_product(r.prod_a.value, bases[r.prod_a.basis_version_id].area_m2)
        if r.prod_a.accepted and r.prod_a.basis_version_id in bases else None) for r in records)
    accepted = tuple(c for c in contributions if c.record.prod_a.accepted)
    observed = exact_sum(c.absolute_a_kg for c in accepted if c.absolute_a_kg is not None)
    if not accepted and state.snapshot_complete:
        observed = Decimal(0)
    a_gate = energy_basis_gate(c.basis for c in accepted)
    # Empty complete harvest window is a known zero denominator; it never opens
    # the ratio gate and does not fabricate a basis record.
    a_complete = state.snapshot_complete and all(r.prod_a.accepted for r in records)
    official_a = observed if a_complete and (not accepted or a_gate.absolute_compatible) else None
    energy_bases = tuple(t.basis for c in energy.components for t in c.basis_totals)
    gate = energy_basis_gate(energy_bases + tuple(c.basis for c in accepted))
    reasons = set(energy.reason_codes) | set(gate.reason_codes) | set(state.reasons)
    if not state.snapshot_complete:
        reasons.add('harvest_snapshot_incomplete')
    if not records:
        reasons.add('no_harvest_in_window')
    if any(not r.prod_a.accepted for r in records):
        reasons.add('unusable_class_a_production')
    if any(c.basis is None for c in accepted):
        reasons.add('production_a_basis_gap')
    if observed == 0:
        reasons.add('zero_class_a_production')
    if observed is None:
        reasons.add('no_usable_class_a_production')
    whole = (context.start, context.end) == (cycle.start, cycle.end)
    if not whole:
        reasons.add('descriptive_subwindow_not_cycle_comparable')
    usable = energy.official_total_kwh is not None and official_a is not None and official_a > 0 and gate.absolute_compatible
    total = ratio(energy.official_total_kwh, official_a) if usable else None
    heat = ratio(energy.components[0].official_absolute_kwh, official_a) if usable else None
    light = ratio(exact_sum(c.official_absolute_kwh for c in energy.components[1:]), official_a) if usable else None
    assumptions = tuple(sorted({f'{b.assumption_id}:v{b.assumption_version}'
        for b in energy_bases + tuple(c.basis for c in accepted) if b and b.status == BasisStatus.ASSUMED}))
    # Explicit Resources-only area scenario, leaving verified Production fixed.
    sensitivity = usable and bool(energy_bases) and all(b and b.status == BasisStatus.ASSUMED
        and (b.assumption_id, b.assumption_version, b.area_m2) == ('AGC_SHARED_BASIS_V1', 1, Decimal('62.5'))
        for b in energy_bases) and all(c.basis and c.basis.area_m2 == Decimal('62.5') and (
            (c.basis.status == BasisStatus.VERIFIED and c.basis.basis_kind == 'production_area') or
            (c.basis.status == BasisStatus.ASSUMED and c.basis.basis_kind == 'growing_area_assumed'
             and (c.basis.assumption_id, c.basis.assumption_version) == ('AGC_SHARED_BASIS_V1', 1)))
            for c in accepted)
    factor = Decimal('1.536') if sensitivity else None
    sensitive = ratio(exact_product(energy.official_total_kwh, factor), official_a) if sensitivity else None
    return EnergyIntensity(publication, context, cycle, energy, state, contributions, observed,
        official_a, gate, assumptions, total, heat, light, bool(usable and whole),
        tuple(sorted(reasons)), factor, sensitive)


def portfolio(members):
    members = tuple(members)
    if not members:
        raise ValueError('Portfolio requires an explicit nonempty population')
    publication = members[0].publication
    if any(m.publication != publication for m in members) or len({m.cycle.cycle_id for m in members}) != len(members):
        raise ValueError('Portfolio publication mismatch or duplicate cycle')
    candidates = tuple(m for m in members if m.comparison_eligible)
    signatures = {(m.context.start, m.context.end, m.assumptions, m.metric_version,
                   m.energy.quantity_version, m.basis_gate.status) for m in candidates}
    compatible = len(signatures) <= 1
    included = candidates if compatible else ()
    energy = exact_sum(m.energy.official_total_kwh for m in included)
    a = exact_sum(m.official_a_kg for m in included)
    ids = tuple(m.cycle.cycle_id for m in included)
    reasons = (() if included else ('no_comparable_eligible_population',))
    if not compatible:
        reasons += ('mixed_windows_or_assumption_versions',)
    return EnergyPortfolio(publication, members, ids,
        tuple(m.cycle.cycle_id for m in members if m.cycle.cycle_id not in ids),
        energy, a, ratio(energy, a), reasons)


class EnergyIntensityService:
    def __init__(self, energy_service, harvest_adapter):
        self.energy = energy_service
        self.harvest = harvest_adapter

    def get_intensity(self, context):
        energy = self.energy.get_summary(context)
        publication = self.harvest.publication(context.publication_id)
        if publication != energy.publication:
            raise ValueError('Energy and harvest publication versions differ')
        cycle = self.energy.adapter.cycle_window(context)
        if self.harvest.cycle_window(context) != cycle:
            raise ValueError('Energy and harvest cycle windows differ')
        effective = replace(context, start=energy.window_start, end=energy.window_end)
        records = self.harvest.read_harvest_records(effective, publication)
        state = self.harvest.harvest_state(effective)
        bases = self.energy.adapter.read_production_bases(effective)
        return join_intensity(energy, cycle, records, state, bases)

    def get_portfolio(self, contexts):
        return portfolio(self.get_intensity(c) for c in contexts)
