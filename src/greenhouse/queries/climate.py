"""Pure BR-01 service over the sealed climate mart and the T14 engine.

The declared evaluation group is climate with an explicit RuleBasis:
requested (t_heat_sp/t_vent_sp/co2_sp) or realized (t_heat_vip/
t_ventlee_vip/co2_vip) — evaluated separately, never coalesced. Slot inputs
combine ONLY that metric's own actual and required setpoint verdicts into
one evaluation-input status with invalid > suspect > missing > accepted
precedence (main T15 decision); the frozen per-measure triplets stay in the
mart/detail and a slot never poisons its sibling metric or the other basis.
A selected schedule revision may cover only part of the request (uncovered
metrics stay rule_not_configured with joint valid 0); revision selection is
at most ONE per metric, and an explicit empty selection stays unknown.
"""
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal
from hashlib import sha256
from json import dumps
from threading import Lock
from typing import Protocol

from greenhouse.contracts import (
    ClimateEvaluation, CycleWindow, IntradayContext, LineageResult, PublicationRef,
    QualityStatus, QueryContext, RuleBasis, RuleScheduleRevision, RuleSlotReview,
    RuleDurationResult, ThresholdSlot, TimeBasis,
)
from greenhouse.queries.rule_duration import evaluate_rule_schedule, evaluate_rule_schedule_detail

CLIMATE_CONTEXT = 'climate'
CLIMATE_METRICS = ('temperature', 'co2')
REQUESTED_SETPOINTS = {'temperature': ('t_heat_sp', 't_vent_sp'), 'co2': ('co2_sp',)}
REALIZED_SETPOINTS = {'temperature': ('t_heat_vip', 't_ventlee_vip'), 'co2': ('co2_vip',)}
ACTUAL_CODES = {'temperature': 'Tair', 'co2': 'CO2air'}
VALUE_FIELDS = {'temperature': 'tair', 'co2': 'co2air'}

STATUS_PRECEDENCE = {
    QualityStatus.INVALID: 0,
    QualityStatus.SUSPECT: 1,
    QualityStatus.MISSING: 2,
    QualityStatus.ACCEPTED: 3,
}


class ClimateAdapter(Protocol):
    def pin_publication(self, scope_id: str) -> PublicationRef: ...
    def publication(self, publication_id: str) -> PublicationRef: ...
    def cycle_window(self, context: QueryContext) -> CycleWindow: ...
    def read_climate_slots(self, context: QueryContext, publication: PublicationRef): ...
    def schedule(self, rule_version: str) -> RuleScheduleRevision: ...
    def read_lineage(self, publication: PublicationRef, *, revision_id: str | None = None,
                     record_key: str | None = None) -> LineageResult: ...


def _triplet(quality_map, code):
    """Frozen quality triplet of one measure; an ABSENT policy measure is an
    explicit unknown, never a silent accept."""
    entry = (quality_map or {}).get(code)
    if entry is None:
        return {'status': QualityStatus.MISSING.value, 'applicability': 'unknown',
                'reason': 'measure_quality_absent'}
    return entry


def _verdict(triplet, value=None):
    """Is this measure usable as an evaluation input? Accepted verdict counts
    only when applicability is APPLICABLE — except the ONE numeric case:
    a SELECTED CO2 setpoint with status accepted, applicability unknown and
    EXACT Decimal value 0 keeps its number so the engine can return
    sp0_off_mode (it stays unknown, never valid). Nothing else passes; never
    integer coercions (0.5/−0.5 are NOT zero)."""
    if triplet.get('status') != 'accepted':
        return False
    if triplet.get('applicability') == 'applicable':
        return True
    if value is None:
        return False
    if not isinstance(value, Decimal):
        return False
    return value == Decimal(0)


def combined_quality(triplets):
    """One evaluation-input status for THIS slot/metric: invalid > suspect >
    missing > accepted across the metric's actual and required basis
    setpoints. The original triplets stay frozen for detail; a non-accepted
    outcome MASKS the input values (actual=None) so the engine reasons
    unknown — evaluation-input masking, never a source rewrite.
    Invalid/suspect keep their classification."""
    statuses = (QualityStatus(triplet.get('status', 'missing')) for triplet in triplets)
    return min(statuses, key=lambda status: STATUS_PRECEDENCE[status])


def build_slots(rows, basis: RuleBasis):
    """Frozen mart rows → typed ThresholdSlots for BOTH requested metrics at
    the declared basis. Values flow only through _verdict; everything else
    is masked (actual/None) keeping the frozen verdict in the source row."""
    setpoint_codes = REQUESTED_SETPOINTS if basis == RuleBasis.REQUESTED else REALIZED_SETPOINTS
    slots = []
    for row in rows:
        quality_map = row.measure_quality or {}
        for metric in CLIMATE_METRICS:
            triplets = [_triplet(quality_map, ACTUAL_CODES[metric])]
            triplets.extend(_triplet(quality_map, code)
                            for code in setpoint_codes[metric])
            status = combined_quality(triplets)
            if metric == 'co2':
                sp_value = row.co2_sp if basis == RuleBasis.REQUESTED else row.co2_vip
                zero_exception = (triplets[-1].get('status') == 'accepted'
                                  and triplets[-1].get('applicability') == 'unknown'
                                  and sp_value is not None and Decimal(sp_value) == Decimal(0))
                sp_usable = _verdict(triplets[-1], sp_value)
                # Accepted-with-unknown zero stays a numeric 0 for the engine.
                if zero_exception:
                    setpoint = Decimal(0)
                elif sp_usable:
                    setpoint = Decimal(sp_value)
                else:
                    setpoint = None
                actual = (Decimal(row.co2air)
                          if (triplets[0].get('status') == 'accepted'
                              and triplets[0].get('applicability') == 'applicable'
                              and status == QualityStatus.ACCEPTED) else None)
                slots.append(ThresholdSlot(
                    start=row.slot_start, metric=metric, measurement_context=CLIMATE_CONTEXT,
                    basis=basis, actual=actual, setpoint=setpoint,
                    quality=status))
            else:
                low_code, high_code = setpoint_codes[metric]
                lower = getattr(row, low_code)
                upper = getattr(row, high_code)
                lower_usable = (triplets[1].get('status') == 'accepted'
                                and triplets[1].get('applicability') == 'applicable')
                upper_usable = (triplets[2].get('status') == 'accepted'
                                and triplets[2].get('applicability') == 'applicable')
                # Invalid/suspect keep their verdict but never their numeric.
                actual = (Decimal(row.tair)
                          if (triplets[0].get('status') == 'accepted'
                              and triplets[0].get('applicability') == 'applicable'
                              and status == QualityStatus.ACCEPTED) else None)
                slots.append(ThresholdSlot(
                    start=row.slot_start, metric=metric, measurement_context=CLIMATE_CONTEXT,
                    basis=basis, actual=actual,
                    lower_setpoint=Decimal(lower) if lower_usable else None,
                    upper_setpoint=Decimal(upper) if upper_usable else None,
                    quality=status))
    return tuple(slots)


def replace_intraday(context: IntradayContext, start, end):
    """Same declared identity with a narrower half-open source window."""
    return IntradayContext(
        scope_id=context.scope_id, cycle=context.cycle, compartment_ids=context.compartment_ids,
        start=start, end=end, measurement_context=context.measurement_context,
        basis=context.basis, cadence_minutes=context.cadence_minutes,
        time_basis=context.time_basis)


def intraday_context(context: QueryContext, cycle: CycleWindow, basis: RuleBasis):
    """Day-grain windows map to source-naive calendar slots of one cycle."""
    return IntradayContext(
        scope_id=context.scope_id, cycle=cycle, compartment_ids=context.compartment_ids,
        start=datetime.combine(context.start, time.min), end=datetime.combine(context.end, time.min),
        measurement_context=CLIMATE_CONTEXT, basis=basis, time_basis=TimeBasis.SOURCE_NAIVE)


def episode_key(episode):
    """Stable identity of one episode for user-selection re-resolution
    across rule/filter/basis reruns (T13 stale-selection lesson)."""
    return (episode.metric, episode.original_start.isoformat(),
            episode.original_end.isoformat(), episode.rule_version)


# ------------------------------------------------- bounded result cache (T15) --
# Main T15 decision after the measured 5.36s smoke: a SMALL, bounded,
# thread-safe LRU (max 2 entries) over the immutable evaluation RESULT DTO on
# the real service/UI path. The cache holds evaluated results ONLY — never
# connections, never raw slot rows, never the current pointer. Keys carry the
# database namespace, the full pinned publication/version identity, the
# clamped cycle context and basis, and the signature of the explicitly
# selected immutable rule revision content (not merely its version string).
# Hits occur only AFTER the pin/context validation has run; a foreign or
# unpinned context raises exactly as before and is never served or cached.
CACHE_MAX_ENTRIES = 2

_CACHE_LOCK = Lock()
_CACHE = {}
_CACHE_ORDER = []


def rule_content_signature(revision):
    """Content signature of the explicitly selected clock- frozen schedule
    revision (segment fields included, not just the version string)."""
    if revision is None:
        return None
    payload = [revision.rule_version, revision.rule_id, revision.version,
               revision.label, bool(revision.is_demo), revision.evidence_reference,
               [(segment.metric, segment.measurement_context, segment.basis.value,
                 segment.start.isoformat(), segment.end.isoformat(), str(segment.delta))
                for segment in revision.segments]]
    return sha256(dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def publication_identity(publication):
    versions = publication.versions
    return (publication.publication_id, publication.scope_id, publication.snapshot_id,
            publication.committed_at.isoformat(), publication.finality_status,
            publication.time_basis.value, versions.code_version,
            versions.quality_policy_version, versions.parser_version,
            versions.metric_version, versions.config_bundle_hash,
            versions.pipeline_run_id)


def context_identity(context, cycle):
    """Declared identity + the CLAMPED window that the evaluation used.
    (time_basis lives on the pinned publication, already in its identity;
    the context's own rule_version is included so a declared-rule context
    can never be served by an entry keyed with another selection.)"""
    return (context.scope_id, context.publication_id, context.snapshot_id,
            context.cycle_id, cycle.cycle_id, cycle.compartment_id,
            cycle.start.isoformat(), cycle.end.isoformat(),
            tuple(context.compartment_ids), context.start.isoformat(),
            context.end.isoformat(), context.rule_version or '')


def evaluation_cache_key(database, publication, context, cycle, basis, signature):
    """`database` is the NON-SECRET endpoint tuple (host, port, dbname, user)
    from the connection — never a password or DSN; adapters without a
    namespace disable caching instead of sharing one anonymous key."""
    return (database,) + publication_identity(publication) \
        + context_identity(context, cycle) + (basis.value, signature or '')


def _cache_get(key):
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE_ORDER.remove(key)
            _CACHE_ORDER.append(key)
            return _CACHE[key]
        return None


def _cache_put(key, evaluation):
    with _CACHE_LOCK:
        _CACHE[key] = evaluation
        if key in _CACHE_ORDER:
            _CACHE_ORDER.remove(key)
        _CACHE_ORDER.append(key)
        while len(_CACHE_ORDER) > CACHE_MAX_ENTRIES:
            stale = _CACHE_ORDER.pop(0)
            _CACHE.pop(stale, None)


def page_episodes(result: RuleDurationResult, *, metric_order=CLIMATE_METRICS,
                  metric: str | None = None, min_minutes: Decimal | None = None,
                  limit: int = 50, offset: int = 0):
    """Bounded/paged episode list: longest run first (original minutes), then
    requested metric order and start time. `metric` (None = all) and the
    user-selectable minimum duration (None = no filter; the demo default is
    15 minutes on the screen) filter ONLY this list — summary totals come
    from the same result unfiltered. Returns the page plus the FILTERED
    total so paging UIs know the size."""
    if (type(limit) is not int or limit < 1 or limit > 500
            or type(offset) is not int or offset < 0):
        raise ValueError('Episode paging takes a bounded page offset/limit')
    if metric is not None and metric not in metric_order:
        raise ValueError(f'Unknown metric filter {metric}')
    episodes = [episode for episode in result.episodes
                if (metric is None or episode.metric == metric)
                and (min_minutes is None or episode.original_minutes >= min_minutes)]
    ordered = sorted(episodes, key=lambda episode: (
        -episode.original_minutes, metric_order.index(episode.metric),
        episode.original_start, episode.original_end))
    return tuple(ordered[offset:offset + limit]), len(ordered)


class ClimateService:
    """Same action discipline as IrrigationService: one pinned publication,
    declared basis per evaluation, no SQL and no re-derivation here."""

    def __init__(self, adapter: ClimateAdapter):
        self.adapter = adapter

    def pin_publication(self, scope_id):
        return self.adapter.pin_publication(scope_id)

    def get_evaluation(self, context: QueryContext, *, basis: RuleBasis,
                       rule_version: str | None = None,
                       use_cache: bool = True) -> ClimateEvaluation:
        """Summary + episode step. Full-cycle slots evaluate ONCE per action
        (the engine needs the whole cycle for episodes); per-slot traces are
        NOT attached here — bounded detail comes from get_slot_review per
        selected episode/page so a rerun never prebuilds thousands of rows.

        Bounded result cache (max 2 entries, main T15 decision): lookup
        happens only after pin/context validation; the key carries the
        non-secret endpoint namespace, full publication/version identity,
        the clamped cycle context, basis and the rule revision CONTENT
        signature. The rule-change benchmark passes use_cache=False to
        force a real recompute; adapters without a namespace disable the
        cache entirely (no anonymous key)."""
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        effective = replace(context, start=max(context.start, cycle.start),
                            end=min(context.end, cycle.end))
        if effective.start >= effective.end:
            raise ValueError('The chosen window does not overlap the cycle')
        revisions = (self.adapter.schedule(rule_version),) if rule_version else ()
        signature = rule_content_signature(revisions[0]) if revisions else None
        database = getattr(self.adapter, 'database_key', None)
        enable_cache = use_cache and database is not None
        key = evaluation_cache_key(database or '', publication, effective, cycle,
                                   basis, signature) if enable_cache else None
        if enable_cache:
            cached = _cache_get(key)
            if cached is not None:
                return cached
        rows = self.adapter.read_climate_slots(effective, publication)
        slots = build_slots(rows, basis)
        result = evaluate_rule_schedule(
            intraday_context(effective, cycle, basis), revisions, slots, CLIMATE_METRICS,
            publication=publication, versions=publication.versions)
        if result.publication != publication or result.versions != publication.versions:
            raise ValueError('Engine result must carry the pinned publication/versions')
        evaluation = ClimateEvaluation(
            publication=publication, context=effective, basis=basis,
            requested_rule_version=rule_version or None, result=result, slot_reviews=(),
            rule_id=revisions[0].rule_id if revisions else None,
            rule_label=revisions[0].label if revisions else None,
            rule_is_demo=revisions[0].is_demo if revisions else None,
            rule_evidence_reference=revisions[0].evidence_reference if revisions else None)
        if enable_cache:
            _cache_put(key, evaluation)
        return evaluation

    def get_slot_review(self, context: QueryContext, *, basis: RuleBasis,
                        rule_version: str | None = None,
                        window_start=None, window_end=None) -> tuple[RuleSlotReview, ...]:
        """Bounded per-slot trace for the CURRENT view (selected episode or
        page): one engine call over the SAME slot rows windowed to [start,
        end); the review rows reuse the engine's own classification path, so
        they can never drift from the summary evaluations. Returned reviews
        cover BOTH metrics of this window (chart/detail bound to the view)."""
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        effective = replace(context, start=max(context.start, cycle.start),
                            end=min(context.end, cycle.end))
        start = max(datetime.combine(effective.start, time.min),
                    window_start or datetime.combine(effective.start, time.min))
        end = min(datetime.combine(effective.end, time.min),
                  window_end or datetime.combine(effective.end, time.min))
        if start >= end:
            raise ValueError('The bounded review window is empty')
        rows = self.adapter.read_climate_slots(
            effective, publication, window_start=start, window_end=end)
        revisions = (self.adapter.schedule(rule_version),) if rule_version else ()
        slots = build_slots(rows, basis)
        intraday = replace_intraday(
            intraday_context(effective, cycle, basis), start, end)
        _, reviews = evaluate_rule_schedule_detail(
            intraday, revisions, slots, CLIMATE_METRICS,
            publication=publication, versions=publication.versions)
        if not rows:
            return reviews
        setpoints = REQUESTED_SETPOINTS if basis == RuleBasis.REQUESTED else REALIZED_SETPOINTS
        # Source-facts enrichment (T15 export preflight): each review carries
        # the ORIGINAL frozen setpoints of THIS basis and the frozen per-measure
        # quality note, independent of evaluation-input masking.
        by_start = {row.slot_start: row for row in rows}
        enriched = []
        for review in reviews:
            row = by_start.get(review.start)
            if row is None:
                enriched.append(review)
                continue
            if review.metric == 'temperature':
                source_lower, source_upper = (
                    getattr(row, setpoints[review.metric][0]),
                    getattr(row, setpoints[review.metric][1]))
                source_setpoint = None
            else:
                source_lower = source_upper = None
                source_setpoint = getattr(row, setpoints[review.metric][0])
            codes = (ACTUAL_CODES[review.metric], *setpoints[review.metric])
            quality_map = (row.measure_quality or {})
            triples = [
                f'{code} {(quality_map.get(code) or {}).get("status")}'
                f'/{(quality_map.get(code) or {}).get("applicability")}'
                + (f' ({(quality_map.get(code) or {}).get("reason")})'
                   if (quality_map.get(code) or {}).get('reason') else '')
                for code in codes]
            enriched.append(replace(
                review, source_lower=source_lower, source_upper=source_upper,
                source_setpoint=source_setpoint, source_quality=' | '.join(triples)))
        return tuple(enriched)

    def slot_facts(self, context: QueryContext, *, window_start=None, window_end=None):
        """Frozen source identity of the window's slots (lineage drilldown)."""
        publication = self.adapter.publication(context.publication_id)
        context.matches(publication)
        cycle = self.adapter.cycle_window(context)
        start = window_start or datetime.combine(cycle.start, datetime.min.time())
        end = window_end or datetime.combine(cycle.end, datetime.min.time())
        rows = self.adapter.read_climate_slots(
            context, publication, window_start=start, window_end=end)
        if any(not cycle.start <= row.slot_start.date() < cycle.end for row in rows):
            raise ValueError('Slot facts leaked outside the pinned cycle')
        return rows

    def get_lineage(self, publication: PublicationRef, *, revision_id=None, record_key=None):
        if (revision_id is None) == (record_key is None):
            raise ValueError('Select exactly one revision_id or logical record key')
        if self.adapter.publication(publication.publication_id) != publication:
            raise ValueError('Pinned publication metadata differs')
        return self.adapter.read_lineage(publication, revision_id=revision_id, record_key=record_key)
