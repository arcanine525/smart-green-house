"""Recompute replay BR-01/03 with existing rule engine on a pinned snapshot."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from greenhouse.contracts import CycleWindow, IntradayContext, RuleBasis
from greenhouse.queries.climate import build_slots as climate_slots, CLIMATE_METRICS, CLIMATE_CONTEXT
from greenhouse.queries.drain import build_slots as drain_slots, DRAIN_METRICS, DRAIN_CONTEXT, validate_drain_profile
from greenhouse.queries.rule_duration import evaluate_rule_schedule
from greenhouse.sensor.replay_publication import read_publication
from greenhouse.sensor.replay_slots import build_replay_rows


@dataclass(frozen=True)
class ReplayEvaluation:
    publication_id: str
    replay_id: str
    scope_id: str
    quality_policy_version: str
    observed_through: datetime
    window_end: datetime
    provisional: bool
    climate_rule_version: str | None
    drain_rule_version: str | None
    climate: object
    drain: object


def evaluate_publication(connection, publication_id, *, climate_rule=None, drain_rule=None,
                         climate_basis=RuleBasis.REQUESTED):
    """Evaluate cycle from its start only through the cursor, without caching.

    Revisions must be explicitly assigned to the replay scope and compartment;
    no historical rule is silently reassigned. Both engine calls use one pinned
    immutable membership, so late deliveries cannot alter the returned result.
    """
    meta, events = read_publication(connection, publication_id)
    replay, scope, cycle_id, compartment, policy, cursor, observed, received, state = meta
    if observed is None:
        raise ValueError('replay cursor has not been observed')
    if climate_basis not in (RuleBasis.REQUESTED, RuleBasis.REALIZED):
        raise ValueError('climate basis must be requested or realized')
    cycle_row = connection.execute('''SELECT start_source_time,end_source_time_exclusive
        FROM core.growing_cycle WHERE cycle_id=%s AND compartment_id=%s''',
        (cycle_id, compartment)).fetchone()
    if not cycle_row:
        raise ValueError('replay cycle and compartment are not registered')
    cycle = CycleWindow(cycle_id, compartment, cycle_row[0].date(), cycle_row[1].date())
    cycle_start, cycle_end = datetime.combine(cycle.start, time.min), datetime.combine(cycle.end, time.min)
    end = min(cycle_end, observed + timedelta(minutes=5))
    if end <= cycle_start or observed > cycle_end:
        raise ValueError('replay cursor outside cycle window')
    climates, drains = build_replay_rows(connection, events, replay_id=replay, scope_id=scope,
        cycle_id=cycle_id, compartment_id=compartment, quality_policy_version=policy, cycle_end=cycle_end)
    climate_context = IntradayContext(scope, cycle, (compartment,), cycle_start, end, CLIMATE_CONTEXT, climate_basis)
    drain_context = IntradayContext(scope, cycle, (compartment,), cycle_start, end, DRAIN_CONTEXT, RuleBasis.MEASURED)
    validate_drain_profile(drain_rule)
    climate = evaluate_rule_schedule(climate_context, (climate_rule,) if climate_rule else (),
                                     climate_slots(climates, climate_basis), CLIMATE_METRICS)
    drain = evaluate_rule_schedule(drain_context, (drain_rule,) if drain_rule else (),
                                   drain_slots(drains), DRAIN_METRICS)
    return ReplayEvaluation(publication_id, replay, scope, policy, observed, end,
        not state['complete'], climate_rule.rule_version if climate_rule else None,
        drain_rule.rule_version if drain_rule else None, climate, drain)


def map_historical_rule(connection, publication_id, rule_version):
    """Explicitly apply a historical rule to replay of the same source snapshot.

    Returns a replay-scoped in-memory revision with the original version and
    evidence retained. It does not register or modify a database rule.
    """
    from dataclasses import replace
    from greenhouse.db.rule_schedule import read_rule_schedule

    meta, events = read_publication(connection, publication_id)
    replay, scope, cycle_id, compartment, policy, cursor, observed, received, state = meta
    if not events:
        raise ValueError('cannot map a rule without source provenance')
    snapshots = {event['source_provenance']['snapshot_id'] for event in events}
    if len(snapshots) != 1:
        raise ValueError('replay publication contains multiple source snapshots')
    revision = read_rule_schedule(connection, rule_version)
    if revision is None:
        raise ValueError('unknown source rule version')
    source = connection.execute('SELECT snapshot_id FROM meta.data_scope WHERE data_scope_id=%s',
                                (revision.scope_id,)).fetchone()
    if not source or source[0] != next(iter(snapshots)) or revision.compartment_id != compartment:
        raise ValueError('source rule snapshot or compartment does not match replay')
    return replace(revision, scope_id=scope,
                   evidence_reference=revision.evidence_reference + f' [replay mapping from {revision.scope_id}]')
