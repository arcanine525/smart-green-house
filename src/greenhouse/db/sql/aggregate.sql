SELECT r.record_type,c.compartment_id,count(*) AS records,
 count(*) FILTER(WHERE r.is_close_boundary) AS closing_boundary
FROM core.record_revision r JOIN core.growing_cycle c USING(cycle_id)
WHERE r.run_id=%s GROUP BY r.record_type,c.compartment_id ORDER BY c.compartment_id,r.record_type
