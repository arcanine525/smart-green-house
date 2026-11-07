"""Pure threshold duration/episode engine for BR-01 and BR-03 (T14).

DB-independent oracle over typed contracts only (no SQL, no adapters) with
one small declared boundary (main decision):

* the DECLARED evaluation group (measurement_context + basis, on the
  IntradayContext) is shared by every requested metric of one evaluation:
  basis requested/realized permits only temperature/co2 (BR-01 climate),
  and basis measured permits only ec/ph (BR-03 drain). Other
  contexts/bases are separate evaluations; a mixed temperature+EC OR is a
  rejection, never a synthetic fixture;
* the caller selects AT MOST ONE schedule revision per requested
  (metric, context, basis): passing two revisions that both carry the same
  declared-group metric is refused before evaluation — even when their
  segments do not overlap — because temporal changes belong as segments
  INSIDE one immutable revision with a single rule identity. Different
  metrics may still select their own revisions; nothing is chosen silently
  (no first/latest fallback). An EXPLICIT empty revision tuple is legal:
  every requested metric then reports `rule_not_configured` over its
  expected duration with rule_version None — no dummy profile needed;
* one immutable `RuleScheduleRevision` may carry segments for several
  metrics and other contexts/bases (inert here); within one revision, the
  same (metric, context, basis) key may not overlap in time (contract AND
  DB-enforced), and the revision, once registered, is sealed as a whole
  (membership cannot grow afterwards);
* optional provenance — publication and versions — never contradicts the
  pinned context: when a PublicationRef is supplied its scope and
  source-naive time basis must match, and matching explicit Versions must
  equal the publication's, otherwise the rejection is raised; result
  versions derive from the publication when only one is supplied. The
  engine never queries latest publication pointers and never invents
  metadata;
* requested metrics stay explicit: a requested metric without any
  applicable segment in the declared group reports `rule_not_configured`
  with exact expected duration and NULL public out KPIs; a declared two-
  metric request stays ONE OR over ALL requested metrics (contributions
  carry each contributing rule identity), and a one-metric request has no
  OR row;
* durations accumulate on the cadence slot grid (every slot means
  `[start, start+cadence)`), clip to the cycle and the requested window;
  the exact end-of-cycle sample stays stored upstream and adds nothing;
  duplicate slot starts, off-grid slots, unrequested metric slots and
  context/basis mismatches are refused with documented validation errors;
* unknown reasons are attributed ONCE per slot along a documented priority
  (rule_not_configured > quality_invalid > quality_suspect > missing_input
  > sp0_off_mode > inverted_bounds) so reasons never double count; a
  suspect measure contributes its own suspect bucket plus unknown minutes;
* joint OR results sum the union of violations ONLY over jointly valid
  slots (every contributing metric evaluated at that slot); per-metric
  hours are never added; a fully unknown metric keeps coverage 0% with
  NULL out KPIs and joint valid 0;
* episodes are built over the FULL cycle in an internal full-cycle builder,
  then the returned list keeps ONLY episodes with positive overlap with the
  report window (original start/end/duration preserved on those, plus the
  clipped overlap). The minimum-duration filter uses the original duration
  and never changes summary totals; both LO/HI directions inside one
  continuous out run stay one episode; per-slot direction plus a
  deterministic peak deviation/time are part of each episode.

Version identities come from the single selected revision per metric;
publication/versions identity metadata is pinned by the caller and never
invented. Timestamps are source-naive throughout (TimeBasis.SOURCE_NAIVE).
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from greenhouse.contracts import (
    RULE_METRIC_UNITS, UNKNOWN_REASON_PRIORITY, IntradayContext, PublicationRef, QualityStatus,
    RuleBasis, RuleDeviationDirection, RuleDurationResult, RuleEpisode, RuleJointDuration,
    RuleMetricApplied, RuleMetricDuration, RuleScheduleRevision, RuleSlotDeviation,
    RuleSlotReview, ThresholdSlot, TimeBasis, UnknownReasonShare, Versions,
    rule_segment_sort_key,
)

ZERO = Decimal(0)
VALID_CLASSES = ('valid', 'out_low', 'out_high')
OUT_CLASSES = ('out_low', 'out_high')
CLIMATE_METRICS = ('temperature', 'co2')
NUTRITION_METRICS = ('ec', 'ph')


def _grid_anchor(context) -> datetime:
    return datetime.combine(context.cycle.start, datetime.min.time())


def _grid_index(context, when: datetime) -> int:
    offset = when - _grid_anchor(context)
    total = offset.total_seconds()
    if total < 0 or total % context.cadence.total_seconds() != 0:
        raise ValueError(
            f'timestamp {when.isoformat()} sits off the {context.cadence_minutes}-minute grid '
            'anchored at cycle start (source slots must sit on exact slot starts)')
    return int(total // context.cadence.total_seconds())



def _minutes(delta: timedelta) -> Decimal:
    seconds = delta.days * 86400 + delta.seconds + delta.microseconds / 1_000_000
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(seconds) / Decimal(60)


def _ratio(numerator, denominator, factor) -> Decimal | None:
    """Public out KPI with exact 50-digit Decimal math; None when the valid
    population is empty (never a printed zero)."""
    if denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * Decimal(factor) / Decimal(denominator)


def _hours(minutes) -> Decimal:
    """Exact minute→hour conversion at 50-digit precision."""
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(minutes) / Decimal(60)


def _coverage(numerator, denominator) -> Decimal:
    if denominator == 0:
        return ZERO
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * 100 / Decimal(denominator)


def _primary_reason(code: str) -> int:
    return UNKNOWN_REASON_PRIORITY.index(code)


def _segment_identity(segment):
    return (segment.start, segment.end, segment.delta, segment.lower, segment.upper)


def _candidate_segment(segment_list, when: datetime):
    """Resolve the ONE segment covering the slot start; >1 explicit overlap
    means the pinned selection is ambiguous and is refused."""
    matches = [segment for segment in segment_list if segment.start <= when < segment.end]
    if len(matches) > 1:
        raise ValueError(
            f'ambiguous schedule coverage at {when.isoformat()} for '
            f'{matches[0].metric}/{matches[0].measurement_context}/{matches[0].basis.value}: '
            'explicitly selected revisions overlap for the same metric key; resolve the '
            'authoritative revision before the engine')
    return matches[0] if matches else None


def _validate_selection(context, revisions):
    """Declared-group selection boundary: scope/compartment must match, and
    at most ONE schedule revision may be selected per requested
    (metric, context, basis) — temporal changes belong as segments inside
    one immutable revision, so even two DISJOINT revisions for the same
    metric are refused instead of mislabeling episodes/summaries. Different
    metrics may select their own revisions; nothing is chosen silently."""
    holders = {}
    for revision in revisions:
        revision.matches(context.scope_id, context.compartment_ids)
        for segment in revision.segments:
            if segment.measurement_context != context.measurement_context \
                    or segment.basis != context.basis:
                continue  # inert here: other contexts/bases belong to separate evaluations
            previous = holders.get(segment.metric)
            if previous is not None and previous != revision.rule_version:
                raise ValueError(
                    f'metric {segment.metric} is selected from schedule revisions '
                    f'{previous} and {revision.rule_version}: at most one revision per '
                    'metric/context/basis; temporal changes belong as segments inside '
                    'one immutable revision, and the engine never chooses first/latest')
            holders[segment.metric] = revision.rule_version


def _validate_metrics(context, metrics):
    """Declared basis allows only its metrics: climate bases rule
    temperature/CO2; the measured basis rules EC/pH. A missing metric keeps
    its declared REQUEST context — nothing fabricated."""
    if context.basis == RuleBasis.MEASURED:
        allowed = NUTRITION_METRICS
    else:
        allowed = CLIMATE_METRICS
    for metric in metrics:
        if metric not in allowed:
            raise ValueError(
                f'basis {context.basis.value} rules only metrics {allowed}; '
                f'metric {metric} does not belong to this declared evaluation group — '
                'use separate evaluations for other contexts/bases')


def _applied_bounds(metric, state: ThresholdSlot, segment):
    """Bounds the slot is judged against. Climate bounds derive per slot
    from its own setpoints (deltas never become fixed bounds); nutrition
    bounds are the stored fixed lower/upper."""
    if metric == 'co2':
        return max(ZERO, state.setpoint - segment.delta), state.setpoint + segment.delta
    if metric == 'temperature':
        return state.lower_setpoint - segment.delta, state.upper_setpoint + segment.delta
    return segment.lower, segment.upper


def _classify_slot(metric: str, state: ThresholdSlot | None, segment):
    """Exact slot classification: (class_code, deviation, direction, lower,
    upper). Boundary equality sits inside; missing content yields unknown
    classes in the documented priority order. Applied bounds come from the
    same decision path (never re-derived elsewhere): present for valid/out
    classes and for the inverted pair (the cause of that unknown), absent
    for semantic unknowns (rule/not/missing/quality/sp0) where no bounds
    were applied."""
    if segment is None:
        return 'rule_not_configured', ZERO, None, None, None
    if state is None:
        return 'missing_input', ZERO, None, None, None
    if state.quality == QualityStatus.INVALID:
        return 'quality_invalid', ZERO, None, None, None
    if state.quality == QualityStatus.SUSPECT:
        return 'quality_suspect', ZERO, None, None, None
    if metric == 'co2':
        if state.actual is None or state.setpoint is None:
            return 'missing_input', ZERO, None, None, None
        if state.setpoint == 0:
            return 'sp0_off_mode', ZERO, None, None, None
        lower, upper = _applied_bounds(metric, state, segment)
    elif metric == 'temperature':
        if state.actual is None or state.lower_setpoint is None or state.upper_setpoint is None:
            return 'missing_input', ZERO, None, None, None
        lower, upper = _applied_bounds(metric, state, segment)
        if lower > upper:
            return 'inverted_bounds', ZERO, None, lower, upper
    else:
        if state.actual is None:
            return 'missing_input', ZERO, None, None, None
        lower, upper = segment.lower, segment.upper
    if state.actual < lower:
        with localcontext() as ctx:
            ctx.prec = 50
            return ('out_low', Decimal(lower) - Decimal(state.actual),
                    RuleDeviationDirection.LOW, lower, upper)
    if state.actual > upper:
        with localcontext() as ctx:
            ctx.prec = 50
            return ('out_high', Decimal(state.actual) - Decimal(upper),
                    RuleDeviationDirection.HIGH, lower, upper)
    return 'valid', ZERO, None, lower, upper


@dataclass(frozen=True)
class _SlotRecord:
    """Engine-local evaluation record for one declared-group slot (never
    exported); carries the applied bounds captured at classification time
    so the detail views reuse the EXACT same decision."""
    start: datetime
    slot_end: datetime
    class_code: str
    deviation: Decimal
    direction: RuleDeviationDirection | None
    minutes_inside_cycle: Decimal
    minutes_in_effective: Decimal
    membership: tuple | None
    applied_lower: Decimal | None
    applied_upper: Decimal | None


def _grid_view(context, revisions, slots, requested_metrics):
    """Validate + index slot states by metric (declared context/basis) and
    build the segment registry (segment, rule_version) per metric key."""
    slot_states = defaultdict(dict)
    for slot in slots:
        if not isinstance(slot, ThresholdSlot):
            raise ValueError('Slots must be ThresholdSlot contracts')
        if slot.measurement_context != context.measurement_context:
            raise ValueError(
                f"slot for {slot.metric} declares context '{slot.measurement_context}' but the "
                f"evaluation pins '{context.measurement_context}': separate evaluations handle "
                'other contexts/bases')
        if slot.basis != context.basis:
            raise ValueError(
                f"slot for {slot.metric} declares basis '{slot.basis.value}' but the "
                f"evaluation pins '{context.basis.value}': separate evaluations handle other "
                'contexts/bases')
        if slot.metric not in requested_metrics:
            raise ValueError(
                f'slot for {slot.metric} is not part of the requested metrics '
                f'{requested_metrics}: pass only the declared evaluation inputs')
        index = _grid_index(context, slot.start)
        key = slot.metric
        if index in slot_states[key]:
            raise ValueError(
                f'duplicate slot {slot.start.isoformat()} for {key}: resolve upstream record '
                'identity before the engine (the engine never double counts)')
        slot_states[key][index] = slot
    segment_registry = defaultdict(list)
    for revision in revisions:
        for segment in sorted(revision.segments, key=rule_segment_sort_key):
            if segment.measurement_context != context.measurement_context \
                    or segment.basis != context.basis:
                continue  # inert here: other contexts/bases belong to separate evaluations
            segment_registry.setdefault(segment.metric, []).append((segment, revision.rule_version))
    return slot_states, segment_registry


def evaluate_rule_schedule(context, revisions, slots, metrics,
                           episode_min_minutes=None, publication=None, versions=None):
    """Evaluate typed threshold slots against explicitly selected immutable
    schedule revisions inside the DECLARED context/basis group.

    `metrics` keeps the requested order; `slots` come from the whole cycle
    record grain. The summary durations clip to effective_start/-
    effective_end. Episodes build over the full cycle internally; the
    result list keeps only episodes with positive requested-window overlap.
    `episode_min_minutes` filters only the list (original duration basis),
    never the summary totals. An explicit empty revision tuple is legal and
    keeps every requested metric unknown; publication/versions must agree
    with the pinned context when supplied.
    """
    core = _evaluate_core(context, revisions, slots, metrics, publication, versions)
    episodes = _build_episodes(
        context, metrics, core.group_records, core.segment_registry, core.cycle_end,
        core.effective_start, core.effective_end, episode_min_minutes)
    return _public_result(context, metrics, core, episodes, publication, core.versions)


def evaluate_rule_schedule_detail(context, revisions, slots, metrics,
                                  episode_min_minutes=None, publication=None,
                                  versions=None):
    """SAME evaluation call path as evaluate_rule_schedule, plus one typed
    `RuleSlotReview` per requested metric slot inside the effective window.
    The review rows reuse the records' classification/bounds captured at
    decision time, so timings/episodes and detail traces can never drift.
    """
    core = _evaluate_core(context, revisions, slots, metrics, publication, versions)
    episodes = _build_episodes(
        context, metrics, core.group_records, core.segment_registry, core.cycle_end,
        core.effective_start, core.effective_end, episode_min_minutes)
    result = _public_result(context, metrics, core, episodes, publication, core.versions)
    return result, tuple(_build_slot_reviews(context, metrics, core))


def _evaluate_core(context, revisions, slots, metrics, publication, versions):
    """Shared validation + slot classification for both public entrypoints."""
    if not isinstance(context, IntradayContext):
        raise ValueError('Evaluation pins an IntradayContext')
    if not isinstance(revisions, tuple) \
            or any(not isinstance(revision, RuleScheduleRevision) for revision in revisions):
        raise ValueError(
            'Pass explicitly selected RuleScheduleRevision contracts as a tuple '
            '(an empty tuple is a legal no-rule selection: metrics stay unknown)')
    _validate_selection(context, revisions)
    if not isinstance(slots, tuple) \
            or any(not isinstance(slot, ThresholdSlot) for slot in slots):
        raise ValueError('Typed ThresholdSlot inputs are required (whole-cycle record grain; '
                         'an empty tuple is a legal fully-missing stream)')
    if not metrics or len(set(metrics)) != len(metrics) \
            or any(metric not in RULE_METRIC_UNITS for metric in metrics):
        raise ValueError('Requested metrics must be explicit, nonempty, distinct and known')
    _validate_metrics(context, metrics)
    if publication is not None:
        if not isinstance(publication, PublicationRef):
            raise ValueError('Result publication must be a PublicationRef contract when set')
        if publication.scope_id != context.scope_id:
            raise ValueError(
                f"publication scope '{publication.scope_id}' does not match the pinned "
                f"evaluation scope '{context.scope_id}': provenance must agree with context")
        if publication.time_basis != context.time_basis:
            raise ValueError(
                'publication time basis must match the declared source_naive evaluation')
        if versions is not None and versions != publication.versions:
            raise ValueError(
                'explicit versions contradict the supplied publication: derive versions '
                'from the publication or pass none — the engine never invents identity')
        if versions is None:
            versions = publication.versions

    cycle_end = datetime.combine(context.cycle.end, datetime.min.time())
    cycle_start = _grid_anchor(context)
    for slot in slots:
        if slot.start > cycle_end:
            raise ValueError(
                f'slot {slot.start.isoformat()} starts beyond the cycle end; the upstream record '
                'grain stops at the cycle boundary (the exact end sample stays stored)')
    effective_start, effective_end = max(context.start, cycle_start), min(context.end, cycle_end)
    if effective_start >= effective_end:
        raise ValueError('The requested window carries zero duration inside the cycle')
    expected = _minutes(effective_end - effective_start)
    cadence = context.cadence
    cadence_minutes = Decimal(context.cadence_minutes)
    # Preserve the existing floor-grid semantics: every iterated slot is full.
    total_slots = int((cycle_end - cycle_start) // cadence)

    slot_states, segment_registry = _grid_view(context, revisions, slots, tuple(metrics))

    applications = []
    metric_results = []
    group_records = {}
    for metric in metrics:
        segment_pairs = segment_registry.get(metric, [])
        rule_version = segment_pairs[0][1] if segment_pairs else None
        segment_list = [pair[0] for pair in segment_pairs]
        states = slot_states.get(metric, {})
        records = []
        for step in range(total_slots):
            when = cycle_start + step * cadence
            slot_end = min(when + cadence, cycle_end)
            if slot_end <= when:
                break  # exact end-of-cycle sample: stored upstream, zero duration
            segment = _candidate_segment(segment_list, when)
            class_code, deviation, direction, applied_lower, applied_upper = _classify_slot(
                metric, states.get(step), segment)
            start_clipped = max(when, effective_start)
            end_clipped = min(slot_end, effective_end)
            if start_clipped >= end_clipped:
                minutes_effective = ZERO
            elif start_clipped == when and end_clipped == slot_end:
                minutes_effective = cadence_minutes
            else:
                # Only the boundary slots need fractional duration arithmetic.
                minutes_effective = _minutes(end_clipped - start_clipped)
            records.append(_SlotRecord(
                start=when, slot_end=slot_end, class_code=class_code, deviation=deviation,
                direction=direction, minutes_inside_cycle=cadence_minutes,
                minutes_in_effective=minutes_effective,
                membership=None if segment is None else _segment_identity(segment),
                applied_lower=applied_lower, applied_upper=applied_upper))
        # `valid_minutes` is the evaluated/eligible population: strict in-range
        # PLUS out-of-range slots (out hours are a subset of this population,
        # mirroring the BR-01/BR-03 fixtures valid25/out15/unknown5). Suspect /
        # invalid / missing slots stay outside this population.
        valid = sum((record.minutes_in_effective for record in records
                     if record.minutes_in_effective > ZERO
                     and record.class_code in VALID_CLASSES), ZERO)
        out = sum((record.minutes_in_effective for record in records
                   if record.minutes_in_effective > ZERO and record.class_code in OUT_CLASSES),
                  ZERO)
        out_low = sum((record.minutes_in_effective for record in records
                       if record.minutes_in_effective > ZERO and record.class_code == 'out_low'),
                      ZERO)
        out_high = sum((record.minutes_in_effective for record in records
                        if record.minutes_in_effective > ZERO and record.class_code == 'out_high'),
                       ZERO)
        unknown = sum((record.minutes_in_effective for record in records
                       if record.minutes_in_effective > ZERO
                       and record.class_code not in VALID_CLASSES), ZERO)
        suspect = sum((record.minutes_in_effective for record in records
                       if record.minutes_in_effective > ZERO
                       and record.class_code == 'quality_suspect'), ZERO)
        reason_totals = defaultdict(lambda: ZERO)
        for record in records:
            if record.minutes_in_effective > ZERO and record.class_code not in VALID_CLASSES:
                reason_totals[record.class_code] += record.minutes_in_effective
        applications.append(RuleMetricApplied(
            metric=metric, measurement_context=context.measurement_context, basis=context.basis,
            rule_version=rule_version, unit=RULE_METRIC_UNITS[metric]))
        sorted_reasons = tuple(sorted(
            (UnknownReasonShare(code, total) for code, total in reason_totals.items()),
            key=lambda share: (_primary_reason(share.reason_code),)))
        metric_results.append(RuleMetricDuration(
            metric=metric, measurement_context=context.measurement_context, basis=context.basis,
            rule_version=rule_version, expected_minutes=expected, valid_minutes=valid,
            out_minutes=out, out_low_minutes=out_low, out_high_minutes=out_high,
            suspect_minutes=suspect, unknown_minutes=unknown, unknown_reasons=sorted_reasons,
            out_hours=None if valid == ZERO else _hours(out),
            out_percent=_ratio(out, valid, 100),
            coverage_pct=_coverage(valid, expected)))
        group_records[metric] = records

    joint_results = _joint_summaries(metric_results, group_records, expected, metrics)
    return _CoreEvaluation(
        applications=tuple(applications), metric_results=tuple(metric_results),
        joint_results=tuple(joint_results), group_records=group_records,
        segment_registry=segment_registry, slot_states=slot_states,
        cycle_end=cycle_end, effective_start=effective_start,
        effective_end=effective_end, versions=versions)


def _public_result(context, metrics, core, episodes, publication, versions):
    return RuleDurationResult(
        scope_id=context.scope_id, cycle_id=context.cycle.cycle_id,
        compartment_id=context.compartment_ids[0],
        requested_start=context.start, requested_end=context.end,
        effective_start=core.effective_start, effective_end=core.effective_end,
        requested_metrics=tuple(metrics), applications=core.applications,
        metric_results=core.metric_results, joint_results=core.joint_results,
        episodes=tuple(episodes), publication=publication, versions=versions,
        time_basis=TimeBasis.SOURCE_NAIVE)


def _build_slot_reviews(context, metrics, core):
    """Type the per-slot trace from the SAME evaluation records (bounded to
    the effective window); ordering follows requested metrics then time."""
    reviews = []
    for metric in metrics:
        segment_pairs = core.segment_registry.get(metric, [])
        rule_version = segment_pairs[0][1] if segment_pairs else None
        states = core.slot_states.get(metric, {})
        for step, record in enumerate(core.group_records[metric]):
            if record.minutes_in_effective <= ZERO:
                continue  # outside the requested window: not part of the report
            state = states.get(step)
            reviews.append(RuleSlotReview(
                start=record.start, metric=metric,
                measurement_context=context.measurement_context, basis=context.basis,
                rule_version=rule_version,
                actual=None if state is None else state.actual,
                lower=record.applied_lower, upper=record.applied_upper,
                quality=None if state is None else state.quality,
                class_code=record.class_code, deviation=record.deviation,
                direction=record.direction, minutes=record.minutes_in_effective))
    return reviews


@dataclass(frozen=True)
class _CoreEvaluation:
    applications: tuple
    metric_results: tuple
    joint_results: tuple
    group_records: dict
    segment_registry: dict
    slot_states: dict
    cycle_end: datetime
    effective_start: datetime
    effective_end: datetime
    versions: Versions | None


def _joint_summaries(metric_results, group_records, expected, metrics):
    """ONE OR over ALL requested metrics of a legal request (>=2), summed
    over the jointly KNOWN slot intersection only (every contributing metric
    evaluated inside the requested window); a one-metric request has no OR
    row. Window clipping already lives in each slot's minutes, so off-grid
    query boundaries clip exactly once per slot."""
    if len(metrics) < 2:
        return ()
    joint_valid = joint_out = ZERO
    total_steps = len(next(iter(group_records.values())))
    for step in range(total_steps):
        states = [group_records[metric][step] for metric in metrics]
        if any(record.minutes_in_effective <= ZERO for record in states):
            continue  # outside the effective window: not jointly countable
        if any(record.class_code not in VALID_CLASSES for record in states):
            continue  # unknown/missing/suspect slots leave the joint intersection
        joint_valid += states[0].minutes_in_effective
        if any(record.class_code in OUT_CLASSES for record in states):
            joint_out += states[0].minutes_in_effective
    return (RuleJointDuration(
        metrics=tuple(metrics), measurement_context=metric_results[0].measurement_context,
        basis=metric_results[0].basis,
        contributions=tuple(
            RuleMetricApplied(
                metric=result.metric, measurement_context=result.measurement_context,
                basis=result.basis, rule_version=result.rule_version,
                unit=RULE_METRIC_UNITS[result.metric]) for result in metric_results),
        jointly_valid_minutes=joint_valid, joint_out_minutes=joint_out,
        joint_out_percent=_ratio(joint_out, joint_valid, 100),
        joint_coverage_pct=_coverage(joint_valid, expected)),)


def _build_full_cycle_runs(group_records):
    """INTERNAL full-cycle runs: contiguous OUT slots per metric with the
    same segment membership identity (any non-OUT class, a slot gap or a
    segment content change breaks a run)."""
    full_runs = {}
    for key, records in sorted(group_records.items()):
        runs = []
        run = None
        run_identity = None
        for record in records:
            is_out = record.class_code in OUT_CLASSES
            if run is not None and (not is_out or record.membership != run_identity):
                runs.append(run)
                run = None
            if is_out and run is not None and record.membership == run_identity:
                run.append(record)
            elif is_out and run is None:
                run = [record]
                run_identity = record.membership
        if run is not None:
            runs.append(run)
        full_runs[key] = runs
    return full_runs


def _build_episodes(context, metrics, group_records, segment_registry, cycle_end,
                    effective_start, effective_end, min_minutes):
    """Full-cycle runs → result episodes. Only episodes with POSITIVE
    requested-window overlap are returned, each keeping original facts plus
    clipped facts. The optional min filter (original duration basis)
    applies to the returned list and never to summary totals."""
    full_runs = _build_full_cycle_runs(group_records)
    episodes = []
    for metric, runs in sorted(full_runs.items()):
        segment_pairs = segment_registry.get(metric, [])
        if not segment_pairs:
            continue  # absent rule: out classes cannot exist anywhere
        rule_version = segment_pairs[0][1]
        for run in runs:
            slots_deviations = tuple(RuleSlotDeviation(
                start=record.start, direction=record.direction, deviation=record.deviation)
                for record in run)
            original_start = run[0].start
            original_end = min(run[-1].start + context.cadence, cycle_end)
            original_minutes = sum((record.minutes_inside_cycle for record in run), ZERO)
            clipped_spans = [
                (max(record.start, effective_start),
                 min(record.start + context.cadence, effective_end))
                for record in run
                if max(record.start, effective_start) < min(record.start + context.cadence,
                                                            effective_end)]
            if not clipped_spans:
                continue  # zero requested-window overlap: kept out of the result list
            clipped_minutes = sum((_minutes(end - start) for start, end in clipped_spans), ZERO)
            directions = tuple(sorted(
                {record.direction for record in run if record.direction is not None},
                key=lambda direction: direction.value))
            peak = max(item.deviation for item in slots_deviations)
            episodes.append(RuleEpisode(
                metric=metric, measurement_context=context.measurement_context,
                basis=context.basis, rule_version=rule_version, slots=slots_deviations,
                original_start=original_start, original_end=original_end,
                original_minutes=original_minutes,
                clipped_start=clipped_spans[0][0], clipped_end=clipped_spans[-1][1],
                clipped_minutes=clipped_minutes, directions=directions,
                peak_deviation=peak,
                peak_time=next(item.start for item in slots_deviations
                               if item.deviation == peak)))
    episodes = [episode for episode in episodes
                if min_minutes is None or episode.original_minutes >= min_minutes]
    episodes.sort(key=lambda episode: (
        metrics.index(episode.metric), episode.original_start, episode.original_end))
    return episodes
