SELECT f.revision_id,f.logical_record_key,f.cycle_id,f.compartment_id,f.harvest_date,
 f.correction_id,f.correction_version,f.paired,f.source_unit,v.measure_code,v.value,
 v.quality,v.applicability_status,v.reason_code,v.basis_status,v.basis_version_id,v.area_m2,
 v.basis_kind,v.assumption_id,v.assumption_version
FROM mart.fact_harvest_record f JOIN meta.publication p USING(publication_id,data_scope_id)
JOIN meta.data_scope ds USING(data_scope_id)
CROSS JOIN LATERAL (VALUES
 ('ProdA',f.prod_a_kg_m2,f.prod_a_quality,f.prod_a_applicability,f.prod_a_reason,
  f.prod_a_basis_status,f.prod_a_basis_id,f.prod_a_area_m2,f.prod_a_basis_kind,f.prod_a_assumption_id,f.prod_a_assumption_version),
 ('ProdB',f.prod_b_kg_m2,f.prod_b_quality,f.prod_b_applicability,f.prod_b_reason,
  f.prod_b_basis_status,f.prod_b_basis_id,f.prod_b_area_m2,f.prod_b_basis_kind,f.prod_b_assumption_id,f.prod_b_assumption_version)
) v(measure_code,value,quality,applicability_status,reason_code,basis_status,basis_version_id,area_m2,basis_kind,assumption_id,assumption_version)
WHERE f.publication_id=%s AND f.data_scope_id=%s AND ds.snapshot_id=%s
 AND f.cycle_id=%s AND f.compartment_id=%s AND f.harvest_date>=%s AND f.harvest_date<%s
ORDER BY f.harvest_date,f.logical_record_key,v.measure_code
