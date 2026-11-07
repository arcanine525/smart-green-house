"""Water action and export contracts: screen, CSV and JSON reuse one DTO set
of one pinned publication. Decimal exact, NULL empty, 0 preserved, pin once."""
import csv
import io
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from greenhouse.app import exports, figures, wiring
from greenhouse.contracts import IrrigationRuleProfile, IrrigationSummary, RuleStatus, from_json
from greenhouse.db.irrigation_rules import register_irrigation_rule
from greenhouse.db.query_adapter import PostgresQueryAdapter
from greenhouse.queries import IrrigationService
from greenhouse.queries.irrigation import percent


class CountingAdapter(PostgresQueryAdapter):
    def __init__(self, connection, **kwargs):
        super().__init__(connection, **kwargs)
        self.pointer_reads = 0
        self.by_id_reads = 0

    def pin_publication(self, scope_id):
        self.pointer_reads += 1
        return super().pin_publication(scope_id)

    def publication(self, publication_id):
        self.by_id_reads += 1
        return super().publication(publication_id)


def csv_rows(payload):
    text = payload.decode('utf-8-sig')
    lines = text.splitlines()
    data = [line for line in lines if not line.startswith('# ')]
    return ([line for line in lines if line.startswith('# ')],
            list(csv.DictReader(io.StringIO('\n'.join(data)))))


def action_of(factory, rows=None, *, basis='verified', start=None, end=None):
    kwargs = {'basis': basis}
    if rows is not None:
        kwargs['rows'] = rows
    e = factory(**kwargs)
    if start is not None or end is not None:
        e.context = replace(e.context, start=start or e.context.start, end=end or e.context.end)
    return e, wiring.water_action(e.service, e.publication, e.context)


def test_context_window_is_the_single_inclusive_conversion():
    assert wiring.context_window(date(2020, 1, 1), date(2020, 1, 4)) == (date(2020, 1, 1), date(2020, 1, 5))
    with pytest.raises(ValueError):
        wiring.context_window(date(2020, 1, 4), date(2020, 1, 1))


def test_raw_snapshot_mapping_is_manifest_driven(tmp_path):
    import json
    flat = tmp_path / 'flat' / 'snap-abc'
    flat.mkdir(parents=True)
    (flat / 'manifest.json').write_text(json.dumps({'snapshot_id': 'snap-abc', 'dataset': 'x'}))
    nested = tmp_path / 'nested' / 'agc-2nd-2019' / 'abc'
    nested.mkdir(parents=True)
    (nested / 'manifest.json').write_text(json.dumps({'snapshot_id': 'agc-2nd-2019-abc'}))
    (tmp_path / 'nested' / '.downloads').mkdir()  # dot entries stay unmapped
    assert wiring.raw_snapshot_mapping(tmp_path / 'flat') == {'snap-abc': flat.resolve()}
    assert wiring.raw_snapshot_mapping(tmp_path / 'nested') == {'agc-2nd-2019-abc': nested.resolve()}
    assert wiring.raw_snapshot_mapping(tmp_path / 'missing') == {}


def test_raw_snapshot_mapping_supports_root_mount_and_same_basenames(tmp_path):
    import json
    root = tmp_path / 'direct'
    root.mkdir()
    (root / 'manifest.json').write_text(json.dumps({'snapshot_id': 'direct-root'}))
    (root / 'AICU').mkdir()
    assert wiring.raw_snapshot_mapping(root) == {'direct-root': root.resolve()}

    both = tmp_path / 'both'
    for dataset in ('dataset-a', 'dataset-b'):
        snapshot = both / dataset / 'same-sha'
        snapshot.mkdir(parents=True)
        (snapshot / 'manifest.json').write_text(json.dumps({'snapshot_id': 'ambiguous'}))
    # Same-basename candidates must not overwrite each other before the
    # duplicate-snapshot-ID check: the ambiguous identity maps to nothing.
    assert wiring.raw_snapshot_mapping(both) == {}


def test_raw_snapshot_mapping_keeps_containment_for_symlink_candidates(tmp_path):
    import json
    import os
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'manifest.json').write_text(json.dumps({'snapshot_id': 'escape'}))
    root = tmp_path / 'rawroot'
    root.mkdir()
    link = root / 'escape-link'
    link.symlink_to(outside, target_is_directory=True)
    (root / 'manifest.json').write_text(json.dumps({'snapshot_id': 'direct-root'}))
    mapping = wiring.raw_snapshot_mapping(root)
    assert mapping == {'direct-root': root.resolve()}  # escaping symlink stays unmapped


def test_raw_snapshot_mapping_skips_invalid_and_ambiguous_manifests(tmp_path):
    import json
    import os
    invalid = tmp_path / 'invalid'
    invalid.mkdir()
    (invalid / 'manifest.json').write_text('not-json{')
    no_id = tmp_path / 'noid'
    no_id.mkdir()
    (no_id / 'manifest.json').write_text(json.dumps({'dataset': 'x'}))
    empty = tmp_path / 'empty' / 'some-snapshot'
    empty.mkdir(parents=True)  # directory named like a snapshot but no manifest
    first, second = tmp_path / 'one', tmp_path / 'two'
    for directory in (first, second):
        directory.mkdir()
        (directory / 'manifest.json').write_text(json.dumps({'snapshot_id': 'dup'}))
    locked = tmp_path / 'locked'
    locked.mkdir()
    (locked / 'manifest.json').write_text(json.dumps({'snapshot_id': 'locked'}))
    os.chmod(locked / 'manifest.json', 0o000)  # unreadable manifest is skipped, not fatal
    try:
        assert wiring.raw_snapshot_mapping(tmp_path) == {}
    finally:
        os.chmod(locked / 'manifest.json', 0o644)
    # With the manifest readable again only the single-declaration id maps;
    # 'dup' stays excluded (ambiguous) even though both files still exist.
    kept = tmp_path / 'kept'
    kept.mkdir()
    (kept / 'manifest.json').write_text(json.dumps({'snapshot_id': 'kept'}))
    assert wiring.raw_snapshot_mapping(tmp_path) == {'kept': kept.resolve(), 'locked': locked.resolve()}


def test_action_pins_by_id_and_never_reads_pointer_mid_action(published_scope):
    e = published_scope()
    counting = CountingAdapter(e.connection, raw_snapshots={e.snapshot: e.raw})
    service = IrrigationService(counting)
    publication = wiring.resolve_publication(service, e.publication.publication_id)
    action = wiring.water_action(service, publication, e.context)
    assert counting.pointer_reads == 0
    assert counting.by_id_reads >= 1
    assert action.summary.drain_fraction_pct == Decimal('35')
    assert action.summary.publication.publication_id == e.publication.publication_id


def test_day_export_exact_values_null_empty_zero_preserved(published_scope):
    e, action = action_of(published_scope, rows=(('10', '2'), ('30', None)))
    metadata, rows = csv_rows(exports.day_csv(action))
    assert len(rows) == 2
    assert rows[0]['irrigation_l_m2'] == '10' and rows[0]['drain_l_m2'] == '2'
    assert rows[0]['paired'] == 'true' and rows[0]['drain_fraction_pct'] == '20'
    assert rows[1]['irrigation_l_m2'] == '30' and rows[1]['drain_l_m2'] == ''
    assert rows[1]['paired'] == 'false' and rows[1]['drain_fraction_pct'] == ''
    assert rows[0]['irrigation_quality'] == 'accepted' and rows[1]['drain_quality'] == 'missing'
    assert rows[0]['revision_id'] and rows[0]['logical_record_key']
    headers = {line.split('=')[0][2:] for line in metadata if '=' in line}
    assert {'quantity_unit', 'scope', 'snapshot', 'publication', 'cycle', 'compartment',
            'requested_window', 'effective_window', 'basis_status', 'rule_status'} <= headers
    assert any('effective_window=[2020-01-01,2020-01-03)' in line for line in metadata)
    assert any('requested_window=[2020-01-01,2020-01-03)' in line for line in metadata)


def test_day_export_metadata_carries_summary_coverage_and_counts(published_scope):
    e, action = action_of(published_scope, rows=(('10', '2'), ('30', None)))  # partial pairing
    metadata, rows = csv_rows(exports.day_csv(action))
    summary = action.summary
    assert any(f'status={summary.status.value}' in line for line in metadata)
    assert any(f'expected_days={summary.expected_days}' in line for line in metadata)
    assert any(f'accepted_days: irrigation={summary.irrigation_accepted_days},'
               f'drain={summary.drain_accepted_days},paired={summary.paired_days}' in line for line in metadata)
    assert any(f'coverage_pct: irrigation={summary.irrigation_coverage_pct},'
               f'drain={summary.drain_coverage_pct},paired={summary.paired_coverage_pct}'
               in line for line in metadata)
    assert any(f'paired_l_m2: numerator(drain)={summary.paired_drain_l_m2},'
               f'denominator(irrigation)={summary.paired_irrigation_l_m2}' in line for line in metadata)
    assert any(f'day_counts: unknown={summary.unknown_days},suspect={summary.suspect_days},'
               f'drain_exceeds_irrigation={summary.drain_exceeds_irrigation_days}' in line for line in metadata)
    # Metadata values must equal the displayed action's own DTO, not recomputed.
    assert str(summary.paired_irrigation_l_m2) == '10' and str(summary.paired_coverage_pct) == '50'


def test_rule_profile_numeric_version_in_exports(published_scope):
    e = published_scope((('10', '2'), ('10', '5')))
    tag = uuid4().hex
    profile = IrrigationRuleProfile('rule-version:' + tag, 'rule:' + tag, 2, e.scope, e.comp,
                                    Decimal('20'), Decimal('40'), Decimal('0'), Decimal('100'),
                                    'synthetic review thresholds; not agronomic guidance', True, 'demo_rule')
    register_irrigation_rule(e.connection, profile)
    action = wiring.water_action(e.service, e.publication,
                                 replace(e.context, rule_version=profile.rule_version))
    metadata, rows = csv_rows(exports.day_csv(action))
    assert rows[0]['rule_version'] == profile.rule_version
    assert any('rule_profile=' + profile.rule_version in line and 'profile_version=2' in line
               for line in metadata)
    _, weekly = csv_rows(exports.weekly_csv(action))
    assert weekly[0]['rule_version'] == profile.rule_version
    assert weekly[0]['rule_profile_version'] == '2'
    assert weekly[0]['rule_profile_is_demo'] == 'true'


def test_day_export_zero_and_over_one_hundred_ratio(published_scope):
    e, action = action_of(published_scope, rows=(('0', '0'), ('0', '0')))
    _, rows = csv_rows(exports.day_csv(action))
    assert rows[0]['irrigation_l_m2'] == '0' and rows[0]['drain_l_m2'] == '0'
    e, action = action_of(published_scope, rows=(('5', '6'), ('5', '6')))
    metadata, rows = csv_rows(exports.day_csv(action))
    assert rows[0]['drain_fraction_pct'] == '120' and rows[0]['drain_exceeds_irrigation'] == 'true'
    assert any('basis_status=verified' in line for line in metadata)


def test_weekly_export_carries_iso_and_paired_totals(published_scope):
    created = published_scope((('10', '2'), ('30', '12'), ('5', '1'), ('10', '0')),
                              start=date(2019, 12, 30), end=date(2020, 1, 8))
    context = replace(created.context, start=date(2019, 12, 29), end=date(2020, 1, 9))
    action = wiring.water_action(created.service, created.publication, context)
    metadata, rows = csv_rows(exports.weekly_csv(action))
    assert [(row['iso_year'], row['iso_week'], row['expected_days']) for row in rows] == \
        [('2020', '1', '7'), ('2020', '2', '2')]
    first = rows[0]
    assert first['paired_irrigation_l_m2'] == '55' and first['paired_drain_l_m2'] == '15'
    assert first['drain_fraction_pct'] == str(percent(Decimal(15), Decimal(55)))
    assert any(f'pipeline_run={created.publication.versions.pipeline_run_id}' in line for line in metadata)


def test_summary_json_roundtrips_contract_v2(published_scope):
    e, action = action_of(published_scope)
    payload = json.loads(exports.summary_json(action))
    assert payload['contract_version'] == 2
    assert from_json(IrrigationSummary, exports.summary_json(action)) == action.summary


def test_assumed_basis_metadata_keeps_visible_label(published_scope):
    e, action = action_of(published_scope, rows=(('5', '6'), ('5', '6')), basis='assumed_unverified')
    metadata, _ = csv_rows(exports.day_csv(action))
    assert any('basis_status=assumed_unverified' in line for line in metadata)
    assert any('basis_label=giả định chưa xác minh' in line for line in metadata)
    assert any('assumptions=AGC_SHARED_BASIS_V1 v1' in line for line in metadata)


def test_rule_metadata_and_counts_in_exports(published_scope):
    e = published_scope((('10', '2'), ('10', '5')))
    tag = uuid4().hex
    profile = IrrigationRuleProfile('rule-version:' + tag, 'rule:' + tag, 1, e.scope, e.comp,
                                    Decimal('20'), Decimal('40'), Decimal('0'), Decimal('100'),
                                    'synthetic review thresholds; not agronomic guidance', True, 'demo_rule')
    register_irrigation_rule(e.connection, profile)
    action = wiring.water_action(e.service, e.publication,
                                 replace(e.context, rule_version=profile.rule_version))
    metadata, rows = csv_rows(exports.day_csv(action))
    assert rows[0]['rule_status'] == 'within_range' and rows[1]['rule_status'] == 'outside_range'
    assert rows[0]['rule_version'] == profile.rule_version
    _, weekly = csv_rows(exports.weekly_csv(action))
    assert weekly[0]['rule_status'] == 'configured' and weekly[0]['outside_rule_days'] == '1'
    assert any('rule_profile=' + profile.rule_version in line and 'demo=true' in line for line in metadata)
    assert any(f'rule_version={profile.rule_version}' in line for line in metadata)
    assert action.summary.rule_status == RuleStatus.CONFIGURED


def test_daily_figure_keeps_missing_days_as_gaps(published_scope):
    e = published_scope((('10', '2'),))
    action = wiring.water_action(e.service, e.publication, e.context)
    assert len(action.days) == 2 and action.days[1].irrigation.value is None
    figure = figures.daily_figure(action.days)
    assert len(figure.data) == 3
    for trace in figure.data:
        assert trace.y[0] is not None
        assert trace.y[1] is None
        assert trace.connectgaps is False


def test_resolve_rejects_pre_mart_publication(legacy_scope):
    service = IrrigationService(PostgresQueryAdapter(legacy_scope.connection))
    with pytest.raises(ValueError, match='mart'):
        wiring.resolve_publication(service, legacy_scope.publication_id)
