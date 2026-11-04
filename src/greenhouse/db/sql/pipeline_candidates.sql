INSERT INTO meta.workflow_candidate
SELECT %s,r.revision_id FROM core.record_revision r
WHERE r.data_scope_id=%s AND NOT EXISTS(
 SELECT 1 FROM core.record_revision newer WHERE newer.data_scope_id=r.data_scope_id
 AND newer.record_type=r.record_type AND newer.logical_record_key=r.logical_record_key
 AND newer.revision_number>r.revision_number);
