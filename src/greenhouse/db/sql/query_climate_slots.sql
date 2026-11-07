SELECT f.revision_id,f.logical_record_key,f.quality_policy_version,
 f.correction_id,f.correction_version,f.slot_start,
 f.tair,f.co2air,f.t_heat_sp,f.t_vent_sp,f.co2_sp,
 f.t_heat_vip,f.t_ventlee_vip,f.co2_vip,f.measure_quality
FROM mart.fact_climate_slot f
WHERE f.publication_id=%s AND f.data_scope_id=%s AND f.cycle_id=%s AND f.compartment_id=%s
 AND f.slot_start>=%s AND f.slot_start<%s
ORDER BY f.slot_start
