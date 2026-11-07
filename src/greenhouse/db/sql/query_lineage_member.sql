SELECT r.revision_id,r.record_type,r.logical_record_key,r.cycle_id,c.compartment_id,cor.version AS correction_version,
 s.source_time_serial,s.source_timestamp
FROM meta.publication_member pm JOIN core.record_revision r USING(revision_id)
JOIN core.growing_cycle c USING(cycle_id) JOIN meta.source_record s USING(source_record_id)
LEFT JOIN meta.correction cor ON cor.correction_id=r.correction_id
WHERE pm.publication_id=%s AND pm.data_scope_id=%s AND r.snapshot_id=%s
 AND ((%s::text IS NOT NULL AND r.revision_id=%s) OR (%s::text IS NOT NULL AND pm.logical_record_key=%s))
ORDER BY r.record_type
