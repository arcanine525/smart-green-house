"""Pure BR-05 oracle and a thin harvest service on structural adapters.

Record grain preserved: indexes/fills never invent records, never collapse
same-date records, and never present a paired share without pairing counts.
Navigation cumulatives are explicit cycle-scope products: a record
contributes only when cycle.start <= harvest_date < effective_end, so a
record AFTER the window can never contribute an amount or a basis label.
`cumulative_a` sums accepted ProdA values; `cumulative_total` sums A+B over
PAIRED records ONLY — the two measure sets differ and stay documented.
Weight-based shares use the SAME paired window records, never mean of
per-record percentages.
NOTE on the shared v1 dedup: Production rows share a logical date key in the
ingestion staging, so same-date records with different content are withheld
as conflicts upstream; the mart keeps record grain from whatever membership
contains without collapsing keys (BR-05 §2).
"""
from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
from datetime import timedelta
from typing import Protocol

from greenhouse.contracts import (
    ASSUMPTION_LABEL, BasisStatus, HarvestCompleteness, HarvestDay, HarvestQualityCounts,
    HarvestRecord, HarvestRuleProfile, HarvestSummary, HarvestUnresolvedIssue, LineageResult,
    PublicationRef, QueryContext, ResultStatus, RuleStatus, UnresolvedSourceAudit,
)


class HarvestAdapter(Protocol):
    def pin_publication(self, scope_id: str) -> PublicationRef: ...
    def publication(self, publication_id: str) -> PublicationRef: ...
    def cycle_window(self, context: QueryContext): ...
    def read_harvest_records(self, context: QueryContext, publication: PublicationRef) -> tuple[HarvestRecord, ...]: ...
    def harvest_state(self, context: QueryContext) -> HarvestCompleteness: ...
    def harvest_rule(self, context: QueryContext) -> HarvestRuleProfile | None: ...
    def read_lineage(self, publication: PublicationRef, *, revision_id: str | None = None,
                     record_key: str | None = None) -> LineageResult: ...
    def read_unresolved_source(self, context: QueryContext, publication: PublicationRef,
                               unresolved_item: HarvestUnresolvedIssue) -> UnresolvedSourceAudit: ...


def share(numerator, denominator):
    """100 × numerator / denominator at 50 digits; zero denominator → None."""
    if denominator is None or denominator == 0:
        return None
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


def verify_grain(context, publication, cycle, cycle_records):
    """Each record must belong to the pinned identity/cycle; keys unique."""
    context.matches(publication)
    if (cycle.cycle_id, cycle.compartment_id) != (context.cycle_id, context.compartment_ids[0]):
        raise ValueError('Cycle/compartment mismatch')
    keys = set()
    for record in cycle_records:
        if record.publication != publication or (record.cycle_id, record.compartment_id) != (
                cycle.cycle_id, cycle.compartment_id):
            raise ValueError('Mixed publication/cycle/compartment record grain')
        if not cycle.start <= record.harvest_date < cycle.end:
            raise ValueError('Adapter returned a record outside the cycle window')
        if record.logical_record_key in keys:
            raise ValueError('Duplicate logical record key in published grain')
        keys.add(record.logical_record_key)


def cumulative(contributors, end, completeness, origin):
    """Cycle-scope cumulative VALUES at an exclusive boundary.

    A record contributes only when harvest_date < end. `cumulative_a` sums
    accepted ProdA over contributors; `cumulative_total` sums A+B over PAIRED
    contributors — two INDEPENDENT measure sets, each applying the no-usable
    vs verified-empty rules: unknown → NULL, no usable amounts under a
    complete snapshot → known 0. The origin (cycle start) is independent
    metadata and always present; the basis label is drawn only from
    contributing records.
    """
    a_values = [record.prod_a.value for record in contributors
                if record.prod_a.accepted]
    a = exact_sum(a_values)
    if a is None:
        a = Decimal(0) if completeness.snapshot_complete else None
    total = exact_sum(value for record in contributors if record.paired
                      for value in (record.prod_a.value, record.prod_b.value))
    if total is None:
        total = Decimal(0) if completeness.snapshot_complete else None
    labels = {record.prod_a.basis_status for record in contributors
              if record.prod_a.accepted}
    label = (ASSUMPTION_LABEL if BasisStatus.ASSUMED in labels
             else 'verified' if labels == {BasisStatus.VERIFIED} else None)
    return a, total, origin, label


def harvest_quality_counts(records):
    """Typed quality partition of THIS window's real records, per measure.

    Service-owned so UI and exports show the same contract value; distinct
    from whole-cycle completeness and from frozen unresolved sources."""
    a = Counter(record.prod_a.quality.value for record in records)
    b = Counter(record.prod_b.quality.value for record in records)
    return HarvestQualityCounts(
        population_records=len(records),
        prod_a_accepted=a.get('accepted', 0), prod_a_missing=a.get('missing', 0),
        prod_a_invalid=a.get('invalid', 0), prod_a_suspect=a.get('suspect', 0),
        prod_b_accepted=b.get('accepted', 0), prod_b_missing=b.get('missing', 0),
        prod_b_invalid=b.get('invalid', 0), prod_b_suspect=b.get('suspect', 0))


def summarize_harvest(context, publication, cycle, cycle_records, completeness):
    """Period summary on the clipped window plus cycle-scope cumulative A."""
    verify_grain(context, publication, cycle, cycle_records)
    start, end = max(context.start, cycle.start), min(context.end, cycle.end)
    if start >= end:
        raise ValueError('Query does not overlap the cycle')
    window = [record for record in cycle_records if start <= record.harvest_date < end]
    reasons = set()
    a_values = [record.prod_a.value for record in window if record.prod_a.accepted]
    b_values = [record.prod_b.value for record in window if record.prod_b.accepted]
    if not window and completeness.snapshot_complete:
        # A verified empty event window is a known zero, not unknown data.
        a_values, b_values = [Decimal(0)], [Decimal(0)]
        reasons.add('known_zero_event_window')
    if window and not a_values and not b_values:
        reasons.add('records_without_usable_measures')
    pairs = [record for record in window if record.paired]
    paired_a = exact_sum(record.prod_a.value for record in pairs)
    paired_b = exact_sum(record.prod_b.value for record in pairs)
    total = exact_sum([paired_a, paired_b]) if pairs else None
    a_share = share(paired_a, total) if pairs else None
    b_share = share(paired_b, total) if pairs else None
    if pairs and total == 0:
        reasons.add('no_paired_mass')
    # Cumulative contributors live in the CYCLE before this window's end; a
    # record after it contributes neither amount nor basis.
    cumulative_a, cumulative_total, cumulative_origin, cumulative_label = cumulative(
        tuple(record for record in cycle_records if record.harvest_date < end),
        end, completeness, cycle.start)
    for record in window:
        reasons.update(record.reason_codes)
    if not completeness.snapshot_complete:
        reasons.add('incomplete_snapshot')
    reasons.update(completeness.reasons)
    if pairs and len(pairs) != len(window):
        reasons.add('partial_paired_records')
    if (start, end) != (context.start, context.end):
        # The request diverged from the effective cycle window: actual
        # clipping (even a single boundary).
        reasons.add('window_clipped_to_cycle')
    if start > cycle.start:
        # Interior zoom: cumulative amounts descend from earlier cycle data
        # without claiming any clipping.
        reasons.add('cumulative_from_cycle_start')
    if not completeness.snapshot_complete and window:
        reasons.add('unranked_incomplete_compartment')
    return HarvestSummary(
        publication=publication, context=context, window_start=start, window_end=end,
        prod_a_observed=exact_sum(a_values), prod_b_observed=exact_sum(b_values),
        paired_prod_a=paired_a, paired_prod_b=paired_b, paired_total=total,
        class_a_share_pct=a_share, class_b_share_pct=b_share,
        accepted_records=len(window), paired_records=len(pairs), completeness=completeness,
        cumulative_a=cumulative_a, cumulative_total=cumulative_total,
        cumulative_origin=cumulative_origin, cumulative_basis_label=cumulative_label,
        status=window_status(window, completeness), reason_codes=tuple(sorted(reasons)),
        record_quality=harvest_quality_counts(window))


def window_status(window, completeness):
    """Complete information > partial > unknown: zero only when verified."""
    if not window:
        return ResultStatus.COMPLETE if completeness.snapshot_complete else ResultStatus.UNKNOWN
    if not completeness.snapshot_complete:
        return ResultStatus.PARTIAL
    return ResultStatus.COMPLETE if all(record.paired for record in window) else ResultStatus.PARTIAL


def record_share(record):
    """Per-record class-B share at 50 digits: only when BOTH measures are
    usable; zero paired mass is an unknown INPUT, never a 0% or 100% verdict."""
    if not record.paired:
        return None
    a, b = record.prod_a.value, record.prod_b.value
    if a + b == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(b) * 100 / (Decimal(a) + Decimal(b))


def evaluate_harvest_rule_record(record, profile):
    """Explicit query-time rule per record; equality sits inside the bound."""
    share = record_share(record)
    if share is None:
        status = RuleStatus.UNKNOWN
        reasons = {'harvest_rule_unknown'}
    elif share > profile.max_class_b_share_pct:
        status = RuleStatus.OUTSIDE_RANGE
        reasons = {'outside_harvest_rule'}
    else:
        status = RuleStatus.WITHIN_RANGE
        reasons = set()
    return replace(record, rule_status=status, rule_version=profile.rule_version,
                   rule_profile=profile, reason_codes=tuple(sorted(set(record.reason_codes) | reasons)))


def evaluate_harvest_rule_row(row, profile, kind):
    """Day/week rows: the row's own class-B share is the rule input."""
    share = row.class_b_share_pct
    if share is None:
        status = RuleStatus.UNKNOWN
        reasons = {'harvest_rule_unknown'}
    elif share > profile.max_class_b_share_pct:
        status = RuleStatus.OUTSIDE_RANGE
        reasons = {'outside_harvest_rule'}
    else:
        status = RuleStatus.WITHIN_RANGE
        reasons = set()
    return replace(row, rule_status=status, rule_version=profile.rule_version,
                   rule_profile=profile,
                   reason_codes=tuple(sorted(set(row.reason_codes) | reasons)))


def attach_harvest_rule_records(records, profile):
    """Decorate the query-side share on every record, then the optional rule."""
    decorated = tuple(replace(record, class_b_share_pct=record_share(record))
                      for record in records)
    if profile is None:
        return decorated
    return tuple(evaluate_harvest_rule_record(record, profile) for record in decorated)


def attach_harvest_rule_days(days, profile):
    if profile is None:
        return tuple(days)
    return tuple(evaluate_harvest_rule_row(day, profile, 'day') for day in days)


def attach_harvest_rule_summary(summary, records, profile):
    """WINDOW-grain verdict + record-grain counters, kept separate.

    The rule verdict of a period/week row comes from THAT window's own B
    share (no denominator → unknown, equality inside) — never inferred from
    a violating record inside it. Counters count records of exactly THIS
    window (callers pass the window subset; earlier records never leak in)."""
    if profile is None:
        return summary
    share = summary.class_b_share_pct
    if share is None:
        status = RuleStatus.UNKNOWN
        reasons = {'harvest_rule_unknown'}
    elif share > profile.max_class_b_share_pct:
        status = RuleStatus.OUTSIDE_RANGE
        reasons = {'outside_harvest_rule'}
    else:
        status = RuleStatus.WITHIN_RANGE
        reasons = set()
    evaluated = tuple(evaluate_harvest_rule_record(record, profile) for record in records)
    outside = sum(item.rule_status == RuleStatus.OUTSIDE_RANGE for item in evaluated)
    unknown = sum(item.rule_status == RuleStatus.UNKNOWN for item in evaluated)
    reasons.update(summary.reason_codes)
    return replace(summary, rule_status=status, rule_version=profile.rule_version,
                   rule_profile=profile, outside_rule_records=outside,
                   rule_unknown_records=unknown, reason_codes=tuple(sorted(reasons)))


def day_rows(context, publication, cycle, cycle_records, completeness):
    """Calendar rows over the effective window; zero-fill only when verified."""
    verify_grain(context, publication, cycle, cycle_records)
    start, end = max(context.start, cycle.start), min(context.end, cycle.end)
    if start >= end:
        raise ValueError('Query does not overlap the cycle')
    by_day = {}
    for record in cycle_records:
        if start <= record.harvest_date < end:
            by_day.setdefault(record.harvest_date, []).append(record)
    result = []
    when = start
    while when < end:
        rows = by_day.get(when, [])
        pairs = [record for record in rows if record.paired]
        a_sum = exact_sum(record.prod_a.value for record in rows if record.prod_a.accepted)
        b_sum = exact_sum(record.prod_b.value for record in rows if record.prod_b.accepted)
        paired_a = exact_sum(record.prod_a.value for record in pairs)
        paired_b = exact_sum(record.prod_b.value for record in pairs)
        mass = exact_sum([paired_a, paired_b]) if pairs else None
        basis_states = {record.prod_a.basis_status for record in rows
                        if record.prod_a.accepted} | {record.prod_b.basis_status
                                                      for record in rows if record.prod_b.accepted}
        label = (ASSUMPTION_LABEL if BasisStatus.ASSUMED in basis_states
                 else 'verified' if basis_states == {BasisStatus.VERIFIED} else None)
        status = (BasisStatus.ASSUMED if BasisStatus.ASSUMED in basis_states
                  else BasisStatus.VERIFIED if basis_states == {BasisStatus.VERIFIED}
                  else BasisStatus.UNKNOWN)
        reasons = set()
        for record in rows:
            reasons.update(record.reason_codes)
        if rows:
            if len(pairs) != len(rows):
                reasons.add('partial_paired_records')
            if not completeness.snapshot_complete:
                reasons.add('unranked_incomplete_compartment')
            if pairs and (paired_a or 0) + (paired_b or 0) == 0:
                reasons.add('no_paired_mass')
        elif completeness.snapshot_complete:
            a_sum = Decimal(0)
            b_sum = Decimal(0)
            reasons.add('known_zero_harvest_day')
        else:
            reasons.add('unverified_no_harvest_record')
        reasons.update(completeness.reasons)
        cumulative_a, cumulative_total, cumulative_origin, _ = cumulative(
            tuple(record for record in cycle_records if record.harvest_date <= when),
            when + timedelta(days=1), completeness, cycle.start)
        result.append(HarvestDay(
            publication=publication, cycle_id=cycle.cycle_id, compartment_id=cycle.compartment_id,
            harvest_date=when, prod_a=a_sum, prod_b=b_sum, record_count=len(rows),
            record_present=bool(rows), paired_records=len(pairs),
            paired_prod_a=paired_a, paired_prod_b=paired_b,
            class_a_share_pct=share(paired_a, mass) if pairs else None,
            class_b_share_pct=share(paired_b, mass) if pairs else None,
            basis_status=status, basis_label=label, reason_codes=tuple(sorted(reasons)),
            cumulative_a=cumulative_a, cumulative_total=cumulative_total,
            cumulative_origin=cumulative_origin))
        when += timedelta(days=1)
    return tuple(result)


class HarvestService:
    def __init__(self, adapter: HarvestAdapter):
        self.adapter = adapter

    def pin_publication(self, scope_id):
        return self.adapter.pin_publication(scope_id)

    def _input(self, context):
        """Pin by ID; read the whole CYCLE record grain once plus its state."""
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        records = self.adapter.read_harvest_records(
            replace(context, start=cycle.start, end=cycle.end), publication)
        state = self.adapter.harvest_state(context)
        rule = self.adapter.harvest_rule(context)
        return publication, cycle, records, state, rule

    def get_harvest_records(self, context):
        """Accepted records inside the effective window, real grain only."""
        publication, cycle, records, _, rule = self._input(context)
        start, end = max(context.start, cycle.start), min(context.end, cycle.end)
        verify_grain(context, publication, cycle, records)
        if start >= end:
            raise ValueError('Query does not overlap the cycle')
        window = (record for record in records if start <= record.harvest_date < end)
        return attach_harvest_rule_records(window, rule)

    def get_harvest_summary(self, context):
        publication, cycle, records, state, rule = self._input(context)
        summary = summarize_harvest(context, publication, cycle, records, state)
        start, end = max(context.start, cycle.start), min(context.end, cycle.end)
        window = tuple(record for record in records if start <= record.harvest_date < end)
        return attach_harvest_rule_summary(summary, window, rule)

    def get_harvest_days(self, context):
        publication, cycle, records, state, rule = self._input(context)
        days = day_rows(context, publication, cycle, records, state)
        return attach_harvest_rule_days(days, rule)

    def get_harvest_weekly(self, context):
        publication, cycle, records, state, rule = self._input(context)
        days = day_rows(context, publication, cycle, records, state)
        start, end = days[0].harvest_date, days[-1].harvest_date + timedelta(days=1)
        result = []
        while start < end:
            right = min(end, start + timedelta(days=7 - start.weekday()))
            week_context = replace(context, start=start, end=right)
            week_rows = [day for day in days if start <= day.harvest_date < right]
            # Cycle-scope cumulative at the week's exclusive right boundary:
            # records before `right` contribute; nothing after does.
            cumulative_a, cumulative_total, cumulative_origin, cumulative_label = cumulative(
                tuple(record for record in records if record.harvest_date < right),
                right, state, cycle.start)
            summary = week_summary(week_context, publication, cycle, week_rows, state,
                                   cumulative_a, cumulative_total, cumulative_origin,
                                   cumulative_label)
            summary = attach_harvest_rule_summary(
                summary,
                tuple(record for record in records if start <= record.harvest_date < right),
                rule)
            year, week, _ = start.isocalendar()
            result.append(replace(summary, iso_year=year, iso_week=week))
            start = right
        return tuple(result)

    def get_harvest_cycle(self, context):
        """Whole-cycle summary: the cumulative BR-04 canonical input. The
        request is deliberately replaced by the full-cycle context — that
        replacement is NOT clipping and carries no clip reason."""
        publication, cycle, records, state, rule = self._input(context)
        full = replace(context, start=cycle.start, end=cycle.end)
        summary = summarize_harvest(full, publication, cycle, records, state)
        return attach_harvest_rule_summary(summary, records, rule)

    def get_lineage(self, publication, *, revision_id=None, record_key=None):
        """Accepted-record provenance; the pinned publication must match."""
        if (revision_id is None) == (record_key is None):
            raise ValueError('Select exactly one revision_id or logical record key')
        if self.adapter.publication(publication.publication_id) != publication:
            raise ValueError('Pinned publication metadata differs')
        return self.adapter.read_lineage(publication, revision_id=revision_id,
                                         record_key=record_key)

    def get_unresolved_source(self, context, unresolved_item, *, rule_grain=None):
        """Authorized read-only audit of one frozen unresolved source row.

        The selected rule comes from the pinned context and the grain is an
        explicit caller choice; both travel with the audit for traceability
        only (the audit never feeds a KPI)."""
        publication, _, _, _, _ = self._input(context)
        return self.adapter.read_unresolved_source(context, publication, unresolved_item,
                                                   rule_grain=rule_grain)


def week_summary(week_context, publication, cycle, week_days, state,
                 cumulative_a, cumulative_total, cumulative_origin, cumulative_label):
    """Weekly HarvestSummary assembled from the frozen day rows. Cumulative
    values arrive as cycle-scope products (computed by the caller from the
    cycle record grain); they are carried, never recomputed here."""
    accepted = sum(day.record_count for day in week_days)
    a_values = [day.prod_a for day in week_days if day.prod_a is not None]
    b_values = [day.prod_b for day in week_days if day.prod_b is not None]
    if not a_values and not b_values and state.snapshot_complete and not accepted:
        a_values, b_values = [Decimal(0)], [Decimal(0)]  # verified zero-event week
    paired_days = [day for day in week_days if day.paired_records]
    paired_a = exact_sum(day.paired_prod_a for day in paired_days)
    paired_b = exact_sum(day.paired_prod_b for day in paired_days)
    total = exact_sum([paired_a, paired_b]) if paired_days else None
    reasons = set()
    for day in week_days:
        reasons.update(day.reason_codes)
    if not a_values and not b_values:
        reasons.add('known_zero_event_window' if state.snapshot_complete and not accepted
                    else 'no_usable_records')
    if sum(day.paired_records for day in week_days) != accepted:
        reasons.add('partial_paired_records')
    if not state.snapshot_complete:
        reasons.add('incomplete_snapshot')
    return HarvestSummary(
        publication=publication, context=week_context,
        window_start=week_context.start, window_end=week_context.end,
        prod_a_observed=exact_sum(a_values), prod_b_observed=exact_sum(b_values),
        paired_prod_a=paired_a, paired_prod_b=paired_b, paired_total=total,
        class_a_share_pct=share(paired_a, total) if paired_days else None,
        class_b_share_pct=share(paired_b, total) if paired_days else None,
        accepted_records=accepted, paired_records=sum(day.paired_records for day in week_days),
        completeness=state, cumulative_a=cumulative_a, cumulative_total=cumulative_total,
        cumulative_origin=cumulative_origin, cumulative_basis_label=cumulative_label,
        status=week_status(state, week_days), reason_codes=tuple(sorted(reasons)))
        # record_quality stays None: day rows are aggregates; a per-record
        # quality partition is only stated on the real-record population.


def week_status(state, week_days):
    """Classify by ACTUAL event days and usable measures — never by the size
    of the synthetic calendar list: verified unknown rows keep the status
    consistent with the period summary."""
    event_days = [day for day in week_days if day.record_present]
    if not event_days:
        return ResultStatus.COMPLETE if state.snapshot_complete else ResultStatus.UNKNOWN
    if not state.snapshot_complete:
        return ResultStatus.PARTIAL
    if any(day.record_present and day.paired_records != day.record_count
           for day in event_days):
        return ResultStatus.PARTIAL
    return ResultStatus.COMPLETE
