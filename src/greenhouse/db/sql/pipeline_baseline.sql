SELECT r.revision_id,r.logical_record_key,r.content_hash,v.measure_code,v.value
FROM core.record_revision r JOIN core.measure_value v USING(revision_id)
WHERE r.data_scope_id=%s AND r.revision_number=1
