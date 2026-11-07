SELECT c.cycle_id,c.compartment_id,c.start_source_time,c.end_source_time_exclusive
FROM mart.dim_harvest_cycle c JOIN meta.data_scope ds USING(data_scope_id)
WHERE c.publication_id=%s AND c.data_scope_id=%s AND ds.snapshot_id=%s AND c.cycle_id=%s AND c.compartment_id=%s
