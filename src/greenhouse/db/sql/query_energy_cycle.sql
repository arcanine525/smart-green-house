SELECT cycle_id,compartment_id,start_source_time,end_source_time_exclusive
FROM mart.dim_energy_cycle
WHERE publication_id=%s AND data_scope_id=%s AND cycle_id=%s AND compartment_id=%s
