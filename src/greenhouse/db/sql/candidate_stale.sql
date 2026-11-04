WITH latest AS (
 SELECT r.revision_id FROM core.record_revision r WHERE r.data_scope_id=%s
 AND NOT EXISTS(SELECT 1 FROM core.record_revision newer WHERE newer.data_scope_id=r.data_scope_id
 AND newer.record_type=r.record_type AND newer.logical_record_key=r.logical_record_key
 AND newer.revision_number>r.revision_number)
), frozen AS (SELECT revision_id FROM meta.workflow_candidate WHERE workflow_id=%s)
SELECT EXISTS((SELECT * FROM latest EXCEPT SELECT * FROM frozen)
 UNION ALL (SELECT * FROM frozen EXCEPT SELECT * FROM latest))
