"""CSV/JSON exports reusing the exact displayed DTOs of one pinned action.

Rules: Decimal → exact string (never rounded, source scale kept), NULL →
empty field, 0 → '0', bool → 'true'/'false', tuple → ';'-joined, no extra
rounding and no re-pinning. Metadata headers document units, filters, window,
quality/basis/assumption and rule identity. Schema documented in
src/greenhouse/app/README.md.
"""
import csv
import io
import json

from greenhouse.contracts import (
    ClimateEvaluation, HarvestDay, HarvestRecord, HarvestSummary, IrrigationDay,
    IrrigationSummary, UnresolvedSourceAudit, to_json,
)

EXPORT_VERSION = 'greenhouse-water-csv-v1'
HARVEST_EXPORT_VERSION = 'greenhouse-harvest-csv-v1'

DAY_COLUMNS = (
    'resource_date',
    'irrigation_l_m2', 'drain_l_m2',
    'irrigation_quality', 'irrigation_applicability', 'irrigation_reason_code', 'irrigation_source_unit',
    'irrigation_basis_status', 'irrigation_basis_version_id', 'irrigation_area_m2', 'irrigation_basis_kind',
    'irrigation_assumption_id', 'irrigation_assumption_version', 'irrigation_basis_label',
    'drain_quality', 'drain_applicability', 'drain_reason_code', 'drain_source_unit',
    'drain_basis_status', 'drain_basis_version_id', 'drain_area_m2', 'drain_basis_kind',
    'drain_assumption_id', 'drain_assumption_version', 'drain_basis_label',
    'paired', 'drain_fraction_pct', 'drain_exceeds_irrigation',
    'rule_status', 'rule_version',
    'revision_id', 'logical_record_key', 'correction_id', 'correction_version',
    'reason_codes',
)

SUMMARY_COLUMNS = (
    'iso_year', 'iso_week', 'window_start', 'window_end', 'expected_days',
    'irrigation_observed_l_m2', 'drain_observed_l_m2',
    'paired_irrigation_l_m2', 'paired_drain_l_m2', 'drain_fraction_pct',
    'irrigation_coverage_pct', 'drain_coverage_pct', 'paired_coverage_pct',
    'irrigation_accepted_days', 'drain_accepted_days', 'paired_days',
    'unknown_days', 'suspect_days', 'drain_exceeds_irrigation_days',
    'outside_rule_days', 'rule_unknown_days',
    'status', 'comparison_eligible',
    'basis_status', 'basis_label', 'basis_version_ids', 'assumption_ids', 'assumption_versions',
    'corrections',
    'rule_status', 'rule_version', 'rule_profile_version', 'rule_profile_label',
    'rule_profile_is_demo', 'rule_profile_evidence',
    'unit', 'ratio_unit', 'time_basis',
    'publication_id', 'scope_id', 'snapshot_id',
    'pipeline_run_id', 'quality_policy_version', 'parser_version', 'code_version',
    'config_bundle_hash', 'metric_version',
    'reason_codes',
)


def join(values, separator=';'):
    return separator.join(str(item) for item in values) if values else ''


def _cell(value):
    """Exact contract text: Decimal str(), None → '', bool → true/false, tuple → join."""
    if value is None:
        return ''
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, tuple):
        return join(value)
    return str(value)


def _row(names, read):
    return {name: _cell(read(name)) for name in names}


MEASURE_FIELD = {'l_m2': 'value'}
VERSION_FIELDS = {'pipeline_run_id', 'quality_policy_version', 'parser_version', 'code_version',
                  'config_bundle_hash', 'metric_version'}
DAY_LEVEL = {'drain_fraction_pct', 'drain_exceeds_irrigation', 'reason_codes'}


def day_rows(days):
    def read(day: IrrigationDay, name):
        if name in DAY_LEVEL:
            return getattr(day, name)
        if name.startswith(('irrigation_', 'drain_')):
            prefix, field = name.split('_', 1)
            return getattr(getattr(day, prefix), MEASURE_FIELD.get(field, field))
        return getattr(day, name)
    return [_row(DAY_COLUMNS, lambda name, day=day: read(day, name)) for day in days]


def summary_rows(summaries):
    profile_fields = {'rule_profile_version': 'version', 'rule_profile_label': 'label',
                      'rule_profile_is_demo': 'is_demo', 'rule_profile_evidence': 'evidence_reference'}

    def read(summary: IrrigationSummary, name):
        if name in profile_fields:
            profile = summary.rule_profile
            return getattr(profile, profile_fields[name]) if profile else None
        if name == 'corrections':
            return tuple(f'{item.correction_id} v{item.version}' for item in summary.corrections)
        if name in VERSION_FIELDS:
            return getattr(summary.publication.versions, name)
        if name == 'time_basis':
            return summary.publication.time_basis
        if name in ('publication_id', 'scope_id', 'snapshot_id'):
            return getattr(summary.publication, name)
        return getattr(summary, name)
    return [_row(SUMMARY_COLUMNS, lambda name, summary=summary: read(summary, name)) for summary in summaries]


def export_metadata(action, summary):
    """Header lines shared by every CSV of this action (no credentials)."""
    publication, context, versions = action.publication, action.context, action.publication.versions
    lines = [
        f'export={EXPORT_VERSION}',
        'quantity_unit=L/m²', 'ratio_unit=%', 'coverage_unit=%',
        'decimal=exact source scale (no rounding)', 'null=empty field', 'bool=true/false',
        f'scope={publication.scope_id}', f'snapshot={publication.snapshot_id}',
        f'publication={publication.publication_id}',
        f'finality={publication.finality_status}', f'committed_at={publication.committed_at.isoformat()}',
        f'cycle={context.cycle_id}', f'compartment={context.compartment_ids[0]}',
        f'requested_window=[{context.start.isoformat()},{context.end.isoformat()})',
        f'effective_window=[{summary.window_start.isoformat()},{summary.window_end.isoformat()})',
        'time_basis=source_naive (source timezone unresolved)',
        f'status={summary.status.value}', f'expected_days={summary.expected_days}',
        f'accepted_days: irrigation={summary.irrigation_accepted_days},drain={summary.drain_accepted_days},paired={summary.paired_days}',
        f'coverage_pct: irrigation={summary.irrigation_coverage_pct},drain={summary.drain_coverage_pct},paired={summary.paired_coverage_pct}',
        f'paired_l_m2: numerator(drain)={summary.paired_drain_l_m2},denominator(irrigation)={summary.paired_irrigation_l_m2}',
        f'day_counts: unknown={summary.unknown_days},suspect={summary.suspect_days},drain_exceeds_irrigation={summary.drain_exceeds_irrigation_days}',
        f'pipeline_run={versions.pipeline_run_id}', f'quality_policy={versions.quality_policy_version}',
        f'parser_version={versions.parser_version}', f'code_version={versions.code_version}',
        f'config_bundle_hash={versions.config_bundle_hash}', f'metric_version={versions.metric_version}',
        f'basis_status={summary.basis_status.value}', f'basis_label={summary.basis_label or ""}',
        f'basis_version_ids={join(summary.basis_version_ids)}',
        f'assumptions={join(summary.assumption_ids)} v{join(summary.assumption_versions, "v")}',
        f'rule_status={summary.rule_status.value}', f'rule_version={summary.rule_version or ""}',
    ]
    profile = summary.rule_profile
    if profile is not None:
        lines.append(
            f'rule_profile={profile.rule_version} profile_version={profile.version} '
            f'bounds drain%[{profile.min_drain_pct},{profile.max_drain_pct}] '
            f'irrigation[{profile.min_irrigation_l_m2},{profile.max_irrigation_l_m2}] L/m² '
            f'demo={str(profile.is_demo).lower()} evidence={profile.evidence_reference}')
    if summary.corrections:
        lines.append('corrections=' + join((f'{item.correction_id} v{item.version}' for item in summary.corrections)))
    return [f'# {line}' for line in lines]


def _document(columns, rows, metadata):
    buffer = io.StringIO(newline='')
    for line in metadata:
        buffer.write(line + '\n')
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8-sig')


def day_csv(action):
    return _document(DAY_COLUMNS, day_rows(action.days), export_metadata(action, action.summary))


def weekly_csv(action):
    return _document(SUMMARY_COLUMNS, summary_rows(action.weekly), export_metadata(action, action.summary))


def summary_json(action):
    """JSON contract v2 envelope; codec semantics untouched."""
    return to_json(action.summary).encode('utf-8')


HARVEST_RECORD_COLUMNS = (
    'harvest_date',
    'prod_a_kg_m2', 'prod_b_kg_m2',
    'prod_a_quality', 'prod_a_applicability', 'prod_a_reason_code', 'source_unit',
    'prod_b_quality', 'prod_b_applicability', 'prod_b_reason_code',
    'prod_a_basis_status', 'prod_a_basis_version_id', 'prod_a_area_m2', 'prod_a_basis_kind',
    'prod_a_assumption_id', 'prod_a_assumption_version', 'prod_a_basis_label',
    'prod_b_basis_status', 'prod_b_basis_version_id', 'prod_b_area_m2', 'prod_b_basis_kind',
    'prod_b_assumption_id', 'prod_b_assumption_version', 'prod_b_basis_label',
    'paired', 'class_b_share_pct', 'rule_status', 'rule_version',
    'revision_id', 'logical_record_key', 'correction_id', 'correction_version',
    'reason_codes',
)

HARVEST_DAY_COLUMNS = (
    'harvest_date',
    'prod_a_kg_m2', 'prod_b_kg_m2', 'paired_prod_a_kg_m2', 'paired_prod_b_kg_m2',
    'class_a_share_pct', 'class_b_share_pct',
    'record_count', 'record_present', 'paired_records',
    'cumulative_a_kg_m2', 'cumulative_total_kg_m2', 'cumulative_origin',
    'basis_status', 'basis_label',
    'rule_status', 'rule_version',
    'reason_codes',
)

HARVEST_SUMMARY_COLUMNS = (
    'iso_year', 'iso_week', 'window_start', 'window_end',
    'prod_a_observed_kg_m2', 'prod_b_observed_kg_m2',
    'paired_prod_a_kg_m2', 'paired_prod_b_kg_m2', 'paired_total_kg_m2',
    'class_a_share_pct', 'class_b_share_pct',
    'accepted_records', 'paired_records',
    'cumulative_a_kg_m2', 'cumulative_total_kg_m2', 'cumulative_origin', 'cumulative_basis_label',
    'rule_status', 'rule_version',
    'rule_profile_version', 'rule_profile_label', 'rule_profile_is_demo', 'rule_profile_evidence',
    'rule_threshold_pct', 'outside_rule_records', 'rule_unknown_records',
    'status',
    'state_snapshot_complete', 'state_source_resolved', 'state_expected_files', 'state_files_present',
    'state_files_loaded', 'state_files_failed', 'state_expected_relative_path',
    'state_records_read', 'state_records_accepted', 'state_records_quarantine',
    'state_records_duplicate', 'state_records_conflict', 'state_unresolved_records',
    'state_conflicted_records', 'state_accepted_records', 'state_paired_records',
    'state_prod_a_usable_records', 'state_prod_b_usable_records', 'state_event_a_dates',
    'state_event_b_dates', 'state_reasons',
    'unit', 'metric_version',
    'publication_id', 'scope_id', 'snapshot_id',
    'pipeline_run_id', 'quality_policy_version', 'parser_version', 'code_version',
    'config_bundle_hash',
    'reason_codes',
)

HARVEST_VERSION_FIELDS = {'pipeline_run_id', 'quality_policy_version', 'parser_version',
                          'code_version', 'config_bundle_hash'}

PROFILE_FIELDS = {'rule_profile_version': 'version', 'rule_profile_label': 'label',
                  'rule_profile_is_demo': 'is_demo', 'rule_profile_evidence': 'evidence_reference'}


def harvest_record_rows(records):
    def read(record: HarvestRecord, name):
        if name.startswith('prod_a_'):
            measure, field = record.prod_a, name[len('prod_a_'):]
        elif name.startswith('prod_b_'):
            measure, field = record.prod_b, name[len('prod_b_'):]
        elif name == 'source_unit':
            return record.prod_a.source_unit
        else:
            return getattr(record, name)
        if field == 'kg_m2':
            return measure.value
        return getattr(measure, field)
    return [_row(HARVEST_RECORD_COLUMNS, lambda name, record=record: read(record, name))
            for record in records]


def harvest_day_rows(days):
    name_map = {'prod_a_kg_m2': 'prod_a', 'prod_b_kg_m2': 'prod_b',
                'paired_prod_a_kg_m2': 'paired_prod_a', 'paired_prod_b_kg_m2': 'paired_prod_b',
                'cumulative_a_kg_m2': 'cumulative_a', 'cumulative_total_kg_m2': 'cumulative_total'}

    def read(day: HarvestDay, name):
        if name in name_map:
            return getattr(day, name_map[name])
        return getattr(day, name)
    return [_row(HARVEST_DAY_COLUMNS, lambda name, day=day: read(day, name)) for day in days]


STATE_FIELD = {    'state_snapshot_complete': 'snapshot_complete', 'state_source_resolved': 'source_resolved',
    'state_expected_files': 'expected_files', 'state_files_present': 'files_present',
    'state_files_loaded': 'files_loaded', 'state_files_failed': 'files_failed',
    'state_expected_relative_path': 'expected_relative_path',
    'state_records_read': 'records_read', 'state_records_accepted': 'records_accepted',
    'state_records_quarantine': 'records_quarantine', 'state_records_duplicate': 'records_duplicate',
    'state_records_conflict': 'records_conflict',
    'state_unresolved_records': 'unresolved_records', 'state_conflicted_records': 'conflicted_records',
    'state_accepted_records': 'accepted_records', 'state_paired_records': 'paired_records',
    'state_prod_a_usable_records': 'prod_a_usable_records',
    'state_prod_b_usable_records': 'prod_b_usable_records',
    'state_event_a_dates': 'event_a_dates', 'state_event_b_dates': 'event_b_dates',
    'state_reasons': 'reasons',
}


def harvest_summary_rows(summaries):
    field_map = {'prod_a_observed_kg_m2': 'prod_a_observed', 'prod_b_observed_kg_m2': 'prod_b_observed',
                 'paired_prod_a_kg_m2': 'paired_prod_a', 'paired_prod_b_kg_m2': 'paired_prod_b',
                 'paired_total_kg_m2': 'paired_total', 'cumulative_a_kg_m2': 'cumulative_a',
                 'cumulative_total_kg_m2': 'cumulative_total'}

    def read(summary: HarvestSummary, name):
        state = summary.completeness
        if name in field_map:
            return getattr(summary, field_map[name])
        if name in PROFILE_FIELDS:
            profile = summary.rule_profile
            return getattr(profile, PROFILE_FIELDS[name]) if profile else None
        if name == 'rule_threshold_pct':
            return summary.rule_profile.max_class_b_share_pct if summary.rule_profile else None
        if name in STATE_FIELD:
            return getattr(state, STATE_FIELD[name])
        if name in HARVEST_VERSION_FIELDS:
            return getattr(summary.publication.versions, name)
        if name in ('publication_id', 'scope_id', 'snapshot_id'):
            return getattr(summary.publication, name)
        return getattr(summary, name)
    return [_row(HARVEST_SUMMARY_COLUMNS, lambda name, summary=summary: read(summary, name))
            for summary in summaries]


def harvest_export_metadata(action, evaluation_grain):
    """Header lines for one pinned harvest action (no credentials).

    `evaluation_grain` names the row grain of THIS export ('record', 'day',
    'week', 'unresolved_issue_identity') and the constant verdict-semantics
    note keeps rule_status cells self-explaining: a row verdict is that
    row's own share, never a summary/weekly mix-up."""
    publication, context, versions = action.publication, action.context, action.publication.versions
    summary, state = action.summary, action.summary.completeness
    lines = [
        f'export={HARVEST_EXPORT_VERSION}',
        f'evaluation_grain={evaluation_grain}',
        "verdict_semantics=every row verdict is that row's OWN share compared to the "
        'selected rule threshold (equal is inside); record counters stay separate '
        'grain facts; a requested-window summary verdict is NOT any week row',
        'quantity_unit=kg/m²', 'share_unit=%', 'source_quantity_unit=kg/m²/harvest',
        'decimal=exact source scale (no rounding)', 'null=empty field', 'bool=true/false',
        f'scope={publication.scope_id}', f'snapshot={publication.snapshot_id}',
        f'publication={publication.publication_id}',
        f'finality={publication.finality_status}', f'committed_at={publication.committed_at.isoformat()}',
        f'cycle={context.cycle_id}', f'compartment={context.compartment_ids[0]}',
        f'requested_window=[{context.start.isoformat()},{context.end.isoformat()})',
        f'effective_window=[{summary.window_start.isoformat()},{summary.window_end.isoformat()})',
        'time_basis=source_naive (source timezone unresolved)',
        (
            f'observed_kg_m2: prod_a={summary.prod_a_observed},prod_b={summary.prod_b_observed}; '
            f'paired_kg_m2: prod_a={summary.paired_prod_a},prod_b={summary.paired_prod_b},'
            f'pair_set_only_total={summary.paired_total}'
        ),
        f'share_pct: a={summary.class_a_share_pct},b={summary.class_b_share_pct} '
        f'(weighted paired masses; per-week subsets state their own records)',
        f'counts: accepted_records={summary.accepted_records},paired_records={summary.paired_records}',
        (
            f'cumulative: origin={summary.cumulative_origin or ""}, '
            f'a={summary.cumulative_a},total={summary.cumulative_total},'
            f'label={summary.cumulative_basis_label or ""} '
            f'(a = accepted ProdA; total = paired A+B; contributors stop at effective end)'
        ),
        (
            f'completeness: snapshot_complete={state.snapshot_complete},'
            f'source_resolved={state.source_resolved},expected_files={state.expected_files},'
            f'files_present={state.files_present},files_loaded={state.files_loaded},'
            f'files_failed={state.files_failed},'
            f'unresolved={state.unresolved_records},conflicts={state.conflicted_records},'
            f'state_accepted={state.accepted_records},state_paired={state.paired_records},'
            f'usable: prod_a={state.prod_a_usable_records},prod_b={state.prod_b_usable_records},'
            f'event_dates: a={state.event_a_dates},b={state.event_b_dates}'
        ),
        f'expected_relative_path={state.expected_relative_path or ""}',
        f'pipeline_run={versions.pipeline_run_id}', f'quality_policy={versions.quality_policy_version}',
        f'parser_version={versions.parser_version}', f'code_version={versions.code_version}',
        f'config_bundle_hash={versions.config_bundle_hash}', f'metric_version={summary.metric_version}',
        f'status={summary.status.value}',
        f'rule_status={summary.rule_status.value}', f'rule_version={summary.rule_version or ""}',
    ]
    if summary.record_quality is not None:
        q = summary.record_quality
        lines.append(
            'record_quality_population: window_records=' + str(q.population_records) +
            ' prod_a: accepted=' + str(q.prod_a_accepted) + ',missing=' + str(q.prod_a_missing) +
            ',invalid=' + str(q.prod_a_invalid) + ',suspect=' + str(q.prod_a_suspect) +
            '; prod_b: accepted=' + str(q.prod_b_accepted) + ',missing=' + str(q.prod_b_missing) +
            ',invalid=' + str(q.prod_b_invalid) + ',suspect=' + str(q.prod_b_suspect) +
            '; scope: only the effective window records (separate from whole-cycle '
            'state_* completeness and from frozen unresolved sources; no-event days '
            'are never claimed as coverage)')
    profile = summary.rule_profile
    if profile is not None:
        lines.append(
            f'rule_profile={profile.rule_version} profile_version={profile.version} '
            f'max_class_b_share={profile.max_class_b_share_pct} % '
            f'demo={str(profile.is_demo).lower()} evidence={profile.evidence_reference}')
    return [f'# {line}' for line in lines]


def harvest_record_csv(action):
    return _document(HARVEST_RECORD_COLUMNS, harvest_record_rows(action.records),
                     harvest_export_metadata(action, 'record'))


def harvest_day_csv(action):
    return _document(HARVEST_DAY_COLUMNS, harvest_day_rows(action.days),
                     harvest_export_metadata(action, 'day'))


def harvest_week_csv(action):
    return _document(HARVEST_SUMMARY_COLUMNS, harvest_summary_rows(action.weekly),
                     harvest_export_metadata(action, 'week'))


def harvest_summary_json(action):
    """Harvest export wrapper over the canonical summary envelope. The wrapper
    keys are export metadata only; `summary` stays the UNCHANGED canonical
    {contract_version: 2, data: …} JSON v2 envelope decodable by
    from_json(HarvestSummary, …). Water exports stay untouched."""
    return json.dumps({'harvest_export_version': 1,
                       'export_kind': 'window_summary',
                       'grain_note': 'the summary is the verdict of the effective '
                                     'window; per-week/per-day/per-record verdicts and '
                                     'counters are in the weekly/days/records CSVs of '
                                     'the same action; the UI rule/grain choice stays '
                                     'UI context (the audit CSV records it when an '
                                     'audit runs)',
                       'summary': json.loads(to_json(action.summary))},
                      ensure_ascii=False, indent=2).encode('utf-8')


EXCLUDED_COLUMNS = (
    'source_record_id', 'disposition', 'reason_code', 'issue_column',
    'relative_path', 'line_number', 'source_time_serial',
)


def excluded_source_rows(summary):
    """Frozen unresolved-issue provenance; quarantined values stay excluded."""
    return [_row(EXCLUDED_COLUMNS, lambda name, item=item: getattr(item, name))
            for item in summary.completeness.unresolved_items]


def excluded_source_csv(action):
    """Frozen unresolved-issue provenance export (no raw amounts)."""
    return _document(EXCLUDED_COLUMNS, excluded_source_rows(action.summary),
                     harvest_export_metadata(action, 'unresolved_issue_identity'))


def unresolved_source_audit_csv(audit):
    """One audit as CSV with its pinned context; only VERIFIED audits carry raw rows."""
    publication = audit.publication
    context_lines = []
    if publication is not None:
        versions = publication.versions
        context_lines = [
            f'# publication={publication.publication_id}',
            f'# scope={publication.scope_id}',
            f'# snapshot={audit.snapshot_id}',
            f'# cycle={audit.cycle_id or ""}',
            f'# compartment={audit.compartment_id or ""}',
            f'# requested_window=[{audit.requested_start.isoformat() if audit.requested_start else ""},'
            f'{audit.requested_end.isoformat() if audit.requested_end else ""})',
            f'# pipeline_run={versions.pipeline_run_id}',
            f'# quality_policy={versions.quality_policy_version}',
            f'# parser_version={versions.parser_version}',
            f'# code_version={versions.code_version}',
            f'# config_bundle_hash={versions.config_bundle_hash}',
            f'# committed_at={publication.committed_at.isoformat()}',
        ]
    lines = [
        '# export=greenhouse-harvest-unresolved-audit-v2',
        f'# source={audit.source_record_id}',
        f'# disposition={audit.disposition or ""}',
        f'# reason_code={audit.reason_code}',
        f'# issue_column={audit.issue_column or ""}',
        f'# rule_version={audit.rule_version or ""}',
        f'# rule_grain={audit.rule_grain or ""}',
        f'# relative_path={audit.relative_path or ""}',
        f'# line_number={audit.line_number if audit.line_number is not None else ""}',
        f'# source_time_serial={audit.source_time_serial if audit.source_time_serial is not None else ""}',
        f'# file_verified={str(audit.file_verified).lower()}',
        f'# record_verified={str(audit.record_verified).lower()}',
        f'# note={audit.note or ""}',
        ('# verified raw explanation only; rule/grain context is traceability, '
         'these values never recompute a KPI'
         if audit.record_verified else
         '# UNVERIFIED audit: no raw values are exposed (missing/drifted/forbidden)'),
    ]
    columns = ('column', 'raw_token')
    rows = [{'column': audit.headers[i], 'raw_token': audit.tokens[i]}
            for i in range(len(audit.tokens))]
    return _document(columns, rows, context_lines + lines)


# --------------------------------------------------------- climate (BR-01)

CLIMATE_SUMMARY_COLUMNS = (
    'metric', 'measurement_context', 'basis', 'rule_version', 'unit',
    'expected_minutes', 'valid_minutes', 'out_minutes',
    'out_low_minutes', 'out_high_minutes', 'suspect_minutes', 'unknown_minutes',
    'out_hours', 'out_percent', 'coverage_pct', 'unknown_reasons',
)
CLIMATE_JOINT_COLUMNS = (
    'metrics', 'measurement_context', 'basis', 'contributions',
    'jointly_valid_minutes', 'joint_out_minutes', 'joint_out_percent', 'joint_coverage_pct',
)
CLIMATE_EPISODE_COLUMNS = (
    'metric', 'basis', 'rule_version', 'unit',
    'original_start', 'original_end', 'original_minutes',
    'clipped_start', 'clipped_end', 'clipped_minutes',
    'directions', 'peak_deviation', 'peak_deviation_time', 'slot_count',
)
CLIMATE_DETAIL_COLUMNS = (
    'slot_start', 'metric', 'basis', 'rule_version', 'unit',
    'actual', 'applied_lower', 'applied_upper', 'quality', 'class_code',
    'deviation', 'direction', 'minutes', 'unknown_reason',
    'source_lower', 'source_upper', 'source_setpoint', 'source_quality',
)


def pad(note, width=400):
    """Bounded provenance text: kept verbatim up to `width` characters."""
    return (note or '')[:width]


def climate_export_metadata(action):
    """Header lines for one pinned climate action (no credentials, no rounding)."""
    publication, context, versions, result = (
        action.publication, action.context, action.publication.versions, action.result)
    lines = [
        f'export={EXPORT_VERSION}', "quantity units: temperature=°C, co2=ppm", 'percent_unit=%',
        'decimal=exact source scale (no rounding)', 'null=empty field', 'bool=true/false',
        f'scope={publication.scope_id}', f'snapshot={publication.snapshot_id}',
        f'publication={publication.publication_id}',
        f'finality={publication.finality_status}', f'committed_at={publication.committed_at.isoformat()}',
        f'cycle={context.cycle_id}', f'compartment={context.compartment_ids[0]}',
        f'requested_window=[{context.start.isoformat()},{context.end.isoformat()})',
        f'effective_window=[{result.effective_start.isoformat()},{result.effective_end.isoformat()})',
        'time_basis=source_naive (source timezone unresolved)',
        f'declared_context={result.metric_results[0].measurement_context if result.metric_results else ""}',
        f'declared_basis={result.metric_results[0].basis.value if result.metric_results else ""}',
        f'quality_policy={versions.quality_policy_version}',
        f'pipeline_run={versions.pipeline_run_id}', f'parser_version={versions.parser_version}',
        f'code_version={versions.code_version}', f'config_bundle_hash={versions.config_bundle_hash}',
        f'requested_rule_version={getattr(action, "requested_rule_version", None) or ""}',
        f'rule_identities=' + join(f'{item.metric}:{item.rule_version}' for item in result.applications),
        # Selected-revision provenance, verbatim from the registered rule.
        f'rule_id={action.rule_id or ""}',
        f'rule_label={action.rule_label or ""}',
        f'rule_is_demo={str(action.rule_is_demo).lower() if action.rule_is_demo is not None else ""}',
        f'rule_evidence={pad(action.rule_evidence_reference or "", 400)}',
        'episode_filter=list-only (summary totals never filtered); '
        'episode windows carry ORIGINAL and CLIPPED facts',
    ]
    return [f'# {line}' for line in lines]


def climate_summary_rows(action):
    rows = []
    for report in action.result.metric_results:
        rows.append({
            'metric': report.metric, 'measurement_context': report.measurement_context,
            'basis': report.basis.value, 'rule_version': report.rule_version, 'unit': report.unit,
            'expected_minutes': report.expected_minutes, 'valid_minutes': report.valid_minutes,
            'out_minutes': report.out_minutes, 'out_low_minutes': report.out_low_minutes,
            'out_high_minutes': report.out_high_minutes, 'suspect_minutes': report.suspect_minutes,
            'unknown_minutes': report.unknown_minutes, 'out_hours': report.out_hours,
            'out_percent': report.out_percent, 'coverage_pct': report.coverage_pct,
            'unknown_reasons': ';'.join(f'{share.reason_code}:{share.minutes}'
                                        for share in report.unknown_reasons)})
    return rows


def climate_joint_rows(action):
    return [{
        'metrics': ';'.join(row.metrics), 'measurement_context': row.measurement_context,
        'basis': row.basis.value,
        'contributions': ';'.join(f'{item.metric}:{item.rule_version}' for item in row.contributions),
        'jointly_valid_minutes': row.jointly_valid_minutes, 'joint_out_minutes': row.joint_out_minutes,
        'joint_out_percent': row.joint_out_percent, 'joint_coverage_pct': row.joint_coverage_pct,
    } for row in action.result.joint_results]


def climate_episode_rows(action, episodes):
    return [{
        'metric': episode.metric, 'basis': episode.basis.value,
        'rule_version': episode.rule_version,
        'unit': {'temperature': '°C', 'co2': 'ppm'}.get(episode.metric, ''),
        'original_start': episode.original_start.isoformat(),
        'original_end': episode.original_end.isoformat(),
        'original_minutes': episode.original_minutes,
        'clipped_start': episode.clipped_start.isoformat() if episode.clipped_start else '',
        'clipped_end': episode.clipped_end.isoformat() if episode.clipped_end else '',
        'clipped_minutes': episode.clipped_minutes,
        'directions': ';'.join(direction.value for direction in episode.directions),
        'peak_deviation': episode.peak_deviation,
        'peak_deviation_time': episode.peak_time.isoformat(),
        'slot_count': len(episode.slots),
    } for episode in episodes]


def climate_detail_rows(action, limit=None):
    rows = []
    for review in action.slot_reviews:
        if limit is not None and len(rows) >= limit:
            break
        rows.append({
            'slot_start': review.start.isoformat(), 'metric': review.metric,
            'basis': review.basis.value, 'rule_version': review.rule_version,
            'unit': review.unit, 'actual': review.actual,
            'applied_lower': review.lower, 'applied_upper': review.upper,
            'quality': review.quality.value if review.quality else '',
            'class_code': review.class_code, 'deviation': review.deviation,
            'direction': review.direction.value if review.direction else '',
            'minutes': review.minutes, 'unknown_reason': review.unknown_reason,
            'source_lower': getattr(review, 'source_lower', None),
            'source_upper': getattr(review, 'source_upper', None),
            'source_setpoint': getattr(review, 'source_setpoint', None),
            'source_quality': getattr(review, 'source_quality', None)})
    return rows


def climate_summary_csv(action):
    return _document(CLIMATE_SUMMARY_COLUMNS, climate_summary_rows(action),
                     climate_export_metadata(action))


def climate_joint_csv(action):
    return _document(CLIMATE_JOINT_COLUMNS, climate_joint_rows(action),
                     climate_export_metadata(action))


def climate_episodes_csv(action, episodes, *, total_after_filter=None, filter_note=None):
    metadata = climate_export_metadata(action)
    if filter_note:
        metadata += [f'# episode_list_filter={filter_note}']
    if total_after_filter is not None:
        truncated = len(episodes) < total_after_filter
        metadata += [
            f'# total_after_filter={total_after_filter}',
            f'# exported_rows={len(episodes)}',
            f'# truncated={str(truncated).lower()}'
            + (' (paged export cap; raise the cap or page on screen for more)'
               if truncated else ''),
        ]
    return _document(CLIMATE_EPISODE_COLUMNS, climate_episode_rows(action, episodes), metadata)


def climate_detail_csv(action, limit=None, scope_note=None):
    metadata = climate_export_metadata(action)
    if scope_note:
        metadata += [f'# detail_scope={scope_note}']
    if limit is not None:
        available = len(action.slot_reviews)
        metadata += [f'# detail_rows_exported={min(limit, available or 0)}',
                     f'# detail_rows_available={available}']
    return _document(CLIMATE_DETAIL_COLUMNS, climate_detail_rows(action, limit), metadata)


def climate_result_json(action):
    """Typed ClimateEvaluation envelope (T15): the unchanged engine
    RuleDurationResult plus the pinned publication/context and the
    selected-rule provenance (rule_id/label/is_demo/evidence — verbatim
    from the registered revision, never inferred from version strings).
    Decode with from_json(ClimateEvaluation, ...)."""
    return to_json(action).encode('utf-8')
