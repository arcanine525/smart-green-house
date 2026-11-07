"""Shared frozen quantities for intensity and cost; no prices or harvest joins."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from greenhouse.contracts import (
    ASSUMPTION_LABEL, Applicability, BasisStatus, CycleWindow, EffectiveAreaBasis,
    EnergyBasisGate, EnergyBasisTotal, EnergyComponent, EnergyComponentTotal,
    EnergyQuantity, EnergySummary, PublicationRef, QualityStatus, QueryContext,
)
from greenhouse.queries.irrigation import exact_sum, percent


class EnergyAdapter(Protocol):
    def publication(self, publication_id: str) -> PublicationRef: ...
    def pin_publication(self, scope_id: str) -> PublicationRef: ...
    def cycle_window(self, context: QueryContext) -> CycleWindow: ...
    def read_energy_quantities(self, context: QueryContext, publication: PublicationRef) -> tuple[EnergyQuantity, ...]: ...
    def read_lineage(self, publication: PublicationRef, *, revision_id=None, record_key=None): ...


def energy_basis_gate(bases):
    """Absolute conversion permits distinct VERIFIED measure areas. Pooling
    /m² additionally needs one fixed common area/definition over all contributors.

    A verified Production basis never verifies Resources. The one explicit user
    bridge allows AGC_SHARED_BASIS_V1 v1 growing-area62.5 to meet independently
    verified production_area62.5, with the result still assumed/unverified.
    """
    bases = tuple(bases)
    if not bases or any(b is None for b in bases):
        return EnergyBasisGate(False, False, BasisStatus.UNKNOWN, None,
                               ('incompatible_or_unknown_basis',))
    if any(not isinstance(b, EffectiveAreaBasis) for b in bases):
        raise ValueError('Expected frozen effective basis contracts')
    if len({(b.scope_id, b.compartment_id) for b in bases}) != 1:
        return EnergyBasisGate(False, False, BasisStatus.INCOMPATIBLE, None, ('mixed_basis_scope',))
    assumed = [b for b in bases if b.status == BasisStatus.ASSUMED]
    verified = [b for b in bases if b.status == BasisStatus.VERIFIED]
    assumptions = {(b.assumption_id, b.assumption_version) for b in assumed}
    production_verified = all(b.area_m2 == Decimal('62.5') and b.basis_kind == 'production_area'
                              and b.measure_code in ('ProdA', 'ProdB') for b in verified)
    bridge = bool(assumed and verified and assumptions == {('AGC_SHARED_BASIS_V1', 1)}
                  and all(b.area_m2 == Decimal('62.5') and b.basis_kind == 'growing_area_assumed'
                          and b.measure_code in ('Heat_cons', 'ElecHigh', 'ElecLow', 'Irr', 'Drain', 'ProdA', 'ProdB')
                          for b in assumed)
                  and production_verified)
    sensitivity_bridge = bool(assumed and verified
        and assumptions == {('AGC_FLOOR_96_SENSITIVITY_V1', 1)} and production_verified
        and all(b.area_m2 == Decimal('96') and b.basis_kind == 'floor_area_sensitivity'
                and b.measure_code in ('Heat_cons', 'ElecHigh', 'ElecLow', 'Irr', 'Drain') for b in assumed))
    # Known but conflicting assumption versions/definitions are not reconciled by
    # numeric area equality. Missing/disabled selections arrived above as None.
    if len(assumptions) > 1 or (assumed and verified and not (bridge or sensitivity_bridge)):
        return EnergyBasisGate(False, False, BasisStatus.INCOMPATIBLE, None,
                               ('incompatible_basis_assumptions',))
    if assumptions == {('AGC_SHARED_BASIS_V1', 1)} and any(
            b.area_m2 != Decimal('62.5') or b.basis_kind != 'growing_area_assumed' for b in assumed):
        return EnergyBasisGate(False, False, BasisStatus.INCOMPATIBLE, None,
                               ('assumption_area_or_definition_mismatch',))
    common = len({b.area_m2 for b in bases}) == 1 and (len({b.basis_kind for b in bases}) == 1 or bridge)
    status = BasisStatus.ASSUMED if assumed else BasisStatus.VERIFIED
    reasons = (() if common else ('per_m2_basis_not_common_and_fixed',))
    return EnergyBasisGate(True, common, status,
                           ASSUMPTION_LABEL if assumed else 'verified', reasons)


def calendar_quantities(context, publication, cycle, quantities):
    context.matches(publication)
    if (context.cycle_id, context.compartment_ids[0]) != (cycle.cycle_id, cycle.compartment_id):
        raise ValueError('Energy cycle/compartment mismatch')
    start, end = max(context.start, cycle.start), min(context.end, cycle.end)
    if start >= end:
        raise ValueError('Energy query does not overlap cycle')
    index = {}
    for q in quantities:
        if q.publication != publication or (q.cycle_id, q.compartment_id) != (cycle.cycle_id, cycle.compartment_id):
            raise ValueError('Mixed energy publication/cycle/compartment')
        if not start <= q.resource_date < end or (q.resource_date, q.component) in index:
            raise ValueError('Energy day/component duplicate or outside window')
        index[q.resource_date, q.component] = q
    result = []
    day = start
    while day < end:
        present = [index.get((day, c)) for c in EnergyComponent]
        if any(q is not None for q in present) and any(q is None for q in present):
            raise ValueError('Frozen resource day must expose all three component qualities')
        for component, q in zip(EnergyComponent, present):
            if q is None:
                q = EnergyQuantity(publication, cycle.cycle_id, cycle.compartment_id, day, component,
                    None, 'MJ/m²/day' if component == EnergyComponent.HEAT else 'kWh/m²/day',
                    QualityStatus.MISSING, Applicability.UNKNOWN, 'missing_resource_day',
                    None, None, None, None, None, None, None)
            result.append(q)
        day += timedelta(days=1)
    return tuple(result)


def _sum(rows, field):
    return exact_sum(getattr(q, field) for q in rows if getattr(q, field) is not None)


def component_total(component, rows):
    usable = [q for q in rows if q.accepted]
    groups = {}
    for q in usable:
        groups.setdefault(q.basis, []).append(q)
    totals = tuple(EnergyBasisTotal(basis, len(items), _sum(items, 'source_value'),
        _sum(items, 'normalized_kwh_m2'), _sum(items, 'absolute_source_quantity'), _sum(items, 'absolute_kwh'))
        for basis, items in groups.items())
    gate = energy_basis_gate(q.basis for q in usable)
    # Unknown source basis remains a separately labelled source-only group. Never
    # combine it with known versions or combine incompatible area intervals.
    pool = gate.per_m2_compatible or (usable and all(q.basis is None for q in usable))
    absolute = [q for q in usable if q.absolute_kwh is not None]
    absolute_gate = energy_basis_gate(q.basis for q in absolute)
    observed_absolute = _sum(absolute, 'absolute_kwh') if absolute_gate.absolute_compatible else None
    reasons = {q.reason_code for q in rows if q.reason_code}
    reasons.update(gate.reason_codes)
    if len(usable) != len(rows):
        reasons.add('incomplete_component_quantity')
    if len(absolute) != len(usable):
        reasons.add('component_absolute_basis_gap')
    return EnergyComponentTotal(component, len(rows), len(usable), len(absolute),
        percent(len(usable), len(rows)), _sum(usable, 'source_value') if pool else None,
        _sum(usable, 'normalized_kwh_m2') if pool else None,
        _sum(absolute, 'absolute_source_quantity') if absolute_gate.absolute_compatible else None,
        observed_absolute, observed_absolute if len(absolute) == len(rows) and gate.absolute_compatible else None,
        totals, tuple(sorted(reasons)))


def summarize_energy(context, publication, cycle, quantities):
    rows = calendar_quantities(context, publication, cycle, quantities)
    start, end = rows[0].resource_date, rows[-1].resource_date + timedelta(days=1)
    expected = (end-start).days
    by_day = {when: [q for q in rows if q.resource_date == when]
              for when in {q.resource_date for q in rows}}
    complete = sorted(day for day, items in by_day.items() if all(q.accepted for q in items))
    incomplete = tuple(sorted(set(by_day)-set(complete)))
    intersection = [q for q in rows if q.resource_date in complete]
    intersection_gate = energy_basis_gate(q.basis for q in intersection)
    observed = [q for q in rows if q.accepted]
    observed_gate = energy_basis_gate(q.basis for q in observed)
    gate = energy_basis_gate(q.basis for q in rows)
    observed_total = _sum(observed, 'absolute_kwh') if observed_gate.absolute_compatible else None
    intersection_total = _sum(intersection, 'absolute_kwh') if intersection_gate.absolute_compatible else None
    official = observed_total if not incomplete and gate.absolute_compatible else None
    per_m2 = _sum(rows, 'normalized_kwh_m2') if not incomplete and gate.per_m2_compatible else None
    reasons = {q.reason_code for q in rows if q.reason_code}
    reasons.update(gate.reason_codes)
    if incomplete:
        reasons.add('incomplete_energy_quantity')
    if (start, end) != (context.start, context.end):
        reasons.add('window_clipped_to_cycle')
    components = tuple(component_total(c, [q for q in rows if q.component == c]) for c in EnergyComponent)
    return EnergySummary(publication, context, start, end, expected, len(complete),
        percent(len(complete), expected), incomplete, components, observed_total,
        intersection_total, official, per_m2, gate, tuple(sorted(reasons)))


class EnergyService:
    """Both downstream BRs consume get_quantities; no independent quality path."""
    def __init__(self, adapter: EnergyAdapter):
        self.adapter = adapter

    def pin_publication(self, scope_id):
        return self.adapter.pin_publication(scope_id)

    def _input(self, context):
        if context.rule_version is not None:
            raise ValueError('Energy quantities do not accept a threshold rule')
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        start, end = max(context.start, cycle.start), min(context.end, cycle.end)
        if start >= end:
            raise ValueError('Energy query does not overlap cycle')
        effective = replace(context, start=start, end=end)
        rows = self.adapter.read_energy_quantities(effective, publication)
        return publication, cycle, rows

    def get_quantities(self, context):
        publication, cycle, rows = self._input(context)
        return calendar_quantities(context, publication, cycle, rows)

    def get_summary(self, context):
        publication, cycle, rows = self._input(context)
        return summarize_energy(context, publication, cycle, rows)

    def get_cycle(self, context):
        if context.rule_version is not None:
            raise ValueError('Energy quantities do not accept a threshold rule')
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        return self.get_summary(replace(context, start=cycle.start, end=cycle.end))

    def get_lineage(self, publication, *, revision_id=None, record_key=None):
        if self.adapter.publication(publication.publication_id) != publication:
            raise ValueError('Energy lineage publication differs')
        return self.adapter.read_lineage(publication, revision_id=revision_id, record_key=record_key)
