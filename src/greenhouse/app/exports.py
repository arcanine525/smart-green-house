"""CSV/JSON exports reusing the exact displayed DTOs of one pinned action.

Rules: Decimal → exact string (never rounded, source scale kept), NULL →
empty field, 0 → '0', bool → 'true'/'false', tuple → ';'-joined, no extra
rounding and no re-pinning. Metadata headers document units, filters, window,
quality/basis/assumption and rule identity. Schema documented in
src/greenhouse/app/README.md.
"""
import csv
import io

from greenhouse.contracts import IrrigationDay, IrrigationSummary, to_json

EXPORT_VERSION = 'greenhouse-water-csv-v1'

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
