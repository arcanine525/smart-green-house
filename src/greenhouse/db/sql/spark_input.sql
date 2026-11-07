SELECT r.revision_id,r.source_record_id,r.record_type,r.cycle_id,c.compartment_id,
 r.observed_at::text AS observed_at,r.content_hash,r.is_close_boundary
FROM meta.workflow_candidate w JOIN core.record_revision r USING(revision_id)
JOIN core.growing_cycle c USING(cycle_id) WHERE w.workflow_id=%s
