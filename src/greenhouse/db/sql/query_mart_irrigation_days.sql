SELECT f.revision_id,f.logical_record_key,f.cycle_id,f.compartment_id,f.resource_date,
 f.correction_id,f.correction_version,v.measure_code,v.value,f.source_unit,
 v.quality,v.applicability_status,v.reason_code,v.basis_version_id,v.area_m2,v.basis_kind,v.basis_status,
 v.assumption_id,v.assumption_version,f.paired,f.drain_fraction_pct,f.drain_exceeds_irrigation,f.reason_codes
FROM mart.fact_irrigation_day f JOIN meta.publication p USING(publication_id,data_scope_id)
JOIN meta.data_scope ds USING(data_scope_id)
CROSS JOIN LATERAL (VALUES
 ('Irr',f.irrigation_l_m2,f.irrigation_quality,f.irrigation_applicability,f.irrigation_reason,
 f.irrigation_basis_id,f.irrigation_area_m2,f.irrigation_basis_kind,f.irrigation_basis_status,f.irrigation_assumption_id,f.irrigation_assumption_version),
 ('Drain',f.drain_l_m2,f.drain_quality,f.drain_applicability,f.drain_reason,
 f.drain_basis_id,f.drain_area_m2,f.drain_basis_kind,f.drain_basis_status,f.drain_assumption_id,f.drain_assumption_version)
) v(measure_code,value,quality,applicability_status,reason_code,basis_version_id,area_m2,basis_kind,basis_status,assumption_id,assumption_version)
WHERE f.publication_id=%s AND f.data_scope_id=%s AND ds.snapshot_id=%s
 AND f.cycle_id=%s AND f.compartment_id=%s AND f.resource_date>=%s AND f.resource_date<%s
ORDER BY f.resource_date,v.measure_code
