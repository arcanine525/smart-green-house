SELECT f.revision_id,f.logical_record_key,f.quality_policy_version,
 f.correction_id,f.correction_version,f.slot_start,
 f.ec_drain,f.ph_drain,f.measure_quality
FROM mart.fact_drain_slot f
WHERE f.publication_id=%s AND f.data_scope_id=%s AND f.cycle_id=%s AND f.compartment_id=%s
 AND f.slot_start>=%s AND f.slot_start<%s
ORDER BY f.slot_start
