SELECT cycle.cycle_id,cycle.compartment_id,cycle.start_source_time,cycle.end_source_time_exclusive
FROM mart.dim_drain_cycle cycle
WHERE cycle.publication_id=%s AND cycle.data_scope_id=%s AND cycle.cycle_id=%s
 AND cycle.compartment_id=%s
