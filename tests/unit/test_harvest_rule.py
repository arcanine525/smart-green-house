"""BR-05 harvest rule profiles and the authorized unresolved-source audit.

Complements the T12 mart suite: explicit versioned rule evaluation across
record/day/week grains (no silent default) and the read-only raw audit for
frozen unresolved provenance (verify -> expose, never fabricate).
"""
import csv
import io
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from greenhouse.contracts import (
    HarvestRuleProfile, HarvestSummary, RuleStatus, UnresolvedSourceAudit, from_json, to_json,
)
from greenhouse.db.harvest_adapter import PostgresHarvestQueryAdapter
from greenhouse.db.harvest_rules import register_harvest_rule
from greenhouse.queries.harvest import HarvestService
from test_harvest_mart import contracts_database, D1, D2, independent_percent, OUT_OF_CYCLE, published_harvest


def demo_rule(scope, comp, threshold='10'):
    tag = uuid4().hex
    return HarvestRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, scope, comp,
                              Decimal(threshold),
                              'synthetic review thresholds; not agronomic guidance',
                              True, 'demo_rule')


def test_rule_evaluation_per_grain_and_counts(published_harvest):
    # Serial mapping: 43864 = 2020-02-03, 43865 = 2020-02-04, 43871 = 2020-02-10.
    # Cycle Monday 2020-02-03 .. 2020-02-24 (exclusive) spans exactly 3 ISO weeks.
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0'), (43871, '5', '0')],
                          cycle_start=date(2020, 2, 3), cycle_end=date(2020, 2, 24))
    profile = demo_rule(e['scope'], e['comp'])
    register_harvest_rule(e['connection'], profile)
    context = replace(e['context'], rule_version=profile.rule_version)
    list_records = e['service'].get_harvest_records(context)
    expected_share = independent_percent(Decimal('0.2'), Decimal('1.2'))  # 16.66…%
    assert list_records[0].rule_status == RuleStatus.OUTSIDE_RANGE
    assert abs(list_records[0].class_b_share_pct - expected_share) < Decimal('1e-49')
    assert list_records[1].rule_status == RuleStatus.WITHIN_RANGE  # (2, 0) → 0% inside
    assert list_records[2].rule_status == RuleStatus.WITHIN_RANGE  # (5, 0) → 0% inside
    assert all(item.rule_version == profile.rule_version and item.rule_profile == profile
               for item in list_records)
    days = e['service'].get_harvest_days(context)
    weekly = e['service'].get_harvest_weekly(context)
    summary = e['service'].get_harvest_summary(context)
    # Day grain: only event days carry their own verdict; 2020-02-08 sits
    # BETWEEN events and honestly shows record_present=false (no-event day).
    assert [day.rule_status for day in days[0:2]] == [RuleStatus.OUTSIDE_RANGE,
                                                      RuleStatus.WITHIN_RANGE]
    assert days[7].rule_status == RuleStatus.WITHIN_RANGE  # 2020-02-10 event
    assert not days[5].record_present  # 2020-02-08 between events
    assert all(not day.record_present for day in days if day.rule_status == RuleStatus.UNKNOWN)
    assert sum(day.rule_status == RuleStatus.UNKNOWN for day in days) == 18  # 21 days - 3 events
    # Week grain: windows [2020-02-03, 2020-02-10), [2020-02-10, 2020-02-17),
    # [2020-02-17, 2020-02-24); each verdict is that window's OWN share (§4.2:
    # 6.25% ≤ 10 stays within while one record inside is outside).
    assert [week.window_start.isoformat() for week in weekly] == ['2020-02-03', '2020-02-10',
                                                                  '2020-02-17']
    assert [week.window_end.isoformat() for week in weekly] == ['2020-02-10', '2020-02-17',
                                                                '2020-02-24']
    assert weekly[0].rule_status == RuleStatus.WITHIN_RANGE
    assert abs(weekly[0].class_b_share_pct - independent_percent(
        Decimal('0.2'), Decimal('3.2'))) < Decimal('1e-49')  # 6.25%
    assert weekly[0].outside_rule_records == 1 and weekly[0].rule_unknown_records == 0
    assert weekly[1].rule_status == RuleStatus.WITHIN_RANGE  # own share 0%
    assert weekly[1].class_b_share_pct == 0
    assert weekly[1].outside_rule_records == 0 and weekly[1].rule_unknown_records == 0
    assert weekly[2].rule_status == RuleStatus.UNKNOWN  # empty week: no denominator
    assert weekly[2].class_b_share_pct is None
    assert weekly[2].outside_rule_records == 0 and weekly[2].rule_unknown_records == 0
    assert all('outside_harvest_rule' not in week.reason_codes for week in weekly)
    # Whole-window summary keeps both layers separate: own-share verdict + counters.
    assert summary.rule_status == RuleStatus.WITHIN_RANGE  # own share ≈2.44%
    assert summary.outside_rule_records == 1 and summary.rule_unknown_records == 0
    assert summary.accepted_records == 3 and summary.rule_profile == profile
    # Typed per-measure quality partition of THIS window's real records.
    q = summary.record_quality
    assert q.population_records == 3 and q.prod_a_accepted == 3 and q.prod_b_accepted == 3
    assert (q.prod_a_missing, q.prod_a_invalid, q.prod_a_suspect) == (0, 0, 0)
    assert (q.prod_b_missing, q.prod_b_invalid, q.prod_b_suspect) == (0, 0, 0)
    assert all(week.record_quality is None for week in weekly)  # aggregates carry none
    with pytest.raises(ValueError):
        replace(q, prod_a_missing=1)  # inconsistent partition is rejected instantly
    with pytest.raises(ValueError):
        replace(summary, outside_rule_records=summary.accepted_records + 1)  # population bound


def test_week_rule_counts_never_leak_across_windows(published_harvest):
    """Weekly counters count exactly their own [start, right) window. A Sunday
    violating record (43863 = 2020-02-02) never reaches the Monday week."""
    e = published_harvest([(43863, '1', '1'), (43864, '30', '1')],
                          cycle_start=date(2020, 2, 1), cycle_end=date(2020, 2, 9))
    profile = demo_rule(e['scope'], e['comp'])
    register_harvest_rule(e['connection'], profile)
    context = replace(e['context'], rule_version=profile.rule_version)
    weekly = e['service'].get_harvest_weekly(context)
    # Windows split at the Monday boundary: [2020-02-01, 2020-02-03), [2020-02-03, 2020-02-09).
    assert [(week.window_start.isoformat(), week.window_end.isoformat()) for week in weekly] == [
        ('2020-02-01', '2020-02-03'), ('2020-02-03', '2020-02-09')]
    first, second = weekly
    # The 50% Sunday record flags ITS week only (window share > 10): one counter.
    assert first.rule_status == RuleStatus.OUTSIDE_RANGE
    assert first.accepted_records == 1
    assert first.outside_rule_records == 1 and first.rule_unknown_records == 0
    assert 'outside_harvest_rule' in first.reason_codes
    # The next week re-aggregates its OWN records only: own share ≈3.22% within,
    # zero counters, zero flags — the Sunday violation did not leak over.
    assert second.rule_status == RuleStatus.WITHIN_RANGE
    assert second.accepted_records == 1
    assert second.outside_rule_records == 0 and second.rule_unknown_records == 0
    assert 'outside_harvest_rule' not in second.reason_codes
    # Without a rule_version the weekly rows stay honestly not_configured.
    no_rule = e['service'].get_harvest_weekly(e['context'])
    assert all(item.rule_status == RuleStatus.NOT_CONFIGURED for item in no_rule)


def test_summary_rule_verdict_is_window_own_share_not_record_flags(published_harvest):
    """A violating record stays a record-grain counter; the WINDOW verdict comes
    from the window's own share — zooming to only the second record flips it."""
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0'), (43871, '5', '0')],
                          cycle_start=date(2020, 2, 3), cycle_end=date(2020, 2, 24))
    profile = demo_rule(e['scope'], e['comp'])
    register_harvest_rule(e['connection'], profile)
    context = replace(e['context'], rule_version=profile.rule_version)
    summary = e['service'].get_harvest_summary(context)  # own share = 0.2/8.2 ≈ 2.44%
    assert summary.rule_status == RuleStatus.WITHIN_RANGE
    assert summary.outside_rule_records == 1 and summary.rule_unknown_records == 0
    assert 'outside_harvest_rule' not in summary.reason_codes
    # Zoom [2020-02-03, 2020-02-04): exactly the first (outside) record.
    only_out = e['service'].get_harvest_summary(replace(
        context, start=date(2020, 2, 3), end=date(2020, 2, 4)))
    assert only_out.rule_status == RuleStatus.OUTSIDE_RANGE
    assert only_out.outside_rule_records == 1 and 'outside_harvest_rule' in only_out.reason_codes
    # Zoom [2020-02-04, 2020-02-21): own share 0% is within; no flag inherits
    # from the outside record that this window excludes (summary leak check).
    rest = e['service'].get_harvest_summary(replace(
        context, start=date(2020, 2, 4), end=date(2020, 2, 21)))
    assert rest.rule_status == RuleStatus.WITHIN_RANGE
    assert rest.class_b_share_pct == 0
    assert rest.accepted_records == 2
    assert rest.outside_rule_records == 0 and rest.rule_unknown_records == 0
    assert 'outside_harvest_rule' not in rest.reason_codes


def test_no_rule_keeps_not_configured_and_unknown_flags(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '')])  # missing B: no record share
    list_records = e['service'].get_harvest_records(e['context'])
    assert all(item.rule_status == RuleStatus.NOT_CONFIGURED and item.rule_version is None
               for item in list_records)
    unpaired = [item for item in list_records if not item.paired]
    assert all(item.class_b_share_pct is None for item in unpaired)  # no fabricated 0%
    with pytest.raises(ValueError, match='harvest rule'):
        e['service'].get_harvest_records(replace(e['context'], rule_version='unknown:rule'))


def test_record_csv_rows_carry_the_current_rule(published_harvest):
    """Export parity for R1: every record CSV row states the rule version of
    the CURRENT evaluation, not a stale selection artifact."""
    from greenhouse.app import exports, wiring
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0')], cycle_start=date(2020, 2, 3),
                          cycle_end=date(2020, 2, 24))
    profile = demo_rule(e['scope'], e['comp'])
    register_harvest_rule(e['connection'], profile)
    action = wiring.harvest_action(e['service'], e['publication'],
                                   replace(e['context'], rule_version=profile.rule_version))
    text = exports.harvest_record_csv(action).decode('utf-8-sig')
    lines = text.splitlines()
    rows = list(csv.reader(io.StringIO(
        '\n'.join(line for line in lines if not line.startswith('# ')))))
    header = rows[0]
    version_index = header.index('rule_version')
    status_index = header.index('rule_status')
    assert rows[1][version_index] == profile.rule_version  # 16.66…% record
    assert rows[1][status_index] == 'outside_range'
    assert rows[2][version_index] == profile.rule_version  # 0% record stays within
    assert rows[2][status_index] == 'within_range'
    # Without a rule version the rows stay honestly empty.
    plain = wiring.harvest_action(e['service'], e['publication'], e['context'])
    plain_rows = [line for line in exports.harvest_record_csv(plain).decode('utf-8-sig').splitlines()
                  if not line.startswith('# ')]
    plain_csv = list(csv.reader(io.StringIO('\n'.join(plain_rows))))
    assert plain_csv[1][version_index] == '' and plain_csv[1][status_index] == 'not_configured'


def test_rule_exports_state_their_grain(published_harvest):
    """Row-grain labels and the window-summary kind are part of the export
    contract: no reader has to guess which layer a rule_status verdict or a
    rule_version line belongs to."""
    from greenhouse.app import exports, wiring
    e = published_harvest([(D1, '1', '0.2'), (D2, '2', '0')], cycle_start=date(2020, 2, 3),
                          cycle_end=date(2020, 2, 24))
    action = wiring.harvest_action(e['service'], e['publication'], e['context'])
    record_text = exports.harvest_record_csv(action).decode('utf-8-sig')
    day_text = exports.harvest_day_csv(action).decode('utf-8-sig')
    week_text = exports.harvest_week_csv(action).decode('utf-8-sig')
    excluded_text = exports.excluded_source_csv(action).decode('utf-8-sig')
    summary_text = exports.harvest_summary_json(action).decode('utf-8')
    assert 'evaluation_grain=record' in record_text
    assert 'evaluation_grain=day' in day_text
    assert 'evaluation_grain=week' in week_text
    assert "every row verdict is that row's OWN share" in week_text
    assert 'evaluation_grain=unresolved_issue_identity' in excluded_text
    assert '"export_kind": "window_summary"' in summary_text
    assert '"grain_note"' in summary_text
    # The window summary JSON keeps the exact typed body reconstructable.
    payload = json.loads(summary_text)
    assert payload['export_kind'] == 'window_summary'
    assert payload['harvest_export_version'] == 1
    # The nested envelope is the UNCHANGED canonical contract and decodes.
    envelope = payload['summary']
    assert envelope['contract_version'] == 2
    decoded = from_json(HarvestSummary, json.dumps(envelope))
    assert decoded == action.summary  # unchanged canonical body, typed roundtrip
    assert decoded.accepted_records == 2


def test_threshold_equality_is_inside_rule(published_harvest):
    e = published_harvest([(D1, '4', '1')])  # share B = exactly 20%
    profile = demo_rule(e['scope'], e['comp'], '20')
    register_harvest_rule(e['connection'], profile)
    context = replace(e['context'], rule_version=profile.rule_version)
    assert e['service'].get_harvest_records(context)[0].rule_status == RuleStatus.WITHIN_RANGE


def test_rule_rejections_are_loud(published_harvest):
    e = published_harvest([(D1, '1', '0.2')])
    with pytest.raises(ValueError, match='harvest rule'):
        e['service'].get_harvest_records(replace(e['context'], rule_version='unknown:rule'))
    for threshold in ('-1', '101', 'NaN'):
        with pytest.raises(ValueError):
            demo_rule(e['scope'], e['comp'], threshold)
    profile = demo_rule(e['scope'], e['comp'])
    with pytest.raises(ValueError, match='scope'):
        profile.matches(replace(e['context'], cycle_id='other-cycle',
                                compartment_ids=('other-comp',)))
    object.__setattr__(profile, 'max_class_b_share_pct', Decimal('101'))  # bypass DTO check
    with pytest.raises(psycopg.errors.CheckViolation):
        register_harvest_rule(e['connection'], profile)


def test_unresolved_source_audit_authorized_and_verified(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (OUT_OF_CYCLE, '0.32', '0.1')])
    item = e['service'].adapter.harvest_state(e['context']).unresolved_items[0]
    assert item.reason_code == 'out_of_cycle_unresolved'
    service = HarvestService(PostgresHarvestQueryAdapter(
        e['connection'], raw_snapshots={e['snapshot']: e['raw']}))
    audit = service.get_unresolved_source(e['context'], item)
    assert audit.record_verified and audit.file_verified and audit.note is None
    assert audit.headers[0] == 'time' and audit.tokens[0] == str(OUT_OF_CYCLE)
    x_index = audit.headers.index('ProdA')
    assert audit.tokens[x_index] == '0.32'  # exact RAW value only via the audit path
    b_index = audit.headers.index('ProdB')
    assert audit.tokens[b_index] == '0.1'
    assert audit.line_number == 3 and audit.relative_path == 'Synthetic/Production.csv'
    assert from_json(UnresolvedSourceAudit, to_json(audit)) == audit


def test_unresolved_source_audit_refuses_foreign_and_masked(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (OUT_OF_CYCLE, '0.32', '0.1')])
    item = e['service'].adapter.harvest_state(e['context']).unresolved_items[0]
    foreign = replace(item, source_record_id='foreign:' + uuid4().hex)
    with pytest.raises(ValueError, match='frozen'):
        e['service'].get_unresolved_source(e['context'], foreign)
    masked = replace(item, reason_code='invalid_numeric')
    with pytest.raises(ValueError, match='frozen'):
        e['service'].get_unresolved_source(e['context'], masked)


def test_unresolved_audit_matches_the_whole_frozen_identity(published_harvest):
    """Authorization = the COMPLETE frozen identity (source, disposition,
    reason, issue_column). A forged column that RECOMBINES into another real
    frozen issue (A→B for the same source/reason) legitimately succeeds; only
    identities that exist nowhere in the frozen set are refused. Metadata is
    taken from the frozen entry, never from caller fields."""
    e = published_harvest([(D1, '1', '0.2'), (OUT_OF_CYCLE, 'bad-a', 'bad-b')])
    state = e['service'].adapter.harvest_state(e['context'])
    proda = next(item for item in state.unresolved_items
                 if item.reason_code == 'invalid_numeric' and item.issue_column == 'ProdA')
    prodb = next(item for item in state.unresolved_items
                 if item.reason_code == 'invalid_numeric' and item.issue_column == 'ProdB')
    out_of_cycle = next(item for item in state.unresolved_items
                        if item.reason_code == 'out_of_cycle_unresolved')
    assert proda.source_record_id == prodb.source_record_id == out_of_cycle.source_record_id
    service = HarvestService(PostgresHarvestQueryAdapter(
        e['connection'], raw_snapshots={e['snapshot']: e['raw']}))
    # Negative: identities that are frozen NOWHERE are refused.
    with pytest.raises(ValueError, match='frozen'):
        service.get_unresolved_source(e['context'], replace(proda, issue_column='ProdC'))
    flip = 'conflict' if proda.disposition == 'quarantine' else 'quarantine'
    with pytest.raises(ValueError, match='frozen'):
        service.get_unresolved_source(e['context'], replace(proda, disposition=flip))
    with pytest.raises(ValueError, match='frozen'):
        service.get_unresolved_source(e['context'], replace(proda, reason_code='suspect_mass'))
    with pytest.raises(ValueError, match='frozen'):
        service.get_unresolved_source(e['context'], replace(
            proda, source_record_id='foreign:' + uuid4().hex))
    # Negative: the none-column out-of-cycle issue forged to ProdB is not frozen.
    with pytest.raises(ValueError, match='frozen'):
        service.get_unresolved_source(e['context'], replace(out_of_cycle, issue_column='ProdB'))
    # Positive recombinant: A→B with same source/disposition/reason IS the
    # frozen ProdB identity, and the metadata comes from that frozen entry.
    recombined = service.get_unresolved_source(
        e['context'], replace(proda, issue_column='ProdB'))
    assert recombined.issue_column == 'ProdB' and recombined.record_verified
    # Both legitimate issues audit verified, independently and with frozen metadata.
    audit_a = service.get_unresolved_source(e['context'], proda)
    audit_b = service.get_unresolved_source(e['context'], prodb)
    assert audit_a.issue_column == 'ProdA' and audit_b.issue_column == 'ProdB'
    assert audit_a.reason_code == audit_b.reason_code == 'invalid_numeric'
    assert audit_a.record_verified and audit_b.record_verified
    assert audit_a.note is None and audit_b.note is None
    assert audit_a.source_time_serial == Decimal(OUT_OF_CYCLE)
    assert audit_a.line_number == 3 and len(audit_a.headers) == len(audit_a.tokens)
    assert audit_a.disposition == proda.disposition  # frozen entry metadata, not caller fields
    assert audit_a.rule_version is None and audit_a.rule_grain is None  # unconfigured context
    # With a rule selected, the audit carries the pinned context's rule and the
    # caller's explicit grain — traceability only, never a KPI input.
    profile = demo_rule(e['scope'], e['comp'])
    register_harvest_rule(e['connection'], profile)
    ruled = service.get_unresolved_source(
        replace(e['context'], rule_version=profile.rule_version), proda, rule_grain='record')
    assert ruled.rule_version == profile.rule_version and ruled.rule_grain == 'record'
    assert ruled.record_verified
    # The pinned context travels with the typed audit and the CSV export.
    assert audit_a.publication.publication_id == e['publication'].publication_id
    assert audit_a.cycle_id == e['cycle'] and audit_a.compartment_id == e['comp']
    assert audit_a.requested_start == e['context'].start
    assert audit_a.requested_end == e['context'].end
    from greenhouse.app import exports
    text = exports.unresolved_source_audit_csv(audit_a).decode('utf-8-sig')
    assert f"publication={e['publication'].publication_id}" in text
    assert f"cycle={e['cycle']}" in text
    assert f"compartment={e['comp']}" in text
    assert f"disposition={proda.disposition}" in text
    # Full run/policy/version parity with the pinned publication's Versions.
    assert f"pipeline_run={e['publication'].versions.pipeline_run_id}" in text
    assert f"quality_policy={e['publication'].versions.quality_policy_version}" in text
    assert f"parser_version={e['publication'].versions.parser_version}" in text
    assert f"code_version={e['publication'].versions.code_version}" in text
    assert f"config_bundle_hash={e['publication'].versions.config_bundle_hash}" in text
    assert 'record_verified=false' not in text and 'record_verified=true' in text
    text_ruled = exports.unresolved_source_audit_csv(ruled).decode('utf-8-sig')
    assert f"rule_version={profile.rule_version}" in text_ruled
    assert "rule_grain=record" in text_ruled


def test_unresolved_source_audit_refuses_absent_state(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (OUT_OF_CYCLE, '0.32', '0.1')])
    item = e['service'].adapter.harvest_state(e['context']).unresolved_items[0]
    moved = replace(e['context'], cycle_id='no-such-cycle')
    with pytest.raises(ValueError, match='absent'):
        e['service'].get_unresolved_source(moved, item)


def test_unresolved_source_audit_honest_without_raw(published_harvest):
    e = published_harvest([(D1, '1', '0.2'), (OUT_OF_CYCLE, '0.32', '0.1')])
    item = e['service'].adapter.harvest_state(e['context']).unresolved_items[0]
    # No mount at all: explicit unavailable state, no tokens; the known row
    # metadata (path/line/serial/reason) stays visible on unavailable raw.
    audit = e['service'].get_unresolved_source(e['context'], item)
    assert not audit.file_verified and audit.tokens == () and audit.note == 'raw-mount-missing'
    assert audit.relative_path == 'Synthetic/Production.csv' and audit.line_number == 3
    assert audit.source_time_serial == Decimal(OUT_OF_CYCLE)
    assert audit.reason_code == 'out_of_cycle_unresolved' and audit.issue_column is None
    # Declaring a mount whose file is missing: next honest state.
    service = HarvestService(PostgresHarvestQueryAdapter(
        e['connection'], raw_snapshots={e['snapshot']: e['fixture'] / 'no-such-raw'}))
    audit = service.get_unresolved_source(e['context'], item)
    assert audit.tokens == () and audit.note == 'raw-file-missing'
    assert audit.file_verified is False
    # Drifted file: checksum mismatch stays unverified.
    drift_service = HarvestService(PostgresHarvestQueryAdapter(
        e['connection'], raw_snapshots={e['snapshot']: e['raw']}))
    path = e['raw'] / 'Synthetic/Production.csv'
    original = path.read_bytes()
    path.write_bytes(original.replace(b'0.2', b'0.9'))
    try:
        audit = drift_service.get_unresolved_source(e['context'], item)
        assert audit.tokens == () and audit.note == 'raw-identity-mismatch'
    finally:
        path.write_bytes(original)
