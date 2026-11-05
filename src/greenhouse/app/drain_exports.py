"""BR-03 exports share the engine rows and preserve their pinned provenance."""
from dataclasses import replace
from decimal import Decimal

from greenhouse.app import exports
from greenhouse.contracts import to_json


def metadata(evaluation):
    return exports.climate_export_metadata(
        evaluation, export_version='greenhouse-drain-csv-v1', quantity_units='ec=dS/m, ph=pH') + [
            '# profile_status=' + ('demo_unverified' if evaluation.rule_is_demo else
                                   evaluation.rule_label or 'rule_not_configured'),
            '# demo bounds and engineer-entered bounds are not agronomically validated',
        ]


def summary_csv(evaluation):
    return exports._document(exports.CLIMATE_SUMMARY_COLUMNS,
                             exports.climate_summary_rows(evaluation), metadata(evaluation))


def joint_csv(evaluation):
    return exports._document(exports.CLIMATE_JOINT_COLUMNS,
                             exports.climate_joint_rows(evaluation), metadata(evaluation))


def episodes_csv(evaluation, episodes, *, total, metric, min_minutes):
    rows = exports.climate_episode_rows(evaluation, episodes)
    for row in rows:
        row['unit'] = 'dS/m' if row['metric'] == 'ec' else 'pH'
    return exports._document(exports.CLIMATE_EPISODE_COLUMNS, rows, metadata(evaluation) + [
        f'# episode_list_filter=metric={metric or "all"};min_minutes={min_minutes}',
        f'# total_after_filter={total}', f'# exported_rows={len(rows)}',
        f'# truncated={str(len(rows) < total).lower()}',
    ])


def detail_csv(evaluation, window):
    scoped = replace(evaluation, slot_reviews=window.reviews)
    return exports._document(exports.CLIMATE_DETAIL_COLUMNS,
                             exports.climate_detail_rows(scoped), metadata(evaluation) + [
        f'# detail_scope=[{window.start.isoformat()},{window.end.isoformat()})',
        f'# wanted_end={window.wanted_end.isoformat()}',
        f'# page_slots={window.window_slots}', f'# exported_rows={len(window.reviews)}',
        f'# truncated={str(window.truncated).lower()}',
        '# bounded page only; summary and original episode durations remain complete',
    ])


def result_json(evaluation):
    """Decode with from_json(DrainEvaluation, ...); source and rule IDs stay distinct."""
    return to_json(evaluation).encode('utf-8')


def water_csv(evaluation, water):
    return exports._document(exports.DAY_COLUMNS, exports.day_rows(water.action.days),
        exports.export_metadata(water.action, water.action.summary) + [
            f'# parent_drain_rule={evaluation.requested_rule_version or "rule_not_configured"}',
            f'# parent_drain_rule_label={evaluation.rule_label or ""}',
            f'# parent_drain_rule_evidence={evaluation.rule_evidence_reference or ""}',
            f'# parent_drain_rule_is_demo={evaluation.rule_is_demo}',
            f'# parent_drain_report=[{evaluation.context.start.isoformat()},{evaluation.context.end.isoformat()})',
            f'# event_overlap=[{water.overlap_start.isoformat()},{water.overlap_end.isoformat()})',
            '# grain=one row per distinct source calendar day; full-day amounts, not prorated',
            '# partial_source_dates=' + ';'.join(day.isoformat() for day in water.partial_dates),
            '# interpretation=context for investigation only; no causal attribution',
        ])


def source_rows(facts):
    rows = []
    for fact in facts.rows:
        row = {'slot_start': fact.slot_start.isoformat(), 'revision_id': fact.revision_id,
               'logical_record_key': fact.logical_key,
               'quality_policy_version': fact.quality_policy_version,
               'correction_id': fact.correction_id, 'correction_version': fact.correction_version}
        for metric, column, code in (('ec', 'ec_drain', 'EC_drain_PC'),
                                     ('ph', 'ph_drain', 'pH_drain_PC')):
            value = getattr(fact, column)
            row[metric] = Decimal(str(value)) if value is not None else None
            triplet = fact.measure_quality.get(code, {})
            for key in ('status', 'applicability', 'reason'):
                row[f'{metric}_{key}'] = triplet.get(key)
        rows.append(row)
    return rows


def source_csv(evaluation, facts):
    rows = source_rows(facts)
    columns = ('slot_start', 'ec', 'ph', 'ec_status', 'ec_applicability', 'ec_reason',
               'ph_status', 'ph_applicability', 'ph_reason', 'revision_id', 'logical_record_key',
               'quality_policy_version', 'correction_id', 'correction_version')
    return exports._document(columns, rows, metadata(evaluation) + [
        f'# source_scope=[{facts.start.isoformat()},{facts.end.isoformat()})',
        f'# wanted_end={facts.wanted_end.isoformat()}',
        f'# truncated={str(facts.truncated).lower()}',
        '# raw values and frozen own-measure quality; independent of rule and episode filters',
    ])
