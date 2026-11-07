"""Pure BR-03 service over the sealed drain mart and the T14 engine.

The declared evaluation group is drain with the MEASURED basis and the exact
context 'drain_process_computer' (EC/pH only — no requested/realized duality).
Each slot/metric combines ONLY that measure's own frozen verdict into one
evaluation-input status (invalid > suspect > missing > accepted); a non-
accepted outcome MASKS the value (actual=None) for evaluation, while the
frozen triplet and the original numbers stay untouched in the mart and stay
visible through the source-facts views. pH-0-as-suspect never removes the
accepted EC on the same source row; out-of-target is a business verdict, not
a quality decision; EC negative is quality-invalid per the measure contract.
Fixed bounds come ONLY from the explicitly selected immutable schedule
revision (EC cannot be negative; lower <= upper; equality in-range). With no
revision selected the metrics stay unknown with joint valid 0; a partial
revision leaves uncovered metrics unknown. An explicitly selected profile is
validated on EVERY path (cached, uncached, namespace-less and bounded
detail) to declare only drain EC/pH measured fixed-bound segments before
any cache lookup or mart fetch — caching must not change query validity.

The bounded result cache mirrors the approved climate pattern with a
DRAIN-SAFE key: ('drain',) + non-secret endpoint namespace + full
publication/version identity + clamped cycle context + measured basis + a
content signature of the selected revision (fixed lower/upper included,
delta segments refused). No REVISION string inference, no climate delta
reuse, no connection/raw row retention; adapters without a namespace
disable the cache. Detail traces bound the input window BEFORE any fetch
(main supplementary oracle: the longest EC/pH episodes span ~33-61 days,
so a whole-cycle read is never acceptable).
"""
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from json import dumps
from threading import Lock

from greenhouse.contracts import (
    DrainEvaluation, IntradayContext, LineageResult, PublicationRef, QualityStatus,
    QueryContext, RuleBasis, RuleDurationResult, RuleScheduleRevision, RuleSlotReview,
    ThresholdSlot, TimeBasis, CycleWindow,
)
from greenhouse.queries.climate import episode_key, publication_identity, page_episodes
from greenhouse.queries.rule_duration import evaluate_rule_schedule, evaluate_rule_schedule_detail

DRAIN_CONTEXT = 'drain_process_computer'
DRAIN_METRICS = ('ec', 'ph')
ACTUAL_COLUMNS = {'ec': 'ec_drain', 'ph': 'ph_drain'}
MEASURE_CODES = {'ec': 'EC_drain_PC', 'ph': 'pH_drain_PC'}
STATUS_PRECEDENCE = {'invalid': 0, 'suspect': 1, 'missing': 2, 'accepted': 3}
# Bounded detail: the input window is truncated BEFORE reading the mart.
# max_slots counts 5-minute SLOTS per requested window per metric pair fetch.
DRAIN_REVIEW_MAX_SLOTS = 1000
CADENCE_MINUTES = 5
CACHE_MAX_ENTRIES = 2

_CACHE_LOCK = Lock()
_CACHE = {}
_CACHE_ORDER = []


def triplet_of(quality, code):
    """Frozen triplet of one drain measure; an ABSENT policy measure is an
    explicit unknown, never a silent accept (same semantics as climate)."""
    entry = (quality or {}).get(code)
    if entry is None:
        return {'status': 'missing', 'applicability': 'unknown',
                'reason': 'measure_quality_absent'}
    return {'status': entry.get('status'), 'applicability': entry.get('applicability'),
            'reason': entry.get('reason')}


def build_slots(rows):
    """Frozen mart rows -> typed ThresholdSlots for BOTH drain metrics at the
    measured basis. The slot's quality is THIS metric's own combined verdict;
    the numeric value survives only when the triplet is accepted AND the
    measure is applicable (EC negative and pH-0-suspect therefore never
    reach the engine as numbers — they keep their unknown class and the
    frozen values stay in the mart/source facts)."""
    slots = []
    for row in rows:
        for metric in DRAIN_METRICS:
            triplet = triplet_of(row.measure_quality, MEASURE_CODES[metric])
            status = triplet.get('status') or 'missing'
            raw = getattr(row, ACTUAL_COLUMNS[metric])
            actual = None
            if status == 'accepted' and triplet.get('applicability') == 'applicable' \
                    and raw is not None:
                actual = Decimal(str(raw))
            slots.append(ThresholdSlot(
                start=row.slot_start, metric=metric, measurement_context=DRAIN_CONTEXT,
                basis=RuleBasis.MEASURED, actual=actual,
                quality=QualityStatus(status)))
    return tuple(slots)


def intraday_context(context: QueryContext, cycle: CycleWindow):
    """Day-grain windows map to source-naive calendar slots of one cycle, in
    the EXACT drain_process_computer context at MEASURED basis."""
    effective = replace(context, start=max(context.start, cycle.start),
                        end=min(context.end, cycle.end))
    return IntradayContext(
        scope_id=context.scope_id, cycle=cycle, compartment_ids=context.compartment_ids,
        start=datetime.combine(context.start, time.min),
        end=datetime.combine(context.end, time.min),
        measurement_context=DRAIN_CONTEXT, basis=RuleBasis.MEASURED, time_basis=TimeBasis.SOURCE_NAIVE)


def drain_rule_content_signature(revision):
    """Content signature of the explicitly selected FIXED-BOUNDS revision
    (lower/upper per segment included; delta segments are refused — they
    belong to climate's setpoint-relative semantics, not drain fixed
    bounds)."""
    if revision is None:
        return None
    for segment in revision.segments:
        if segment.delta is not None:
            raise ValueError('Drain revisions carry fixed bounds, not delta segments')
    payload = [revision.rule_version, revision.rule_id, revision.version,
               revision.label, bool(revision.is_demo), revision.evidence_reference,
               [(segment.metric, segment.measurement_context, segment.basis.value,
                 segment.start.isoformat(), segment.end.isoformat(),
                 str(segment.lower), str(segment.upper))
                for segment in revision.segments]]
    return sha256(dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def validate_drain_profile(revision):
    """Declared drain profile boundary: an explicitly selected BR-03 profile
    carries ONLY fixed-bound EC/pH segments in the exact
    'drain_process_computer' context at the MEASURED basis — no delta and
    no foreign group. Partial EC-only or pH-only profiles stay legal (the
    uncovered metrics remain unknown) and a no-rule selection stays legal.
    This runs on EVERY service path — cached, use_cache=False,
    namespace-less adapters and bounded detail — BEFORE any cache lookup
    or mart fetch, so caching can never change query validity."""
    if revision is None:
        return
    for segment in revision.segments:
        if segment.measurement_context != DRAIN_CONTEXT:
            raise ValueError(
                f'Drain profile {revision.rule_version} declares segment '
                f'{segment.metric!r} in context {segment.measurement_context!r}; '
                f'drain evaluations declare {DRAIN_CONTEXT!r} only')
        if segment.metric not in DRAIN_METRICS:
            raise ValueError(
                f'Drain profile {revision.rule_version} declares metric '
                f'{segment.metric!r}; drain evaluations rule EC/pH only')
        if segment.basis is not RuleBasis.MEASURED:
            raise ValueError(
                f'Drain profile {revision.rule_version} declares basis '
                f'{segment.basis.value}; drain evaluations are MEASURED only')
        if segment.delta is not None:
            raise ValueError(
                f'Drain profile {revision.rule_version} carries delta bounds '
                f'on segment {segment.metric!r}; drain evaluations use '
                'fixed bounds, not delta segments')


def validate_revision_for_publication(revision, publication, cycle):
    """Pre-cache identity validation: the cache key hashes CONTENT, but two
    Databases could hold same-content revisions claiming different
    scope/compartment. The engine also validates on a miss, so this MUST
    run before any cache lookup — a foreign/fake revision can never be
    served from a hit."""
    if revision is None:
        return
    if revision.scope_id != publication.scope_id \
            or revision.compartment_id != cycle.compartment_id:
        raise ValueError(
            f'Selected revision {revision.rule_version} claims '
            f'{revision.scope_id}/{revision.compartment_id} while the pinned '
            f'view is {publication.scope_id}/{cycle.compartment_id}')


def bounded_trace_context(trace_cycle, context_scope, compartment_ids, start, end):
    """A WINDOW-cycle intraday context for the trace page only: the small
    CycleWindow (enclosing the whole-page dates, built by
    _clipped_read_window) keeps the engine's per-slot classification
    IDENTICAL to the summary/evaluation path (same _validate/_classify
    call path) while the record allocation and episode building never
    span the whole long cycle — summary owns original whole-cycle
    metadata."""
    return IntradayContext(
        scope_id=context_scope, cycle=trace_cycle, compartment_ids=compartment_ids,
        start=start, end=end, measurement_context=DRAIN_CONTEXT,
        basis=RuleBasis.MEASURED, time_basis=TimeBasis.SOURCE_NAIVE)


def evaluation_cache_key(database, publication, context, cycle, signature):
    """Drain-safe key: a ('drain',) prefix + the non-secret endpoint tuple +
    full publication/versions identity + clamped cycle context + the fixed
    declared basis + the revision CONTENT signature."""
    endpoint = database or ()
    return (('drain',) + tuple(str(part) for part in endpoint)
            + publication_identity(publication)
            + (context.scope_id, context.publication_id, context.snapshot_id,
               context.cycle_id, cycle.cycle_id, cycle.compartment_id,
               cycle.start.isoformat(), cycle.end.isoformat(),
               tuple(context.compartment_ids), context.start.isoformat(),
               context.end.isoformat(), context.rule_version or '',
               RuleBasis.MEASURED.value, signature or ''))


def _cache_get(key):
    with _CACHE_LOCK:
        returned = _CACHE.get(key)
        if key in _CACHE_ORDER:
            _CACHE_ORDER.remove(key)
            _CACHE_ORDER.append(key)
        return returned


def _cache_put(key, evaluation):
    with _CACHE_LOCK:
        _CACHE[key] = evaluation
        if key not in _CACHE_ORDER:
            _CACHE_ORDER.append(key)
        while len(_CACHE_ORDER) > CACHE_MAX_ENTRIES:
            stale = _CACHE_ORDER.pop(0)
            _CACHE.pop(stale, None)


@dataclass(frozen=True)
class _ReadWindow:
    """Plan of one bounded read: the ANCHOR is the 5-minute grid floor of
    the requested start (so a straddling slot keeps its exact overlap),
    `fetch_end` is the exclusive fetch bound (never beyond the caller's
    wanted end), and the small CycleWindow always ENCLOSES the page at
    whole-date boundaries so same-day pages never collapse."""
    anchor: datetime
    end: datetime          # exclusive fetch/evaluate bound of this page
    span_slots: int
    truncated: bool
    trace_cycle: CycleWindow


def _clipped_read_window(start, wanted_end, max_slots, cycle_id, compartment_id):
    """Exact clipping semantics per the review-preflight: floor read anchor
    + min(wanted_end) bounds (never ceil-past-end), a strict integer
    slot cap validated BEFORE any fetch, and a small trace cycle that
    ENCLOSES the page at whole-date boundaries while keeping the REAL
    cycle/compartment identity (the narrowed dates are page-local, the
    identity is not)."""
    if type(max_slots) is not int or max_slots < 1 or max_slots > DRAIN_REVIEW_MAX_SLOTS:
        raise ValueError(f'Window slot cap must be an int <= {DRAIN_REVIEW_MAX_SLOTS}')
    day_mid = datetime.combine(start.date(), time.min)
    back_seconds = (start - day_mid).total_seconds() % (CADENCE_MINUTES * 60)
    anchor = start - timedelta(seconds=back_seconds)
    span_seconds = (wanted_end - anchor).total_seconds()
    full_slots, remainder = divmod(span_seconds, CADENCE_MINUTES * 60)
    full_span_slots = int(full_slots) + (1 if remainder else 0)
    truncated = full_span_slots > max_slots
    covered_slots = min(full_span_slots, max_slots)
    cap_end = anchor + timedelta(minutes=CADENCE_MINUTES * covered_slots)
    fetch_end = min(cap_end, wanted_end)
    cycle_end_date = (fetch_end - timedelta(microseconds=1)).date() + timedelta(days=1)
    trace_cycle = CycleWindow(cycle_id, compartment_id, anchor.date(), cycle_end_date)
    return _ReadWindow(anchor=anchor, end=fetch_end, span_slots=covered_slots,
                       truncated=truncated, trace_cycle=trace_cycle)


@dataclass(frozen=True)
class SlotReviewWindow:
    """Bounded detail result: the reviews were built from a fetch that never
    exceeded max_slots; `end` is the actual end of the fetched/evaluated
    window and `truncated` says whether the caller's requested end had to
    be cut — the way T17 pages long episodes WITHOUT materializing
    whole-cycle windows."""
    reviews: tuple[RuleSlotReview, ...]
    start: datetime
    end: datetime
    wanted_end: datetime
    truncated: bool
    window_slots: int


@dataclass(frozen=True)
class SlotFactsWindow:
    """Bounded source-facts page: frozen rows exactly as materialized plus
    the truthful read window (a possible final partial slot is CEILED in,
    a capped end carries truncated=True)."""
    rows: tuple
    start: datetime
    end: datetime
    wanted_end: datetime
    truncated: bool
    window_slots: int


class DrainService:
    """Same action discipline as ClimateService: one pinned publication, the
    declared drain/measured group per evaluation, no SQL and no re-derivation
    here."""

    def __init__(self, adapter):
        self.adapter = adapter

    def pin_publication(self, scope_id):
        return self.adapter.pin_publication(scope_id)

    def get_evaluation(self, context: QueryContext, *, rule_version: str | None = None,
                       use_cache: bool = True) -> DrainEvaluation:
        """Summary + episode step over one pinned publication. Full-cycle slots
        evaluate ONCE per call (the engine needs the whole cycle to build the
        original episode spans); per-slot traces are NOT attached here —
        bounded detail comes from get_slot_review windows (the longest drain
        episodes span weeks, so trace windows must stay explicit)."""
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        effective = replace(context, start=max(context.start, cycle.start),
                            end=min(context.end, cycle.end))
        if effective.start >= effective.end:
            raise ValueError('The chosen window does not overlap the cycle')
        revisions = (self.adapter.schedule(rule_version),) if rule_version else ()
        # The declared-profile boundary runs FIRST and identically on every
        # path (cached, use_cache=False, namespace-less): caching must not
        # change query validity, so no delta/foreign-group revision is
        # accepted as an all-unknown result on any path.
        validate_drain_profile(revisions[0] if revisions else None)
        database = getattr(self.adapter, 'database_key', None)
        enable_cache = use_cache and database is not None
        validate_revision_for_publication(revisions[0] if revisions else None,
                                          publication, cycle)
        key = evaluation_cache_key(
            database or '', publication, effective, cycle,
            drain_rule_content_signature(revisions[0]) if revisions else None) if enable_cache else None
        if enable_cache:
            cached = _cache_get(key)
            if cached is not None:
                return cached
        rows = self.adapter.read_drain_slots(effective, publication)
        slots = build_slots(rows)
        result = evaluate_rule_schedule(
            intraday_context(effective, cycle), revisions, slots, DRAIN_METRICS,
            publication=publication, versions=publication.versions)
        evaluation = DrainEvaluation(
            publication=publication, context=effective, basis=RuleBasis.MEASURED,
            requested_rule_version=rule_version or None, result=result, slot_reviews=(),
            rule_id=revisions[0].rule_id if revisions else None,
            rule_label=revisions[0].label if revisions else None,
            rule_is_demo=revisions[0].is_demo if revisions else None,
            rule_evidence_reference=revisions[0].evidence_reference if revisions else None)
        if enable_cache:
            _cache_put(key, evaluation)
        return evaluation

    def get_slot_review(self, context: QueryContext, *, rule_version: str | None = None,
                        window_start=None, window_end=None,
                        max_slots: int = DRAIN_REVIEW_MAX_SLOTS) -> SlotReviewWindow:
        """Bounded per-slot trace: the read window is clamped to the pinned
        cycle view AND to max_slots BEFORE any fetch, with EXACT clipping
        (floor grid anchor + min(wanted_end) so a straddling slot keeps its
        partial overlap and no slot beyond the wanted end is ever fetched).
        Reviews reuse the engine's own classification path on a small
        window-cycle context (identical per-slot math, no whole-cycle
        episode allocation; summary owns original metadata), and carry the
        frozen source facts for the same slots."""
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        effective = replace(context, start=max(context.start, cycle.start),
                            end=min(context.end, cycle.end))
        revisions = (self.adapter.schedule(rule_version),) if rule_version else ()
        # Same declared-profile boundary as the summary path — detail must
        # never bypass it (validated before the bounded fetch).
        validate_drain_profile(revisions[0] if revisions else None)
        validate_revision_for_publication(revisions[0] if revisions else None,
                                          publication, cycle)
        start = max(datetime.combine(effective.start, time.min),
                    window_start or datetime.combine(effective.start, time.min))
        wanted_end = min(datetime.combine(effective.end, time.min),
                         window_end or datetime.combine(effective.end, time.min))
        if start >= wanted_end:
            raise ValueError('The bounded review window is empty')
        plan = _clipped_read_window(start, wanted_end, max_slots, context.cycle_id,
                                    context.compartment_ids[0])
        rows = self.adapter.read_drain_slots(
            effective, publication, window_start=plan.anchor, window_end=plan.end)
        slots = build_slots(rows)
        # The engine gets the REQUESTED (possibly off-grid) start so the
        # straddling slot's minutes clip EXACTLY at the requested boundary.
        _, reviews = evaluate_rule_schedule_detail(
            bounded_trace_context(plan.trace_cycle, context.scope_id,
                                  context.compartment_ids, start, plan.end),
            revisions, slots, DRAIN_METRICS,
            publication=publication, versions=publication.versions)
        # The engine builds records from the small window's midnight anchor
        # (zero-minute ghosts before the requested start); the trace page
        # reports every slot OVERLAPPING [start, plan.end) — including the
        # straddler that starts before the requested start, with true
        # partial minutes.
        reviews = tuple(
            review for review in reviews
            if review.start < plan.end
            and review.start + timedelta(minutes=CADENCE_MINUTES) > start)
        measured = {row.slot_start: row for row in rows}
        enriched = []
        for review in reviews:
            row = measured.get(review.start)
            if row is None:
                enriched.append(review)
                continue
            triplet = triplet_of(row.measure_quality, MEASURE_CODES[review.metric])
            source_quality = (f"{MEASURE_CODES[review.metric]} {triplet.get('status')}"
                              f"/{triplet.get('applicability')}"
                              + (f" ({triplet.get('reason')})" if triplet.get('reason') else '')
                              + f"; source={getattr(row, ACTUAL_COLUMNS[review.metric])}")
            enriched.append(replace(review, source_quality=source_quality))
        return SlotReviewWindow(
            reviews=tuple(enriched), start=start, end=plan.end, wanted_end=wanted_end,
            truncated=plan.truncated, window_slots=plan.span_slots)

    def slot_facts(self, context: QueryContext, *, window_start=None,
                   window_end=None, max_slots: int = DRAIN_REVIEW_MAX_SLOTS):
        """Independent frozen source identity of the slot rows: clamp uses
        the QUERY CONTEXT window (not just the cycle), the read window uses
        the SAME exact-clip helper as the trace (floor anchor + min(wanted
        end)), and it never materializes more than max_slots. Values stay
        original whether the evaluation masked them or not."""
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        view_start = max(datetime.combine(context.start, time.min),
                         datetime.combine(cycle.start, time.min))
        view_end = min(datetime.combine(context.end, time.min),
                       datetime.combine(cycle.end, time.min))
        start = max(view_start, window_start or view_start)
        wanted_end = min(view_end, window_end or view_end)
        if start >= wanted_end:
            raise ValueError('The bounded source window is empty inside the query view')
        plan = _clipped_read_window(start, wanted_end, max_slots, context.cycle_id,
                                    context.compartment_ids[0])
        rows = self.adapter.read_drain_slots(context, publication,
                                             window_start=plan.anchor,
                                             window_end=plan.end)
        if any(not cycle.start <= row.slot_start.date() < cycle.end for row in rows):
            raise ValueError('Slot facts leaked outside the pinned cycle')
        if len({row.slot_start for row in rows}) != len(rows):
            raise ValueError('Duplicate drain slot grain in the pinned mart')
        return SlotFactsWindow(
            rows=tuple(rows), start=start, end=plan.end, wanted_end=wanted_end,
            truncated=plan.truncated, window_slots=plan.span_slots)

    def get_lineage(self, publication: PublicationRef, *, revision_id=None,
                    record_key=None):
        if (revision_id is None) == (record_key is None):
            raise ValueError('Select exactly one revision_id or logical record key')
        if self.adapter.publication(publication.publication_id) != publication:
            raise ValueError('Pinned publication metadata differs')
        return self.adapter.read_lineage(publication, revision_id=revision_id,
                                         record_key=record_key)
