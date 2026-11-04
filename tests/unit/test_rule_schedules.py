"""BR-01/BR-03 shared immutable rule schedules + pure duration/episode engine.

Covers the dispatch acceptance fixtures verbatim against the final T14
boundary: the DECLARED evaluation group (measurement_context + RuleBasis,
required on the IntradayContext) is shared by every requested metric —
climate runs declare climate/requested (or climate/realized) and rule only
temperature/CO2; drain runs declare drain/measured and rule only ec/pH.
A mixed temperature+EC request is a rejection, never a synthetic OR;
requested-but-unconfigured metrics stay in the result as explicit unknown
with their expected duration and NULL public out KPIs; a legal multi-metric
request yields ONE OR over the jointly known intersection with per-metric
contributions; a one-metric request has no OR row. Episodes build on the
full cycle and keep original plus clipped facts. Registration is atomic and
seals the WHOLE revision against later appends at the schema level; one
evaluation selects at most ONE revision per metric (disjoint also refused);
an explicit empty revision selection stays unknown; publication/versions
provenance must agree with the pinned context when supplied.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import psycopg
import pytest

from greenhouse.contracts import (
    CycleWindow, IntradayContext, PublicationRef, QualityStatus, RuleBasis,
    RuleDeviationDirection, RuleMetricApplied, RuleScheduleRevision, RuleScheduleSegment,
    ThresholdSlot, Versions, from_json, to_json, rule_segment_sort_key,
)
from greenhouse.db.rule_schedule import read_rule_schedule, register_rule_schedule
from greenhouse.queries.rule_duration import evaluate_rule_schedule
from test_query_adapter import contracts_database

FIVE = timedelta(minutes=5)
WINDOW_START = datetime(2020, 1, 1, 0, 0)
WINDOW_END = datetime(2020, 1, 1, 0, 30)
CLIMATE = 'climate'
DRAIN = 'drain'
EVIDENCE = 'synthetic demo thresholds; not agronomic guidance'


@pytest.fixture(scope='module')
def rule_engine_database(contracts_database):
    """Fresh per-module project database with one mapped scope/cycle."""
    with psycopg.connect(**contracts_database, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO meta.dataset_snapshot VALUES (%s,%s,'synthetic://t14','fixture')",
            ('snapshot:t14', '0' * 64))
        connection.execute(
            "INSERT INTO meta.data_scope VALUES (%s,%s,'historical',NULL,'fixture')",
            ('scope:t14', 'snapshot:t14'))
        connection.execute(
            "INSERT INTO core.site VALUES (%s,'T14 site','source_naive','fixture')",
            ('site:t14',))
        connection.execute(
            "INSERT INTO core.compartment VALUES (%s,%s,'T14 compartment','fixture')",
            ('comp:t14', 'site:t14'))
        connection.execute(
            "INSERT INTO core.team VALUES (%s,'T14 team',false,'fixture')", ('team:t14',))
        connection.execute(
            "INSERT INTO core.crop VALUES (%s,'tomato',NULL,'fixture')", ('crop:tomato',))
        connection.execute(
            "INSERT INTO core.growing_cycle VALUES (%s,%s,%s,%s,%s,%s,'fixture')",
            ('cycle:t14', 'comp:t14', 'team:t14', 'crop:tomato',
             datetime(2019, 12, 16), datetime(2020, 5, 30)))
        yield connection


def exact_pct(numerator, denominator):
    """Engine-replicated coverage/percent math (50-digit division)."""
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(numerator) * Decimal(100) / Decimal(denominator)


def exact_hours(minutes):
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(minutes) / Decimal(60)


def climate_cycle():
    return CycleWindow(cycle_id='cycle:t14', compartment_id='comp:t14',
                       start=date(2019, 12, 16), end=date(2020, 5, 30))


def climate_context(basis=RuleBasis.REQUESTED, start=WINDOW_START, end=WINDOW_END,
                    scope='scope:t14'):
    return IntradayContext(
        scope_id=scope, cycle=climate_cycle(), compartment_ids=('comp:t14',),
        start=start, end=end, measurement_context=CLIMATE, basis=basis)


def drain_context(basis=RuleBasis.MEASURED, start=WINDOW_START, end=WINDOW_END,
                  scope='scope:t14'):
    return IntradayContext(
        scope_id=scope, cycle=climate_cycle(), compartment_ids=('comp:t14',),
        start=start, end=end, measurement_context=DRAIN, basis=basis)


def climate_slots(start, entries, *, context=CLIMATE, basis=RuleBasis.REQUESTED):
    """One 5-minute slot per entry; each entry maps metric → payload dict
    (actual/setpoint/lower_setpoint/upper_setpoint/quality)."""
    slots = []
    when = start
    for payloads in entries:
        for metric, payload in payloads.items():
            slots.append(ThresholdSlot(
                start=when, metric=metric, measurement_context=context, basis=basis,
                actual=payload.get('actual'), setpoint=payload.get('setpoint'),
                lower_setpoint=payload.get('lower_setpoint'),
                upper_setpoint=payload.get('upper_setpoint'),
                quality=payload.get('quality', QualityStatus.ACCEPTED)))
        when = when + FIVE
    return tuple(slots)


def drain_slots(start, rows, *, context=DRAIN):
    """rows of (ec, ph, ph_quality); None keeps that metric's row absent
    (missing stream → unknown, never carried forward)."""
    slots = []
    when = start
    for ec, ph, ph_quality in rows:
        if ec is not None:
            slots.append(ThresholdSlot(
                start=when, metric='ec', measurement_context=context,
                basis=RuleBasis.MEASURED, actual=ec))
        if ph is not None:
            slots.append(ThresholdSlot(
                start=when, metric='ph', measurement_context=context,
                basis=RuleBasis.MEASURED, actual=ph, quality=ph_quality))
        when = when + FIVE
    return tuple(slots)


def demo_revision(tag, segments):
    return RuleScheduleRevision(
        rule_version=f'rule-version:{tag}', rule_id=f'rule:{tag}', version=1,
        scope_id='scope:t14', compartment_id='comp:t14', evidence_reference=EVIDENCE,
        is_demo=True, label='demo_rule',
        segments=tuple(sorted(segments, key=rule_segment_sort_key)))


def register(connection, revision):
    outcome = register_rule_schedule(connection, revision)
    assert outcome == {'outcome': 'registered', 'rule_version': revision.rule_version}
    return revision


def climate_rule(connection, tag, *, metric='temperature', delta=Decimal('0'),
                 basis=RuleBasis.REQUESTED, context=CLIMATE,
                 start=datetime(2019, 12, 16), end=datetime(2020, 5, 30)):
    return register(connection, demo_revision(tag, (RuleScheduleSegment(
        metric=metric, measurement_context=context, basis=basis,
        start=start, end=end, delta=delta),)))


def drain_rule(connection, tag, *, ec=(Decimal('2'), Decimal('5')),
               ph=(Decimal('5.5'), Decimal('6.5')),
               start=datetime(2019, 12, 16), end=datetime(2020, 5, 30)):
    segments = [RuleScheduleSegment(
        metric='ec', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
        start=start, end=end, lower=ec[0], upper=ec[1])]
    if ph is not None:
        segments.append(RuleScheduleSegment(
            metric='ph', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
            start=start, end=end, lower=ph[0], upper=ph[1]))
    return register(connection, demo_revision(tag, segments))


def evaluate(revisions, slots, metrics, *, context=None, min_minutes=None,
             publication=None, versions=None):
    return evaluate_rule_schedule(
        context if context is not None else climate_context(),
        tuple(revisions), tuple(slots), tuple(metrics), episode_min_minutes=min_minutes,
        publication=publication, versions=versions)


# ---------------------------------------------------------------- contract

def test_contract_rejects_mixed_modes_and_out_of_range():
    with pytest.raises(ValueError, match='fixed bounds'):
        RuleScheduleSegment(metric='temperature', measurement_context=CLIMATE,
                            basis=RuleBasis.REQUESTED, start=WINDOW_START, end=WINDOW_END,
                            lower=Decimal('20'), upper=Decimal('25'))
    with pytest.raises(ValueError, match='no delta'):
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN,
                            basis=RuleBasis.MEASURED, start=WINDOW_START, end=WINDOW_END,
                            delta=Decimal('1'))
    with pytest.raises(ValueError, match='ordered'):
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN,
                            basis=RuleBasis.MEASURED, start=WINDOW_START, end=WINDOW_END,
                            lower=Decimal('5'), upper=Decimal('2'))
    with pytest.raises(ValueError, match='measured basis'):
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN,
                            basis=RuleBasis.REQUESTED, start=WINDOW_START, end=WINDOW_END,
                            lower=Decimal('2'), upper=Decimal('5'))
    with pytest.raises(ValueError, match='not measured'):
        RuleScheduleSegment(metric='co2', measurement_context=CLIMATE,
                            basis=RuleBasis.MEASURED, start=WINDOW_START, end=WINDOW_END,
                            delta=Decimal('50'))
    with pytest.raises(ValueError, match='>= 0'):
        RuleScheduleSegment(metric='temperature', measurement_context=CLIMATE,
                            basis=RuleBasis.REQUESTED, start=WINDOW_START, end=WINDOW_END,
                            delta=Decimal('-1'))


def test_revision_rejects_overlap_sliding_adjacent_legal_and_unsorted():
    first = RuleScheduleSegment(metric='temperature', measurement_context=CLIMATE,
                                basis=RuleBasis.REQUESTED, start=WINDOW_START,
                                end=WINDOW_START + timedelta(minutes=30), delta=Decimal('0'))
    with pytest.raises(ValueError, match='overlap'):  # sliding overlap, same key
        RuleScheduleRevision(
            rule_version='rule-version:bad-slide', rule_id='rule:bad-slide', version=1,
            scope_id='scope:t14', compartment_id='comp:t14', evidence_reference='synthetic',
            is_demo=True, label='demo_rule', segments=(
                first, replace(first, start=WINDOW_START + timedelta(minutes=5),
                               end=WINDOW_START + timedelta(minutes=15))))
    adjacent = replace(first, start=WINDOW_START + timedelta(minutes=30),
                       end=WINDOW_START + timedelta(minutes=35))
    revision = RuleScheduleRevision(
        rule_version='rule-version:ok-adjacent', rule_id='rule:ok-adjacent', version=1,
        scope_id='scope:t14', compartment_id='comp:t14', evidence_reference='synthetic',
        is_demo=True, label='demo_rule', segments=(first, adjacent))
    assert len(revision.segments) == 2  # exact adjacency is legal
    with pytest.raises(ValueError, match='sorted'):
        replace(revision, segments=(adjacent, first))


def test_revision_interleaved_same_key_overlap_rejected_and_multi_metric_legal():
    """Canonical sort starts with TIME: only a per-key last-end guard catches
    temperature/co2/temperature interleavings; same-time different metrics
    stay legal."""
    temp_a = RuleScheduleSegment(metric='temperature', measurement_context=CLIMATE,
                                 basis=RuleBasis.REQUESTED, start=WINDOW_START,
                                 end=WINDOW_START + timedelta(minutes=20), delta=Decimal('0'))
    co2 = RuleScheduleSegment(metric='co2', measurement_context=CLIMATE,
                              basis=RuleBasis.REQUESTED, start=WINDOW_START + timedelta(minutes=5),
                              end=WINDOW_START + timedelta(minutes=10), delta=Decimal('50'))
    temp_b = replace(temp_a, start=WINDOW_START + timedelta(minutes=10),
                     end=WINDOW_START + timedelta(minutes=30))
    with pytest.raises(ValueError, match='overlap'):
        demo_revision('rule-version:bad-interleave', (temp_a, co2, temp_b))
    same_time = demo_revision('rule-version:ok-multi', (
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=WINDOW_START, end=WINDOW_START + timedelta(minutes=30),
                            lower=Decimal('2'), upper=Decimal('5')),
        RuleScheduleSegment(metric='ph', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=WINDOW_START, end=WINDOW_START + timedelta(minutes=30),
                            lower=Decimal('5.5'), upper=Decimal('6.5'))))
    assert len(same_time.segments) == 2  # different metrics may share one interval


def test_slots_carry_their_own_quality():
    with pytest.raises(ValueError, match='setpoints'):
        ThresholdSlot(start=WINDOW_START, metric='ec', measurement_context=DRAIN,
                      basis=RuleBasis.MEASURED, actual=None, setpoint=Decimal('4'))


def test_applied_unit_mismatch_rejected():
    with pytest.raises(ValueError, match='unit'):
        RuleMetricApplied(metric='ec', measurement_context=DRAIN,
                          basis=RuleBasis.MEASURED, rule_version='rule:unit', unit='pH')


# ------------------------------------------------------------------- BR-01

def test_temperature_dispatch_fixture_and_peak(rule_engine_database):
    temp = climate_rule(rule_engine_database, 't14-temp')
    slots = climate_slots(WINDOW_START, [
        {'temperature': dict(actual=Decimal('19'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25'))},
        {'temperature': dict(actual=Decimal('21'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25'))},
        {'temperature': dict(actual=Decimal('26'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25'))},
        {'temperature': dict(actual=Decimal('26'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25'))},
        {'temperature': dict(actual=None, lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25'))},
        {'temperature': dict(actual=Decimal('24'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25'))}])
    result = evaluate((temp,), slots, ('temperature',))
    report = result.metric_results[0]
    assert result.effective_start == WINDOW_START and result.effective_end == WINDOW_END
    assert report.expected_minutes == Decimal('30')
    assert report.valid_minutes == Decimal('25')  # evaluated population: in-range + out
    assert report.out_minutes == Decimal('15')
    assert report.out_low_minutes == Decimal('5') and report.out_high_minutes == Decimal('10')
    assert report.unknown_minutes == Decimal('5')
    assert report.suspect_minutes == Decimal('0')
    assert [share.reason_code for share in report.unknown_reasons] == ['missing_input']
    assert report.unknown_reasons[0].minutes == Decimal('5')
    assert report.out_hours == exact_hours(Decimal('15')) == Decimal('0.25')
    assert report.out_percent == exact_pct(15, 25) == Decimal('60')
    assert report.coverage_pct == exact_pct(25, 30)  # 83.333…
    assert report.rule_version == temp.rule_version
    assert result.applications == (RuleMetricApplied(
        metric='temperature', measurement_context=CLIMATE, basis=RuleBasis.REQUESTED,
        rule_version=temp.rule_version, unit='°C'),)
    assert [(episode.original_minutes, episode.directions) for episode in result.episodes] == [
        (Decimal('5'), (RuleDeviationDirection.LOW,)),
        (Decimal('10'), (RuleDeviationDirection.HIGH,))]
    first, second = result.episodes
    assert first.original_start == WINDOW_START and first.original_end == WINDOW_START + FIVE
    assert first.peak_deviation == Decimal('1')  # 19 below the 20 lower bound
    assert first.slots[0].start == WINDOW_START
    assert second.original_minutes == Decimal('10') and second.peak_deviation == Decimal('1')
    assert second.peak_time == WINDOW_START + FIVE * 2
    assert result.joint_results == ()  # one-metric request: no OR row


def test_co2_boundaries_off_mode_and_missing_setpoint(rule_engine_database):
    co2 = climate_rule(rule_engine_database, 't14-co2', metric='co2', delta=Decimal('50'))
    slots = climate_slots(WINDOW_START, [
        {'co2': dict(actual=Decimal('400'), setpoint=Decimal('0'))},         # off mode
        {'co2': dict(actual=Decimal('700'), setpoint=Decimal('700'))},       # inside 650..750
        {'co2': dict(actual=Decimal('400'), setpoint=None)},                 # missing input
        {'co2': dict(actual=Decimal('900'), setpoint=Decimal('700'))},       # out high
        {'co2': dict(actual=Decimal('650'), setpoint=Decimal('700'))}])      # equality inside
    result = evaluate((co2,), slots, ('co2',))
    report = result.metric_results[0]
    assert report.valid_minutes == Decimal('15')  # slots 1,3,4 are evaluated
    assert report.out_minutes == Decimal('5') and report.out_low_minutes == Decimal('0')
    assert report.out_high_minutes == Decimal('5')
    assert report.unknown_minutes == Decimal('15')  # centered setpoint + unprovided tail slots
    assert [(share.reason_code, share.minutes) for share in report.unknown_reasons] == [
        ('missing_input', Decimal('10')), ('sp0_off_mode', Decimal('5'))]
    assert report.suspect_minutes == Decimal('0')
    assert report.out_hours == exact_hours(Decimal('5'))
    assert report.out_percent == exact_pct(5, 15)
    assert report.coverage_pct == exact_pct(15, 30)  # 50
    episode, = result.episodes
    assert episode.original_minutes == Decimal('5')
    assert episode.directions == (RuleDeviationDirection.HIGH,)
    assert episode.peak_deviation == Decimal('150')  # 900 above the 750 upper bound
    assert episode.peak_time == WINDOW_START + FIVE * 3


def test_temperature_or_co2_union_within_jointly_known_intersection(rule_engine_database):
    """Two jointly known slots: temperature out on both, CO2 out on the second
    only → OR category sums 10, never the per-metric 15 sum."""
    temp = climate_rule(rule_engine_database, 't14-or-temp')
    co2 = climate_rule(rule_engine_database, 't14-or-co2', metric='co2', delta=Decimal('50'))
    slots = climate_slots(WINDOW_START, [
        {'temperature': dict(actual=Decimal('26'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25')),
         'co2': dict(actual=Decimal('500'), setpoint=Decimal('500'))},
        {'temperature': dict(actual=Decimal('30'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25')),
         'co2': dict(actual=Decimal('900'), setpoint=Decimal('500'))}])
    result = evaluate((temp, co2), slots, ('temperature', 'co2'))
    joint = result.joint_results[0]
    assert joint.metrics == ('temperature', 'co2')  # exact requested order
    assert joint.jointly_valid_minutes == Decimal('10')
    assert joint.joint_out_minutes == Decimal('10')  # temp out twice, CO2 never added twice
    assert joint.joint_out_percent == Decimal('100')
    assert joint.joint_coverage_pct == exact_pct(10, 30)
    assert joint.contributions == (
        RuleMetricApplied(metric='temperature', measurement_context=CLIMATE,
                          basis=RuleBasis.REQUESTED, rule_version=temp.rule_version, unit='°C'),
        RuleMetricApplied(metric='co2', measurement_context=CLIMATE,
                          basis=RuleBasis.REQUESTED, rule_version=co2.rule_version,
                          unit='ppm'))
    temp_report, co2_report = result.metric_results
    assert temp_report.out_minutes == Decimal('10') and co2_report.out_minutes == Decimal('5')
    assert [episode.metric for episode in result.episodes] == ['temperature', 'co2']


def test_unknown_reasons_partition_once(rule_engine_database):
    """Slot with an INVALID verdict is attributed once (invalid outranks its
    own missing values); zero evaluated population → NULL public KPIs."""
    temp = climate_rule(rule_engine_database, 't14-temp-partition')
    slots = (ThresholdSlot(start=WINDOW_START, metric='temperature',
                           measurement_context=CLIMATE, basis=RuleBasis.REQUESTED,
                           actual=None, lower_setpoint=None, upper_setpoint=None,
                           quality=QualityStatus.INVALID),)
    result = evaluate((temp,), slots, ('temperature',),
                      context=climate_context(end=WINDOW_START + FIVE))
    assert [(share.reason_code, share.minutes) for share in
            result.metric_results[0].unknown_reasons] == [('quality_invalid', Decimal('5'))]
    assert result.metric_results[0].unknown_minutes == Decimal('5')
    assert result.metric_results[0].valid_minutes == Decimal('0')
    assert result.metric_results[0].out_hours is None
    assert result.metric_results[0].out_percent is None
    assert result.metric_results[0].coverage_pct == Decimal('0')


def test_suspect_keeps_its_own_bucket(rule_engine_database):
    temp = climate_rule(rule_engine_database, 't14-temp-suspect')
    slots = (ThresholdSlot(start=WINDOW_START, metric='temperature',
                           measurement_context=CLIMATE, basis=RuleBasis.REQUESTED,
                           actual=Decimal('26'), lower_setpoint=Decimal('20'),
                           upper_setpoint=Decimal('25'), quality=QualityStatus.SUSPECT),)
    result = evaluate((temp,), slots, ('temperature',),
                      context=climate_context(end=WINDOW_START + FIVE))
    assert [(share.reason_code, share.minutes) for share in
            result.metric_results[0].unknown_reasons] == [('quality_suspect', Decimal('5'))]
    assert result.metric_results[0].suspect_minutes == Decimal('5')
    assert result.metric_results[0].out_minutes == Decimal('0')
    assert result.metric_results[0].unknown_minutes == Decimal('5')
    assert result.episodes == ()  # suspect never counts as out


def test_temperature_continuity_requested_vs_realized_separate(rule_engine_database):
    """Requested needs its own setpoints: a missing requested pair stays
    unknown even while realized values exist (no backstop); realized runs
    declare the realized basis and replay deterministically."""
    requested = climate_rule(rule_engine_database, 't14-temp-req',
                             basis=RuleBasis.REQUESTED)
    realized = climate_rule(rule_engine_database, 't14-temp-real',
                            basis=RuleBasis.REALIZED)
    request_only = evaluate(
        (requested,),
        (ThresholdSlot(start=WINDOW_START, metric='temperature', measurement_context=CLIMATE,
                       basis=RuleBasis.REQUESTED, actual=Decimal('24'),
                       lower_setpoint=None, upper_setpoint=None),),
        ('temperature',), context=climate_context(end=WINDOW_START + FIVE))
    assert request_only.metric_results[0].unknown_minutes == Decimal('5')
    assert request_only.metric_results[0].unknown_reasons[0].reason_code == 'missing_input'
    assert request_only.episodes == ()
    realized_slots = (ThresholdSlot(start=WINDOW_START, metric='temperature',
                                    measurement_context=CLIMATE, basis=RuleBasis.REALIZED,
                                    actual=Decimal('24'), lower_setpoint=Decimal('20'),
                                    upper_setpoint=Decimal('25')),)
    realized_result = evaluate((realized,), realized_slots, ('temperature',),
                               context=climate_context(basis=RuleBasis.REALIZED))
    assert realized_result.metric_results[0].valid_minutes == Decimal('5')
    assert realized_result.metric_results[0].out_minutes == Decimal('0')
    replay = evaluate((realized,), realized_slots, ('temperature',),
                      context=climate_context(basis=RuleBasis.REALIZED))
    assert replay == realized_result  # same revision reproduces exact outcomes


def test_inverted_temperature_bounds_are_unknown(rule_engine_database):
    temp = climate_rule(rule_engine_database, 't14-temp-inverted')
    slots = (ThresholdSlot(start=WINDOW_START, metric='temperature',
                           measurement_context=CLIMATE, basis=RuleBasis.REQUESTED,
                           actual=Decimal('20'), lower_setpoint=Decimal('30'),
                           upper_setpoint=Decimal('10')),)
    result = evaluate((temp,), slots, ('temperature',),
                      context=climate_context(end=WINDOW_START + FIVE))
    assert [(share.reason_code, share.minutes) for share in
            result.metric_results[0].unknown_reasons] == [('inverted_bounds', Decimal('5'))]
    assert result.metric_results[0].valid_minutes == Decimal('0')
    assert result.metric_results[0].unknown_minutes == Decimal('5')
    assert result.episodes == ()


# ------------------------------------------------------------------- BR-03

def test_drain_dispatch_fixture(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain')
    slots = drain_slots(WINDOW_START, [
        (Decimal('3'), Decimal('6'), QualityStatus.ACCEPTED),
        (Decimal('6'), Decimal('6'), QualityStatus.ACCEPTED),
        (Decimal('6'), Decimal('7'), QualityStatus.ACCEPTED),
        (None, Decimal('6'), QualityStatus.ACCEPTED),
        (Decimal('3'), Decimal('0'), QualityStatus.SUSPECT),
        (Decimal('6'), Decimal('6'), QualityStatus.ACCEPTED)])
    result = evaluate((drain,), slots, ('ec', 'ph'), context=drain_context())
    ec, ph = result.metric_results
    assert ec.expected_minutes == Decimal('30')
    assert ec.valid_minutes == Decimal('25') and ec.out_minutes == Decimal('15')
    assert ec.out_low_minutes == Decimal('0') and ec.out_high_minutes == Decimal('15')
    assert ec.unknown_minutes == Decimal('5')
    assert [(share.reason_code, share.minutes) for share in ec.unknown_reasons] == [
        ('missing_input', Decimal('5'))]
    assert ec.out_hours == exact_hours(Decimal('15')) == Decimal('0.25')
    assert ec.out_percent == exact_pct(15, 25) == Decimal('60')
    assert ec.coverage_pct == exact_pct(25, 30)
    assert ph.valid_minutes == Decimal('25') and ph.out_minutes == Decimal('5')
    assert ph.out_high_minutes == Decimal('5')
    assert ph.suspect_minutes == Decimal('5')
    assert ph.unknown_minutes == Decimal('5')
    assert [(share.reason_code, share.minutes) for share in ph.unknown_reasons] == [
        ('quality_suspect', Decimal('5'))]
    assert ph.out_hours == exact_hours(Decimal('5'))
    assert ph.out_percent == exact_pct(5, 25) == Decimal('20')
    assert ph.coverage_pct == exact_pct(25, 30)
    joint = result.joint_results[0]
    assert joint.metrics == ('ec', 'ph')
    assert joint.jointly_valid_minutes == Decimal('20')  # ec known ∩ ph known
    assert joint.joint_out_minutes == Decimal('15')
    assert joint.joint_out_percent == exact_pct(15, 20) == Decimal('75')
    assert joint.joint_coverage_pct == exact_pct(20, 30)  # 66.666…
    assert joint.contributions[0].rule_version == drain.rule_version
    assert [(episode.metric, episode.original_minutes) for episode in result.episodes] == [
        ('ec', Decimal('10')), ('ec', Decimal('5')), ('ph', Decimal('5'))]
    ec_first, ec_second, ph_only = result.episodes
    assert ec_first.original_start == WINDOW_START + FIVE
    assert ec_first.original_end == WINDOW_START + FIVE * 3
    assert ec_first.peak_deviation == Decimal('1') and ec_first.peak_time == WINDOW_START + FIVE
    assert ec_second.original_start == WINDOW_START + FIVE * 5
    assert ph_only.original_minutes == Decimal('5')
    assert ph_only.peak_deviation == Decimal('0.5')  # ph 7 above the 6.5 upper bound


def test_missing_ph_request_keeps_full_or_with_valid_zero(rule_engine_database):
    """Requested ec+ph under drain/measured with ONLY ec data available: the
    legal two-metric request stays one OR with jointly valid 0 — ph remains
    explicit rule_not_configured, never silently degraded."""
    drain = drain_rule(rule_engine_database, 't14-drain-absent', ph=None)
    slots = drain_slots(WINDOW_START, [(Decimal('3'), None, None)] * 6)
    result = evaluate((drain,), slots, ('ec', 'ph'), context=drain_context())
    ec, ph = result.metric_results
    assert [report.metric for report in result.metric_results] == ['ec', 'ph']
    assert ec.valid_minutes == Decimal('30') and ec.out_minutes == Decimal('0')
    assert ec.coverage_pct == Decimal('100')
    assert ph.rule_version is None
    assert ph.expected_minutes == Decimal('30')  # requested duration preserved
    assert ph.valid_minutes == Decimal('0') and ph.out_minutes == Decimal('0')
    assert ph.unknown_minutes == Decimal('30')
    assert [(share.reason_code, share.minutes) for share in ph.unknown_reasons] == [
        ('rule_not_configured', Decimal('30'))]
    assert ph.out_hours is None and ph.out_percent is None
    assert ph.coverage_pct == Decimal('0')
    assert result.applications[1] == RuleMetricApplied(
        metric='ph', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
        rule_version=None, unit='pH')
    joint, = result.joint_results
    assert joint.metrics == ('ec', 'ph')
    assert joint.jointly_valid_minutes == Decimal('0')
    assert joint.joint_out_minutes == Decimal('0')
    assert joint.joint_out_percent is None
    assert joint.joint_coverage_pct == Decimal('0')
    assert result.episodes == ()


def test_empty_revision_selection_reports_explicit_unknown(rule_engine_database):
    """No schedule revisions at all is a legal explicit selection: requested
    metrics keep expected duration, rule_version None, rule_not_configured
    and NULL public out KPIs; a legal pair keeps its OR at jointly valid 0.
    No dummy profile and no default/demo revision is fabricated."""
    slots = drain_slots(WINDOW_START, [(Decimal('3'), Decimal('6'), QualityStatus.ACCEPTED)] * 2)
    result = evaluate((), slots, ('ec', 'ph'),
                      context=drain_context(end=WINDOW_START + FIVE * 2))
    ec, ph = result.metric_results
    for report in (ec, ph):
        assert report.rule_version is None
        assert report.expected_minutes == Decimal('10')
        assert report.valid_minutes == Decimal('0')
        assert report.unknown_minutes == Decimal('10')
        assert [(share.reason_code, share.minutes)
                for share in report.unknown_reasons] == [('rule_not_configured', Decimal('10'))]
        assert report.out_hours is None and report.out_percent is None
        assert report.coverage_pct == Decimal('0')
    joint, = result.joint_results
    assert joint.metrics == ('ec', 'ph')
    assert joint.jointly_valid_minutes == Decimal('0') and joint.joint_out_minutes == Decimal('0')
    assert joint.joint_out_percent is None and joint.joint_coverage_pct == Decimal('0')
    assert result.episodes == ()


def test_no_rules_with_empty_slots_report_unknown_in_both_groups():
    climate = evaluate((), (), ('temperature',), context=climate_context())
    report, = climate.metric_results
    assert report.rule_version is None
    assert report.unknown_minutes == Decimal('30')  # expected duration preserved
    assert [(share.reason_code, share.minutes)
            for share in report.unknown_reasons] == [('rule_not_configured', Decimal('30'))]
    assert report.out_hours is None and report.out_percent is None
    assert climate.joint_results == () and climate.episodes == ()  # one metric: no OR
    drain = evaluate((), (), ('ec', 'ph'), context=drain_context())
    joint, = drain.joint_results
    assert joint.jointly_valid_minutes == Decimal('0') and joint.joint_out_minutes == Decimal('0')
    assert joint.joint_out_percent is None and joint.joint_coverage_pct == Decimal('0')
    assert drain.episodes == ()


def test_temperature_in_drain_measured_rejected_even_when_absent(rule_engine_database):
    """The declared group boundary: measured rules only ec/ph. Temperature is
    rejected under drain/measured even with no slots and no rule for it — a
    mixed temperature+EC request is a rejection, never a synthetic OR."""
    drain = drain_rule(rule_engine_database, 't14-drain-boundary')
    with pytest.raises(ValueError, match='does not belong'):
        evaluate((drain,), (), ('temperature',), context=drain_context())
    with pytest.raises(ValueError, match='does not belong'):
        evaluate((drain,), drain_slots(WINDOW_START, [(Decimal('3'), Decimal('6'),
                                                       QualityStatus.ACCEPTED)]),
                 ('temperature', 'ec'), context=drain_context())


def test_missing_rule_interval_is_explicit_unknown(rule_engine_database):
    partial = register(rule_engine_database, demo_revision('t14-drain-partial', (
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN,
                            basis=RuleBasis.MEASURED, start=datetime(2019, 12, 16),
                            end=WINDOW_START + FIVE, lower=Decimal('2'), upper=Decimal('5')),)))
    slots = drain_slots(WINDOW_START, [(Decimal('9'), None, None)] * 2)
    result = evaluate((partial,), slots, ('ec',),
                      context=drain_context(end=WINDOW_START + FIVE * 2))
    report = result.metric_results[0]
    assert report.rule_version == partial.rule_version
    assert [(share.reason_code, share.minutes) for share in report.unknown_reasons] == [
        ('rule_not_configured', Decimal('5'))]  # slot 2 sits outside the segment interval
    assert report.valid_minutes == Decimal('5') and report.out_minutes == Decimal('5')
    assert report.out_high_minutes == Decimal('5')
    assert report.out_hours == exact_hours(Decimal('5'))
    assert report.out_percent == Decimal('100')
    assert report.coverage_pct == exact_pct(5, 10)
    episode, = result.episodes
    assert episode.original_minutes == Decimal('5')
    assert episode.original_start == WINDOW_START


def test_multiple_revisions_same_metric_rejected_even_disjoint(rule_engine_database):
    """At most ONE selected revision per requested (metric, context, basis):
    refusal happens BEFORE evaluation even when the two segments do not
    overlap — temporal changes belong as segments inside one immutable
    revision. Registering both revisions at rest stays legal; the ENGINE
    never chooses first/latest, and negating the order changes nothing."""
    early = register(rule_engine_database, demo_revision('t14-sel-early', (
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=datetime(2019, 12, 16), end=WINDOW_START + FIVE,
                            lower=Decimal('2'), upper=Decimal('5')),)))
    later = register(rule_engine_database, demo_revision('t14-sel-later', (
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=WINDOW_START + FIVE, end=datetime(2020, 5, 30),
                            lower=Decimal('2'), upper=Decimal('5')),)))
    slots = drain_slots(WINDOW_START, [(Decimal('9'), None, None)] * 2)
    for selection in ((early, later), (later, early)):
        with pytest.raises(ValueError, match='at most one revision'):
            evaluate(selection, slots, ('ec',), context=drain_context())
    result = evaluate((later,), slots, ('ec',), context=drain_context())
    assert result.metric_results[0].rule_version == later.rule_version  # explicit replay stays exact


def test_publication_metadata_must_agree_with_context(rule_engine_database):
    """Optional provenance is coherent or refused: publication scope and
    source-naive basis must match the pinned context; explicit versions must
    equal the publication's (or derive from it). Pure fixtures may omit
    provenance entirely — nothing is invented or looked up."""
    temp = climate_rule(rule_engine_database, 't14-pub')
    versions = Versions(pipeline_run_id='publication:run', quality_policy_version='publication:quality',
                        parser_version='publication:parser', code_version='publication:code',
                        config_bundle_hash='publication:bundle')
    publication = PublicationRef(publication_id='publication:t14', scope_id='scope:t14',
                                 snapshot_id='snapshot:t14', versions=versions,
                                 committed_at=datetime(2020, 3, 1, tzinfo=timezone.utc),
                                 finality_status='complete')
    slots = climate_slots(WINDOW_START, [
        {'temperature': dict(actual=Decimal('21'), lower_setpoint=Decimal('20'),
                             upper_setpoint=Decimal('25'))}])
    derived = evaluate((temp,), slots, ('temperature',), publication=publication)
    assert derived.publication is publication and derived.versions == versions
    explicit = evaluate((temp,), slots, ('temperature',), publication=publication,
                        versions=versions)
    assert explicit == derived  # matching explicit versions equals the derivation
    foreign = PublicationRef(publication_id='publication:t14', scope_id='scope:other',
                             snapshot_id='snapshot:t14', versions=versions,
                             committed_at=datetime(2020, 3, 1, tzinfo=timezone.utc),
                             finality_status='complete')
    with pytest.raises(ValueError, match='publication scope'):
        evaluate((temp,), slots, ('temperature',), publication=foreign)
    with pytest.raises(ValueError, match='contradict'):
        evaluate((temp,), slots, ('temperature',), publication=publication, versions=replace(
            versions, config_bundle_hash='publication:other-bundle'))


def test_end_of_cycle_boundary_sample_leaves_durations_untouched(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-end')
    slots = (ThresholdSlot(start=datetime(2020, 5, 30), metric='ec',
                           measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                           actual=Decimal('7')),)
    result = evaluate((drain,), slots, ('ec',),
                      context=drain_context(start=datetime(2020, 5, 29),
                                            end=datetime(2020, 5, 30)))
    report = result.metric_results[0]
    assert report.expected_minutes == Decimal('1440')
    assert report.unknown_minutes == Decimal('1440')  # the exact endpoint adds nothing
    assert report.valid_minutes == Decimal('0')
    assert report.out_hours is None and report.out_percent is None
    assert result.episodes == ()
    with pytest.raises(ValueError, match='beyond the cycle'):
        evaluate((drain,), (ThresholdSlot(start=datetime(2020, 5, 30, 0, 5), metric='ec',
                                          measurement_context=DRAIN,
                                          basis=RuleBasis.MEASURED, actual=Decimal('7')),),
                 ('ec',), context=drain_context(start=datetime(2020, 5, 29),
                                                end=datetime(2020, 5, 30)))


def test_query_window_clips_episode_and_summary(rule_engine_database):
    """Episode 00:00..00:15 with query 00:02..00:12: original minutes stay 15,
    the overlap reports 10, and the summary out duration follows the query."""
    drain = drain_rule(rule_engine_database, 't14-drain-clip')
    slots = drain_slots(WINDOW_START, [(Decimal('9'), None, None)] * 3)
    result = evaluate((drain,), slots, ('ec',),
                      context=drain_context(start=WINDOW_START + timedelta(minutes=2),
                                            end=WINDOW_START + timedelta(minutes=12)))
    report = result.metric_results[0]
    assert report.expected_minutes == Decimal('10')
    assert report.out_minutes == Decimal('10')  # 3 + 5 + 2 clipped slot minutes
    assert report.out_percent == Decimal('100') and report.out_hours == exact_hours(10)
    episode, = result.episodes
    assert episode.original_start == WINDOW_START
    assert episode.original_end == WINDOW_START + timedelta(minutes=15)
    assert episode.original_minutes == Decimal('15')
    assert episode.clipped_start == WINDOW_START + timedelta(minutes=2)
    assert episode.clipped_end == WINDOW_START + timedelta(minutes=12)
    assert episode.clipped_minutes == Decimal('10')


def test_min_episode_filter_changes_only_list(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-min')
    slots = drain_slots(WINDOW_START, [(Decimal('9'), None, None)] * 3)
    unfiltered = evaluate((drain,), slots, ('ec',),
                          context=drain_context(start=WINDOW_START + timedelta(minutes=2),
                                                end=WINDOW_START + timedelta(minutes=12)))
    narrowed = evaluate((drain,), slots, ('ec',),
                        context=drain_context(start=WINDOW_START + timedelta(minutes=2),
                                              end=WINDOW_START + timedelta(minutes=12)),
                        min_minutes=Decimal('20'))
    assert len(unfiltered.episodes) == 1
    assert narrowed.episodes == ()  # 15 < 20 filtered from the LIST only
    assert unfiltered.metric_results[0].out_minutes == Decimal('10')
    assert narrowed.metric_results[0].out_minutes == Decimal('10')  # summary untouched
    assert unfiltered.metric_results[0].out_percent == \
        narrowed.metric_results[0].out_percent


def test_window_outside_overlap_no_episodes_and_null_kpis(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-outside')
    slots = drain_slots(WINDOW_START, [(Decimal('9'), None, None)] * 3)
    result = evaluate((drain,), slots, ('ec',),
                      context=drain_context(start=WINDOW_START + timedelta(hours=2),
                                            end=WINDOW_START + timedelta(hours=3)))
    assert result.episodes == ()           # zero overlap: never surfaced
    report = result.metric_results[0]
    assert report.out_minutes == Decimal('0')
    assert report.valid_minutes == Decimal('0')
    assert report.out_hours is None and report.out_percent is None  # NULL, not green 0
    assert report.expected_minutes == Decimal('60')
    assert report.unknown_minutes == Decimal('60')  # inputs absent: explicit unknown
    assert [(share.reason_code, share.minutes) for share in report.unknown_reasons] == [
        ('missing_input', Decimal('60'))]


# --------------------------------------------------- input validation

def test_slot_grid_rejections(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-grid')
    off_grid = ThresholdSlot(start=WINDOW_START + timedelta(minutes=2), metric='ec',
                             measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                             actual=Decimal('3'))
    with pytest.raises(ValueError, match='grid'):
        evaluate((drain,), (off_grid,), ('ec',), context=drain_context())
    duplicate = ThresholdSlot(start=WINDOW_START, metric='ec', measurement_context=DRAIN,
                              basis=RuleBasis.MEASURED, actual=Decimal('3'))
    with pytest.raises(ValueError, match='duplicate slot'):
        evaluate((drain,), (duplicate, duplicate), ('ec',), context=drain_context())
    unrequested = ThresholdSlot(start=WINDOW_START, metric='ph', measurement_context=DRAIN,
                                basis=RuleBasis.MEASURED, actual=Decimal('6'))
    with pytest.raises(ValueError, match='not part of the requested metrics'):
        evaluate((drain,), (unrequested,), ('ec',), context=drain_context())


def test_declared_group_slot_mismatches_rejected(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-mismatch')
    with pytest.raises(ValueError, match='declares context'):
        evaluate((drain,), climate_slots(WINDOW_START, [
            {'temperature': dict(actual=Decimal('21'), lower_setpoint=Decimal('20'),
                                 upper_setpoint=Decimal('25'))}], context=CLIMATE),
            ('ec',), context=drain_context())
    with pytest.raises(ValueError, match='declares basis'):
        evaluate((drain,), (ThresholdSlot(start=WINDOW_START, metric='ec',
                                          measurement_context=DRAIN,
                                          basis=RuleBasis.REQUESTED,
                                          actual=Decimal('3')),),
                 ('ec',), context=drain_context())


def test_foreign_scope_rejected(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-scope')
    slots = drain_slots(WINDOW_START, [(Decimal('3'), None, None)])
    with pytest.raises(ValueError, match='outside the requested scope'):
        evaluate((drain,), slots, ('ec',), context=drain_context(scope='scope:other'))


# --------------------------------------------------- registration semantics

def test_same_content_registration_reuses_and_drift_refused(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-reuse')
    assert register_rule_schedule(rule_engine_database, drain) == {
        'outcome': 'reuse', 'rule_version': drain.rule_version}
    assert read_rule_schedule(rule_engine_database, drain.rule_version) == drain
    with pytest.raises(ValueError, match='drift'):
        register_rule_schedule(rule_engine_database, replace(
            drain, evidence_reference='changed provenance note'))
    with pytest.raises(ValueError, match='drift'):
        register_rule_schedule(rule_engine_database, replace(
            drain, segments=drain.segments + (
                RuleScheduleSegment(metric='ec', measurement_context=DRAIN,
                                    basis=RuleBasis.MEASURED, start=datetime(2020, 6, 1),
                                    end=datetime(2020, 6, 2),
                                    lower=Decimal('2'), upper=Decimal('5')),)))
    assert read_rule_schedule(rule_engine_database, 'rule-version:none') is None


def test_db_rejects_overlap_when_repository_bypassed(rule_engine_database):
    """The overnight guarantee lives in the 008 exclusion constraint, not only
    in the repository guard. A bypass creates the header and probe segments
    in ONE manual transaction (that is precisely when overlap is checked —
    a committed revision is sealed against any further append); exact
    adjacency inside that same registration transaction stays legal."""
    with pytest.raises(psycopg.errors.ExclusionViolation):
        with rule_engine_database.transaction():
            rule_engine_database.execute(
                "INSERT INTO config.rule_schedule_revision VALUES "
                "('rule-version:t14-dbx', 'rule:dbx', 1, 'scope:t14', 'comp:t14', "
                "'bypass overlap probe', false, 'bypass')")
            rule_engine_database.execute(
                "INSERT INTO config.rule_schedule_segment (rule_version, metric, "
                "measurement_context, basis, source_start, source_end, delta, lower, upper) "
                "VALUES ('rule-version:t14-dbx', 'ec', 'drain', 'measured', "
                "'2019-12-16 00:00:00', '2019-12-16 00:10:00', NULL, 2, 5)")  # first segment
            rule_engine_database.execute(
                "INSERT INTO config.rule_schedule_segment (rule_version, metric, "
                "measurement_context, basis, source_start, source_end, delta, lower, upper) "
                "VALUES ('rule-version:t14-dbx', 'ec', 'drain', 'measured', "
                "'2019-12-16 00:05:00', '2019-12-16 00:15:00', NULL, 2, 5)")  # overlaps
    assert rule_engine_database.execute(
        "SELECT count(*) FROM config.rule_schedule_revision WHERE rule_version='rule-version:t14-dbx'"
    ).fetchone()[0] == 0  # the failed append left NO partial revision behind
    with rule_engine_database.transaction():
        rule_engine_database.execute(
            "INSERT INTO config.rule_schedule_revision VALUES "
            "('rule-version:t14-dbx', 'rule:dbx', 1, 'scope:t14', 'comp:t14', "
            "'bypass adjacency probe', false, 'bypass')")
        rule_engine_database.execute(
            "INSERT INTO config.rule_schedule_segment (rule_version, metric, "
            "measurement_context, basis, source_start, source_end, delta, lower, upper) "
            "VALUES ('rule-version:t14-dbx', 'ec', 'drain', 'measured', "
            "'2019-12-16 00:00:00', '2019-12-16 00:10:00', NULL, 2, 5)")
        rule_engine_database.execute(
            "INSERT INTO config.rule_schedule_segment (rule_version, metric, "
            "measurement_context, basis, source_start, source_end, delta, lower, upper) "
            "VALUES ('rule-version:t14-dbx', 'ec', 'drain', 'measured', "
            "'2019-12-16 00:10:00', '2020-04-25 00:00:00', NULL, 2, 5)")  # exact adjacency
    assert rule_engine_database.execute(
        "SELECT count(*) FROM config.rule_schedule_segment WHERE rule_version='rule-version:t14-dbx'"
    ).fetchone()[0] == 2  # header + both adjacent segments committed together


def test_registered_revision_is_sealed_against_appends(rule_engine_database):
    """Immutable revision means the WHOLE schedule is frozen: after commit, a
    non-overlapping (or any) segment insert into the same rule_version is
    refused at the schema level; the stored content under one version cannot
    grow afterwards."""
    drain = drain_rule(rule_engine_database, 't14-seal')
    with pytest.raises(psycopg.errors.RaiseException, match='sealed'):
        rule_engine_database.execute(
            "INSERT INTO config.rule_schedule_segment (rule_version, metric, "
            "measurement_context, basis, source_start, source_end, delta, lower, upper) "
            "VALUES ('rule-version:t14-seal', 'ec', 'drain', 'measured', "
            "'2020-06-01 00:00:00', '2020-06-02 00:00:00', NULL, 2, 5)")  # disjoint: seal, not EXCLUDE
    assert rule_engine_database.execute(
        "SELECT count(*) FROM config.rule_schedule_segment WHERE rule_version='rule-version:t14-seal'"
    ).fetchone()[0] == 2  # only the registered ec+ph segments remain
    assert read_rule_schedule(rule_engine_database, drain.rule_version) == drain


def test_failed_segment_append_leaves_no_partial_revision(rule_engine_database):
    """Registration is atomic: an injected failure on the first segment rolls
    the header back so no incomplete revision survives, and the retry
    registers the full candidate."""
    candidate = demo_revision('t14-atomic', (
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=datetime(2019, 12, 16), end=WINDOW_START + FIVE,
                            lower=Decimal('2'), upper=Decimal('5')),
        RuleScheduleSegment(metric='ph', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=datetime(2019, 12, 16), end=datetime(2020, 5, 30),
                            lower=Decimal('5.5'), upper=Decimal('6.5'))))
    rule_engine_database.execute(
        "CREATE FUNCTION t14_inject_segment_failure() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN IF NEW.rule_version = 'rule-version:t14-atomic' THEN "
        "RAISE EXCEPTION 'injected append failure for the atomicity probe'; END IF; "
        "RETURN NEW; END $$")
    rule_engine_database.execute(
        "CREATE TRIGGER t14_inject_fail BEFORE INSERT ON config.rule_schedule_segment "
        "FOR EACH ROW EXECUTE FUNCTION t14_inject_segment_failure()")
    with pytest.raises(psycopg.errors.RaiseException, match='injected append failure'):
        register_rule_schedule(rule_engine_database, candidate)
    assert rule_engine_database.execute(
        "SELECT count(*) FROM config.rule_schedule_revision WHERE rule_version='rule-version:t14-atomic'"
    ).fetchone()[0] == 0  # zero leftover headers
    assert rule_engine_database.execute(
        "SELECT count(*) FROM config.rule_schedule_segment WHERE rule_version='rule-version:t14-atomic'"
    ).fetchone()[0] == 0  # zero leftover segments
    rule_engine_database.execute("DROP TRIGGER t14_inject_fail ON config.rule_schedule_segment")
    rule_engine_database.execute("DROP FUNCTION t14_inject_segment_failure()")
    outcome = register_rule_schedule(rule_engine_database, candidate)
    assert outcome == {'outcome': 'registered', 'rule_version': candidate.rule_version}
    assert read_rule_schedule(rule_engine_database, candidate.rule_version) == candidate


def test_register_on_normal_connection_and_seal_after_commit(rule_engine_database, contracts_database):
    """Registration also runs as one transaction on a NORMAL connection
    (autocommit=False; psycopg nests the repository block as a savepoint
    inside the implicitly started transaction), commits with it, and the
    committed revision is sealed against later appends."""
    candidate = demo_revision('t14-normal-conn', (
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=datetime(2019, 12, 16), end=WINDOW_START + FIVE,
                            lower=Decimal('2'), upper=Decimal('5')),
        RuleScheduleSegment(metric='ph', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=datetime(2019, 12, 16), end=datetime(2020, 5, 30),
                            lower=Decimal('5.5'), upper=Decimal('6.5'))))
    with psycopg.connect(**contracts_database) as connection:  # autocommit=False
        assert register_rule_schedule(connection, candidate) == {
            'outcome': 'registered', 'rule_version': candidate.rule_version}
    with psycopg.connect(**contracts_database, autocommit=True) as reviewer:
        assert read_rule_schedule(reviewer, candidate.rule_version) == candidate
        with pytest.raises(psycopg.errors.RaiseException, match='sealed'):
            reviewer.execute(
                "INSERT INTO config.rule_schedule_segment (rule_version, metric, "
                "measurement_context, basis, source_start, source_end, delta, lower, upper) "
                "VALUES ('rule-version:t14-normal-conn', 'ec', 'drain', 'measured', "
                "'2020-06-01 00:00:00', '2020-06-02 00:00:00', NULL, 2, 5)")
        assert read_rule_schedule(reviewer, candidate.rule_version) == candidate  # membership frozen


def test_registration_nested_in_outer_savepoint_survives_failure_and_retries(
        rule_engine_database, contracts_database):
    """The real caller path: normal connection with an ALREADY-OPEN outer
    transaction. An injected append failure rolls back ONLY the repository
    savepoint — zero leftover revisions/segments, outer transaction stays
    usable — and the retry inside the same outer transaction succeeds and
    seals on commit. Top savepoint nesting stamps/compares the SAME
    top-level xid, so the seal never misfires inside registration."""
    candidate = demo_revision('t14-nested-atomic', (
        RuleScheduleSegment(metric='ec', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=datetime(2019, 12, 16), end=WINDOW_START + FIVE,
                            lower=Decimal('2'), upper=Decimal('5')),
        RuleScheduleSegment(metric='ph', measurement_context=DRAIN, basis=RuleBasis.MEASURED,
                            start=datetime(2019, 12, 16), end=datetime(2020, 5, 30),
                            lower=Decimal('5.5'), upper=Decimal('6.5'))))
    rule_engine_database.execute(
        "CREATE FUNCTION t14_inject_nested_failure() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN IF NEW.rule_version = 'rule-version:t14-nested-atomic' THEN "
        "RAISE EXCEPTION 'injected nested append failure'; END IF; "
        "RETURN NEW; END $$")
    rule_engine_database.execute(
        "CREATE TRIGGER t14_inject_nested_fail BEFORE INSERT ON config.rule_schedule_segment "
        "FOR EACH ROW EXECUTE FUNCTION t14_inject_nested_failure()")
    with psycopg.connect(**contracts_database) as connection:  # autocommit=False
        with connection.transaction():  # outer transaction owned by the caller
            assert connection.execute(
                "SELECT count(*) FROM config.rule_schedule_revision "
                "WHERE rule_version='rule-version:t14-nested-atomic'").fetchone()[0] == 0
            with pytest.raises(psycopg.errors.RaiseException, match='injected nested append'):
                register_rule_schedule(connection, candidate)
            # Only the savepoint rolled back: no partial revision either way.
            assert connection.execute(
                "SELECT count(*) FROM config.rule_schedule_revision "
                "WHERE rule_version='rule-version:t14-nested-atomic'").fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM config.rule_schedule_segment "
                "WHERE rule_version='rule-version:t14-nested-atomic'").fetchone()[0] == 0
            assert connection.execute(
                "SELECT 'outer transaction stayed usable after the savepoint rollback'"
            ).fetchone()[0] == 'outer transaction stayed usable after the savepoint rollback'
            # DDL must run through the SAME outer connection: an external
            # autocommit connection would block on the table locks this outer
            # transaction still holds (main c0 diagnosis). Inside the outer
            # transaction the DDL commits together with the retry below.
            connection.execute(
                "DROP TRIGGER t14_inject_nested_fail ON config.rule_schedule_segment")
            connection.execute("DROP FUNCTION t14_inject_nested_failure()")
            assert register_rule_schedule(connection, candidate) == {
                'outcome': 'registered', 'rule_version': candidate.rule_version}  # retry succeeds
        assert connection.execute(  # outer commit included the retry registration
            "SELECT count(*) FROM config.rule_schedule_segment "
            "WHERE rule_version='rule-version:t14-nested-atomic'").fetchone()[0] == 2
    with psycopg.connect(**contracts_database, autocommit=True) as reviewer:
        assert read_rule_schedule(reviewer, candidate.rule_version) == candidate
        with pytest.raises(psycopg.errors.RaiseException, match='sealed'):
            reviewer.execute(
                "INSERT INTO config.rule_schedule_segment (rule_version, metric, "
                "measurement_context, basis, source_start, source_end, delta, lower, upper) "
                "VALUES ('rule-version:t14-nested-atomic', 'ec', 'drain', 'measured', "
                "'2020-06-01 00:00:00', '2020-06-02 00:00:00', NULL, 2, 5)")


def test_adjacent_db_intervals_register_then_split_episodes_on_content(rule_engine_database):
    revision = RuleScheduleRevision(
        rule_version='rule-version:t14-adjacent', rule_id='rule:t14-adjacent', version=1,
        scope_id='scope:t14', compartment_id='comp:t14', evidence_reference=EVIDENCE,
        is_demo=True, label='demo_rule', segments=(
            RuleScheduleSegment(metric='ec', measurement_context=DRAIN,
                                basis=RuleBasis.MEASURED, start=datetime(2019, 12, 16),
                                end=WINDOW_START + FIVE, lower=Decimal('2'), upper=Decimal('5')),
            RuleScheduleSegment(metric='ec', measurement_context=DRAIN,
                                basis=RuleBasis.MEASURED, start=WINDOW_START + FIVE,
                                end=datetime(2020, 5, 30), lower=Decimal('7'), upper=Decimal('9'))))
    register(rule_engine_database, revision)
    assert register_rule_schedule(rule_engine_database, revision) == {
        'outcome': 'reuse', 'rule_version': revision.rule_version}
    slots = drain_slots(WINDOW_START, [(Decimal('10'), None, None)] * 2)
    result = evaluate((revision,), slots, ('ec',), context=drain_context())
    assert result.metric_results[0].out_minutes == Decimal('10')  # 10 above both bounds
    # Different segment content breaks the run even across exact adjacency.
    assert [episode.original_minutes for episode in result.episodes] == [
        Decimal('5'), Decimal('5')]
    assert result.episodes[0].clipped_start == WINDOW_START
    assert result.episodes[1].original_start == WINDOW_START + FIVE


def test_codec_roundtrip_for_evaluation_contracts(rule_engine_database):
    drain = drain_rule(rule_engine_database, 't14-drain-codec')
    slots = drain_slots(WINDOW_START, [(Decimal('9'), Decimal('6'), QualityStatus.ACCEPTED)])
    result = evaluate((drain,), slots, ('ec', 'ph'), context=drain_context())
    assert from_json(RuleScheduleRevision, to_json(drain)) == drain
    assert from_json(type(result), to_json(result)) == result
    for episode in result.episodes:
        assert from_json(type(episode), to_json(episode)) == episode
