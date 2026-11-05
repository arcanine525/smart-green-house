"""Exact BR04 exports of the same pinned DTOs shown on screen."""
from greenhouse.app import exports
from greenhouse.contracts import to_dict, to_json


def metadata(result):
    p, c = result.publication, result.context
    return [f'# export_version=greenhouse-energy-intensity-v1',
        f'# publication_id={p.publication_id}', f'# scope_id={p.scope_id}', f'# snapshot_id={p.snapshot_id}',
        f'# cycle_id={c.cycle_id}', f'# window=[{c.start.isoformat()},{c.end.isoformat()})',
        f'# effective_window=[{result.energy.window_start},{result.energy.window_end})',
        f'# quality_policy={p.versions.quality_policy_version}', f'# parser={p.versions.parser_version}',
        f'# pipeline={p.versions.code_version}', f'# config_bundle_hash={p.versions.config_bundle_hash}',
        f'# metric_version={result.metric_version}', f'# quantity_version={result.energy.quantity_version}',
        '# units=energy:kWh;harvest_a:kg;intensity:kWh/kg A;source heat:MJ/m²/day;electricity:kWh/m²/day',
        f'# basis_status={result.basis_gate.status}', f'# basis_label={result.basis_gate.label or ""}',
        '# assumptions=' + ';'.join(result.assumptions),
        '# sensitivity=Resources area96m² / primary62.5m²; factor1.536; Production remains62.5m²; scenario only',
        '# NULL is empty; accepted zero stays0; no extrapolation; A-only denominator; whole-cycle ranking only']


def summary_row(r):
    return dict(cycle_id=r.cycle.cycle_id, compartment_id=r.cycle.compartment_id,
        start=r.energy.window_start, end_exclusive=r.energy.window_end, energy_kwh=r.energy.official_total_kwh,
        heat_kwh=r.energy.components[0].official_absolute_kwh,
        high_kwh=r.energy.components[1].official_absolute_kwh, low_kwh=r.energy.components[2].official_absolute_kwh,
        observed_energy_kwh=r.energy.observed_total_kwh, production_a_kg=r.official_a_kg,
        observed_a_kg=r.observed_a_kg, intensity_kwh_kg_a=r.intensity_kwh_kg_a,
        heat_kwh_kg_a=r.heat_kwh_kg_a, light_kwh_kg_a=r.light_kwh_kg_a,
        complete_days=r.energy.complete_days, expected_days=r.energy.expected_days,
        coverage_pct=r.energy.complete_coverage_pct, harvest_snapshot_complete=r.harvest_state.snapshot_complete,
        comparison_eligible=r.comparison_eligible, basis_status=r.basis_gate.status,
        basis_label=r.basis_gate.label, assumptions=';'.join(r.assumptions),
        sensitivity_factor=r.sensitivity_factor, sensitivity_kwh_kg_a=r.sensitivity_kwh_kg_a,
        reasons=';'.join(r.reason_codes))


def summary_csv(result):
    row = summary_row(result)
    return exports._document(tuple(row), [row], metadata(result))


def portfolio_csv(result):
    rows = []
    for m in result.members:
        row = summary_row(m)
        row['included_in_portfolio'] = m.cycle.cycle_id in result.included_cycle_ids
        row['portfolio_reasons'] = ';'.join(result.reason_codes) if m.cycle.cycle_id in result.excluded_cycle_ids else ''
        rows.append(row)
    return exports._document(tuple(rows[0]), rows, metadata(result.members[0]) + [
        '# grain=one row per cycle; includes excluded cycles and reasons',
        '# portfolio_included=' + ';'.join(result.included_cycle_ids),
        '# portfolio_excluded=' + ';'.join(result.excluded_cycle_ids),
        '# portfolio_reasons=' + ';'.join(result.reason_codes),
        f'# portfolio_energy_kwh={result.energy_kwh}', f'# portfolio_a_kg={result.production_a_kg}',
        f'# portfolio_intensity_kwh_kg_a={result.intensity_kwh_kg_a}',
        '# aggregation=sum(eligible energy)/sum(eligible A); never average intensities'])


def basis_fields(b):
    return {f'basis_{key}': value for key, value in (to_dict(b).items() if b else [])}


def quantity_rows(quantities):
    rows = []
    for q in quantities:
        row = dict(date=q.resource_date, component=q.component, source_value=q.source_value,
            source_unit=q.source_unit, quality=q.quality, applicability=q.applicability,
            reason=q.reason_code, accepted=q.accepted, normalized_kwh_m2=q.normalized_kwh_m2,
            absolute_source_quantity=q.absolute_source_quantity, absolute_kwh=q.absolute_kwh,
            revision_id=q.revision_id, logical_record_key=q.logical_record_key,
            correction_id=q.correction.correction_id if q.correction else None,
            correction_version=q.correction.version if q.correction else None,
            quantity_version=q.quantity_version)
        row.update(basis_fields(q.basis)); rows.append(row)
    return rows


def _rows_csv(result, rows):
    columns = tuple(dict.fromkeys(key for row in rows for key in row))
    return exports._document(columns, rows, metadata(result))


def quantities_csv(result, quantities):
    return _rows_csv(result, quantity_rows(quantities))


def harvest_rows(result):
    rows = []
    for c in result.harvest_a:
        r = c.record
        row = dict(date=r.harvest_date, revision_id=r.revision_id, logical_record_key=r.logical_record_key,
            prod_a_kg_m2=r.prod_a.value, prod_a_quality=r.prod_a.quality,
            prod_a_applicability=r.prod_a.applicability, prod_a_reason=r.prod_a.reason_code,
            absolute_a_kg=c.absolute_a_kg, prod_b_kg_m2=r.prod_b.value,
            prod_b_quality=r.prod_b.quality, paired=r.paired, correction_id=r.correction_id,
            correction_version=r.correction_version)
        row.update(basis_fields(c.basis)); rows.append(row)
    return rows


def harvest_csv(result):
    rows = harvest_rows(result)
    if not rows:
        return exports._document(('date', 'revision_id', 'absolute_a_kg'), [], metadata(result))
    return _rows_csv(result, rows)


def result_json(result):
    return to_json(result).encode('utf-8')


def portfolio_json(result):
    return to_json(result).encode('utf-8')
