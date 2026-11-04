SELECT DISTINCT c.cycle_id,c.compartment_id,c.start_source_time,c.end_source_time_exclusive
FROM core.growing_cycle c JOIN meta.source_folder_mapping m USING(cycle_id,compartment_id)
JOIN meta.data_scope ds ON ds.snapshot_id=m.snapshot_id
WHERE ds.data_scope_id=%s AND ds.snapshot_id=%s AND c.cycle_id=%s AND c.compartment_id=%s
