"""Pure BR-02 oracle and a thin service depending only on query contracts."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Protocol

from greenhouse.contracts import (
    ASSUMPTION_LABEL, METRIC_VERSION, WATER_UNIT, Applicability, BasisStatus,
    CorrectionRef, CycleWindow, IrrigationDay, IrrigationSummary, IrrigationRuleProfile, LineageResult, MeasureValue,
    PublicationRef, QualityStatus, QueryContext, ResultStatus, RuleStatus,
)


class QueryAdapter(Protocol):
    def pin_publication(self, scope_id: str) -> PublicationRef: ...
    def publication(self, publication_id: str) -> PublicationRef: ...
    def cycle_window(self, context: QueryContext) -> CycleWindow: ...
    def read_irrigation_days(self, context: QueryContext, publication: PublicationRef) -> tuple[IrrigationDay, ...]: ...
    def irrigation_rule(self, context: QueryContext) -> IrrigationRuleProfile | None: ...
    def read_lineage(self, publication: PublicationRef, *, revision_id: str | None = None,
                     record_key: str | None = None) -> LineageResult: ...


def basis_compatible(left, right):
    known = (BasisStatus.VERIFIED, BasisStatus.ASSUMED)
    if left.basis_status not in known or right.basis_status not in known:
        return False
    if (left.area_m2, left.basis_kind) != (right.area_m2, right.basis_kind):
        return False
    if left.basis_status != right.basis_status:
        return False
    return left.basis_status == BasisStatus.VERIFIED or (
        left.assumption_id, left.assumption_version) == (right.assumption_id, right.assumption_version)


def percent(numerator, denominator):
    # Derived percentages use 50 significant digits; raw values/sums retain scale.
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * 100 / Decimal(denominator)


def exact_sum(values):
    values = tuple(values)
    if not values:
        return None
    integer_digits = max(max(v.adjusted() + 1, 1) for v in values)
    fractional_digits = max(max(-v.as_tuple().exponent, 0) for v in values)
    with localcontext() as ctx:
        ctx.prec = max(50, integer_digits + fractional_digits + len(str(len(values))) + 1)
        return sum(values, Decimal(0))


def evaluate_day(day):
    irr, drain = day.irrigation, day.drain
    compatible = basis_compatible(irr, drain)
    paired = irr.accepted and drain.accepted and compatible
    reasons = set(day.reason_codes)
    reasons.update(m.reason_code for m in (irr, drain) if m.reason_code)
    if not compatible:
        reasons.add('incompatible_or_unknown_basis')
    ratio = percent(drain.value, irr.value) if paired and irr.value > 0 else None
    if paired and irr.value == 0:
        reasons.add('no_irrigation_quantity')
    exceeds = drain.value > irr.value if paired else None
    if exceeds:
        reasons.add('drain_exceeds_irrigation')
    return replace(day, paired=paired, drain_fraction_pct=ratio,
                   drain_exceeds_irrigation=exceeds, reason_codes=tuple(sorted(reasons)))


def calendar_days(context, publication, cycle, days):
    """Validate grain before filling gaps; never choose a winner or pool /m²."""
    context.matches(publication)
    if (cycle.cycle_id, cycle.compartment_id) != (context.cycle_id, context.compartment_ids[0]):
        raise ValueError('Cycle/compartment mismatch')
    start, end = max(context.start, cycle.start), min(context.end, cycle.end)
    if start >= end:
        raise ValueError('Query does not overlap the cycle')
    index = {}
    for day in days:
        if day.publication != publication or (day.cycle_id, day.compartment_id) != (cycle.cycle_id, cycle.compartment_id):
            raise ValueError('Mixed publication/cycle/compartment grain')
        if not start <= day.resource_date < end:
            raise ValueError('Adapter returned a day outside the effective window')
        if day.resource_date in index:
            raise ValueError('Duplicate cycle/resource_date grain')
        index[day.resource_date] = day
    result = []
    when = start
    while when < end:
        day = index.get(when)
        if day is None:
            absent = MeasureValue(None, WATER_UNIT, QualityStatus.MISSING, Applicability.UNKNOWN,
                                  reason_code='missing_resource_day')
            day = IrrigationDay(publication, cycle.cycle_id, cycle.compartment_id, when,
                                absent, absent, reason_codes=('missing_resource_day',))
        result.append(evaluate_day(day))
        when += timedelta(days=1)
    return tuple(result)


def summarize_irrigation(context, publication, cycle, days):
    """Observed totals independent of basis; ratio uses the same accepted pairs."""
    rows = calendar_days(context, publication, cycle, days)
    expected = len(rows)
    irr = [d.irrigation.value for d in rows if d.irrigation.accepted]
    drain = [d.drain.value for d in rows if d.drain.accepted]
    pairs = [d for d in rows if d.paired]
    paired_irr = exact_sum(d.irrigation.value for d in pairs)
    paired_drain = exact_sum(d.drain.value for d in pairs)
    reasons = {r for d in rows for r in d.reason_codes if r != 'no_irrigation_quantity'}
    if (rows[0].resource_date, rows[-1].resource_date + timedelta(days=1)) != (context.start, context.end):
        reasons.add('window_clipped_to_cycle')
    # Changing physical denominators cannot silently enter a ratio of /m² sums.
    signatures = {(d.irrigation.area_m2, d.irrigation.basis_kind,
                   d.irrigation.assumption_id, d.irrigation.assumption_version) for d in pairs}
    compatible_window = len(signatures) <= 1
    if not compatible_window:
        reasons.add('incompatible_or_unknown_basis')
    ratio = percent(paired_drain, paired_irr) if pairs and paired_irr > 0 and compatible_window else None
    if not irr:
        reasons.add('no_accepted_irrigation')
    if not drain:
        reasons.add('no_accepted_drain')
    if not pairs:
        reasons.add('no_paired_days')
    elif paired_irr == 0:
        reasons.add('no_irrigation_quantity')
    if len(pairs) < expected:
        reasons.add('partial_coverage')
    suspect = sum(any(m.quality == QualityStatus.SUSPECT for m in (d.irrigation, d.drain)) for d in rows)
    if suspect:
        reasons.add('suspect_values_excluded')
    measures = [m for d in pairs for m in (d.irrigation, d.drain)]
    if not compatible_window:
        basis = BasisStatus.INCOMPATIBLE
    elif any(m.basis_status == BasisStatus.ASSUMED for m in measures):
        basis = BasisStatus.ASSUMED
    elif measures:
        basis = BasisStatus.VERIFIED
    else:
        # Report basis independently of quality, even when all values are missing.
        all_measures = [m for d in rows for m in (d.irrigation, d.drain)]
        basis = (BasisStatus.UNKNOWN if any(m.basis_status == BasisStatus.UNKNOWN for m in all_measures)
                 else BasisStatus.INCOMPATIBLE if any(not basis_compatible(d.irrigation, d.drain) for d in rows)
                 else BasisStatus.ASSUMED if any(m.basis_status == BasisStatus.ASSUMED for m in all_measures)
                 else BasisStatus.VERIFIED)
    status = ResultStatus.UNKNOWN if not irr and not drain else ResultStatus.COMPLETE if len(pairs) == expected and compatible_window else ResultStatus.PARTIAL
    return IrrigationSummary(
        publication=publication, context=context, window_start=rows[0].resource_date,
        window_end=rows[-1].resource_date + timedelta(days=1), expected_days=expected,
        irrigation_observed_l_m2=exact_sum(irr), drain_observed_l_m2=exact_sum(drain),
        paired_irrigation_l_m2=paired_irr, paired_drain_l_m2=paired_drain, drain_fraction_pct=ratio,
        irrigation_coverage_pct=percent(len(irr), expected), drain_coverage_pct=percent(len(drain), expected),
        paired_coverage_pct=percent(len(pairs), expected), irrigation_accepted_days=len(irr),
        drain_accepted_days=len(drain), paired_days=len(pairs),
        unknown_days=sum(not d.irrigation.accepted or not d.drain.accepted for d in rows),
        suspect_days=suspect, drain_exceeds_irrigation_days=sum(d.drain_exceeds_irrigation is True for d in rows),
        status=status, comparison_eligible=len(pairs) == expected and ratio is not None and compatible_window,
        basis_status=basis, basis_label=ASSUMPTION_LABEL if basis == BasisStatus.ASSUMED else 'verified' if basis == BasisStatus.VERIFIED else None,
        basis_version_ids=tuple(sorted({m.basis_version_id for d in rows for m in (d.irrigation, d.drain) if m.basis_version_id})),
        assumption_ids=tuple(sorted({m.assumption_id for d in rows for m in (d.irrigation, d.drain) if m.assumption_id})),
        assumption_versions=tuple(sorted({m.assumption_version for d in rows for m in (d.irrigation, d.drain) if m.assumption_version is not None})),
        corrections=tuple(CorrectionRef(key, version) for key, version in sorted({(d.correction_id, d.correction_version) for d in rows if d.correction_id})),
        reason_codes=tuple(sorted(reasons)),
    )


def result_cache_key(context, publication, *, scenario_version=None):
    context.matches(publication)
    return (METRIC_VERSION, publication, context, scenario_version)


class IrrigationService:
    def __init__(self, adapter: QueryAdapter):
        self.adapter = adapter

    def pin_publication(self, scope_id):
        return self.adapter.pin_publication(scope_id)

    def _input(self, context):
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        effective = replace(context, start=max(context.start, cycle.start), end=min(context.end, cycle.end))
        rule = self.adapter.irrigation_rule(context)
        return publication, cycle, self.adapter.read_irrigation_days(effective, publication), rule

    def get_irrigation_days(self, context):
        publication, cycle, days, rule = self._input(context)
        result = calendar_days(context, publication, cycle, days)
        return tuple(evaluate_irrigation_rule(d, rule) for d in result) if rule else result

    def get_irrigation_summary(self, context):
        publication, cycle, days, rule = self._input(context)
        result = summarize_irrigation(context, publication, cycle, days)
        return attach_irrigation_rule(result, calendar_days(context, publication, cycle, days), rule)

    def get_irrigation_weekly(self, context):
        publication, cycle, days, rule = self._input(context)
        rows = calendar_days(context, publication, cycle, days)
        start, end = rows[0].resource_date, rows[-1].resource_date + timedelta(days=1)
        result = []
        while start < end:
            right = min(end, start + timedelta(days=7-start.weekday()))
            week_context = replace(context, start=start, end=right)
            week_rows = tuple(d for d in rows if start <= d.resource_date < right)
            summary = summarize_irrigation(week_context, publication, cycle, week_rows)
            summary = attach_irrigation_rule(summary, week_rows, rule)
            year, week, _ = start.isocalendar()
            result.append(replace(summary, iso_year=year, iso_week=week))
            start = right
        return tuple(result)

    def get_irrigation_cycle(self, context):
        context.matches(self.adapter.publication(context.publication_id))
        cycle = self.adapter.cycle_window(context)
        return self.get_irrigation_summary(replace(context, start=cycle.start, end=cycle.end))

    def get_lineage(self, publication, *, revision_id=None, record_key=None):
        if (revision_id is None) == (record_key is None):
            raise ValueError('Select exactly one revision_id or logical record key')
        if self.adapter.publication(publication.publication_id) != publication:
            raise ValueError('Pinned publication metadata differs')
        return self.adapter.read_lineage(publication, revision_id=revision_id, record_key=record_key)


def evaluate_irrigation_rule(day, profile):
    """Explicit query-time rule; unknown input is never a pass/fail conclusion."""
    reasons = set(day.reason_codes)
    if not day.paired or day.drain_fraction_pct is None:
        status = RuleStatus.UNKNOWN
        reasons.add('irrigation_rule_unknown')
    elif (profile.min_drain_pct <= day.drain_fraction_pct <= profile.max_drain_pct
          and profile.min_irrigation_l_m2 <= day.irrigation.value <= profile.max_irrigation_l_m2):
        status = RuleStatus.WITHIN_RANGE
    else:
        status = RuleStatus.OUTSIDE_RANGE
        reasons.add('outside_irrigation_rule')
    return replace(day, rule_status=status, rule_version=profile.rule_version,
                   rule_profile=profile, reason_codes=tuple(sorted(reasons)))


def attach_irrigation_rule(summary, days, profile):
    if profile is None:
        return summary
    evaluated = tuple(evaluate_irrigation_rule(day, profile) for day in days)
    reasons = set(summary.reason_codes)
    outside = sum(d.rule_status == RuleStatus.OUTSIDE_RANGE for d in evaluated)
    unknown = sum(d.rule_status == RuleStatus.UNKNOWN for d in evaluated)
    if outside:
        reasons.add('outside_irrigation_rule')
    if unknown:
        reasons.add('irrigation_rule_unknown')
    return replace(summary, rule_status=RuleStatus.CONFIGURED, rule_version=profile.rule_version,
                   rule_profile=profile, outside_rule_days=outside, rule_unknown_days=unknown,
                   reason_codes=tuple(sorted(reasons)))
